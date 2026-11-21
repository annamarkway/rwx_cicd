"""Build-time cache of DuckDB's expected results for the TPC executor tests.

test_execute_tpch/tpcds compare sqlglot's executor with DuckDB running the
same query, transpiled by sqlglot. DuckDB's answer depends on the patch only
through that transpiled SQL, so at the pinned commit we store, per query,
the transpiled SQL and DuckDB's result. A check reuses the result only when
the patched sqlglot transpiles the query to exactly the same SQL.

Usage: python precompute.py <pinned sqlglot dir> <out.pkl>
"""

import os
import pickle
import sys

src, dest = sys.argv[1], sys.argv[2]
os.chdir(src)
sys.path.insert(0, src)
os.environ["SKIP_INTEGRATION"] = "0"

from sqlglot import transpile  # noqa: E402
from tests.test_executor import TestExecutor  # noqa: E402

TestExecutor.setUpClass()
cache = {}
for tpch, sqls, conn in ((True, TestExecutor.tpch_sqls, TestExecutor.tpch_conn),
                         (False, TestExecutor.tpcds_sqls, TestExecutor.tpcds_conn)):
    for meta, sql, _ in sqls:
        try:
            duck_sql = transpile(sql, write="duckdb")[0]
            cache[(tpch, sql)] = (duck_sql, conn.execute(duck_sql).fetchdf())
        except Exception as e:  # noqa: BLE001
            print("skip", repr(e)[:100], file=sys.stderr)
TestExecutor.tearDownClass()
with open(dest, "wb") as f:
    pickle.dump(cache, f)
print(f"cached {len(cache)} DuckDB results", file=sys.stderr)
