from __future__ import annotations

from pathlib import Path
from typing import Any

from xmrs_xbosm.common.config import load_yaml
from xmrs_xbosm.common.io import append_jsonl
from xmrs_xbosm.common.models import TelemetryEvent, utc_now_iso


def _read_stub_input(stub_path: str | None) -> list[dict[str, Any]]:
    if not stub_path:
        return [
            {"metric": "cpu_util", "value": 0.42, "host": "sim-1"},
            {"metric": "queue_depth", "value": 3, "host": "sim-1"},
        ]
    p = Path(stub_path)
    if p.suffix.lower() == ".json":
        import json

        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            return [data]
        raise ValueError(f"JSON input must be object or list of objects: {p}")
    raise ValueError(f"Unsupported stub input extension: {p}")


def run_collect(config_path: str) -> Path:
    cfg = load_yaml(config_path)
    out = Path(cfg["output_jsonl"])
    source = str(cfg.get("source_name", "collector"))
    stub = cfg.get("stub_input")

    for row in _read_stub_input(stub):
        ev = TelemetryEvent(ts=utc_now_iso(), source=source, kind="stub_sample", payload=row)
        append_jsonl(out, ev.to_json_dict())

    return out
