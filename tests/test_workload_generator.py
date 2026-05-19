from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from xmrs_xbosm.xbosm.generator import (
    REGIMES,
    expand_model_spec_to_generator_config,
    generate_ledger_workload,
    generate_ledger_workload_from_model_spec,
    load_workload_json_config,
    lognormal_mu_sigma_from_median_p95,
    validate_workload_config,
)


def _minimal_cfg() -> dict:
    return {
        "steps": 20,
        "start_ledger_index": 10,
        "initial_regime": "NORMAL",
        "seed": 123,
        "markov": {
            "NORMAL": {"NORMAL": 0.9, "STRESSED": 0.08, "BURST": 0.02},
            "STRESSED": {"NORMAL": 0.25, "STRESSED": 0.55, "BURST": 0.20},
            "BURST": {"NORMAL": 0.35, "STRESSED": 0.35, "BURST": 0.30},
        },
        "lognormal": {
            "NORMAL": {
                "tx_count": {"mu": 2.0, "sigma": 0.25},
                "interval_s": {"mu": 0.8, "sigma": 0.12},
            },
            "STRESSED": {
                "tx_count": {"mu": 2.8, "sigma": 0.35},
                "interval_s": {"mu": 0.95, "sigma": 0.15},
            },
            "BURST": {
                "tx_count": {"mu": 3.5, "sigma": 0.45},
                "interval_s": {"mu": 0.65, "sigma": 0.18},
            },
        },
    }


def test_validate_and_generate_deterministic() -> None:
    cfg = _minimal_cfg()
    validate_workload_config(cfg)
    rng = np.random.default_rng(123)
    df = generate_ledger_workload(cfg, rng=rng)
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == [
        "ledger_index",
        "regime",
        "interval_s",
        "tx_count",
        "send_spacing_s",
    ]
    assert len(df) == 20
    assert df["ledger_index"].tolist() == list(range(10, 30))
    assert set(df["regime"].unique()) <= set(REGIMES)
    assert (df["interval_s"] > 0).all()
    assert (df["tx_count"] >= 0).all()
    expected_spacing = df["interval_s"] / df["tx_count"].clip(lower=1)
    np.testing.assert_allclose(df["send_spacing_s"].to_numpy(), expected_spacing.to_numpy())


def test_load_json_and_write_csv(tmp_path: Path) -> None:
    cfg = _minimal_cfg()
    cfg["steps"] = 5
    cfg["output_csv"] = str(tmp_path / "out.csv")
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    loaded = load_workload_json_config(p)
    df = generate_ledger_workload(loaded)
    assert len(df) == 5
    csv_path = Path(cfg["output_csv"])
    assert csv_path.exists()
    roundtrip = pd.read_csv(csv_path)
    pd.testing.assert_frame_equal(df.reset_index(drop=True), roundtrip, check_dtype=False)


def test_lognormal_mu_sigma_from_percentiles() -> None:
    mu, sigma = lognormal_mu_sigma_from_median_p95(3.9, 5.8)
    assert math.isfinite(mu) and math.isfinite(sigma) and sigma > 0


def test_model_spec_expand_and_generate() -> None:
    spec_path = Path(__file__).resolve().parents[1] / "configs" / "workload_model_spec.example.json"
    spec = load_workload_json_config(spec_path)
    spec = dict(spec)
    spec["steps"] = 25
    spec["seed"] = 7
    cfg = expand_model_spec_to_generator_config(spec)
    validate_workload_config(cfg)
    df = generate_ledger_workload_from_model_spec(spec)
    assert len(df) == 25
    assert set(df["regime"].unique()) <= set(REGIMES)


def test_markov_row_must_sum_to_one() -> None:
    cfg = _minimal_cfg()
    cfg["markov"]["NORMAL"]["BURST"] = 0.05  # row no longer sums to 1
    with pytest.raises(ValueError, match="sum to 1"):
        validate_workload_config(cfg)
