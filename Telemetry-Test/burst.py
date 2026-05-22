"""
XBOSM burst scoring.

Given ledger telemetry, produce a continuous burst_score in [0, 1] and a
binary burst flag, using:
  - a rolling baseline z-score on tx_count (how far above recent normal)
  - an interval-elongation factor (closes stretching = network under load)

Two changes for REAL captures vs the original:

1. MIN-RUN FILTER. A single elevated ledger is not a burst -- real tx_count
   jitters above baseline constantly. A burst window must sustain for
   `min_run` consecutive flagged closes (default 3) to count. Isolated
   single-ledger spikes are dropped. This is what collapses "80 windows"
   down to the handful of genuine sustained episodes.

2. DOWNWEIGHTED INTERVAL TERM. Real XRPL close times are quantized to whole
   seconds, so interval_s is 3.0/4.0/5.0 -- the elongation ratio is mostly
   quantization noise, not signal. interval_weight (default 0.5, was 2.0)
   leans the decision onto the tx_count z-score where the real signal lives.
   Set interval_weight higher only for sub-second-resolution captures.

Interpretable on purpose. No black-box thresholds buried in it.
"""

import numpy as np
import pandas as pd


def score_bursts(df, window=20, spike_z=8.0, score_threshold=0.55,
                 interval_weight=0.5, min_run=1, z_threshold=None):
    df = df.copy()
    # backward compat: z_threshold overrides spike_z if explicitly passed
    threshold = z_threshold if z_threshold is not None else spike_z
    tx = df["tx_count"].astype(float)

    # rolling baseline EXCLUDING the current point (shift(1)) so a burst
    # doesn't inflate its own baseline
    base_mean = tx.rolling(window, min_periods=5).mean().shift(1)
    base_std = tx.rolling(window, min_periods=5).std().shift(1)
    base_std = base_std.where(base_std > 1e-6)  # avoid div-by-zero
    z = ((tx - base_mean) / base_std).fillna(0.0)

    # interval elongation relative to recent cadence (downweighted; see header)
    iv = df["interval_s"].astype(float)
    iv_base = iv.rolling(window, min_periods=5).mean().shift(1)
    iv_factor = (iv / iv_base).fillna(1.0).clip(lower=0.5, upper=3.0)

    # combined signal: tx z-score dominates, elongation adds a small load term
    signal = z + interval_weight * (iv_factor - 1.0)

    # map to [0,1] with sigmoid centered at threshold
    score = 1.0 / (1.0 + np.exp(-(signal - threshold)))

    raw_flag = score > score_threshold

    # --- min-run filter: keep a flag only if it sits in a run of >= min_run
    flags = raw_flag.to_numpy()
    kept = np.zeros_like(flags)
    i, n = 0, len(flags)
    while i < n:
        if flags[i]:
            j = i
            while j < n and flags[j]:
                j += 1
            if (j - i) >= min_run:
                kept[i:j] = True
            i = j
        else:
            i += 1

    df["burst_z"] = z.round(3)
    df["interval_factor"] = iv_factor.round(3)
    df["burst_score"] = score.round(3)
    df["burst_raw"] = raw_flag           # pre-filter, for inspection
    df["burst"] = kept                   # post-filter, what everything uses
    return df


def burst_windows(df):
    """Collapse the per-ledger burst flag into contiguous windows."""
    flags = df["burst"].to_numpy()
    windows = []
    i, n = 0, len(flags)
    while i < n:
        if flags[i]:
            j = i
            while j < n and flags[j]:
                j += 1
            seg = df.iloc[i:j]
            windows.append({
                "start_ledger": int(seg["ledger_index"].iloc[0]),
                "end_ledger": int(seg["ledger_index"].iloc[-1]),
                "ledgers": int(j - i),
                "peak_tx": int(seg["tx_count"].max()),
                "peak_score": float(seg["burst_score"].max()),
            })
            i = j
        else:
            i += 1
    return windows
