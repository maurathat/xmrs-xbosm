from __future__ import annotations

import argparse

from xmrs_xbosm.collector.service import run_collect


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m xmrs_xbosm.collector")
    p.add_argument("--config", required=True)
    args = p.parse_args()
    run_collect(args.config)


if __name__ == "__main__":
    main()
