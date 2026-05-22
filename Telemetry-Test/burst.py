"""
XBOSM burst scoring.

Real ledger traffic has TWO kinds of burst, with opposite shapes, and no
single run-length threshold catches both:

  SPIKE    : one ledger, extreme tx_count (e.g. 556 vs a ~92 baseline).
             Clears in a single close. Genuine, but only 1 ledger wide.
  SUSTAINED: a run of moderately-elevated closes -- network under load for
             several ledgers (congestion episode).

A flat min_run filter forces a bad trade: low min_run lets single-ledger
NOISE through; high min_run kills real single-ledger SPIKES. So the decision
is split into two INDEPENDENT tracks, and a ledger is a burst if EITHER fires:

  spike track     : flag a lone ledger iff its z-score >= spike_z (high bar,
                    default 8.0 -- far above the ~2 noise floor). NOT gated
                    by min_run -- a single extreme ledger is a burst on its own.
  sustained track : flag a ledger iff it sits in a run of >= min_run closes
                    that each clear the ordinary score_threshold.

This keeps tx=556 single-ledger spikes (via the spike track) AND multi-ledger
congestion (via the sustained track), while dropping the z~=2 single-ledger
jitter that is pure noise.

The detector flags by LOCAL deviation, not absolute count: a modest count in a
quiet neighborhood is a real anomaly. Interval term stays downweighted
(default 0.5): real XRPL close times are quantized to whole seconds, so the
elongation ratio is mostly noise.
"""

import numpy as np
import pandas as pd


def score_bursts(df, window=20, score_threshold=0.55,
                 interval_weight=0.5, min_run=3, spike_z=8.0,
                 z_threshold=None):
    # backward compat: if a caller passes z_threshold, treat it as the
    # sustained-track sigmoid center; otherwise default to 2.0.
    sustained_center = z_threshold if z_threshold is not None else 2.0

    df = df.copy()
    tx = df["tx_count"].astype(float)

    base_mean = tx.rolling(window, min_periods=5).mean().shift(1)
    base_std = tx.rolling(window, min_periods=5).std().shift(1)
    base_std = base_std.where(base_std > 1e-6)
    z = ((tx - base_mean) / base_std).fillna(0.0)

    iv = df["interval_s"].astype(float)
    iv_base = iv.rolling(window, min_periods=5).mean().shift(1)
    iv_factor = (iv / iv_base).fillna(1.0).clip(lower=0.5, upper=3.0)

    signal = z + interval_weight * (iv_factor - 1.0)
    score = 1.0 / (1.0 + np.exp(-(signal - sustained_center)))

    raw_flag = (score > score_threshold).to_numpy()
    z_arr = z.to_numpy()

    # --- track 1: sustained runs of >= min_run flagged closes ------------
    sustained = np.zeros(len(df), dtype=bool)
    i, n = 0, len(raw_flag)
    while i < n:
        if raw_flag[i]:
            j = i
            while j < n and raw_flag[j]:
                j += 1
            if (j - i) >= min_run:
                sustained[i:j] = True
            i = j
        else:
            i += 1

    # --- track 2: lone extreme spikes (NOT gated by min_run) -------------
    spike = z_arr >= spike_z

    # a ledger is a burst if EITHER track fires
    burst = sustained | spike

    df["burst_z"] = z.round(3)
    df["interval_factor"] = iv_factor.round(3)
    df["burst_score"] = score.round(3)
    df["burst_raw"] = raw_flag
    df["is_spike"] = spike
    df["is_sustained"] = sustained
    df["burst"] = burst
    return df


def burst_windows(df):
    """Collapse the per-ledger burst flag into contiguous windows.
    `kind` labels whether the window is a lone spike or sustained."""
    flags = df["burst"].to_numpy()
    windows = []
    i, n = 0, len(flags)
    while i < n:
        if flags[i]:
            j = i
            while j < n and flags[j]:
                j += 1
            seg = df.iloc[i:j]
            kind = "sustained" if bool(seg["is_sustained"].any()) else "spike"
            windows.append({
                "start_ledger": int(seg["ledger_index"].iloc[0]),
                "end_ledger": int(seg["ledger_index"].iloc[-1]),
                "ledgers": int(j - i),
                "peak_tx": int(seg["tx_count"].max()),
                "peak_score": float(seg["burst_score"].max()),
                "peak_z": float(seg["burst_z"].max()),
                "kind": kind,
            })
            i = j
        else:
            i += 1
    return windows
