# XBOSM Telemetry Test

Does selective forwarding cut XRPL gossip bandwidth without breaking message
reachability? This harness captures live XRPL ledger telemetry and runs a
Monte-Carlo gossip simulation to find out.

**Headline result** (selective-k8 vs flood, at the rippled default of 21 peers,
≥99.5% worst-case reachability):
**≈67% relay-bandwidth reduction under realistic sender-suppressed gossip**
(≈39% under an aggressive peer-aware floor). The saving scales 38%→84% across
peer degrees 12→50.

---

## Quick start

Run on a machine with open network (e.g. the Mac Mini) — the live capture
needs to reach `xrplcluster.com`, which sandboxed environments block.

```bash
# one command: venv + deps + 45-min live capture + analysis
chmod +x run_on_mini.sh
./run_on_mini.sh 45

# OR step by step:
python3 collect_xrpl.py --minutes 45 --out xrpl_test_telemetry.csv
python3 run_test.py --csv xrpl_test_telemetry.csv

# degree sensitivity sweep -> degree_sweep.csv (the paper's robustness table)
python3 run_test.py --degree-sweep 12,16,21,30,50
```

Quick connection check before committing to a full capture:
```bash
python3 collect_xrpl.py --minutes 2 --out smoke.csv
```

---

## Files

| File | Role |
|---|---|
| `collect_xrpl.py` | Live XRPL collector. Subscribes to the public `ledger` stream, records one row per closed ledger using the authoritative `txn_count`. Auto-reconnects across endpoints, stops after `--minutes`. |
| `telemetry.py` | Telemetry layer. `load_csv()` reads a real capture; `synth_telemetry()` generates synthetic stand-in data for testing without a capture. |
| `burst.py` | Two-track burst scorer (see Parameters). Flags congestion events by local deviation, not absolute count. |
| `gossip.py` | Monte-Carlo gossip simulation on a peer graph. Three send-side suppression models: `none` / `sender` / `aware`. |
| `run_test.py` | Orchestrator. Telemetry → burst scoring → policy simulation → bracketed bandwidth result + recommendation. Also hosts `--degree-sweep`. |
| `run_on_mini.sh` | One-command wrapper: venv setup, capture, analysis. |

**Generated artifacts** (overwritten each run — the raw `xrpl_test_telemetry.csv`
is the only file worth preserving; rename captures you want to keep):
- `telemetry_scored.csv` — per-ledger burst scores and flags
- `policy_comparison.csv` — per-policy bandwidth across all three suppression models
- `summary.json` — full run summary
- `degree_sweep.csv` — sensitivity table (only from `--degree-sweep`)

All files import each other as flat siblings — keep them in one directory and
run from inside it.

---

## Pipeline

```
live XRPL ledger stream
        │  collect_xrpl.py
        ▼
  telemetry CSV  ──load_csv──►  burst scoring (burst.py)
                                       │
                                       ▼
                     peer-graph gossip sim (gossip.py)
                       × 3 suppression models
                                       │
                                       ▼
            reachability vs bandwidth + recommendation
                          (run_test.py)
```

The gossip simulation depends only on the peer graph (nodes, degree, seed),
**not** on the telemetry — so capture length doesn't change runtime (~6s).
Telemetry sets the transaction *volume* that scales total bandwidth, and drives
burst detection.

---

## Parameters — why each is what it is

Every parameter is sourced or calibrated, none assumed. This matters: the
headline number is only defensible because its inputs are.

- **Peer degree = 21** (`--degree`, default 21). The rippled documented default
  *soft-maximum* number of peers (xrpl.org peering docs). It is a cap, not a
  measured typical value — real nodes may sit below it, hubs run far higher.
  This is the single biggest lever on the bandwidth number, which is why
  results always travel with the degree assumption and a full sweep.

- **Spike threshold z = 8** (`spike_z` in `burst.py`). Calibrated against real
  captured data: at 8 standard deviations above the trailing 20-ledger
  baseline, the detector cleanly isolates genuine congestion from ordinary
  variance. z=6 admits merely-busy ledgers; z=10 discards real spikes. The
  detector flags *local* deviation, not absolute count — a modest count in a
  quiet neighborhood is a real anomaly.

- **Minimum sustained run = 3** (`min_run` in `burst.py`). The sustained-burst
  track requires ≥3 consecutive elevated ledgers. **Keep this at 3.** Dropping
  it to 1 collapses the two-track design and re-admits single-ledger noise.
  Note: real captures may contain *only* single-ledger spikes and no sustained
  episode — an empty sustained result is correct in that case, not a bug. The
  spike track catches the spikes regardless.

- **Suppression models** (`mode` in `gossip.py`): `sender` is the realistic
  headline (never relay back to your sender); `aware` is the optimistic floor
  (never relay to an already-informed peer); `none` is the dedup-free upper
  bound. Reachability is identical across all three by construction.

- **200 nodes, 300 trials, seeds 0/1.** Fixed seeds make every number
  reproducible.

---

## Limitations

These ride with the result wherever it goes:

- **Single short capture** (45 min, 5 single-ledger spikes, no sustained
  episode). Fine for the bandwidth model; thin for any burst-*detection* claim.
- **Idealized topology.** Uniform-degree random graph; real XRPL connectivity is
  heterogeneous and the degree is a soft-max, not a measured distribution.
- **Simulated, not instrumented, relay behavior.** Bandwidth comes from the
  gossip sim, not from a real rippled node. Whether real relay matches the
  `sender` model or the `aware` floor is the gap between the 67% and 39%
  figures — closing it needs an instrumented node.
- **No consensus interaction modeled.** Transaction propagation only; no
  validator flow, no adversarial conditions. Not a deployment claim.

---

## Contact

XBOSM / XMRS — overlay routing research · mauraclark@proton.me
