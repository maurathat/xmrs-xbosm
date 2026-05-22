#!/usr/bin/env python3
"""
Live XRPL ledger telemetry collector for XBOSM. Runs on your Mac Mini
(open network), NOT in the Claude sandbox.

Subscribes to the public XRPL `ledger` stream and records one row per closed
ledger using the AUTHORITATIVE txn_count from each ledgerClosed event --
not a count of pushed transactions, which undercounts because the server
only relays you a subset.

Emits the canonical schema consumed by telemetry.load_csv / run_test.py:
    timestamp, ledger_index, interval_s, tx_count

Interval is derived from on-ledger close times (ledger_time), so it reflects
real cadence rather than network-jittered arrival times. Note: XRPL close
times are quantized to whole seconds, so intervals look like 3.0/4.0/5.0 --
that's expected, and it's why burst detection should lean on the tx_count
z-score more than the interval term.

Reconnects automatically (public servers drop connections) and stops itself
after --minutes.

Usage:
    python collect_xrpl.py --minutes 45 --out xrpl_test_telemetry.csv
    python collect_xrpl.py --minutes 60 --endpoint wss://s1.ripple.com
"""

import argparse
import asyncio
import csv
import json
import ssl
import time

import websockets

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

RIPPLE_EPOCH = 946684800  # unix seconds at 2000-01-01 (XRPL epoch offset)
DEFAULT_ENDPOINTS = [
    "wss://xrplcluster.com",
    "wss://s1.ripple.com",
    "wss://s2.ripple.com",
]


async def collect(endpoints, minutes, out_path):
    deadline = time.time() + minutes * 60
    prev_ledger_time = None
    last_index = None
    n_rows = 0

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["timestamp", "ledger_index", "interval_s", "tx_count"]
        )
        writer.writeheader()
        f.flush()

        ep_i = 0
        while time.time() < deadline:
            endpoint = endpoints[ep_i % len(endpoints)]
            try:
                async with websockets.connect(
                    endpoint, ping_interval=20, ping_timeout=20, max_size=2 ** 22,
                    ssl=_SSL_CTX,
                ) as ws:
                    await ws.send(json.dumps(
                        {"command": "subscribe", "streams": ["ledger"]}))
                    print(f"[connected] {endpoint}")

                    while time.time() < deadline:
                        remaining = deadline - time.time()
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=min(30, max(1, remaining)))
                        except asyncio.TimeoutError:
                            continue

                        msg = json.loads(raw)
                        if msg.get("type") != "ledgerClosed":
                            continue

                        idx = msg.get("ledger_index")
                        if idx is None or idx == last_index:
                            continue  # skip dupes on reconnect overlap

                        ltime = msg.get("ledger_time")        # ripple-epoch seconds
                        txn = int(msg.get("txn_count", 0))

                        if prev_ledger_time is None or ltime is None:
                            interval = ""
                        else:
                            iv = round(ltime - prev_ledger_time, 3)
                            interval = iv if iv > 0 else ""    # guard coarse/dup times
                        prev_ledger_time = ltime
                        last_index = idx

                        unix_ts = (ltime + RIPPLE_EPOCH) if ltime is not None else time.time()
                        writer.writerow({
                            "timestamp": round(unix_ts, 3),
                            "ledger_index": idx,
                            "interval_s": interval,
                            "tx_count": txn,
                        })
                        f.flush()
                        n_rows += 1
                        mins_left = (deadline - time.time()) / 60
                        print(f"ledger {idx}  tx={txn:<5} interval={interval}s   "
                              f"[{n_rows} rows | {mins_left:.1f} min left]")

            except (websockets.ConnectionClosed, OSError) as e:
                print(f"[disconnected] {endpoint}: {e!r} -- rotating endpoint, retry in 3s")
                ep_i += 1
                await asyncio.sleep(3)
            except Exception as e:  # noqa: BLE001  keep collecting through transient errors
                print(f"[error] {e!r} -- retry in 5s")
                await asyncio.sleep(5)

    print(f"\nDone. Wrote {n_rows} ledgers to {out_path}")
    return n_rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=45)
    ap.add_argument("--out", default="xrpl_test_telemetry.csv")
    ap.add_argument("--endpoint", help="override WebSocket endpoint")
    args = ap.parse_args()
    endpoints = [args.endpoint] if args.endpoint else DEFAULT_ENDPOINTS
    try:
        asyncio.run(collect(endpoints, args.minutes, args.out))
    except KeyboardInterrupt:
        print("\nInterrupted -- partial capture saved.")


if __name__ == "__main__":
    main()
