"""XRPL mainnet WebSocket client: ledger close timing and per-ledger transaction counts.

Connects to Ripple's public hub, subscribes to ``ledger`` and ``transactions`` streams,
and appends one CSV row per validated ledger close (after the first), with columns
``interval_s`` and ``tx_count``.

``interval_s`` is the delta between successive ``ledger_time`` values (Ripple epoch
seconds) on ``ledgerClosed`` messages — not wall-clock time between WebSocket events.
``tx_count`` is the ``txn_count`` field on ``ledgerClosed``, not a manual count of
``transaction`` stream messages (which can disagree around boundaries).

Requires: ``websocket-client`` (``import websocket``). If TLS verification fails on your
Python build, ``pip install certifi`` (used automatically when importable).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import signal
import threading
import time
from pathlib import Path
from typing import Any

import websocket


def _default_sslopt() -> dict[str, Any]:
    """Use Mozilla's CA bundle via certifi when available (common macOS Python fix)."""
    try:
        import certifi

        return {"ca_certs": certifi.where()}
    except ImportError:
        return {}


__all__ = [
    "DEFAULT_WS_URL",
    "XRPLLedgerMetricsRecorder",
    "main",
]

logger = logging.getLogger(__name__)

DEFAULT_WS_URL = "wss://s1.ripple.com/"

_SUBSCRIBE_PAYLOAD = {
    "id": "xmrs_xbosm_subscribe",
    "command": "subscribe",
    "streams": ["ledger", "transactions"],
}


