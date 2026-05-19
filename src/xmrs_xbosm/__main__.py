"""Unified CLI: `python -m xmrs_xbosm` or `xmrs-xbosm`."""

from __future__ import annotations

import argparse
import sys

from xmrs_xbosm.collector.service import run_collect
from xmrs_xbosm.xbosm.generator import run_generate
from xmrs_xbosm.xmrs.simulator import run_simulate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xmrs-xbosm")
    sub = parser.add_subparsers(dest="command", required=True)

    p_col = sub.add_parser("collect", help="Run the telemetry collector")
    p_col.add_argument("--config", required=True, help="Path to collector YAML config")
    p_col.set_defaults(_run=lambda a: run_collect(a.config))

    p_gen = sub.add_parser("generate-xbosm", help="Build an XBoSM bundle from collected events")
    p_gen.add_argument("--config", required=True, help="Path to XBoSM generator YAML config")
    p_gen.set_defaults(_run=lambda a: run_generate(a.config))

    p_sim = sub.add_parser("simulate-xmrs", help="Run the XMRS workload simulator")
    p_sim.add_argument("--config", required=True, help="Path to XMRS simulator YAML config")
    p_sim.set_defaults(_run=lambda a: run_simulate(a.config))

    args = parser.parse_args(argv)
    args._run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
