#!/usr/bin/env python3
"""
Generate the two XBOSM paper figures from the analysis artifacts.

Figure 1 — transactions-per-ledger time series, with detected burst events marked.
           Reads telemetry_scored.csv (from run_test.py).
Figure 2 — flood vs selective-k8 relays-per-message vs peer degree.
           The diverging lines are the paper's core visual argument:
           flood scales linearly, selective forwarding stays flat.
           Reads degree_sweep.csv (from run_test.py --degree-sweep).

Run on the analysis host (Mac Mini), from inside Telemetry-Test/:
    python3 make_figures.py

Outputs (PNG @ 200 dpi + SVG):
    figure1_telemetry.png / .svg
    figure2_degree_sweep.png / .svg

Both are styled to match the XBOSM web page / one-pager palette so they sit
in the publication without looking like default matplotlib.
"""

import sys
import pandas as pd
import matplotlib
matplotlib.use("Agg")  # headless: no display needed
import matplotlib.pyplot as plt
from matplotlib import font_manager

# ---- palette (matches the page CSS) ---------------------------------------
INK      = "#161412"
PAPER    = "#f8f5ef"
ACCENT   = "#1a5e63"   # teal
ACCENT2  = "#b4451f"   # burnt sienna
MUTED    = "#6b6358"
LINE     = "#d9d2c5"

# Prefer a serif close to the page's Newsreader/Fraunces; fall back gracefully.
for fam in ["Georgia", "Charter", "Palatino", "DejaVu Serif"]:
    if any(fam.lower() in f.name.lower() for f in font_manager.fontManager.ttflist):
        plt.rcParams["font.family"] = fam
        break

plt.rcParams.update({
    "figure.facecolor": PAPER,
    "axes.facecolor": PAPER,
    "savefig.facecolor": PAPER,
    "axes.edgecolor": INK,
    "axes.labelcolor": INK,
    "text.color": INK,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "axes.linewidth": 1.1,
    "axes.grid": True,
    "grid.color": LINE,
    "grid.linewidth": 0.7,
    "grid.alpha": 0.7,
    "font.size": 11,
})


def figure1(path_csv="telemetry_scored.csv"):
    try:
        df = pd.read_csv(path_csv)
    except FileNotFoundError:
        print(f"  [skip] {path_csv} not found — run run_test.py --csv first")
        return False

    # x axis: ledger sequence (0..N). Use ledger_index if present, else range.
    x = range(len(df))
    tx = df["tx_count"]

    fig, ax = plt.subplots(figsize=(9, 3.6))
    ax.plot(x, tx, color=ACCENT, linewidth=1.2, alpha=0.9, zorder=2)
    ax.fill_between(x, tx, color=ACCENT, alpha=0.08, zorder=1)

    # mark detected bursts. 'burst' col is the post-filter flag from burst.py.
    if "burst" in df.columns:
        burst_idx = [i for i, b in enumerate(df["burst"]) if b]
        if burst_idx:
            ax.scatter(burst_idx, tx.iloc[burst_idx], color=ACCENT2, s=42,
                       zorder=3, edgecolor=PAPER, linewidth=0.8,
                       label=f"detected congestion event ({len(burst_idx)})")
            ax.legend(loc="upper right", frameon=False, fontsize=9.5)

    mean_tx = tx.mean()
    ax.axhline(mean_tx, color=MUTED, linewidth=0.9, linestyle=(0, (4, 3)),
               alpha=0.8, zorder=1)
    ax.annotate(f"mean {mean_tx:.0f}", xy=(len(df) * 0.985, mean_tx),
                xytext=(0, 5), textcoords="offset points",
                ha="right", fontsize=9, color=MUTED, style="italic")

    ax.set_xlabel("ledger (sequence)", fontsize=10.5)
    ax.set_ylabel("transactions per ledger", fontsize=10.5)
    ax.set_title("Figure 1 — XRPL transactions per closed ledger, 45-minute capture",
                 fontsize=12.5, color=INK, pad=12, loc="left", weight="bold")
    ax.margins(x=0.01)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()

    fig.savefig("figure1_telemetry.png", dpi=200, bbox_inches="tight")
    fig.savefig("figure1_telemetry.svg", bbox_inches="tight")
    plt.close(fig)
    print("  wrote figure1_telemetry.png / .svg")
    return True


def figure2(path_csv="degree_sweep.csv"):
    try:
        df = pd.read_csv(path_csv)
    except FileNotFoundError:
        print(f"  [skip] {path_csv} not found — run run_test.py --degree-sweep first")
        return False

    # use the realistic sender model for the headline figure
    sender = df[df["mode"] == "sender"]
    degrees = sorted(sender["degree"].unique())

    flood = [sender[(sender.degree == d) & (sender.policy == "flood")]
             ["relays_per_msg"].iloc[0] for d in degrees]
    k8 = [sender[(sender.degree == d) & (sender.policy == "selective_k8")]
          ["relays_per_msg"].iloc[0] for d in degrees]

    fig, ax = plt.subplots(figsize=(8, 4.6))

    ax.plot(degrees, flood, color=ACCENT2, linewidth=2.4, marker="o",
            markersize=7, markeredgecolor=PAPER, markeredgewidth=1.2,
            label="flood (relays to all peers)", zorder=3)
    ax.plot(degrees, k8, color=ACCENT, linewidth=2.4, marker="s",
            markersize=6.5, markeredgecolor=PAPER, markeredgewidth=1.2,
            label="selective-k8 (relays to 8 peers)", zorder=3)

    # shade the gap between the lines = the bandwidth saved
    ax.fill_between(degrees, k8, flood, color=ACCENT, alpha=0.06, zorder=1)

    # mark the rippled default (annotate near the BOTTOM to clear the legend)
    ax.axvline(21, color=MUTED, linestyle=(0, (4, 3)), linewidth=1, alpha=0.7,
               zorder=1)
    ax.annotate("rippled default\n(21 peers)", xy=(21, min(k8) * 0.92),
                xytext=(6, 0), textcoords="offset points",
                fontsize=9, color=MUTED, style="italic", va="center")

    # annotate the saving at degree 21
    i21 = degrees.index(21)
    cut = (1 - k8[i21] / flood[i21]) * 100
    ax.annotate(f"−{cut:.0f}% at degree 21",
                xy=(21, (flood[i21] + k8[i21]) / 2),
                xytext=(12, 0), textcoords="offset points",
                fontsize=10.5, color=ACCENT, weight="bold", va="center")

    ax.set_xlabel("peer degree", fontsize=10.5)
    ax.set_ylabel("relays per message (sender-suppressed)", fontsize=10.5)
    ax.set_title("Figure 2 — selective forwarding is self-limiting; flooding is not",
                 fontsize=12.5, color=INK, pad=12, loc="left", weight="bold")
    ax.legend(loc="upper left", frameon=False, fontsize=10)
    ax.set_xticks(degrees)
    ax.margins(x=0.04)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()

    fig.savefig("figure2_degree_sweep.png", dpi=200, bbox_inches="tight")
    fig.savefig("figure2_degree_sweep.svg", bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote figure2_degree_sweep.png / .svg  (saving at deg 21: {cut:.1f}%)")
    return True


if __name__ == "__main__":
    print("generating XBOSM paper figures...")
    ok1 = figure1()
    ok2 = figure2()
    if ok1 and ok2:
        print("done — both figures generated.")
    else:
        print("done — some figures skipped (see notes above); run the missing "
              "analysis step then re-run.")
        sys.exit(1 if not (ok1 or ok2) else 0)
