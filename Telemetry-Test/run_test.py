"""
XBOSM telemetry test runner.

Pipeline:
    telemetry (synthetic OR real CSV)
        -> burst scoring
        -> peer-graph gossip simulation per policy, under 3 suppression models
        -> per-ledger bandwidth estimate driven by tx_count
        -> reachability-vs-bandwidth comparison + recommendation

Test question:
    Under real XRPL transaction activity, can selective forwarding reduce
    estimated bandwidth while preserving simulated reachability -- and how
    much of that saving survives realistic duplicate suppression?

Usage:
    python run_test.py                 # synthetic stand-in data
    python run_test.py --csv data.csv  # your real Mac Mini capture
    python run_test.py --minutes 60    # longer synthetic capture
"""

import argparse
import json

import numpy as np
import pandas as pd

import telemetry as tlm
import burst as bst
import gossip as gsp

# bytes per relayed message -- tunable proxy for a relayed validated tx.
BYTES_PER_RELAY = 350

POLICIES = {
    "flood": None,
    "selective_k6": 6,
    "selective_k8": 8,
    "selective_k10": 10,
}

# send-side suppression models, ordered upper-bound -> floor
MODES = {
    "none":   "no suppression (upper bound)",
    "sender": "sender-suppressed (realistic)",
    "aware":  "peer-aware (optimistic floor)",
}
HEADLINE_MODE = "sender"   # the realistic one we lead with

REACH_FLOOR = 0.99


def human_bytes(n):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"


