"""Offline evaluation of selection + run policies on labeled patches.

Usage: python3 tools/evaluate.py [--verbose] <patch_dir:labels.json> ...

The sample directory works too: sample/commits:sample/answers.json.
Running tests is simulated: a test "fails" when it is in the labels, and its
CPU cost is its recorded seconds plus a fixed per-run overhead.
"""

import gzip
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from main import pack  # noqa: E402
from select_tests import ALL, Selector  # noqa: E402

RUN_OVERHEAD = 0.35
verbose = "--verbose" in sys.argv
args = [a for a in sys.argv[1:] if not a.startswith("--")]

tests = json.loads((ROOT / "sample/tests.json").read_text())["tests"]
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
name = {t["number"]: t["id"] for t in tests}
every = set(ms)
sel = Selector(ROOT.parent / "sqlglot_src/sqlglot", json.load(gzip.open(ROOT / "app/map.json.gz", "rt")), tests)


def cost(chosen):
    w = pack(sorted(chosen), ms)
    return (max(sum(ms[n] for n in x) for x in w) + 2000 * len(w)) / 1000 if w else 0.0


def policies(C, F, cut):
    """Yield (policy name, output set, cpu seconds)."""
    yield "static", C, 0.0
    R = {n for n in C if ms[n] <= cut}
    H = C - R
    cpu = (sum(ms[n] for n in R) / 1000 + RUN_OVERHEAD) if R else 0.0
    FR = F & R
    yield "run-cheap", FR | H, cpu
    # Only run when there is nothing heavy to keep anyway.
    if H:
        yield "run-if-no-heavy", C, 0.0
    else:
        yield "run-if-no-heavy", FR, cpu
    # Drop the heavy tests when every cheap candidate passes.
    yield "drop-heavy-if-cheap-pass", (FR | H) if FR else set(), cpu
    # Probe: cheapest first, stop at the first failure, then print the static set.
    order = sorted(R, key=lambda n: ms[n])
    pc, hit = (RUN_OVERHEAD if R else 0.0), False
    for n in order:
        pc += ms[n] / 1000
        if n in F:
            hit = True
            break
    yield "probe-x keep-heavy", (C if hit else H), pc
    yield "probe-x drop-heavy", (C if hit else set()), pc


rows = []
for spec in args:
    pdir, lab = spec.split(":")
    labels = json.load(open(lab))
    labels = labels.get("commits", labels)
    for cid, v in sorted(labels.items()):
        p = Path(pdir) / f"{cid}.patch"
        if not p.exists():
            continue
        C = sel.select(p.read_text())
        note = "; ".join(sel.log)
        C = every if C == ALL else set(C) & every
        F = set(v["failing"])
        rows.append((cid, C, F, note))

for cut in (1500,):
    totals = {}
    for cid, C, F, note in rows:
        for pol, out, cpu in policies(C, F, cut):
            miss = bool(F - out)
            t = totals.setdefault(pol, [0.0, 0, 0.0])
            t[0] += cost(out)
            t[1] += miss
            t[2] += cpu
            if verbose and pol == "static" and (miss or len(C) == len(every)):
                print(f"{cid}: {'MISS ' + str(sorted(name[n] for n in F - out)[:3]) if miss else 'ALL'} {note}")
    print(f"{len(rows)} patches, oracle {sum(cost(F) for _, _, F, _ in rows):.1f}s")
    for pol, (s, m, c) in totals.items():
        print(f"  {pol:26s} sim {s:8.1f}  misses {m:3d}  cpu {c:7.1f}  total {s + 300 * m + c:8.1f}")
