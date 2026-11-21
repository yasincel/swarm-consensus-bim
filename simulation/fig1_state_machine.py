"""Fig. 1 - HQC state machine, black and white, journal style."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Polygon

plt.rcParams.update({"font.family": "Liberation Sans", "pdf.fonttype": 42, "ps.fonttype": 42})
K = "black"
GRAY = "black"

fig, ax = plt.subplots(figsize=(7.48, 3.0))          # 190 mm double-column width
ax.set_xlim(0, 1090); ax.set_ylim(440, 40); ax.axis("off")

W, H = 160, 66
Y = 200
X = dict(prop=90, eval=300, quorum=505, window=715, done=1000)
HUMAN = (210, 365)


def node(cx, cy, title, sub, kind="process", w=W, h=H):
    if kind == "terminal":                 # start / end states: pill shape
        style, lw = f"round,pad=0,rounding_size={h/2}", 1.6
    else:                                  # process states: rectangle
        style, lw = "round,pad=0,rounding_size=4", 1.0
    ax.add_patch(FancyBboxPatch((cx - w / 2, cy - h / 2), w, h, boxstyle=style,
                                fc="white", ec=K, lw=lw, zorder=2))
    ax.text(cx, cy - 10, title, ha="center", va="center", fontsize=8.5, fontweight="bold", zorder=3)
    ax.text(cx, cy + 13, sub, ha="center", va="center", fontsize=7, color=GRAY, zorder=3)


def line(points, label=None, at=None, ha="center", va="bottom"):
    xs, ys = zip(*points)
    ax.plot(xs, ys, color=K, lw=0.9, zorder=1, solid_capstyle="butt")
    ax.annotate("", xy=points[-1], xytext=points[-2],
                arrowprops=dict(arrowstyle="-|>,head_length=0.55,head_width=0.28", color=K, lw=0.9,
                                shrinkA=0, shrinkB=0), zorder=1)
    if label:
        ax.text(*at, label, ha=ha, va=va, fontsize=7.5, style="italic", zorder=3)


# nodes
node(X["prop"], Y, "Proposed", "k IFC alternatives", "terminal")
node(X["eval"], Y, "Evaluating", "dance · stop · decay")
qx, dw, dh = X["quorum"], 72, 52
ax.add_patch(Polygon([(qx - dw, Y), (qx, Y - dh), (qx + dw, Y), (qx, Y + dh)], closed=True,
                     fc="white", ec=K, lw=1.0, zorder=2))
ax.text(qx, Y - 6, "Quorum", ha="center", va="center", fontsize=8.5, fontweight="bold", zorder=3)
ax.text(qx, Y + 12, "reached?", ha="center", va="center", fontsize=7.5, zorder=3)
node(X["window"], Y, "Commit window", "veto period τ (piping)")
node(X["done"], Y, "Committed", "hash on-chain", "terminal")
node(*HUMAN, "Human decision", "full support history", "terminal")

# main flow
line([(X["prop"] + W / 2, Y), (X["eval"] - W / 2, Y)])
line([(X["eval"] + W / 2, Y), (qx - dw, Y)])
line([(qx + dw, Y), (X["window"] - W / 2, Y)], "yes", ((qx + dw + X["window"] - W / 2) / 2, Y - 6))
line([(X["window"] + W / 2, Y), (X["done"] - W / 2, Y)], "no veto", ((X["window"] + X["done"]) / 2, Y - 6))
ax.text((X["window"] + X["done"]) / 2, Y + 6, "no open\nchallenge", ha="center", va="top", fontsize=7.5, style="italic")

# loop back: no quorum
top = 85
line([(qx, Y - dh), (qx, top), (X["eval"], top), (X["eval"], Y - H / 2)],
     "no: next round, support decays", ((qx + X["eval"]) / 2, top - 6))

# veto: reopen
low = 290
vx = X["window"] - 55
line([(vx, Y + H / 2), (vx, low), (X["eval"] + 35, low), (X["eval"] + 35, Y + H / 2)],
     "veto or upheld challenge: reopen", ((vx + X["eval"] + 35) / 2 - 40, low - 6), va="bottom")

# challenge and audit (HQC2 accountability layer)
AUD = (X["window"], 385)
node(*AUD, "Challenge audit", "re-run check · slash")
cx1, cx2 = X["window"] - 15, X["window"] + 45
line([(cx1, Y + H / 2), (cx1, AUD[1] - H / 2)], "challenge (bond)", (cx1 - 5, 322), ha="right", va="center")
line([(cx2, AUD[1] - H / 2), (cx2, Y + H / 2)], "rejected", (cx2 + 5, 322), ha="left", va="center")
ux = vx - 100
ax.plot([AUD[0] - W / 2, ux, ux], [AUD[1], AUD[1], low], color=K, lw=0.9, zorder=1)
ax.text(ux - 6, AUD[1] + 4, "upheld: slash,\nreverse support", ha="right", va="center", fontsize=7.5, style="italic")

# timeout: escalate
tx = X["eval"] - 35
line([(tx, Y + H / 2), (tx, HUMAN[1] - H / 2)], "timeout at $T_{max}$", (tx - 8, (Y + HUMAN[1]) / 2 + 4),
     ha="right", va="center")

fig.tight_layout(pad=0.1)
for ext, kw in [("png", {"dpi": 600}), ("pdf", {}), ("eps", {})]:
    fig.savefig(f"figures/Fig1_state_machine_hqc2.{ext}", **kw)
plt.close(fig)
