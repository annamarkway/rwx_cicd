"""Turn a coverage DB (with per-test contexts) plus opens-*.json into map.json.

Usage (inside the lab image): python extract.py /out/.coverage /out tests.json out.json

map.json:
  lines:  {relpath: {line: [test numbers]}}     lines run *during* a test
  static: {relpath: [lines]}                    lines run outside any test (imports)
  opens:  {relpath: [test numbers]}             non-.py files a test opened
  classes: {"module:qualname": [test numbers]}  classes a test instantiated (MRO-expanded)
"""

import glob
import json
import sys

import coverage

db, outdir, tests_json, dest = sys.argv[1:5]
tests = json.load(open(tests_json))["tests"]
num = {t["id"]: t["number"] for t in tests}

data = coverage.CoverageData(basename=db)
data.read()

lines = {}
static = {}
unknown = set()
for f in data.measured_files():
    rel = f[len("/src/"):] if f.startswith("/src/") else f
    by_line = data.contexts_by_lineno(f)
    fl = {}
    st = []
    for ln, ctxs in by_line.items():
        ids = set()
        is_static = False
        for c in ctxs:
            if not c:
                is_static = True
                continue
            node = c.rsplit("|", 1)[0]
            if node in num:
                ids.add(num[node])
            else:
                unknown.add(node)
        if ids:
            fl[ln] = sorted(ids)
        if is_static:
            st.append(ln)
    lines[rel] = fl
    static[rel] = sorted(st)

opens = {}
for p in glob.glob(f"{outdir}/opens-*.json"):
    for node, files in json.load(open(p)).items():
        if node not in num:
            unknown.add(node)
            continue
        for f in files:
            opens.setdefault(f, set()).add(num[node])
opens = {k: sorted(v) for k, v in opens.items()}

classes = {}
for p in glob.glob(f"{outdir}/classes-*.json"):
    for node, names in json.load(open(p)).items():
        for n in names:
            classes.setdefault(n, set()).add(num[node])
classes = {k: sorted(v) for k, v in classes.items()}

print("unknown contexts:", len(unknown), sorted(unknown)[:5], file=sys.stderr)
json.dump({"lines": lines, "static": static, "opens": opens, "classes": classes}, open(dest, "w"), separators=(",", ":"))
