// Step 3 of the Snowdon Towers case study: run the HQC2 protocol on the real role-check scores.
//
// Usage (from the contract folder):
//   npx hardhat run scripts/casestudy.js
// Optional environment variables:
//   CASE=../casestudy/out/case_scores.json   path to the role-check output
//   RUNS=30                                   runs per scenario
//   THETA=0.4                                 quorum threshold (default 0.5)
//
// Scenarios
//   S1 honest            12 agents (2 per role), all honest.
//   S2 colluder, verif.  one Cost agent claims 1000 for the worst alternative and sends a stop
//                        signal against the leader every round; challenges are enabled.
//   S3 colluder, no aud. as S2, but nobody challenges (shows what the audit layer contributes).
//   S4 colluder, subj.   one Client agent colludes; client scores cannot be challenged.
// Honest agents follow the HQC rules off-chain (recruitment, discovery, endorsement threshold,
// stop signals, vetoes) and act on-chain; each evidence-bearing action of a colluder in a
// verifiable role is challenged with probability P_DETECT, and the auditor resolves it with
// the deterministic score from role_checks.py.

const { ethers } = require("hardhat");
const { time } = require("@nomicfoundation/hardhat-toolbox/network-helpers");
const fs = require("fs");
const path = require("path");

const CASE = process.env.CASE || path.join(__dirname, "..", "..", "casestudy", "out", "case_scores.json");
const RUNS = Number(process.env.RUNS || 30);
const ROLE_NAMES = ["architect", "structural", "mep", "cost", "sustainability", "client"];
const ROLE_ID = { architect: 1, structural: 2, mep: 3, cost: 4, sustainability: 5, client: 6 };
const bit = (r) => 1 << r;
const PARAMS = {
  thetaBps: 5000, alphaBps: 1000, sigmaBps: 3000, capBps: 2500,
  maxRounds: 30, windowSeconds: 48 * 3600,
  mandatoryMask: bit(1) | bit(2) | bit(6),
  vetoMask: bit(2) | bit(6),
  verifiableMask: bit(2) | bit(3) | bit(4) | bit(5),
  tolerance: 50, slashBps: 5000,
  minStake: ethers.parseEther("1"),
  challengeBond: ethers.parseEther("0.1")
};
// off-chain behaviour (same values as the frozen simulation parameters where they exist)
const RHO = 1.0, P_DISC = 0.3, SIGMA = 0.1, ALPHA = 0.3, STOP_EFF = 0.5, ACCEPT = 500, P_DETECT = 0.5;
if (process.env.THETA) PARAMS.thetaBps = Math.round(Number(process.env.THETA) * 10000);
const SUBJECTIVE_SD = 80;      // spread between two judges of a subjective criterion (score units)
const VETO_FLOOR = 250;        // a veto role vetoes a candidate it scores below this

function rng(seed) {           // mulberry32
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6D2B79F5) >>> 0; let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
function gauss(r) { return Math.sqrt(-2 * Math.log(r() + 1e-12)) * Math.cos(2 * Math.PI * r()); }
const h = (t) => ethers.keccak256(ethers.toUtf8Bytes(t));
const hex32 = (sha) => "0x" + sha;

