"""Per-query coverage for the TPC-H / TPC-DS executor tests.

test_execute_tpcds (21 s recorded), test_execute_tpch and test_optimized_tpch
loop over fixture queries. A change that only some queries execute can be
checked by running just those queries. This builds, per query, the lines its
work executes, in a fresh process per query (so caches and lazy imports count
for every query that needs them).

Usage (lab image): python /tools/querymap.py /out/querymap.json [jobs]

querymap.json:
  common  {relpath: [lines]}                 run by imports + setUpClass
  tpch    {relpath: {line: [query index]}}   execute + optimized + transpile
  tpcds   {relpath: {line: [query index]}}
  counts  {"tpch": n, "tpcds": n}
"""

import json
import os
import pickle
import sys
import time

dest = sys.argv[1]
JOBS = int(sys.argv[2]) if len(sys.argv) > 2 else 6
work = os.path.join(os.path.dirname(dest), "qcov")
os.makedirs(work, exist_ok=True)
os.environ.update(SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
sys.path[:0] = ["/src"]
os.chdir("/src")

import coverage  # noqa: E402
import duckdb  # noqa: E402,F401
import numpy  # noqa: E402,F401
import pandas  # noqa: E402,F401
import pandas.testing  # noqa: E402,F401

assert "sqlglot" not in sys.modules


def child(kind, i):
    cov = coverage.Coverage(data_file=os.path.join(work, f"{kind}{i}"), source=["/src"],
                            context="common", config_file=False)
    cov.start()
    from sqlglot import parse_one, transpile
    from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA
    from tests.test_executor import TestExecutor, initializer, mp_execute

    TestExecutor.setUpClass()
    schema, tables, sqls = (
        (TPCH_SCHEMA, TestExecutor.tpch_tables, TestExecutor.tpch_sqls) if kind == "tpch"
        else (TPCDS_SCHEMA, TestExecutor.tpcds_tables, TestExecutor.tpcds_sqls)
    )
    initializer(*pickle.loads(pickle.dumps((schema, tables))))
    cov.switch_context("q")
    meta, sql, optimized = sqls[i]
    try:
        expr = parse_one(sql)
        res = mp_execute(*pickle.loads(pickle.dumps((expr, meta))))
        pickle.loads(pickle.dumps(res))
    except Exception:  # noqa: BLE001
        pass
    for s in (sql, optimized) if kind == "tpch" else (sql,):
        try:
            transpile(s, write="duckdb")
        except Exception:  # noqa: BLE001
            pass
    cov.stop()
    cov.save()


# Query counts, from the fixtures themselves (in a throwaway child).
r, w = os.pipe()
if os.fork() == 0:
    from tests.helpers import load_sql_fixture_pairs

    n = {k: len(list(load_sql_fixture_pairs(f"optimizer/{k[:3]}-{k[3:]}/{k[:3]}-{k[3:]}.sql")))
         for k in ("tpch", "tpcds")}
    os.write(w, json.dumps(n).encode())
    os._exit(0)
os.close(w)
counts = json.loads(os.read(r, 1000))
os.wait()
print("queries", counts, file=sys.stderr)

todo = [(k, i) for k in ("tpcds", "tpch") for i in range(counts[k])]
running = {}
t0 = time.time()
while todo or running:
    while todo and len(running) < JOBS:
        job = todo.pop(0)
        pid = os.fork()
        if pid == 0:
            code = 0
            try:
                child(*job)
            except BaseException as e:  # noqa: BLE001
                print("child failed", job, repr(e), file=sys.stderr)
                code = 1
            os._exit(code)
        running[pid] = job
    pid, status = os.wait()
    running.pop(pid)
print(f"ran {sum(counts.values())} queries in {time.time() - t0:.0f}s", file=sys.stderr)

out = {"common": {}, "tpch": {}, "tpcds": {}, "counts": counts}
common = {}
for k in ("tpch", "tpcds"):
    for i in range(counts[k]):
        data = coverage.CoverageData(basename=os.path.join(work, f"{k}{i}"))
        data.read()
        for f in data.measured_files():
            rel = f[len("/src/"):] if f.startswith("/src/") else f
            for ln, ctxs in data.contexts_by_lineno(f).items():
                if "common" in ctxs:
                    common.setdefault(rel, set()).add(ln)
                if any(c.endswith("q") for c in ctxs):
                    out[k].setdefault(rel, {}).setdefault(str(ln), []).append(i)
out["common"] = {f: sorted(v) for f, v in common.items()}
json.dump(out, open(dest, "w"), separators=(",", ":"))
print("wrote", dest, file=sys.stderr)
