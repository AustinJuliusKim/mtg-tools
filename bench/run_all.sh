#!/usr/bin/env bash
# Run every lane on every dataset; results land in bench/out/, table on stdout.
# Usage: bash bench/run_all.sh   (from the repo root; node 24 + repo venv assumed)
set -euo pipefail

BENCH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$BENCH")"
PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || PY=python3

mkdir -p "$BENCH/out"
rm -f "$BENCH/out"/*.json

echo "== generating synthetic datasets =="
"$PY" "$BENCH/generate.py"

[ -d "$BENCH/js/node_modules" ] || npm --prefix "$BENCH/js" install --silent

run() { # dataset lane cmd...
  local dataset="$1" lane="$2"; shift 2
  echo "-- $dataset / $lane"
  "$@" | "$PY" -c "
import json, sys
result = json.load(sys.stdin)
result['dataset'] = '$dataset'
print(json.dumps(result))
" > "$BENCH/out/$dataset-$lane.json"
}

SYNTH20="$BENCH/data/synth-20k.csv"
SYNTH100="$BENCH/data/synth-100k.csv"
SEALED20="$BENCH/data/sealed-20k.csv"
REAL=("$HOME/Desktop/Binders.csv" "$HOME/Desktop/Binders2.csv" "$HOME/Desktop/Binders3.csv")

for ds in synth-20k synth-100k; do
  file="$BENCH/data/$ds.csv"
  run "$ds" python-app  "$PY" "$BENCH/bench_python.py" --lane app  "$file"
  run "$ds" python-fast "$PY" "$BENCH/bench_python.py" --lane fast "$file"
  run "$ds" python-stage "$PY" "$BENCH/bench_python.py" --lane stage --runs 3 "$file"
  run "$ds" node    node "$BENCH/js/bench_node.mjs" --lane singles "$file"
  run "$ds" browser node "$BENCH/bench_browser.mjs" --lane singles "$file"
done

run sealed-20k python-app "$PY" "$BENCH/bench_python.py" --lane sealed "$SEALED20"
run sealed-20k node    node "$BENCH/js/bench_node.mjs" --lane sealed "$SEALED20"
run sealed-20k browser node "$BENCH/bench_browser.mjs" --lane sealed "$SEALED20"

if [ -f "${REAL[0]}" ]; then
  echo "== real exports found on Desktop — running real-922 =="
  run real-922 python-app  "$PY" "$BENCH/bench_python.py" --lane app  "${REAL[@]}"
  run real-922 python-fast "$PY" "$BENCH/bench_python.py" --lane fast "${REAL[@]}"
  run real-922 node    node "$BENCH/js/bench_node.mjs" --lane singles "${REAL[@]}"
  cp "${REAL[@]}" "$BENCH/data/"   # served copies for the browser lane (gitignored)
  run real-922 browser node "$BENCH/bench_browser.mjs" --lane singles \
    "$BENCH/data/Binders.csv" "$BENCH/data/Binders2.csv" "$BENCH/data/Binders3.csv"
fi

echo
echo "== results =="
"$PY" "$BENCH/assemble.py"
