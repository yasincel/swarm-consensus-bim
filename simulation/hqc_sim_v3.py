"""HQC simulation v3: multi-criteria stakeholders, two-stage HQC, MCDM baselines.

Changes from v2 (addressing "stronger validation"):
  * Each alternative has a score on six role-specific criteria (architecture, structure,
    MEP, cost, carbon, client). An agent observes only its own role's criterion, so
    stakeholders disagree legitimately, not just through noise. True overall quality is
    the weighted sum of criteria; "correct" means choosing the alternative with the
    highest overall quality.
  * Verifiability depends on the criterion: structure, MEP, cost and carbon are computed
    by deterministic checks and can be re-verified; architecture and client judgements
    cannot. A colluder's fabricated evidence is exposed with probability p_detect only on
    verifiable criteria.
  * New baselines: weighted-sum multi-criteria scoring (MCDM) with full review (every agent
    scores every alternative; a strong, centrally coordinated benchmark) and with a review
    budget of 3 alternatives per agent.
  * New method HQC-2S: HQC shortlists three alternatives (by cumulative endorsement
    weight), then a full-review weighted-sum score decides among the finalists.
Cost is evaluation effort (alternative evaluations per decision).
"""
import itertools, json, math, os, random, sys, time
from multiprocessing import Pool
import pandas as pd

ROLES = ["architect", "structural", "mep", "cost", "carbon", "client"]
VERIFIABLE = {1, 2, 3, 4}          # structural, mep, cost, carbon
MANDATORY = {0, 1, 5}              # architect, structural, client
W = [1 / 6] * 6                    # criterion weights
T_MAX, ACCEPT, BUDGET, SHORTLIST = 60, 0.5, 3, 3


def make_alternatives(k, gap, het, rng):
    """Criterion matrix x[i][c]; overall quality Q[i] = sum_c W[c] x[i][c]."""
    base = [0.75, 0.75 - gap] + [rng.uniform(0.30, 0.55) for _ in range(k - 2)]
    x = [[min(1, max(0, b + rng.gauss(0, het))) for _ in ROLES] for b in base]
    # re-centre so each alternative's overall quality equals its base exactly
    for i, b in enumerate(base):
        d = b - sum(W[c] * x[i][c] for c in range(6))
        x[i] = [min(1, max(0, v + d)) for v in x[i]]
    q = [sum(W[c] * x[i][c] for c in range(6)) for i in range(k)]
    return x, q


def obs(x, i, role, noise, rng):
    return min(1.0, max(0.0, rng.gauss(x[i][role], noise)))


# ------------------------------------------------------------------ HQC
def hqc(n, x, noise, colluders, target, rng, theta=0.5, rho=0.6, p_disc=0.3, alpha=0.5,
        sigma=0.1, stop_eff=0.5, quality_weight=True, decay=True, verify=True, p_detect=0.5,
        record=None):
    k = len(x)
    roles = [i % 6 for i in range(n)]
    alt, w, sc = [-1] * n, [0.0] * n, [0.0] * n
    active = set(colluders)
    evals = 0
    cum = [0.0] * k                                  # cumulative endorsement weight (for shortlist)

    def caught(i):
        return verify and roles[i] in VERIFIABLE and rng.random() < p_detect

    for c in list(colluders):
        if caught(c):
            active.discard(c)
        else:
            alt[c], w[c], sc[c] = target, 1.0, 1.0
    for t in range(1, T_MAX + 1):
        dance = [0.0] * k
        for i in range(n):
            if alt[i] >= 0:
                dance[alt[i]] += w[i]
        D = sum(dance)
        new_alt, new_w = alt[:], w[:]
        for i in range(n):
            if i in colluders:
                continue
            if alt[i] < 0:
                ch = -1
                if D > 0 and rng.random() < min(1.0, rho * D / n):
                    r, acc = rng.random() * D, 0.0
                    for j in range(k):
                        acc += dance[j]
                        if r <= acc:
                            ch = j
                            break
                elif rng.random() < p_disc:
                    ch = rng.randrange(k)
                if ch >= 0:
                    s = obs(x, ch, roles[i], noise, rng)
                    evals += 1
                    if s >= ACCEPT:
                        new_alt[i], new_w[i], sc[i] = ch, (s if quality_weight else 1.0), s
            elif decay and rng.random() < alpha * (1.0 - sc[i]):
                new_alt[i], new_w[i] = -1, 0.0
        if sigma > 0:
            for i in range(n):
                if alt[i] < 0 or (i in colluders and i not in active):
                    continue
                is_c = i in active
                if rng.random() < (1.0 if is_c else sigma):
                    if is_c and caught(i):
                        active.discard(i)
                        new_alt[i], new_w[i] = -1, 0.0
                        continue
                    riv = [j for j in range(n) if new_alt[j] >= 0 and new_alt[j] != alt[i] and j not in active]
                    if riv:
                        j = rng.choice(riv)
                        if rng.random() < stop_eff:
                            new_alt[j], new_w[j] = -1, 0.0
        alt, w = new_alt, new_w
        sup, rs = [0.0] * k, [set() for _ in range(k)]
        for i in range(n):
            if alt[i] >= 0:
                sup[alt[i]] += w[i]
                rs[alt[i]].add(roles[i])
        for j in range(k):
            cum[j] += sup[j]
        best = max(range(k), key=lambda j: sup[j])
        if sup[best] >= theta * n and MANDATORY <= rs[best]:
            if record is not None:
                record.update(cum=cum, active=active)
            return best, t, evals
    if record is not None:
        record.update(cum=cum, active=active)
    return None, T_MAX, evals


