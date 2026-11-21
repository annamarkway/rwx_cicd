#!/bin/sh
# Run the image natively (fast on Apple Silicon) under the scorer's other limits.
# Usage: tools/run_native.sh <image> <sample_dir> <out.jsonl> [extra docker args]
IMG=$1; SAMPLE=$(cd "$2" && pwd); OUT=$3; shift 3
exec docker run --rm --network none --cpus 2 --memory 4096m --memory-swap 4096m \
  --pids-limit 256 --read-only --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
  --volume "$SAMPLE/commits":/data/commits:ro --volume "$SAMPLE/tests.json":/data/tests.json:ro \
  "$@" "$IMG" > "$OUT"