class XRPLLedgerMetricsRecorder:
    """Subscribe to XRPL streams and append ``interval_s, tx_count`` rows to a CSV file."""

    def __init__(
        self,
        csv_path: str | Path,
        *,
        ws_url: str = DEFAULT_WS_URL,
        sslopt: dict[str, Any] | None = None,
        reconnect_initial_s: float = 1.0,
        reconnect_max_s: float = 60.0,
        print_ledgers: bool = False,
        stream_tx_count: bool = False,
    ) -> None:
        self._csv_path = Path(csv_path)
        self._ws_url = ws_url
        self._sslopt: dict[str, Any] = _default_sslopt() if sslopt is None else dict(sslopt)
        self._reconnect_initial_s = reconnect_initial_s
        self._reconnect_max_s = reconnect_max_s
        self._print_ledgers = print_ledgers
        self._stream_tx_count = stream_tx_count
        self._stream_tx_seen: int = 0

        self._prev_ledger_time: int | None = None
        self._ws_app: websocket.WebSocketApp | None = None
        self._csv_file: Any = None
        self._writer: csv.DictWriter | None = None
        self._stop = threading.Event()

    def _ensure_csv(self) -> None:
        if self._csv_file is not None:
            return
        self._csv_path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self._csv_path.exists() or self._csv_path.stat().st_size == 0
        self._csv_file = self._csv_path.open("a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(
            self._csv_file,
            fieldnames=["interval_s", "tx_count"],
            extrasaction="ignore",
        )
        if new_file:
            self._writer.writeheader()
            self._csv_file.flush()

    def _append_row(self, interval_s: float, tx_count: int) -> None:
        self._ensure_csv()
        assert self._writer is not None
        self._writer.writerow(
            {"interval_s": f"{interval_s:.6f}", "tx_count": str(tx_count)}
        )
        self._csv_file.flush()

    def _on_open(self, _ws: websocket.WebSocketApp) -> None:
        logger.info("WebSocket connected; subscribing to ledger, transactions")
        _ws.send(json.dumps(_SUBSCRIBE_PAYLOAD))

    def _on_message(self, _ws: websocket.WebSocketApp, message: str) -> None:
        try:
            data: dict[str, Any] = json.loads(message)
        except json.JSONDecodeError:
            logger.warning("Non-JSON message (%d bytes); skipping", len(message))
            return

        mtype = data.get("type")

        if mtype == "response":
            if data.get("status") == "success":
                logger.debug("RPC ok: %s", data.get("result"))
            else:
                logger.error("RPC error response: %s", data)
            return

        if mtype == "ledgerClosed":
            self._handle_ledger_closed(data)
            return

        if mtype == "transaction":
            if self._stream_tx_count:
                self._stream_tx_seen += 1
            return

        # Ping, server status, etc.
        logger.debug("Ignoring message type=%s", mtype)

    def _handle_ledger_closed(self, data: dict[str, Any]) -> None:
        try:
            ledger_time = int(data["ledger_time"])
            txn_count = int(data.get("txn_count", 0))
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("Malformed ledgerClosed (missing or bad fields): %s", e)
            return

        if self._prev_ledger_time is None:
            self._prev_ledger_time = ledger_time
            if self._stream_tx_count:
                self._stream_tx_seen = 0
            return

        interval_s = float(ledger_time - self._prev_ledger_time)
        if interval_s < 0:
            # New close time is *earlier* than our last sample (out-of-order or glitch).
            # Do not move the baseline forward — only update after a valid interval row.
            logger.warning(
                "Non-monotonic ledger_time (cur < prev baseline): prev=%s cur=%s; "
                "skipping CSV row, baseline unchanged",
                self._prev_ledger_time,
                ledger_time,
            )
            if self._stream_tx_count:
                self._stream_tx_seen = 0
            return

        tx_out = self._stream_tx_seen if self._stream_tx_count else txn_count
        self._append_row(interval_s, tx_out)
        if self._stream_tx_count:
            self._stream_tx_seen = 0
        if self._print_ledgers:
            idx = data.get("ledger_index", "?")
            print(
                f"Ledger {idx} | tx_count: {tx_out} | interval_s: {interval_s:.2f}",
                flush=True,
            )
        # Baseline moves only here (and on first close above), never on skipped negatives.
        self._prev_ledger_time = ledger_time

    def _on_error(self, _ws: websocket.WebSocketApp, error: Any) -> None:
        if error is not None:
            logger.error("WebSocket error: %s", error)

    def _on_close(self, _ws: websocket.WebSocketApp, close_status_code: Any, close_msg: Any) -> None:
        logger.info(
            "WebSocket closed code=%s msg=%s",
            close_status_code,
            close_msg,
        )

    def close(self) -> None:
        self._stop.set()
        if self._ws_app is not None:
            self._ws_app.close()
            self._ws_app = None
        if self._csv_file is not None:
            self._csv_file.close()
            self._csv_file = None
            self._writer = None

    def run_forever(self) -> None:
        """Connect (with reconnect backoff) until :meth:`close`."""
        backoff = self._reconnect_initial_s

        while not self._stop.is_set():
            self._ws_app = websocket.WebSocketApp(
                self._ws_url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            try:
                self._ws_app.run_forever(
                    sslopt=self._sslopt if self._sslopt else None,
                    ping_interval=20,
                    ping_timeout=10,
                )
            except Exception:
                logger.exception("run_forever crashed; reconnecting after %.1fs", backoff)
            finally:
                self._ws_app = None

            if self._stop.is_set():
                break

            time.sleep(backoff)
            backoff = min(backoff * 2.0, self._reconnect_max_s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stream XRPL ledger metrics to CSV (interval_s, tx_count).",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="data/processed/xrpl_ledger_metrics.csv",
        help="Output CSV path (default: %(default)s)",
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_WS_URL,
        help="WebSocket endpoint (default: %(default)s)",
    )
    parser.add_argument(
        "--no-certifi",
        action="store_true",
        help="Do not use certifi's CA bundle (default: use certifi if installed)",
    )
    parser.add_argument(
        "--print-ledgers",
        action="store_true",
        help="Print each closed ledger (index, tx_count, interval_s) to stdout",
    )
    parser.add_argument(
        "--stream-tx-count",
        action="store_true",
        help=(
            "Count `transaction` messages between closes as tx_count (demo / timing-based); "
            "default uses authoritative txn_count on ledgerClosed"
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    sslopt: dict[str, Any] | None = {} if args.no_certifi else None
    recorder = XRPLLedgerMetricsRecorder(
        args.output,
        ws_url=args.url,
        sslopt=sslopt,
        print_ledgers=args.print_ledgers,
        stream_tx_count=args.stream_tx_count,
    )

    def _handle_signal(_sig: int, _frame: Any) -> None:
        logger.info("Signal received; closing WebSocket and CSV")
        recorder.close()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        recorder.run_forever()
    finally:
        recorder.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
