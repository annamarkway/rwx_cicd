"""Entry point: one JSON line per commit on stdout, logs on stderr."""

from __future__ import annotations

import gzip
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from select_tests import ALL, Selector  # noqa: E402

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = Path(os.environ.get("BR_SRC", "/opt/sqlglot"))
MAP = Path(os.environ.get("BR_MAP", HERE / "map.json.gz"))
WORKER_MS = 2000
MAX_WORKERS = 8


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def pack(numbers, ms):
    """Split tests over 1..8 workers, minimising busiest load + 2 s a worker."""
    if not numbers:
        return []
    order = sorted(numbers, key=lambda n: -ms[n])
    best = None
    for k in range(1, MAX_WORKERS + 1):
        if k > len(order):
            break
        loads = [0] * k
        bins = [[] for _ in range(k)]
        for n in order:
            i = loads.index(min(loads))
            bins[i].append(n)
            loads[i] += ms[n]
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, bins)
    return best[1]


def main():
    t0 = time.process_time()
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    every = set(ms)
    with gzip.open(MAP, "rt") as f:
        mapdata = json.load(f)
    sel = Selector(SRC, mapdata, tests)
    log(f"loaded in {time.process_time() - t0:.2f}s cpu")
    total = 0
    for patch in sorted((DATA / "commits").glob("*.patch")):
        cid = patch.stem
        try:
            chosen = sel.select(patch.read_text(errors="replace"))
        except Exception as e:  # never crash: run everything
            sel.log.append(f"crash: {e!r}")
            chosen = ALL
        if chosen == ALL:
            chosen = every
        chosen = sorted(set(chosen) & every)
        workers = pack(chosen, ms)
        cost = (max(sum(ms[n] for n in w) for w in workers) + WORKER_MS * len(workers)) if workers else 0
        total += cost
        log(f"{cid}: {len(chosen)} tests, {len(workers)} workers, {cost / 1000:.1f}s; {'; '.join(sel.log)[:200]}")
        print(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")), flush=True)
    log(f"predicted simulated {total / 1000:.1f}s, cpu {time.process_time() - t0:.2f}s")


if __name__ == "__main__":
    main()
