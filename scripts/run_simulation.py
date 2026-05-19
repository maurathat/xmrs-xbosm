#!/usr/bin/env python3
"""End-to-end run: XBoSM workload generation + XMRS network simulation → CSV.

Usage (from repository root)::

    python scripts/run_simulation.py
    python scripts/run_simulation.py --config configs/default.json
    python scripts/run_simulation.py --config configs/simulation_run.example.yaml

Unified JSON (e.g. ``configs/default.json``) is read in one shot: ``workload`` may
contain a full generator spec (``markov``, ``lognormal``, …) instead of
``config_path`` / ``inline``. ``policy`` and ``message`` are merged into the
network simulation. Output path: ``output_csv`` or ``outputs.combined_csv``.

Requires dependencies from ``pyproject.toml`` / ``requirements.txt``. The script
prepends ``src/`` to ``sys.path`` so it can run without an editable install.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

# Repository layout: scripts/ at repo root, package under src/
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from xmrs_xbosm.xbosm.generator import (  # noqa: E402
    expand_model_spec_to_generator_config,
    generate_ledger_workload,
)
from xmrs_xbosm.xmrs.simulator import (  # noqa: E402
    compute_workload_network_load_index,
    run_network_simulation,
)


def _load_run_config(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        data = yaml.safe_load(text)
    elif path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"Run config must be a mapping: {path}")
    return data


def _resolve_path(base: Path, p: str | Path) -> Path:
    pp = Path(p)
    if pp.is_absolute():
        return pp
    return (base / pp).resolve()


def _workload_is_model_spec(workload_block: dict[str, Any]) -> bool:
    """Percentile-based spec (``configs/workload_model_spec.example.json``)."""
    return (
        "interval_distribution" in workload_block
        and "tx_distribution" in workload_block
        and "regimes" in workload_block
        and "markov_transition" in workload_block
    )


def _workload_is_embedded(workload_block: dict[str, Any]) -> bool:
    """Full XBoSM workload in config (e.g. ``default.json``), not an external file."""
    if "config_path" in workload_block or "inline" in workload_block:
        return False
    if _workload_is_model_spec(workload_block):
        return False
    return "markov" in workload_block and "lognormal" in workload_block


def _resolve_output_csv(cfg: dict[str, Any]) -> Path:
    out = cfg.get("output_csv")
    if out is None and isinstance(cfg.get("outputs"), dict):
        out = cfg["outputs"].get("combined_csv")
    if out is None:
        raise ValueError("Config needs 'output_csv' or 'outputs.combined_csv'")
    return Path(str(out))


def _build_workload_config(
    cfg: dict[str, Any],
    workload_block: dict[str, Any],
    cfg_dir: Path,
) -> dict[str, Any]:
    top_seed = cfg.get("seed")

    if _workload_is_model_spec(workload_block):
        merged = copy.deepcopy(workload_block)
        if top_seed is not None:
            merged.setdefault("seed", int(top_seed))
        wcfg = expand_model_spec_to_generator_config(merged)
    elif _workload_is_embedded(workload_block):
        wcfg = copy.deepcopy(workload_block)
    elif "config_path" in workload_block:
        wpath = _resolve_path(cfg_dir, workload_block["config_path"])
        raw = json.loads(wpath.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"Workload JSON must be an object: {wpath}")
        wcfg = dict(raw)
        overrides = workload_block.get("overrides")
        if overrides is not None:
            if not isinstance(overrides, dict):
                raise ValueError("workload.overrides must be a mapping")
            wcfg.update(overrides)
    elif "inline" in workload_block:
        wcfg = dict(workload_block["inline"])
    else:
        raise ValueError(
            "workload must include 'config_path', 'inline', embedded "
            "'markov' + 'lognormal', or model spec "
            "(interval_distribution + tx_distribution + regimes + markov_transition)"
        )

    wcfg.pop("output_csv", None)
    if top_seed is not None:
        wcfg.setdefault("seed", int(top_seed))
    return wcfg


def _build_network_config(cfg: dict[str, Any]) -> dict[str, Any]:
    net = cfg.get("network")
    if not isinstance(net, dict):
        raise ValueError("Config must include a 'network' mapping")

    net_cfg = copy.deepcopy(net)
    # Hints / docs only — not passed to rippled-style sim
    net_cfg.pop("approx_mean_degree", None)

    top_seed = cfg.get("seed")
    if top_seed is not None:
        net_cfg.setdefault("seed", int(top_seed))

    policy = cfg.get("policy")
    if isinstance(policy, dict):
        if "source" in policy:
            net_cfg["source"] = int(policy["source"])
        if "selective_ks" in policy:
            net_cfg["selective_ks"] = [int(x) for x in policy["selective_ks"]]
        elif "selective_k" in policy:
            net_cfg["selective_k"] = int(policy["selective_k"])

    message = cfg.get("message")
    if isinstance(message, dict) and "message_size_bytes" in message:
        net_cfg["message_size_bytes"] = int(message["message_size_bytes"])

    return net_cfg


_COUPLE_KEYS = (
    "send_spacing_base_s",
    "send_spacing_load_scale_s",
    "tx_count_ref",
    "burst_tx_weight",
    "couple_workload_send_spacing",
)


def _apply_workload_coupled_send_spacing(
    net_cfg: dict[str, Any],
    workload_df: pd.DataFrame,
) -> None:
    """Set ``send_spacing_s`` and ``network_load_index`` from ledger workload (in-place).

    Serialization spacing increases with burst-heavy, high-``tx_count`` traffic so
    **flood** (high fan-out per hop) accumulates more delay than **selective**.

    Skip if ``send_spacing_s`` is already set explicitly on ``net_cfg``. If
    ``couple_workload_send_spacing`` is false, use only ``send_spacing_base_s``.
    """
    if "send_spacing_s" in net_cfg:
        net_cfg.setdefault(
            "network_load_index",
            float("nan"),
        )
        return

    base = float(net_cfg.get("send_spacing_base_s", 0.0))
    coupled = bool(net_cfg.get("couple_workload_send_spacing", True))

    if not coupled:
        net_cfg["send_spacing_s"] = base
        net_cfg["network_load_index"] = 0.0
        return

    scale = float(net_cfg.get("send_spacing_load_scale_s", 0.0))
    ref = float(net_cfg.get("tx_count_ref", 12.0))
    burst_w = float(net_cfg.get("burst_tx_weight", 1.75))

    idx = compute_workload_network_load_index(
        workload_df, tx_count_ref=ref, burst_tx_weight=burst_w
    )
    net_cfg["send_spacing_s"] = base + scale * idx
    net_cfg["network_load_index"] = float(idx)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate XBoSM workload, run XMRS network simulation, write combined CSV.",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=_REPO_ROOT / "configs" / "default.json",
        help="Run config YAML or JSON (default: configs/default.json)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Override output CSV path from config",
    )
    args = parser.parse_args(argv)

    cfg_path = args.config.resolve()
    cfg = _load_run_config(cfg_path)
    cfg_dir = cfg_path.parent

    out_csv = args.output or _resolve_output_csv(cfg)
    if not out_csv.is_absolute():
        out_csv = (_REPO_ROOT / out_csv).resolve()
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    workload_block = cfg.get("workload")
    if not isinstance(workload_block, dict):
        raise ValueError("Config must include a 'workload' mapping")

    wcfg = _build_workload_config(cfg, workload_block, cfg_dir)
    df_workload = generate_ledger_workload(wcfg)
    df_workload.insert(0, "record_type", "workload")

    net_cfg = _build_network_config(cfg)
    _apply_workload_coupled_send_spacing(net_cfg, df_workload)
    for k in _COUPLE_KEYS:
        net_cfg.pop(k, None)

    df_network = run_network_simulation(net_cfg)
    df_network.insert(0, "record_type", "network")

    combined = pd.concat([df_workload, df_network], ignore_index=True, sort=False)
    combined.to_csv(out_csv, index=False)

    by_policy = out_csv.parent / "by_policy"
    by_policy.mkdir(parents=True, exist_ok=True)
    for _, row in df_network.iterrows():
        pol = str(row["policy"])
        pd.DataFrame([row]).to_csv(by_policy / f"{pol}.csv", index=False)

    print(f"Wrote {len(df_workload)} workload rows and {len(df_network)} network rows to {out_csv}")
    print(f"Wrote {len(df_network)} per-policy network CSVs under {by_policy}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
