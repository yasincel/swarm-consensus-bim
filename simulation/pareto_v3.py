"""Supplementary runs for v3:
  (1) accuracy-effort Pareto curve: weighted scoring and majority with review budgets 1..k,
      compared with HQC and HQC-2S at the same cells;
  (2) ablation of HQC-2S robustness: stage-1 slashing NOT carried into stage 2 ("no carry-over");
  (3) audit-matched weighted scoring: each colluder faces as many independent audit draws as it
      would face in HQC (one per evidence-bearing action), to test whether HQC-2S's robustness is
      just more auditing.
"""
import json, random, itertools, math
from multiprocessing import Pool
import pandas as pd
import hqc_sim_v3 as s

P = json.load(open("results/frozen_params_v3.json"))


def two_stage_nocarry(n, x, noise, coll, tgt, rng):
    rec = {}
    c1, t1, e1 = s.hqc(n, x, noise, coll, tgt, rng, record=rec, **P)
    k = len(x)
    short = list(range(k)) if k <= 3 else sorted(range(k), key=lambda j: -rec["cum"][j])[:3]
    c2, _, e2 = s.mcdm(n, x, noise, coll, tgt, rng, alts=short, verify=True, p_detect=P["p_detect"])
    return c2, t1 + 1, e1 + e2


def mcdm_audit_matched(n, x, noise, coll, tgt, rng, audits=10):
    """Colluders on verifiable criteria are audited `audits` times; any hit excludes them."""
    roles = [i % 6 for i in range(n)]
    caught = {c for c in coll if roles[c] in s.VERIFIABLE and
              any(rng.random() < P["p_detect"] for _ in range(audits))}
    return s.mcdm(n, x, noise, coll, tgt, rng, verify=False, exclude=frozenset(caught))


def cell(args):
    n, k, gap, noise, f, reps = args
    rows = []
    for r in range(reps):
        seed = 7_000_000 + r * 7919 + n * 13 + k * 101 + int(gap * 1000) + int(f * 100) * 3 + int(noise * 100)
        x, q = s.make_alternatives(k, gap, 0.10, random.Random(seed))
        nc = int(round(f * n)); coll = set(range(n - nc, n)); tgt = k - 1; top = max(q)
        fns = {"HQC": lambda a: s.hqc(*a, **P), "HQC-2S": lambda a: s.hqc_two_stage(*a, **P),
               "HQC-2S no carry-over": lambda a: two_stage_nocarry(*a),
               "Weighted scoring, audit-matched": lambda a: mcdm_audit_matched(*a)}
        for b in sorted({1, 2, 3, 5, 7, 10, 15, k}):
            if b <= k:
                fns[f"WS b={b}"] = (lambda bb: lambda a: s.mcdm(*a, budget=bb, verify=True, p_detect=P["p_detect"]))(b)
                fns[f"MAJ b={b}"] = (lambda bb: lambda a: s.vote(*a, rule="majority", budget=bb))(b)
        for i, (m, fn) in enumerate(fns.items()):
            c, t, e = fn((n, x, noise, coll, tgt, random.Random(seed * 31 + i)))
            rows.append(dict(n=n, k=k, gap=gap, noise=noise, f=f, method=m,
                             correct=c is not None and q[c] >= top - 1e-9, evals=e))
    return rows


if __name__ == "__main__":
    grid = list(itertools.product([12], [10, 20], [0.05, 0.15], [0.05, 0.15, 0.30], [0.0, 0.17, 0.33], [300]))
    with Pool(2) as p:
        out = p.map(cell, grid)
    pd.DataFrame([r for o in out for r in o]).to_csv("results/pareto_v3.csv.gz", index=False)
    print("done")
