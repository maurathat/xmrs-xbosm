#!/usr/bin/env bash
set -euo pipefail

# XBOSM telemetry test -- full run on your Mac Mini.
#   1. sets up an isolated venv with deps (won't touch system Python)
#   2. captures live XRPL telemetry for N minutes
#   3. runs the XBOSM burst + policy analysis on the REAL capture
#
# Needs these alongside it (already in the package):
#   collect_xrpl.py  telemetry.py  burst.py  gossip.py  run_test.py
#
# Usage:
#   chmod +x run_on_mini.sh
#   ./run_on_mini.sh           # 45 min capture (default)
#   ./run_on_mini.sh 60        # 60 min capture

MINUTES="${1:-45}"
CSV="xrpl_test_telemetry.csv"
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

echo "==> XBOSM telemetry test  (capture ${MINUTES} min)"

# isolated environment
if [ ! -d ".venv" ]; then
  echo "==> creating virtualenv (.venv)"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
echo "==> installing deps (websockets, numpy, pandas)"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet websockets numpy pandas certifi

# live capture -- this is the step that needs open network, so it runs here
echo "==> capturing live XRPL telemetry -> ${CSV}"
python collect_xrpl.py --minutes "${MINUTES}" --out "${CSV}"

# analysis on the real capture
echo "==> running XBOSM analysis on real telemetry"
python run_test.py --csv "${CSV}"

echo "==> done. artifacts: ${CSV}, telemetry_scored.csv, policy_comparison.csv, summary.json"
