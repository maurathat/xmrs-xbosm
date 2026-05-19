"""XBoSM bundle builder and synthetic XRPL-style ledger workload generator.

The workload path produces a :class:`pandas.DataFrame` of per-ledger traffic driven by
lognormal emissions and a three-state Markov regime process (NORMAL / STRESSED / BURST).
Configuration is a JSON object (see :func:`load_workload_json_config`).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping, MutableMapping

import numpy as np
import pandas as pd

from xmrs_xbosm.common.config import load_yaml
from xmrs_xbosm.common.io import iter_jsonl
from xmrs_xbosm.common.models import utc_now_iso

REGIMES: tuple[str, ...] = ("NORMAL", "STRESSED", "BURST")
_REGIME_SET = frozenset(REGIMES)

# Standard normal z such that Phi(z) = 0.95 (fit lognormal to median and 95th percentile).
_Z_P95: float = 1.6448536269514722

__all__ = [
    "REGIMES",
    "expand_model_spec_to_generator_config",
    "generate_ledger_workload",
    "generate_ledger_workload_from_model_spec",
    "load_workload_json_config",
    "lognormal_mu_sigma_from_median_p95",
    "run_generate",
    "validate_workload_config",
]


def lognormal_mu_sigma_from_median_p95(median: float, p95: float) -> tuple[float, float]:
    """NumPy ``lognormal(mean, sigma)`` params from median and 95th percentile.

    If ``X`` is lognormal, ``median = exp(mu)`` and ``p95 = exp(mu + z95 * sigma)``.
    Extra percentiles (e.g. p75, p99) are not used here; fit is from p50 + p95 only.
    """
    if median <= 0 or p95 <= 0:
        raise ValueError(f"median and p95 must be positive, got median={median}, p95={p95}")
    if p95 <= median:
        raise ValueError(f"p95 must exceed median, got p95={p95}, median={median}")
    mu = float(math.log(median))
    sigma = float((math.log(p95) - mu) / _Z_P95)
    if sigma <= 0.0 or not math.isfinite(sigma):
        raise ValueError(f"derived sigma must be finite and > 0, got {sigma}")
    return mu, sigma


def expand_model_spec_to_generator_config(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Turn a percentile + regime multiplier spec into :func:`generate_ledger_workload` config.

    Expected keys:

    - ``interval_distribution``: ``{"type": "lognormal", "p50", "p95"}`` (seconds between closes).
    - ``tx_distribution``: ``{"type": "lognormal", "p50", "p95"}`` (base tx per ledger before
      regime multiplier). ``p75`` / ``p99`` if present are ignored for fitting.
    - ``regimes``: per regime ``tx_multiplier`` (multiplicative factor on the base tx draw).
      ``duration_mean`` is reserved for future semi-Markov use; Markov transitions come from
      ``markov_transition`` only.
    - ``markov_transition``: same row-stochastic layout as ``markov`` in the flat config.

    Also passes through ``steps``, ``seed``, ``start_ledger_index``, ``initial_regime``,
    ``tx_count_min``, ``output_csv`` when present.
    """
    idist = spec.get("interval_distribution")
    tdist = spec.get("tx_distribution")
    regimes = spec.get("regimes")
    markov = spec.get("markov_transition")

    if not isinstance(idist, dict) or str(idist.get("type", "")).lower() != "lognormal":
        raise ValueError("interval_distribution must be an object with type 'lognormal'")
    if not isinstance(tdist, dict) or str(tdist.get("type", "")).lower() != "lognormal":
        raise ValueError("tx_distribution must be an object with type 'lognormal'")
    if not isinstance(regimes, dict):
        raise ValueError("regimes must be an object")
    if not isinstance(markov, dict):
        raise ValueError("markov_transition must be an object")

    int_mu, int_sigma = lognormal_mu_sigma_from_median_p95(
        float(idist["p50"]),
        float(idist["p95"]),
    )
    tx_mu_base, tx_sigma = lognormal_mu_sigma_from_median_p95(
        float(tdist["p50"]),
        float(tdist["p95"]),
    )

    lognormal: dict[str, Any] = {}
    for r in REGIMES:
        if r not in regimes:
            raise ValueError(f"regimes missing {r!r}")
        block = regimes[r]
        if not isinstance(block, dict):
            raise ValueError(f"regimes[{r!r}] must be an object")
        mult = float(block.get("tx_multiplier", 1.0))
        if mult <= 0.0:
            raise ValueError(f"regimes[{r!r}].tx_multiplier must be > 0")
        tx_mu = tx_mu_base + math.log(mult)
        lognormal[r] = {
            "tx_count": {"mu": tx_mu, "sigma": tx_sigma},
            "interval_s": {"mu": int_mu, "sigma": int_sigma},
        }

    out: dict[str, Any] = {
        "markov": {k: dict(v) for k, v in markov.items()},
        "lognormal": lognormal,
    }
    for key in (
        "steps",
        "seed",
        "start_ledger_index",
        "initial_regime",
        "tx_count_min",
        "output_csv",
    ):
        if key in spec:
            out[key] = spec[key]
    return out


