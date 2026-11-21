// HQC2 functional and gas tests. Run with: npx hardhat test
// Writes results/gas_results_v2.json.

const { expect } = require("chai");
const { ethers } = require("hardhat");
const { time } = require("@nomicfoundation/hardhat-toolbox/network-helpers");
const fs = require("fs");
const path = require("path");

// Role enum: 1 Architect, 2 Structural, 3 MEP, 4 Cost, 5 Sustainability, 6 Client
const ROLES = [1, 2, 3, 4, 5, 6];
const bit = (r) => 1 << r;
const PARAMS = {
  thetaBps: 5000, alphaBps: 1000, sigmaBps: 3000, capBps: 2500,
  maxRounds: 20, windowSeconds: 48 * 3600,
  mandatoryMask: bit(1) | bit(2) | bit(6),
  vetoMask: bit(2) | bit(6),
  verifiableMask: bit(2) | bit(3) | bit(4) | bit(5),
  tolerance: 50,                       // 0.05 on a 0..1 scale
  slashBps: 5000,                      // 50 % of stake
  minStake: ethers.parseEther("1"),
  challengeBond: ethers.parseEther("0.1")
};

const gasLog = {};
function logGas(fn, receipt) { (gasLog[fn] = gasLog[fn] || []).push(Number(receipt.gasUsed)); }
const h = (t) => ethers.keccak256(ethers.toUtf8Bytes(t));
async function tx(label, p) { const r = await (await p).wait(); if (label) logGas(label, r); return r; }

async function deploy(nAgents) {
  const s = await ethers.getSigners();
  const auditor = s[s.length - 1];
  const C = await ethers.getContractFactory("HQC2");
  const c = await C.deploy(PARAMS, auditor.address);
  const dr = await c.deploymentTransaction().wait();
  logGas("deploy", dr);
  const agents = [];
  for (let i = 0; i < nAgents; i++) {
    const role = ROLES[i % 6];
    await tx("registerAgent", c.registerAgent(s[i + 1].address, role, 1));
    await tx("deposit", c.connect(s[i + 1]).deposit({ value: PARAMS.minStake }));
    agents.push({ signer: s[i + 1], role });
  }
  return { c, agents, auditor };
}
const byRole = (ag, role, nth = 0) => ag.filter((a) => a.role === role)[nth];
async function propose(c, a, k) {
  const alts = Array.from({ length: k }, (_, i) => h("ifc-delta-" + i));
  await tx("proposeChange", c.connect(a.signer).proposeChange(alts));
  return await c.requestCount();
}

