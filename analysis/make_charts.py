"""
Charts for the horizon study. Writes PNGs into analysis/results/.

Two charts, because there are two questions:
  1. How fast does forecast skill decay with lead time?  (skill_decay.png)
  2. In the slice a commuter cares about, is an alert worth firing?
     (commute_precision.png)

Both carry their own light ground so they stay readable on GitHub in either
theme, rather than relying on transparency.
"""

import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

OUT = "analysis/results"

# Palette: one categorical hue for the subject series, one status red for the
# region where the model is worse than guessing. Validated for CVD separation
# and contrast against a light surface.
BLUE = "#2a78d6"
RED = "#e34948"
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#dcdcd8"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "font.family": "DejaVu Sans", "font.size": 10,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2,
    "xtick.color": INK2, "ytick.color": INK2,
    "axes.spines.top": False, "axes.spines.right": False,
})

m = pd.read_csv(f"{OUT}/horizon_metrics.csv")
c = pd.read_csv(f"{OUT}/commute_decision_table.csv")
s = json.load(open(f"{OUT}/summary.json"))["horizons"]

# ---------------------------------------------------------------- chart 1
model = m[m.model == "gbm_full"].sort_values("lead_h")
look = m[m.model == "lookup_line_hour_dow"].sort_values("lead_h")
cal = m[m.model == "gbm_calendar"].sort_values("lead_h")

fig, ax = plt.subplots(figsize=(7.6, 4.6))
floor = look.pr_auc.mean()

# The floor is a reference level, not a peer series -- drawn recessive, labelled
# directly, so the single bold line is unambiguously the subject.
ax.axhspan(0, max(floor, cal.pr_auc.max()), color=GRID, alpha=0.45, lw=0)
ax.axhline(floor, color=INK2, lw=1.2, ls="--")
ax.axhline(cal.pr_auc.mean(), color=INK2, lw=1, ls=":")

ax.plot(model.lead_h, model.pr_auc, color=BLUE, lw=2, marker="o", ms=6,
        markeredgecolor=SURFACE, markeredgewidth=1.5, zorder=3)

for x, y in zip(model.lead_h, model.pr_auc):
    # The curve is near-vertical at the left, so a label centred above the
    # marker lands on the line; push those out to the right instead.
    dx, ha = ((14, "left") if 0 < x < 6 else (0, "center"))
    ax.annotate(f"{y:.3f}", (x, y), textcoords="offset points", xytext=(dx, 8),
                ha=ha, fontsize=9, color=INK, weight="bold")

ax.text(11.5, floor - 0.020, f"lookup table (line x hour x weekday) - {floor:.3f}",
        ha="center", va="top", fontsize=8.5, color=INK2)
ax.text(4.2, cal.pr_auc.mean() + 0.014, f"calendar-only model - {cal.pr_auc.mean():.3f}",
        ha="left", va="bottom", fontsize=8.5, color=INK2)

ax.set_xticks(model.lead_h)
ax.set_xticklabels(["0\n(nowcast)", "1h", "2h", "3h", "6h", "12h", "24h"])
ax.set_xlabel("hours of warning before the hour begins")
ax.set_ylabel("PR-AUC")
ax.set_ylim(0.10, 0.92)
ax.set_xlim(-1.4, 25.4)
ax.grid(axis="y", color=GRID, lw=0.7)
ax.set_axisbelow(True)
ax.set_title("Forecast skill collapses within hours\n"
             "Predicting 15+ min of unplanned major disruption, per line-hour",
             loc="left", color=INK, fontsize=12.5, weight="bold", pad=14)
ax.annotate("not significantly\nbetter than the\nlookup table",
            xy=(24, model.pr_auc.iloc[-1]), xytext=(17.5, 0.42),
            fontsize=8.5, color=RED, ha="center",
            arrowprops=dict(arrowstyle="->", color=RED, lw=1.1))
fig.tight_layout()
fig.savefig(f"{OUT}/skill_decay.png", dpi=170)
print("wrote skill_decay.png")

# ---------------------------------------------------------------- chart 2
# Precision at roughly a 10% alert budget, in the commute slice. The reference
# line is the base rate: below it, the alert is worse than firing at random.
band = c[(c.fires_pct > 8) & (c.fires_pct < 14)].sort_values(["lead_h", "fires_pct"])
band = band.groupby("lead_h").first().reset_index()
base = c.base_rate.iloc[0]

fig, ax = plt.subplots(figsize=(7.6, 4.4))
below = band.precision < base
ax.axhspan(0, base, color=RED, alpha=0.08, lw=0)
ax.axhline(base, color=INK2, lw=1.2, ls="--")
ax.text(15.5, base + 0.018, f"base rate - {100*base:.1f}% of these mornings are disrupted anyway",
        ha="center", va="bottom", fontsize=8.5, color=INK2)
ax.text(0.3, base / 2, "worse than\nfiring at random", fontsize=8.5,
        color=RED, va="center")

ax.plot(band.lead_h, band.precision, color=BLUE, lw=2, zorder=2)
ax.scatter(band.lead_h[~below], band.precision[~below], color=BLUE, s=48,
           zorder=3, edgecolor=SURFACE, linewidth=1.5)
ax.scatter(band.lead_h[below], band.precision[below], color=RED, s=48,
           zorder=3, edgecolor=SURFACE, linewidth=1.5)
for x, y in zip(band.lead_h, band.precision):
    # Below the base-rate line, label underneath so it clears the reference
    # line and its caption.
    dy, va = ((10, "bottom") if y >= base else (-16, "bottom"))
    ax.annotate(f"{y:.2f}", (x, y), textcoords="offset points", xytext=(0, dy),
                ha="center", va=va, fontsize=9, color=INK, weight="bold")

ax.set_xticks(band.lead_h)
ax.set_xticklabels(["0\n(nowcast)", "1h", "2h", "3h", "6h", "12h", "24h"])
ax.set_xlabel("hours of warning before the hour begins")
ax.set_ylabel("precision of the alert")
ax.set_ylim(0, 0.88)
ax.set_xlim(-1.4, 25.4)
ax.grid(axis="y", color=GRID, lw=0.7)
ax.set_axisbelow(True)
ax.set_title("A day-ahead alert is worse than no alert\n"
             "Weekday 07:00-09:00, my four commute lines, ~10% of mornings flagged",
             loc="left", color=INK, fontsize=11.5, weight="bold", pad=14)
fig.tight_layout()
fig.savefig(f"{OUT}/commute_precision.png", dpi=170)
print("wrote commute_precision.png")
