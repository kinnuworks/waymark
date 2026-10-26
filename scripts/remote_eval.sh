#!/usr/bin/env bash
# Runs on the Graviton instance (via scripts/bench_remote.sh): fetches 7-Scenes "heads",
# builds the public sample building, and runs the benchmarks on Graviton.
set -euo pipefail
cd /opt/waymark
DATA_ROOT=${WAYMARK_DATA:-/var/lib/waymark}
export SEVENSCENES="$DATA_ROOT/7scenes"
export WAYMARK_EVAL="$DATA_ROOT/eval"
mkdir -p "$SEVENSCENES" "$WAYMARK_EVAL"
if [ ! -d "$SEVENSCENES/heads/seq-01" ]; then
  cd "$SEVENSCENES"
  curl -sSL -o heads.zip https://download.microsoft.com/download/2/8/5/28564B23-0828-408F-8631-23B1EFF1DAC8/heads.zip
  unzip -q -o heads.zip && rm heads.zip
  cd heads && for z in seq-*.zip; do unzip -q -o "$z" && rm "$z"; done
  cd /opt/waymark
fi
PY=server/.venv/bin/python
what=${1:-all}
case "$what" in
  demo) $PY eval/make_demo.py heads ;;
  bench) $PY eval/sevenscenes.py heads all --step 10 ;;
  speed) $PY eval/speed.py ;;
  all) $PY eval/speed.py; $PY eval/sevenscenes.py heads all --step 10; $PY eval/make_demo.py heads ;;
esac