describe("HQC2 protocol", function () {
  this.timeout(600000);

  it("commits after quorum and an unvetoed, unchallenged window", async function () {
    const { c, agents: ag } = await deploy(6);
    const cId = await propose(c, ag[0], 3);
    const plan = [[1, 850], [1, 800], [1, 780], [0, 700], [1, 760], [1, 820]];
    for (let i = 0; i < 6; i++) await tx("endorse", c.connect(ag[i].signer).endorse(cId, plan[i][0], plan[i][1], h("ev" + i)));
    await tx("stopSignal", c.connect(ag[3].signer).stopSignal(cId, 1, h("objection")));
    await tx("tick", c.tick(cId));
    let r = await c.getRequest(cId);
    expect(r[0]).to.equal(2n); expect(r[3]).to.equal(1n);
    await time.increase(PARAMS.windowSeconds + 1);
    await tx("commit", c.commit(cId));
    r = await c.getRequest(cId);
    expect(r[0]).to.equal(3n);
    await tx("release", c.connect(ag[0].signer).release(cId));
  });

  it("upholds a challenge: removes support, restores stop-signal cuts, slashes and pays the challenger", async function () {
    const { c, agents: ag, auditor } = await deploy(6);
    const cId = await propose(c, ag[0], 3);
    const cost = byRole(ag, 4);      // colluder in a verifiable role
    const arch = byRole(ag, 1);
    await tx("endorse", c.connect(arch.signer).endorse(cId, 0, 800, h("a")));
    await tx(null, c.connect(cost.signer).endorse(cId, 2, 1000, h("fabricated cost report")));
    const before0 = await c.support(cId, 0);
    await tx(null, c.connect(cost.signer).stopSignal(cId, 0, h("fake objection")));
    expect(await c.support(cId, 0)).to.be.lt(before0);

    const stakeBefore = (await c.agents(cost.signer.address)).stake;
    await tx("challenge", c.connect(arch.signer).challenge(cId, cost.signer.address, 310, h("recomputed QTO"), { value: PARAMS.challengeBond }));
    await expect(c.tick(cId)).to.not.be.reverted;          // evaluation continues while a challenge is open
    await tx("resolveChallenge (upheld)", c.connect(auditor).resolveChallenge(1, 300));

    const ch = await c.challenges(1);
    expect(ch.state).to.equal(2n);                            // Upheld
    expect(await c.support(cId, 2)).to.equal(0n);             // fraudulent support removed
    expect(await c.roleMask(cId, 2)).to.equal(0n);
    expect(await c.support(cId, 0)).to.be.gt(0n);
    expect(await c.suspended(cId, cost.signer.address)).to.equal(true);
    const stakeAfter = (await c.agents(cost.signer.address)).stake;
    expect(stakeAfter).to.equal(stakeBefore / 2n);
    expect(await c.credit(arch.signer.address)).to.equal(PARAMS.challengeBond + stakeBefore / 4n);
    await expect(c.connect(cost.signer).stopSignal(cId, 0, h("x"))).to.be.revertedWith("suspended");
  });

  it("rejects a challenge within tolerance and pays the bond to the accused", async function () {
    const { c, agents: ag, auditor } = await deploy(6);
    const cId = await propose(c, ag[0], 2);
    const mep = byRole(ag, 3); const arch = byRole(ag, 1);
    await tx(null, c.connect(mep.signer).endorse(cId, 1, 720, h("energy model")));
    await tx(null, c.connect(arch.signer).challenge(cId, mep.signer.address, 600, h("my recompute"), { value: PARAMS.challengeBond }));
    await tx("resolveChallenge (rejected)", c.connect(auditor).resolveChallenge(1, 700));
    expect((await c.challenges(1)).state).to.equal(3n);       // Rejected
    expect(await c.credit(mep.signer.address)).to.equal(PARAMS.challengeBond);
    expect(await c.support(cId, 1)).to.be.gt(0n);
  });

  it("does not allow challenges against subjective roles (architect, client)", async function () {
    const { c, agents: ag } = await deploy(6);
    const cId = await propose(c, ag[0], 2);
    const client = byRole(ag, 6); const st = byRole(ag, 2);
    await tx(null, c.connect(client.signer).endorse(cId, 0, 900, h("client brief")));
    await expect(c.connect(st.signer).challenge(cId, client.signer.address, 100, h("x"), { value: PARAMS.challengeBond }))
      .to.be.revertedWith("not verifiable");
  });

  it("blocks commit while a challenge is open, and reopens evaluation if the candidate loses fraudulent support", async function () {
    const { c, agents: ag, auditor } = await deploy(6);
    const cId = await propose(c, ag[0], 2);
    for (let i = 0; i < 6; i++) await tx(null, c.connect(ag[i].signer).endorse(cId, 0, 900, h("e" + i)));
    await tx(null, c.tick(cId));
    expect((await c.getRequest(cId))[0]).to.equal(2n);
    const st = byRole(ag, 2); const mep = byRole(ag, 3);
    await tx(null, c.connect(mep.signer).challenge(cId, st.signer.address, 200, h("clash report"), { value: PARAMS.challengeBond }));
    await time.increase(PARAMS.windowSeconds + 1);
    await expect(c.commit(cId)).to.be.revertedWith("open challenge");
    await tx(null, c.connect(auditor).resolveChallenge(1, 210));
    const r = await c.getRequest(cId);
    expect(r[0]).to.equal(1n);                                // back to Evaluating
    expect(Number(await c.roleMask(cId, 0)) & PARAMS.mandatoryMask).to.not.equal(PARAMS.mandatoryMask); // structural role lost
  });

  it("lets a veto role reopen evaluation and halves the candidate's support", async function () {
    const { c, agents: ag } = await deploy(6);
    const cId = await propose(c, ag[0], 2);
    for (let i = 0; i < 6; i++) await tx(null, c.connect(ag[i].signer).endorse(cId, 0, 900, h("e" + i)));
    await tx(null, c.tick(cId));
    const before = await c.support(cId, 0);
    const st = byRole(ag, 2); const mep = byRole(ag, 3);
    await expect(c.connect(mep.signer).veto(cId, h("x"))).to.be.revertedWith("no veto right");
    await tx("veto", c.connect(st.signer).veto(cId, h("clash with core wall")));
    expect((await c.getRequest(cId))[0]).to.equal(1n);
    expect(await c.support(cId, 0)).to.equal(before / 2n);
  });

  it("lets an agent re-dance once per round and abandon its endorsement", async function () {
    const { c, agents: ag } = await deploy(6);
    const cId = await propose(c, ag[0], 2);
    const a = ag[1];
    await tx(null, c.connect(a.signer).endorse(cId, 0, 800, h("e1")));
    await expect(c.connect(a.signer).endorse(cId, 0, 800, h("e1"))).to.be.revertedWith("once per round");
    await tx(null, c.tick(cId));
    const decayed = await c.support(cId, 0);
    expect(decayed).to.be.lt(800n);
    await tx("endorse (re-dance)", c.connect(a.signer).endorse(cId, 1, 700, h("e2")));
    expect(await c.support(cId, 0)).to.equal(0n);
    expect(await c.support(cId, 1)).to.equal(700n);
    await tx("abandon", c.connect(a.signer).abandon(cId));
    expect(await c.support(cId, 1)).to.equal(0n);
    expect(await c.roleMask(cId, 1)).to.equal(0n);
    await expect(c.connect(a.signer).abandon(cId)).to.be.revertedWith("no endorsement");
  });

  it("prevents stake withdrawal while the agent is in an unfinished request", async function () {
    const { c, agents: ag } = await deploy(6);
    const cId = await propose(c, ag[0], 2);
    await tx(null, c.connect(ag[1].signer).endorse(cId, 0, 800, h("e")));
    await expect(c.connect(ag[1].signer).withdraw(1n)).to.be.revertedWith("open requests");
    await expect(c.connect(ag[2].signer).endorse(cId, 0, 800, h("e"))).to.not.be.reverted;
    await expect(c.connect(ag[1].signer).endorse(cId, 0, 800, h("e"))).to.be.revertedWith("once per round");
  });

  it("measures gas per decision with and without one upheld challenge", async function () {
    const scaling = [];
    for (const n of [6, 12, 24, 48]) {
      for (const withChallenge of [false, true]) {
        const { c, agents: ag, auditor } = await deploy(n);
        let total = 0n;
        const add = (r) => { total += r.gasUsed; };
        add(await tx(null, c.connect(ag[0].signer).proposeChange([h("a0"), h("a1"), h("a2")])));
        const cId = await c.requestCount();
        for (let i = 0; i < n; i++) {
          const alt = i % 4 === 3 ? 0 : 1;
          add(await tx(null, c.connect(ag[i].signer).endorse(cId, alt, 700 + (i % 5) * 50, h("ev" + i))));
          if (alt === 0) add(await tx(null, c.connect(ag[i].signer).stopSignal(cId, 1, h("st" + i))));
        }
        if (withChallenge) {
          const accused = ag.find((a, i) => i % 4 === 3 && [2, 3, 4, 5].includes(a.role));
          add(await tx(null, c.connect(ag[0].signer).challenge(cId, accused.signer.address, 100, h("re"), { value: PARAMS.challengeBond })));
          add(await tx(null, c.connect(auditor).resolveChallenge(1, 100)));
        }
        let r = await c.getRequest(cId); let ticks = 0;
        while (r[0] === 1n && ticks < PARAMS.maxRounds) { add(await tx(null, c.tick(cId))); ticks++; r = await c.getRequest(cId); }
        if (r[0] === 2n) { await time.increase(PARAMS.windowSeconds + 1); add(await tx(null, c.commit(cId))); r = await c.getRequest(cId); }
        scaling.push({ agents: n, withChallenge, totalGasPerDecision: Number(total), ticks, finalState: Number(r[0]) });
      }
    }
    gasLog.scaling = scaling;
  });

  after(function () {
    const perFunction = {};
    for (const [fn, v] of Object.entries(gasLog)) {
      if (fn === "scaling") continue;
      perFunction[fn] = { calls: v.length, min: Math.min(...v), max: Math.max(...v), mean: Math.round(v.reduce((a, b) => a + b, 0) / v.length) };
    }
    const params = Object.fromEntries(Object.entries(PARAMS).map(([k, v]) => [k, typeof v === "bigint" ? v.toString() : v]));
    const out = { generatedAt: new Date().toISOString(), params, perFunction, scaling: gasLog.scaling || [] };
    const dir = path.join(__dirname, "..", "results");
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, "gas_results_v2.json"), JSON.stringify(out, null, 2));
    console.log("\nGas results written to results/gas_results_v2.json");
    console.log(JSON.stringify(out, null, 2));
  });
});
