"""Accuracy-effort figure (Fig. 4 in the revised manuscript) from results/pareto_v3.csv.gz."""
import pandas as pd, matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
plt.rcParams.update({"font.family": "Liberation Sans", "font.size": 9})
d = pd.read_csv("results/pareto_v3.csv.gz")
fig, ax = plt.subplots(2, 3, figsize=(10, 6), sharey=True)
for i, k in enumerate([10, 20]):
    for j, f in enumerate([0, 0.17, 0.33]):
        a = ax[i, j]; s = d[(d.k == k) & (d.f == f)]
        g = s.groupby("method").agg(acc=("correct", "mean"), ev=("evals", "mean"))
        for pre, mk, ls, lab in [("WS b=", "s", "-", "Weighted scoring (budget b)"), ("MAJ b=", "^", "--", "Majority (budget b)")]:
            r = g[g.index.str.startswith(pre)].sort_values("ev")
            a.plot(r.ev, r.acc, ls, marker=mk, color="0.45", ms=4, lw=1, label=lab, mfc="white")
        for m, mk, lab in [("HQC", "o", "HQC"), ("HQC-2S", "D", "HQC-2S"), ("HQC-2S no carry-over", "d", "HQC-2S, no carry-over"),
                           ("Weighted scoring, audit-matched", "*", "Weighted scoring, full review, audit-matched")]:
            a.plot(g.loc[m, "ev"], g.loc[m, "acc"], mk, color="black", ms=8 if mk == "*" else 6, label=lab,
                   mfc="black" if m in ("HQC-2S", "Weighted scoring, audit-matched") else "white")
        a.set_title(f"k = {k}, colluders f = {f}", fontsize=9); a.grid(alpha=.25, lw=.5); a.set_ylim(0, 1)
        if i == 1: a.set_xlabel("Evaluations per decision")
        if j == 0: a.set_ylabel("Accuracy")
        for sp in ["top", "right"]: a.spines[sp].set_visible(False)
h, l = ax[0, 0].get_legend_handles_labels()
fig.legend(h, l, loc="lower center", ncol=3, frameon=False, fontsize=8)
fig.tight_layout(rect=(0, 0.09, 1, 1)); fig.savefig("figures/Fig_v3_pareto.png", dpi=300)