# ------------------------------------------------------------------ scoring baselines
def mcdm(n, x, noise, colluders, target, rng, alts=None, budget=None, verify=False, p_detect=0.5,
         exclude=frozenset()):
    """Weighted-sum scoring: mean score per (alternative, criterion), weighted by W.
    Colluders report 1 for the target and 0 elsewhere; with verify, a fabricated report on a
    verifiable criterion is exposed with p_detect and that agent's reports are discarded."""
    k = len(x)
    alts = list(range(k)) if alts is None else list(alts)
    roles = [i % 6 for i in range(n)]
    sums = {(a, c): [0.0, 0] for a in alts for c in range(6)}
    evals = 0
    for i in range(n):
        if i in exclude:
            continue
        seen = alts if budget is None else rng.sample(alts, min(budget, len(alts)))
        r = roles[i]
        if i in colluders:
            if verify and r in VERIFIABLE and rng.random() < p_detect:
                continue
            for a in seen:
                sums[(a, r)][0] += 1.0 if a == target else 0.0
                sums[(a, r)][1] += 1
            continue
        for a in seen:
            sums[(a, r)][0] += obs(x, a, r, noise, rng)
            sums[(a, r)][1] += 1
            evals += 1

    def score(a):
        tot = 0.0
        for c in range(6):
            s, m = sums[(a, c)]
            tot += W[c] * (s / m if m else 0.5)      # missing criterion -> neutral prior
        return tot
    best = max(alts, key=score)
    return best, 1, evals


def vote(n, x, noise, colluders, target, rng, rule, budget=None):
    k = len(x)
    roles = [i % 6 for i in range(n)]
    evals = 0
    for t in range(1, T_MAX + 1):
        votes = [0] * k
        for i in range(n):
            if i in colluders:
                votes[target] += 1
                continue
            seen = range(k) if budget is None else rng.sample(range(k), min(budget, k))
            bj, bs = None, -1
            for j in seen:
                s = obs(x, j, roles[i], noise, rng)
                evals += 1
                if s > bs:
                    bj, bs = j, s
            votes[bj] += 1
        top = max(votes)
        win = [j for j in range(k) if votes[j] == top]
        if rule == "unanimity" and top == n:
            return win[0], t, evals
        if rule == "majority" and len(win) == 1:
            return win[0], t, evals
    return None, T_MAX, evals


def hqc_two_stage(n, x, noise, colluders, target, rng, **p):
    rec = {}
    c1, t1, e1 = hqc(n, x, noise, colluders, target, rng, record=rec, **p)
    k = len(x)
    if k <= SHORTLIST:
        short = list(range(k))
    else:
        short = sorted(range(k), key=lambda j: -rec["cum"][j])[:SHORTLIST]
        if c1 is not None and c1 not in short:
            short[-1] = c1
    exposed = frozenset(set(colluders) - rec["active"])          # slashed in stage 1 stay out
    c2, _, e2 = mcdm(n, x, noise, colluders, target, rng, alts=short, verify=p.get("verify", True),
                     p_detect=p.get("p_detect", 0.5), exclude=exposed)
    return c2, t1 + 1, e1 + e2


