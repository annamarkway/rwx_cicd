#!/bin/sh
# Build map.json from the pinned sqlglot checkout. Usage: tools/mapgen.sh <sqlglot_dir> <out_dir>
set -e
SRC=$(cd "$1" && pwd); OUT=$(mkdir -p "$2" && cd "$2" && pwd); HERE=$(cd "$(dirname "$0")" && pwd)
rm -f "$OUT"/.coverage* "$OUT"/opens-*.json "$OUT"/classes-*.json
docker run --rm -v "$SRC":/src:ro -v "$HERE":/tools:ro -v "$OUT":/out -e PYTHONPATH=/src:/tools \
  -e MAP_OUT=/out -e COVERAGE_FILE=/out/.coverage -e PYTHONDONTWRITEBYTECODE=1 br-lab \
  python -m pytest -q -p no:cacheprovider -p mapplugin -n 4 --cov=/src \
  --cov-context=test --cov-report= tests | tail -3
docker run --rm -v "$HERE":/tools:ro -v "$HERE/../sample":/sample:ro -v "$OUT":/out br-lab \
  python /tools/extract.py /out/.coverage /out /sample/tests.json /out/map.json
