from __future__ import annotations

from pathlib import Path

from xmrs_xbosm.collector.xrpl_ws_client import XRPLLedgerMetricsRecorder


def test_backward_ledger_time_does_not_advance_baseline(tmp_path: Path) -> None:
    """Regression: negative interval must not set _prev_ledger_time to the regressive time."""
    csv = tmp_path / "ledgers.csv"
    r = XRPLLedgerMetricsRecorder(csv)

    r._handle_ledger_closed({"ledger_time": 100, "txn_count": 1})
    assert r._prev_ledger_time == 100

    r._handle_ledger_closed({"ledger_time": 110, "txn_count": 2})
    assert r._prev_ledger_time == 110

    r._handle_ledger_closed({"ledger_time": 105, "txn_count": 99})
    assert r._prev_ledger_time == 110

    r._handle_ledger_closed({"ledger_time": 120, "txn_count": 3})
    assert r._prev_ledger_time == 120

    r.close()
    lines = csv.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == "interval_s,tx_count"
    assert len(lines) == 3
    assert lines[1].startswith("10.")  # 110 - 100
    assert lines[2].startswith("10.")  # 120 - 110, not 120 - 105