def methods(P):
    return {
        "HQC": lambda a: hqc(*a, **P),
        "HQC-2S": lambda a: hqc_two_stage(*a, **P),
        "HQC unverified": lambda a: hqc(*a, **{**P, "verify": False}),
        "A1 no cross-inhibition": lambda a: hqc(*a, **{**P, "sigma": 0.0}),
        "A2 no quality weighting": lambda a: hqc(*a, **{**P, "quality_weight": False}),
        "A3 no decay": lambda a: hqc(*a, **{**P, "decay": False}),
        "B1 unanimity": lambda a: vote(*a, rule="unanimity"),
        "B2 majority, full review": lambda a: vote(*a, rule="majority"),
        "B2b majority, budget 3": lambda a: vote(*a, rule="majority", budget=BUDGET),
        "B4 weighted scoring, full review": lambda a: mcdm(*a, verify=True, p_detect=P.get("p_detect", 0.5)),
        "B4b weighted scoring, budget 3": lambda a: mcdm(*a, budget=BUDGET, verify=True, p_detect=P.get("p_detect", 0.5)),
    }


def one_rep(n, k, gap, noise, f, het, seed, fns):
    x, q = make_alternatives(k, gap, het, random.Random(seed))
    nc = int(round(f * n))
    coll = set(range(n - nc, n))
    target = k - 1
    top = max(q)
    rows = []
    for m_idx, (name, fn) in enumerate(fns.items()):
        c, t, ev = fn((n, x, noise, coll, target, random.Random(seed * 17 + m_idx)))
        rows.append(dict(n=n, k=k, gap=gap, noise=noise, f=f, het=het, seed=seed, method=name,
                         decided=c is not None, correct=c is not None and q[c] >= top - 1e-9,
                         regret=(top - q[c]) if c is not None else None, rounds=t, evals=ev))
    return rows


# ------------------------------------------------------------------ calibration (disjoint seeds)
CAL = dict(rho=[0.6, 1.0], p_disc=[0.15, 0.3], alpha=[0.3, 0.5], sigma=[0.1, 0.3])


def cal_cell(args):
    P, reps = args
    out = []
    for (n, k, gap, f) in [(12, 3, 0.05, 0), (12, 3, 0.15, 0), (12, 10, 0.15, 0), (12, 10, 0.05, 0.17), (12, 3, 0.15, 0.17)]:
        for r in range(reps):
            seed = 50_000_000 + r * 131 + k * 7 + int(gap * 100)
            x, q = make_alternatives(k, gap, 0.10, random.Random(seed))
            nc = int(round(f * n))
            c, t, e = hqc(n, x, 0.15, set(range(n - nc, n)), k - 1, random.Random(seed + 1), **P)
            out.append((c is not None and q[c] >= max(q) - 1e-9, c is None, t))
    acc = sum(o[0] for o in out) / len(out); esc = sum(o[1] for o in out) / len(out)
    rnd = sum(o[2] for o in out) / len(out)
    return dict(**P, acc=acc, escalated=esc, rounds=rnd, objective=acc - 0.5 * esc - 0.002 * rnd)


def calibrate(reps=80):
    combos = [dict(zip(CAL, v)) for v in itertools.product(*CAL.values())]
    with Pool(2) as pool:
        res = pool.map(cal_cell, [(c, reps) for c in combos])
    df = pd.DataFrame(res).sort_values("objective", ascending=False)
    df.to_csv("results/calibration_v3.csv", index=False)
    best = {k: float(df.iloc[0][k]) for k in CAL}
    return {**best, "theta": 0.5, "stop_eff": 0.5, "p_detect": 0.5}


def test_cell(args):
    n, k, gap, noise, f, het, reps, P = args
    fns = methods(P)
    rows = []
    for r in range(reps):
        seed = r * 9973 + n * 31 + k * 17 + int(gap * 1000) + int(noise * 100) * 3 + int(f * 100) * 5 + int(het * 100) * 11
        rows += one_rep(n, k, gap, noise, f, het, seed, fns)
    return rows


if __name__ == "__main__":
    os.makedirs("results", exist_ok=True)
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    t0 = time.time()
    P = calibrate()
    json.dump(P, open("results/frozen_params_v3.json", "w"), indent=2)
    print("frozen:", P, f"{time.time()-t0:.0f}s")
    grid = list(itertools.product([6, 12, 24], [3, 10, 20], [0.0, 0.05, 0.15], [0.05, 0.15, 0.30], [0.0, 0.17, 0.33], [0.10]))
    grid += list(itertools.product([12], [3, 10, 20], [0.05, 0.15], [0.15], [0.0], [0.0, 0.20]))   # heterogeneity sensitivity
    with Pool(2) as pool:
        out = pool.map(test_cell, [(*g, reps, P) for g in grid], chunksize=3)
    pd.DataFrame([r for rows in out for r in rows]).to_csv("results/sim_runs_v3.csv.gz", index=False)
    print(f"grid done: {len(grid)} cells, {time.time()-t0:.0f}s")
