from __future__ import annotations

import argparse

from xmrs_xbosm.xmrs.simulator import run_simulate


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m xmrs_xbosm.xmrs")
    p.add_argument("--config", required=True)
    args = p.parse_args()
    run_simulate(args.config)


if __name__ == "__main__":
    main()