def generate_ledger_workload_from_model_spec(
    spec: MutableMapping[str, Any] | str | Path,
    *,
    rng: np.random.Generator | None = None,
) -> pd.DataFrame:
    """Load or accept a model spec (percentiles + multipliers) and run :func:`generate_ledger_workload`."""
    if isinstance(spec, (str, Path)):
        raw = load_workload_json_config(spec)
    else:
        raw = dict(spec)
    cfg = expand_model_spec_to_generator_config(raw)
    return generate_ledger_workload(cfg, rng=rng)


def load_workload_json_config(path: str | Path) -> dict[str, Any]:
    """Load workload JSON; top level must be an object."""
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Workload config must be a JSON object: {p}")
    return data


def validate_workload_config(cfg: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` if Markov / lognormal sections are inconsistent."""
    for key in ("markov", "lognormal"):
        if key not in cfg:
            raise ValueError(f"Workload config missing required key {key!r}")

    initial = str(cfg.get("initial_regime", "NORMAL"))
    if initial not in _REGIME_SET:
        raise ValueError(f"initial_regime must be one of {sorted(_REGIME_SET)}, got {initial!r}")

    markov = cfg["markov"]
    if not isinstance(markov, dict):
        raise ValueError("markov must be an object mapping regime -> next-regime probabilities")

    for r in REGIMES:
        if r not in markov:
            raise ValueError(f"markov missing row for regime {r!r}")
        row = markov[r]
        if not isinstance(row, dict):
            raise ValueError(f"markov[{r!r}] must be an object")
        total = 0.0
        for t in REGIMES:
            if t not in row:
                raise ValueError(f"markov[{r!r}] missing column for {t!r}")
            p = float(row[t])
            if p < 0.0:
                raise ValueError(f"markov[{r!r}][{t!r}] must be non-negative, got {p}")
            total += p
        if not np.isfinite(total) or total <= 0.0:
            raise ValueError(f"markov row {r!r} must sum to a positive finite value, got {total}")
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"markov row {r!r} must sum to 1.0 (tolerance 1e-6), got {total:.10f}"
            )

    ln = cfg["lognormal"]
    if not isinstance(ln, dict):
        raise ValueError("lognormal must be an object keyed by regime")

    for r in REGIMES:
        if r not in ln:
            raise ValueError(f"lognormal missing regime {r!r}")
        block = ln[r]
        if not isinstance(block, dict):
            raise ValueError(f"lognormal[{r!r}] must be an object")
        for field in ("tx_count", "interval_s"):
            if field not in block:
                raise ValueError(f"lognormal[{r!r}] missing {field!r}")
            params = block[field]
            if not isinstance(params, dict):
                raise ValueError(f"lognormal[{r!r}][{field!r}] must be an object with mu, sigma")
            mu = float(params["mu"])
            sigma = float(params["sigma"])
            if sigma <= 0.0 or not np.isfinite(sigma):
                raise ValueError(
                    f"lognormal[{r!r}][{field!r}].sigma must be finite and > 0, got {sigma}"
                )
            if not np.isfinite(mu):
                raise ValueError(f"lognormal[{r!r}][{field!r}].mu must be finite, got {mu}")

    steps = int(cfg["steps"])
    if steps < 1:
        raise ValueError(f"steps must be >= 1, got {steps}")


def _sample_next_regime(
    rng: np.random.Generator,
    current: str,
    markov: Mapping[str, Mapping[str, float]],
) -> str:
    row = markov[current]
    probs = np.array([float(row[t]) for t in REGIMES], dtype=np.float64)
    probs /= probs.sum()
    idx = int(rng.choice(len(REGIMES), p=probs))
    return REGIMES[idx]


def _sample_lognormal_positive(rng: np.random.Generator, mu: float, sigma: float) -> float:
    """Draw from LogNormal(mu, sigma) in NumPy parameterization (underlying Normal mean/std)."""
    return float(rng.lognormal(mean=mu, sigma=sigma))


def generate_ledger_workload(
    config: MutableMapping[str, Any] | str | Path,
    *,
    rng: np.random.Generator | None = None,
) -> pd.DataFrame:
    """Synthesize ``steps`` ledgers with regime Markov chain and lognormal workloads.

    **Config** (JSON object or path to JSON):

    - ``steps`` (int): number of ledgers.
    - ``start_ledger_index`` (int, default ``1``): first ``ledger_index``.
    - ``initial_regime``: ``NORMAL`` | ``STRESSED`` | ``BURST``.
    - ``markov``: for each regime, probabilities of the next regime (row sums to 1).
    - ``lognormal``: per regime, ``tx_count`` and ``interval_s`` each with ``mu`` and ``sigma``
      (NumPy :func:`numpy.random.Generator.lognormal` parameters).
    - ``seed`` (int, optional): passed to :class:`numpy.random.default_rng` if ``rng`` is omitted.
    - ``tx_count_min`` (int, default ``0``): floor after rounding transaction counts.
    - ``output_csv`` (str, optional): if set, write the DataFrame to this path.

    **Columns:** ``ledger_index``, ``regime``, ``interval_s``, ``tx_count``, ``send_spacing_s``
    where ``send_spacing_s`` is ``interval_s / max(tx_count, 1)`` (uniform split of the ledger
    window across transactions).
    """
    if isinstance(config, (str, Path)):
        cfg: dict[str, Any] = dict(load_workload_json_config(config))
    else:
        cfg = dict(config)

    validate_workload_config(cfg)

    if rng is None:
        seed = cfg.get("seed")
        rng = np.random.default_rng(seed)

    steps = int(cfg["steps"])
    start_idx = int(cfg.get("start_ledger_index", 1))
    tx_floor = int(cfg.get("tx_count_min", 0))
    if tx_floor < 0:
        raise ValueError(f"tx_count_min must be >= 0, got {tx_floor}")

    markov = cfg["markov"]
    ln = cfg["lognormal"]

    regime = str(cfg.get("initial_regime", "NORMAL"))
    rows: list[dict[str, Any]] = []

    for i in range(steps):
        if i > 0:
            regime = _sample_next_regime(rng, regime, markov)

        block = ln[regime]
        tx_mu = float(block["tx_count"]["mu"])
        tx_sigma = float(block["tx_count"]["sigma"])
        int_mu = float(block["interval_s"]["mu"])
        int_sigma = float(block["interval_s"]["sigma"])

        interval_s = _sample_lognormal_positive(rng, int_mu, int_sigma)
        tx_raw = _sample_lognormal_positive(rng, tx_mu, tx_sigma)
        tx_count = max(tx_floor, int(round(tx_raw)))
        send_spacing_s = interval_s / max(tx_count, 1)

        rows.append(
            {
                "ledger_index": start_idx + i,
                "regime": regime,
                "interval_s": interval_s,
                "tx_count": tx_count,
                "send_spacing_s": send_spacing_s,
            }
        )

    df = pd.DataFrame(rows)
    out_csv = cfg.get("output_csv")
    if out_csv:
        out_path = Path(str(out_csv))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out_path, index=False)

    return df


def run_generate(config_path: str) -> Path:
    cfg = load_yaml(config_path)
    input_jsonl = Path(cfg["input_jsonl"])
    out_path = Path(cfg["output_bundle_json"])

    events: list[dict[str, Any]] = list(iter_jsonl(input_jsonl))
    kinds: dict[str, int] = {}
    for e in events:
        k = str(e.get("kind", ""))
        kinds[k] = kinds.get(k, 0) + 1

    bundle: dict[str, Any] = {
        "xbosm_version": str(cfg.get("xbosm_version", "0.1.0")),
        "generated_at": utc_now_iso(),
        "source_events_path": str(input_jsonl.resolve()),
        "event_count": len(events),
        "kinds": kinds,
        "artifacts": [
            {
                "name": "normalized_events",
                "format": "jsonl",
                "path": str(input_jsonl.resolve()),
            }
        ],
        "notes": cfg.get("notes", ""),
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(bundle, f, indent=2)
        f.write("\n")

    return out_path