async function runOnce(caseData, scenario, seed, signers) {
  const r = rng(seed);
  const alts = Object.keys(caseData.alternatives);
  const k = alts.length;
  const truth = caseData.results;
  const auditor = signers[signers.length - 1];
  let gas = 0n;
  const send = async (p) => { const rc = await (await p).wait(); gas += rc.gasUsed; return rc; };

  const C = await ethers.getContractFactory("HQC2");
  const c = await C.deploy(PARAMS, auditor.address);
  gas += (await c.deploymentTransaction().wait()).gasUsed;
  const deployGas = gas;

  // agents: two per role; agent observes its own criterion
  const agents = [];
  for (let i = 0; i < 12; i++) {
    const role = ROLE_NAMES[i % 6];
    const s = signers[i + 1];
    await send(c.registerAgent(s.address, ROLE_ID[role], 1));
    await send(c.connect(s).deposit({ value: PARAMS.minStake }));
    const obs = alts.map((a) => {
      const v = truth[a].scores[role];
      const subj = !caseData.verifiable_roles.includes(role);
      return Math.max(0, Math.min(1000, Math.round(subj ? v + SUBJECTIVE_SD * gauss(r) : v)));
    });
    agents.push({ i, role, s, obs, endorsed: -1, score: 0, lastRound: -1, colluder: false, vetoed: false, slashed: false });
  }
  const worst = alts.indexOf(caseData.ranking_weighted_sum[k - 1]);
  if (scenario !== "S1") {
    const role = scenario === "S4" ? "client" : "cost";
    const col = agents.find((a) => a.role === role);
    col.colluder = true;
  }
  const auditOn = scenario !== "S3";

  await send(c.connect(agents[0].s).proposeChange(alts.map((a) => hex32(caseData.alt_hash[a]))));
  const cId = await c.requestCount();
  const evidence = (a, alt) => hex32(caseData.evidence_hash[alts[alt]][a.role]);

  let challenges = 0, upheld = 0, vetoes = 0, evaluations = 0, guard = 0;
  async function maybeChallenge(col, claimed, alt) {
    if (!auditOn || col.slashed || !caseData.verifiable_roles.includes(col.role) || r() >= P_DETECT) return;
    const honest = agents.filter((a) => !a.colluder);
    const ch = honest[Math.floor(r() * honest.length)];
    const trueScore = truth[alts[alt]].scores[col.role];
    await send(c.connect(ch.s).challenge(cId, col.s.address, trueScore, h("recompute " + col.i + " " + alt), { value: PARAMS.challengeBond }));
    challenges++;
    const id = await c.challengeCount();
    const rc = await send(c.connect(auditor).resolveChallenge(id, trueScore));
    if (Math.abs(claimed - trueScore) > PARAMS.tolerance) { upheld++; col.slashed = true; col.endorsed = -1; }
    return rc;
  }

  // off-chain response of an honest agent to a stop signal received on-chain
  async function receiveStop(b) {
    if (!b.colluder && b.endorsed >= 0 && r() < STOP_EFF) {
      await send(c.connect(b.s).abandon(cId)); b.endorsed = -1;
    }
  }
  async function endorseNow(a, alt, score, ev, round) {
    await send(c.connect(a.s).endorse(cId, alt, score, ev));
    a.endorsed = alt; a.score = score; a.lastRound = round;
  }
  async function stopRandomRival(a) {
    const rivals = agents.filter((b) => b.endorsed >= 0 && b.endorsed !== a.endorsed && !b.slashed);
    if (!rivals.length) return false;
    const b = rivals[Math.floor(r() * rivals.length)];
    try { await send(c.connect(a.s).stopSignal(cId, b.endorsed, a.colluder ? h("fabricated objection " + a.i) : evidence(a, b.endorsed))); }
    catch (e) { return false; }
    await receiveStop(b);
    return true;
  }

  let st = await c.getRequest(cId);
  while ((st[0] === 1n || st[0] === 2n) && guard++ < 200) {
    if (st[0] === 1n) {
      const round = Number(st[1]);
      const sup = [];
      for (let j = 0; j < k; j++) sup.push(Number(await c.support(cId, j)) / 1000);
      const D = sup.reduce((x, y) => x + y, 0);
      const leader = sup.indexOf(Math.max(...sup));
      for (const a of agents) {
        if (a.slashed) continue;
        if (a.colluder) {
          // re-dance for the target every round and inhibit a random rival
          if (a.lastRound !== round) {
            await endorseNow(a, worst, 1000, h("fabricated " + a.i + " " + round), round);
            await maybeChallenge(a, 1000, worst);
          }
          if (!a.slashed && await stopRandomRival(a)) await maybeChallenge(a, 1000, worst);
          continue;
        }
        if (a.endorsed < 0) {
          if (a.lastRound === round) continue;            // already danced this round
          let pick = -1;
          if (D > 0 && r() < Math.min(1, RHO * D / agents.length)) {
            let x = r() * D;
            for (let j = 0; j < k; j++) { x -= sup[j]; if (x <= 0) { pick = j; break; } }
            if (pick < 0) pick = leader;
          } else if (r() < P_DISC) pick = Math.floor(r() * k);
          if (pick >= 0) {
            evaluations++;
            if (a.obs[pick] >= ACCEPT) await endorseNow(a, pick, a.obs[pick], evidence(a, pick), round);
          }
        } else {
          // quality-dependent abandonment, otherwise re-dance (refresh) and occasionally inhibit
          if (r() < ALPHA * (1 - a.score / 1000)) {
            await send(c.connect(a.s).abandon(cId)); a.endorsed = -1; continue;
          }
          if (a.lastRound !== round) await endorseNow(a, a.endorsed, a.score, evidence(a, a.endorsed), round);
          if (r() < SIGMA) await stopRandomRival(a);
        }
      }
      await send(c.tick(cId));
    } else {
      const cand = Number(st[3]);
      const v = agents.find((a) => !a.colluder && !a.vetoed && (a.role === "structural" || a.role === "client") && a.obs[cand] < VETO_FLOOR);
      if (v) { v.vetoed = true; vetoes++; await send(c.connect(v.s).veto(cId, h("below floor " + v.i))); }
      else { await time.increase(PARAMS.windowSeconds + 1); await send(c.commit(cId)); }
    }
    st = await c.getRequest(cId);
  }
  const state = Number(st[0]);
  return {
    scenario, seed, finalState: state === 3 ? "Committed" : state === 4 ? "Escalated" : "Other",
    committed: state === 3 ? alts[Number(st[3])] : null, rounds: Number(st[1]),
    evaluations, challenges, upheld, vetoes, gasExDeploy: Number(gas - deployGas)
  };
}

