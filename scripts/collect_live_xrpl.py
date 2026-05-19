#!/usr/bin/env python3
"""Stream live XRPL mainnet ledger metrics to CSV (close interval + tx count per ledger).

Subscribes over WebSocket to ``ledger`` and ``transactions`` on Ripple's public hub,
writes rows: ``interval_s``, ``tx_count``.

Usage (from repository root)::

    python scripts/collect_live_xrpl.py
    python scripts/collect_live_xrpl.py -o data/processed/xrpl_live.csv -v
    python scripts/collect_live_xrpl.py --url wss://s1.ripple.com/ --no-certifi
    python scripts/collect_live_xrpl.py --print-ledgers
    python scripts/collect_live_xrpl.py --stream-tx-count --print-ledgers

Requires ``websocket-client`` (and ``certifi`` on some macOS Python builds). Prepends
``src/`` to ``sys.path`` so an editable install is optional.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from xmrs_xbosm.collector.xrpl_ws_client import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
