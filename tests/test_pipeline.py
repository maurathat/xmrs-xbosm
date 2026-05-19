from __future__ import annotations

from pathlib import Path

from xmrs_xbosm.collector.service import run_collect
from xmrs_xbosm.xbosm.generator import run_generate
from xmrs_xbosm.xmrs.simulator import run_simulate


def _write_cfg(tmp: Path, name: str, body: str) -> Path:
    p = tmp / name
    p.write_text(body, encoding="utf-8")
    return p


def test_simulate_collect_generate_roundtrip(tmp_path: Path) -> None:
    sim_cfg = _write_cfg(
        tmp_path,
        "xmrs.yaml",
        "\n".join(
            [
                f"output_jsonl: {tmp_path / 'sim.jsonl'}",
                "steps: 3",
                "base_load: 0.2",
                "load_drift_per_step: 0.1",
            ]
        ),
    )
    out_sim = run_simulate(str(sim_cfg))
    assert out_sim.exists()
    assert out_sim.read_text(encoding="utf-8").count("\n") >= 3

    col_cfg = _write_cfg(
        tmp_path,
        "collector.yaml",
        "\n".join(
            [
                f"output_jsonl: {tmp_path / 'collected.jsonl'}",
                "source_name: test-collector",
            ]
        ),
    )
    run_collect(str(col_cfg))

    gen_cfg = _write_cfg(
        tmp_path,
        "xbosm.yaml",
        "\n".join(
            [
                f"input_jsonl: {tmp_path / 'collected.jsonl'}",
                f"output_bundle_json: {tmp_path / 'bundle.json'}",
            ]
        ),
    )
    bundle_path = run_generate(str(gen_cfg))
    text = bundle_path.read_text(encoding="utf-8")
    assert "event_count" in text
    assert "kinds" in text