def _run_degree_sweep(args):
    """Run the gossip sim at multiple peer degrees, write degree_sweep.csv."""
    degrees = [int(d.strip()) for d in args.degree_sweep.split(",")]

    if args.csv:
        df = tlm.load_csv(args.csv)
    else:
        df = tlm.synth_telemetry(minutes=args.minutes)
    total_tx = int(df["tx_count"].sum())

    rows = []
    for deg in degrees:
        print(f"\n--- degree={deg} ---")
        adj = gsp.build_peer_graph(n_nodes=args.nodes, degree=deg, seed=0)
        for m in MODES:
            print(f"  simulating ({m})", flush=True)
            stats = gsp.policy_stats(adj, POLICIES, trials=args.trials, seed=1, mode=m)
            for p in POLICIES:
                fl = stats["flood"]["relays_per_msg"]
                rpm = stats[p]["relays_per_msg"]
                rows.append({
                    "degree": deg,
                    "mode": m,
                    "policy": p,
                    "reachability": stats[p]["reachability"],
                    "reach_min": stats[p]["reach_min"],
                    "relays_per_msg": rpm,
                    "bw_cut_vs_flood": 1.0 - rpm / fl if fl > 0 else 0,
                    "total_bytes": total_tx * rpm * BYTES_PER_RELAY,
                })

    sweep_df = pd.DataFrame(rows)
    sweep_df.to_csv("degree_sweep.csv", index=False)

    # Print summary table (sender mode only, selective_k8 vs flood)
    print("\n" + "=" * 80)
    print("DEGREE SWEEP — selective_k8 bandwidth cut vs flood (sender-suppressed)")
    print("=" * 80)
    print(f"{'degree':>7} {'flood relays':>13} {'k8 relays':>11} {'bw cut':>8} "
          f"{'k8 reach':>9} {'k8 reach_min':>13}")
    print("-" * 65)
    for deg in degrees:
        sub = sweep_df[(sweep_df["degree"] == deg) & (sweep_df["mode"] == HEADLINE_MODE)]
        fl = sub[sub["policy"] == "flood"].iloc[0]
        k8 = sub[sub["policy"] == "selective_k8"].iloc[0]
        cut = k8["bw_cut_vs_flood"] * 100
        print(f"{deg:>7} {fl['relays_per_msg']:>13.0f} {k8['relays_per_msg']:>11.0f} "
              f"{cut:>7.1f}% {k8['reachability']*100:>8.2f}% {k8['reach_min']*100:>12.2f}%")
    print("=" * 80)
    print(f"wrote: degree_sweep.csv ({len(sweep_df)} rows)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="path to real telemetry CSV from collector")
    ap.add_argument("--minutes", type=int, default=45,
                    help="synthetic capture length (ignored if --csv given)")
    ap.add_argument("--nodes", type=int, default=200)
    ap.add_argument("--degree", type=int, default=24)
    ap.add_argument("--trials", type=int, default=300)
    ap.add_argument("--degree-sweep",
                    help="comma-separated degrees to sweep, e.g. 12,16,21,30,50. "
                         "Runs the full sim at each degree and writes degree_sweep.csv")
    args = ap.parse_args()

    if args.degree_sweep:
        _run_degree_sweep(args)
        return

    # 1. telemetry --------------------------------------------------------
    if args.csv:
        df = tlm.load_csv(args.csv)
        source = f"REAL capture: {args.csv}"
    else:
        df = tlm.synth_telemetry(minutes=args.minutes)
        source = f"SYNTHETIC stand-in ({args.minutes} min)"

    total_tx = int(df["tx_count"].sum())
    span_min = (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]) / 60.0

    # 2. burst scoring ----------------------------------------------------
    df = bst.score_bursts(df)
    windows = bst.burst_windows(df)
    n_burst_ledgers = int(df["burst"].sum())

    # 3. policy simulation under each suppression model -------------------
    adj = gsp.build_peer_graph(n_nodes=args.nodes, degree=args.degree, seed=0)
    stats_by_mode = {}
    for m in MODES:
        print(f"... simulating ({m})", flush=True)
        stats_by_mode[m] = gsp.policy_stats(
            adj, POLICIES, trials=args.trials, seed=1, mode=m)

    # reachability is mode-independent; read it from the headline run
    reach = stats_by_mode[HEADLINE_MODE]

    # 4. recommendation: most aggressive policy holding reach >= floor ----
    eligible = sorted(
        [p for p in POLICIES if reach[p]["reach_min"] >= REACH_FLOOR],
        key=lambda p: stats_by_mode[HEADLINE_MODE][p]["relays_per_msg"],
    )
    rec = eligible[0] if eligible else "flood"

    # 5. bandwidth per policy per mode ------------------------------------
    def bytes_for(policy, mode):
        rpm = stats_by_mode[mode][policy]["relays_per_msg"]
        return total_tx * rpm * BYTES_PER_RELAY

    # ---- report ---------------------------------------------------------
    print("=" * 74)
    print("XBOSM TELEMETRY TEST  (dedup-aware bandwidth model)")
    print("=" * 74)
    print(f"Source         : {source}")
    print(f"Ledgers        : {len(df)}   (~{span_min:.1f} min)")
    print(f"Total tx       : {total_tx:,}")
    print(f"Mean tx/ledger : {df['tx_count'].mean():.1f}   peak {df['tx_count'].max()}")
    print(f"Mean interval  : {df['interval_s'].mean():.2f}s")
    print(f"Burst ledgers  : {n_burst_ledgers} / {len(df)} "
          f"({100*n_burst_ledgers/len(df):.1f}%) in {len(windows)} window(s)")
    print(f"Peer graph     : {args.nodes} nodes, ~{args.degree} peers, "
          f"{args.trials} trials/policy/mode")
    print()
    print("relays/msg under three send-side suppression models:")
    print("  none   = full forward set every time         (dedup-free upper bound)")
    print("  sender = never relay back to your sender       (realistic)")
    print("  aware  = never relay to an informed peer       (optimistic floor)")
    print("-" * 74)
    print(f"{'policy':<15}{'reach(avg)':>11}{'reach(min)':>11}"
          f"{'none':>11}{'sender':>11}{'aware':>11}")
    print("-" * 74)
    for p in POLICIES:
        print(f"{p:<15}{reach[p]['reachability']*100:>10.2f}%"
              f"{reach[p]['reach_min']*100:>10.2f}%"
              f"{stats_by_mode['none'][p]['relays_per_msg']:>11.0f}"
              f"{stats_by_mode['sender'][p]['relays_per_msg']:>11.0f}"
              f"{stats_by_mode['aware'][p]['relays_per_msg']:>11.0f}")
    print("-" * 74)
    print(f"RECOMMENDATION : {rec}   "
          f"(most aggressive policy holding reach >= {REACH_FLOOR*100:.0f}%)")
    print()
    print(f"Bandwidth cut vs flood for {rec}:")
    for m, label in MODES.items():
        fl = stats_by_mode[m]["flood"]["relays_per_msg"]
        rp = stats_by_mode[m][rec]["relays_per_msg"]
        cut = (1.0 - rp / fl) * 100
        tag = "   <- headline" if m == HEADLINE_MODE else ""
        print(f"   {label:<34}: {cut:6.1f}%{tag}")
    print(f"   total relayed bytes ({HEADLINE_MODE}): "
          f"{human_bytes(bytes_for(rec, HEADLINE_MODE))}")
    print("=" * 74)

    # ---- artifacts ------------------------------------------------------
    df.to_csv("telemetry_scored.csv", index=False)

    comp_rows = []
    for p in POLICIES:
        row = {
            "policy": p,
            "reachability": reach[p]["reachability"],
            "reach_min": reach[p]["reach_min"],
        }
        for m in MODES:
            rpm = stats_by_mode[m][p]["relays_per_msg"]
            fl = stats_by_mode[m]["flood"]["relays_per_msg"]
            row[f"relays_{m}"] = rpm
            row[f"bw_cut_{m}"] = 1.0 - rpm / fl
            row[f"bytes_{m}"] = bytes_for(p, m)
        comp_rows.append(row)
    pd.DataFrame(comp_rows).to_csv("policy_comparison.csv", index=False)

    summary = {
        "source": source,
        "ledgers": len(df),
        "span_minutes": round(span_min, 1),
        "total_tx": total_tx,
        "burst_ledgers": n_burst_ledgers,
        "burst_windows": windows,
        "graph": {"nodes": args.nodes, "degree": args.degree, "trials": args.trials},
        "suppression_models": MODES,
        "headline_mode": HEADLINE_MODE,
        "stats_by_mode": stats_by_mode,
        "recommendation": rec,
        "reach_floor": REACH_FLOOR,
        "bytes_per_relay": BYTES_PER_RELAY,
    }
    with open("summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    print("wrote: telemetry_scored.csv, policy_comparison.csv, summary.json")


if __name__ == "__main__":
    main()
