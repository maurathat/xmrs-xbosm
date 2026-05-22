"""
XBOSM telemetry layer.

Canonical schema (matches collect_xrpl_test.py output exactly):
    timestamp     : float, unix seconds at ledger close
    ledger_index  : int
    interval_s    : float, seconds since previous close (NaN on first row)
    tx_count      : int, transactions in the closed ledger

Two entry points:
    synth_telemetry(...)  -> generate realistic synthetic XRPL telemetry for testing
    load_csv(path)        -> load a real capture from your Mac Mini collector

The simulation pipeline does not care which one produced the DataFrame.
Swap synthetic for real by passing --csv to run_test.py.
"""

import numpy as np
import pandas as pd

CANONICAL_COLS = ["timestamp", "ledger_index", "interval_s", "tx_count"]


def synth_telemetry(minutes=45, base_interval=3.8, interval_jitter=0.55,
                    base_tx=55, n_bursts=3, seed=7):
    """Generate representative XRPL ledger telemetry.

    Models: ~3.8s ledger cadence with jitter, mild AR(1) drift in tx_count,
    and a few injected burst windows (elevated tx_count + slightly longer
    closes, as happens when the network is under load).

    This is a STAND-IN for a real capture, sized to look like ~45 min of
    mainnet activity. Replace with load_csv() once you have real data.
    """
    rng = np.random.default_rng(seed)
    rows = []
    t = 0.0
    ledger = 90_000_000  # arbitrary plausible starting index
    drift = float(base_tx)

    # estimate ledger count from cadence
    n_ledgers = int(minutes * 60 / base_interval)

    # choose burst windows: (start_ledger_idx, length, intensity)
    burst_windows = []
    for _ in range(n_bursts):
        start = rng.integers(int(0.1 * n_ledgers), int(0.9 * n_ledgers))
        length = int(rng.integers(15, 40))
        intensity = float(rng.uniform(4.0, 8.0))
        burst_windows.append((start, length, intensity))

    def burst_factor(i):
        f = 1.0
        for start, length, intensity in burst_windows:
            if start <= i < start + length:
                # ramp up then down across the window
                phase = (i - start) / length
                shape = np.sin(np.pi * phase)  # 0 -> 1 -> 0
                f = max(f, 1.0 + (intensity - 1.0) * shape)
        return f

    prev_t = None
    for i in range(n_ledgers):
        bf = burst_factor(i)
        # AR(1) drift on baseline tx rate
        drift = 0.92 * drift + 0.08 * base_tx + rng.normal(0, 4)
        drift = max(8.0, drift)
        lam = drift * bf
        tx = int(rng.poisson(lam))

        # bursts stretch close times modestly
        iv = base_interval * (1.0 + 0.18 * (bf - 1.0)) + rng.normal(0, interval_jitter)
        iv = max(1.5, iv)

        t += iv
        ledger += 1
        rows.append({
            "timestamp": round(t, 3),
            "ledger_index": ledger,
            "interval_s": np.nan if prev_t is None else round(iv, 3),
            "tx_count": tx,
        })
        prev_t = t

    df = pd.DataFrame(rows, columns=CANONICAL_COLS)
    return df


def load_csv(path):
    """Load a real capture. Coerces to the canonical schema."""
    df = pd.read_csv(path)
    missing = [c for c in CANONICAL_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"CSV missing required columns: {missing}. "
                         f"Expected {CANONICAL_COLS}")
    df = df[CANONICAL_COLS].copy()
    df["tx_count"] = pd.to_numeric(df["tx_count"], errors="coerce").fillna(0).astype(int)
    df["interval_s"] = pd.to_numeric(df["interval_s"], errors="coerce")
    # backfill the first NaN interval with the median so scoring is stable
    med = df["interval_s"].median()
    df["interval_s"] = df["interval_s"].fillna(med)
    return df
