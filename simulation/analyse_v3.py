"""Tables, paired tests and the collusion figure for simulation v3."""
import json, math
import pandas as pd, numpy as np
from scipy.stats import binomtest
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt

def wilson(k, n, z=1.96):
    p = k / n; d = 1 + z*z/n; c = (p + z*z/(2*n)) / d; h = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / d
    return c - h, c + h

def mcnemar(a, b):
    b01 = int(((a == 1) & (b == 0)).sum()); b10 = int(((a == 0) & (b == 1)).sum())
    p = binomtest(b01, b01 + b10, 0.5).pvalue if b01 + b10 else 1.0
    return b01, b10, p

d = pd.read_csv("results/sim_runs_v3.csv.gz"); d = d[d.het == 0.10]
tab = d.groupby(["f", "k", "method"]).agg(acc=("correct", "mean"), esc=("decided", lambda x: 1 - x.mean()),
                                          evals=("evals", "mean"), regret=("regret", "mean"), N=("correct", "size")).reset_index()
tab.to_csv("results/table_main_v3.csv", index=False)

p = pd.read_csv("results/pareto_v3.csv.gz")
p["rep"] = p.groupby(["n", "k", "gap", "noise", "f", "method"]).cumcount()
key = ["k", "gap", "noise", "f", "rep"]
piv = p.pivot_table(index=key, columns="method", values="correct").reset_index()
ev = p.groupby(["k", "f", "method"]).evals.mean()
tests = []
for k in [10, 20]:
    for f in [0.0, 0.17, 0.33]:
        s = piv[(piv.k == k) & (piv.f == f)]
        e2s = ev[(k, f, "HQC-2S")]
        # budget-matched comparator: the weighted-scoring budget whose effort is closest to HQC-2S
        cands = [m for m in ev.loc[(k, f)].index if m.startswith("WS b=")]
        wm = min(cands, key=lambda m: abs(ev[(k, f, m)] - e2s))
        for other in [wm, "Weighted scoring, audit-matched", "HQC", "HQC-2S no carry-over"]:
            b01, b10, pv = mcnemar(s["HQC-2S"].astype(int), s[other].astype(int))
            n = len(s); lo, hi = wilson(int(s["HQC-2S"].sum()), n)
            tests.append(dict(k=k, f=f, comparator=other, comparator_evals=round(ev[(k, f, other)], 1), hqc2s_evals=round(e2s, 1),
                              hqc2s_acc=round(s["HQC-2S"].mean(), 3), hqc2s_ci=f"[{lo:.3f}, {hi:.3f}]",
                              comparator_acc=round(s[other].mean(), 3), only_hqc2s_correct=b01, only_comparator_correct=b10,
                              p_exact=float(f"{pv:.2g}"), n=n))
t = pd.DataFrame(tests); t.to_csv("results/mcnemar_v3.csv", index=False)
pd.set_option("display.width", 250); print(t.to_string(index=False))

# collusion figure (n = 12, both k)
plt.rcParams.update({"font.family": "Liberation Sans", "font.size": 9})
fig, ax = plt.subplots(1, 2, figsize=(9, 3.6), sharey=True)
styles = [("HQC", "o", "-", "white"), ("HQC-2S", "D", "-", "black"), ("HQC-2S no carry-over", "d", "--", "white"),
          ("WS b=5", "s", ":", "white"), ("Weighted scoring, audit-matched", "*", "-", "black")]
labels = {"Weighted scoring, audit-matched": "Weighted scoring, full review, audit-matched", "WS b=5": "Weighted scoring, budget 5", "HQC-2S no carry-over": "HQC-2S, no carry-over"}
for a, k in zip(ax, [10, 20]):
    g = p[p.k == k].groupby(["method", "f"]).correct.mean()
    for m, mk, ls, fc in styles:
        a.plot([0, 0.17, 0.33], [g[(m, f)] for f in [0, 0.17, 0.33]], ls, marker=mk, color="black", mfc=fc, ms=6, lw=1, label=labels.get(m, m))
    a.set_title(f"k = {k} alternatives"); a.set_xlabel("Share of colluding agents f"); a.set_xticks([0, 0.17, 0.33])
    a.grid(alpha=.25, lw=.5); a.set_ylim(0, 1)
    for sp in ["top", "right"]: a.spines[sp].set_visible(False)
ax[0].set_ylabel("Accuracy")
h, l = ax[0].get_legend_handles_labels(); fig.legend(h, l, loc="lower center", ncol=3, frameon=False, fontsize=8)
fig.tight_layout(rect=(0, 0.14, 1, 1)); fig.savefig("figs/Fig_v3_collusion.png", dpi=300)
