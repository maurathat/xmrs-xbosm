from __future__ import annotations

import argparse

from xmrs_xbosm.xbosm.generator import run_generate


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m xmrs_xbosm.xbosm")
    p.add_argument("--config", required=True)
    args = p.parse_args()
    run_generate(args.config)


if __name__ == "__main__":
    main()
