"""Build map.json with every test run alone in a fresh process.

Run-once code (lazy imports, caches, setUpClass, fixtures loaded once per
class) is attributed to every test that needs it, not just to whichever test
happened to run first in a shared process.

Usage (inside the lab image, /src = pinned sqlglot, /tools = this dir):
  python /tools/isomap.py /sample/tests.json /out [jobs]

Per test, coverage records two contexts:
  A  collection: importing sqlglot and the test module
  B  setup + call + teardown of the test itself
map.json:
  lines   {relpath: {line: [tests]}}   lines run in B
  static  {relpath: [lines]}           lines run in A by any test
  imports {relpath: {line: [test files]}} lines run while importing a test file
  opens   {relpath: [tests]}           non-.py files opened (A or B)
  classes {"module:qualname": [tests]} classes instantiated in B
"""

import glob
import json
import os
import sys
import time

tests_json, out = sys.argv[1], sys.argv[2]
JOBS = int(sys.argv[3]) if len(sys.argv) > 3 else 6
tests = json.load(open(tests_json))["tests"]
iso = os.path.join(out, "iso")
os.makedirs(iso, exist_ok=True)

os.environ.update(SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
sys.path[:0] = ["/src", "/tools"]
os.chdir("/src")

# Heavy third-party imports shared by every child. sqlglot is NOT imported
# here: each child imports it under coverage.
import coverage  # noqa: E402
import duckdb  # noqa: E402,F401
import numpy  # noqa: E402,F401
import pandas  # noqa: E402,F401
import pandas.testing  # noqa: E402,F401
import pytest  # noqa: E402

assert not any(m == "sqlglot" or m.startswith("sqlglot.") for m in sys.modules)


def child(t):
    n = t["number"]
    os.environ["PYTEST_XDIST_WORKER"] = f"t{n}"  # mapplugin names its files by this
    os.environ["MAP_OUT"] = iso
    cov = coverage.Coverage(data_file=os.path.join(iso, f"cov{n}"), source=["/src"],
                            context="A", config_file=False)
    cov.start()
    import mapplugin

    mapplugin.COV = cov
    devnull = open(os.devnull, "w")
    sys.stdout = sys.stderr = devnull
    try:
        pytest.main(["-q", "-p", "no:cacheprovider", "-p", "mapplugin", "-p", "no:xdist",
                     "-o", "addopts=", "--timeout", "900", t["id"]])
    finally:
        cov.stop()
        cov.save()


done = set(int(os.path.basename(p)[3:]) for p in glob.glob(os.path.join(iso, "cov*")))
todo = sorted((t for t in tests if t["number"] not in done), key=lambda t: -t["seconds"])
running = {}
t0 = time.time()
k = 0
while todo or running:
    while todo and len(running) < JOBS:
        t = todo.pop(0)
        pid = os.fork()
        if pid == 0:
            code = 0
            try:
                child(t)
            except BaseException:  # noqa: BLE001
                code = 1
            os._exit(code)
        running[pid] = t
    pid, status = os.wait()
    t = running.pop(pid)
    k += 1
    if status:
        print(f"child for {t['id']} exited {status}", file=sys.stderr, flush=True)
    if k % 100 == 0:
        print(f"{k} done, {len(todo)} left, {time.time() - t0:.0f}s", file=sys.stderr, flush=True)

# ---- extract
lines, static, opens, classes = {}, {}, {}, {}
imports = {}  # rel -> line -> test files whose import (collection) ran it
for t in tests:
    n = t["number"]
    data = coverage.CoverageData(basename=os.path.join(iso, f"cov{n}"))
    if not os.path.exists(data.data_filename()):
        print("missing coverage for", t["id"], file=sys.stderr)
        continue
    data.read()
    for f in data.measured_files():
        rel = f[len("/src/"):] if f.startswith("/src/") else f
        for ln, ctxs in data.contexts_by_lineno(f).items():
            # The static context is "A"; after switch_context("B") lines are
            # labelled "A|B".
            if any(c.endswith("B") for c in ctxs):
                lines.setdefault(rel, {}).setdefault(ln, set()).add(n)
            if "A" in ctxs:
                static.setdefault(rel, set()).add(ln)
                imports.setdefault(rel, {}).setdefault(ln, set()).add(t["file"])
    po = os.path.join(iso, f"opens-t{n}.json")
    if os.path.exists(po):
        for _node, files in json.load(open(po)).items():
            for f in files:
                opens.setdefault(f, set()).add(n)
    pc = os.path.join(iso, f"classes-t{n}.json")
    if os.path.exists(pc):
        for _node, names in json.load(open(pc)).items():
            for c in names:
                classes.setdefault(c, set()).add(n)

json.dump({
    "lines": {f: {str(ln): sorted(v) for ln, v in d.items()} for f, d in lines.items()},
    "static": {f: sorted(v) for f, v in static.items()},
    "imports": {f: {str(ln): sorted(v) for ln, v in d.items()} for f, d in imports.items()},
    "opens": {f: sorted(v) for f, v in opens.items()},
    "classes": {c: sorted(v) for c, v in classes.items()},
}, open(os.path.join(out, "map.json"), "w"), separators=(",", ":"))
print(f"wrote map in {time.time() - t0:.0f}s", file=sys.stderr)
