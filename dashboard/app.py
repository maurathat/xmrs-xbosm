"""Streamlit entrypoint. From the repo root run::

    streamlit run dashboard/app.py

Delegates to ``app/dashboard.py`` (single source of truth).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_spec = importlib.util.spec_from_file_location(
    "_xmrs_streamlit_dashboard",
    _ROOT / "app" / "dashboard.py",
)
_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_mod)
_mod.main()