async function main() {
  const caseData = JSON.parse(fs.readFileSync(CASE, "utf8"));
  const signers = await ethers.getSigners();
  const rows = [];
  for (const sc of ["S1", "S2", "S3", "S4"]) {
    for (let i = 0; i < RUNS; i++) {
      await ethers.provider.send("hardhat_reset", []);
      rows.push(await runOnce(caseData, sc, 1000 + i, signers));
    }
    process.stdout.write(sc + " done\n");
  }
  const summary = {};
  for (const sc of ["S1", "S2", "S3", "S4"]) {
    const s = rows.filter((x) => x.scenario === sc);
    const count = {};
    s.forEach((x) => { const key = x.committed || x.finalState; count[key] = (count[key] || 0) + 1; });
    const mean = (f) => Math.round(s.reduce((a, x) => a + f(x), 0) / s.length);
    summary[sc] = { runs: s.length, outcomes: count, meanRounds: mean((x) => x.rounds),
      meanEvaluations: mean((x) => x.evaluations), meanChallenges: +(s.reduce((a, x) => a + x.challenges, 0) / s.length).toFixed(2),
      upheldRate: +(s.reduce((a, x) => a + x.upheld, 0) / Math.max(1, s.reduce((a, x) => a + x.challenges, 0))).toFixed(2),
      meanGasExDeploy: mean((x) => x.gasExDeploy) };
  }
  const out = { generatedAt: new Date().toISOString(), case: path.basename(CASE), thetaBps: PARAMS.thetaBps,
    weightedSumRanking: caseData.ranking_weighted_sum, params: { RHO, P_DISC, SIGMA, ACCEPT, P_DETECT, SUBJECTIVE_SD, VETO_FLOOR },
    summary, runs: rows };
  fs.mkdirSync(path.join(__dirname, "..", "results"), { recursive: true });
  fs.writeFileSync(path.join(__dirname, "..", "results", `casestudy_results_theta${PARAMS.thetaBps}.json`), JSON.stringify(out, null, 2));
  console.log("Weighted-sum ranking:", caseData.ranking_weighted_sum.join(" > "));
  console.log(JSON.stringify(summary, null, 2));
  console.log(`Written results/casestudy_results_theta${PARAMS.thetaBps}.json`);
}

main().catch((e) => { console.error(e); process.exitCode = 1; });
