"""Run candidate tests on a patched copy of sqlglot, in a forked child.

The parent imports the heavy third-party test dependencies once (pytest,
pandas, duckdb, numpy) and forks a child per commit, so each run only pays
for importing sqlglot and the tests themselves.

Any doubt (crash, timeout, a test that never reported) counts as a failure:
the caller then keeps that test in its output.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path

from select_tests import Fallback, apply, parse_patch

WORK = Path(os.environ.get("BR_WORK", "/tmp/w"))
COPY = ("sqlglot", "tests", "sqlglotc", "setup.py", "setup.cfg", "pyproject.toml")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class Runner:
    def __init__(self, src: Path):
        self.src = src
        self.ready = False

    def prepare(self):
        if self.ready:
            return
        t = time.time()
        if WORK.exists():
            shutil.rmtree(WORK)
        WORK.mkdir(parents=True)
        for name in COPY:
            p = self.src / name
            if p.is_dir():
                shutil.copytree(p, WORK / name, copy_function=shutil.copy2)
            elif p.exists():
                shutil.copy2(p, WORK / name)
        os.environ["SKIP_INTEGRATION"] = "0"
        os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        # Heavy imports shared by every forked child.
        import pytest  # noqa: F401
        try:
            import _pytest.subtests  # noqa: F401
        except ImportError:
            pass
        import numpy  # noqa: F401
        import pandas  # noqa: F401
        import pandas.testing  # noqa: F401
        import duckdb  # noqa: F401
        import unittest.mock  # noqa: F401
        import concurrent.futures  # noqa: F401

        _install_inline_executor()
        self.ready = True
        log(f"runner ready in {time.time() - t:.2f}s")

    # -- patch the work tree in place, and put it back afterwards
    def _apply(self, patch_text: str):
        undo = []
        for fd in parse_patch(patch_text):
            if fd.binary:
                raise Fallback("binary")
            if fd.old and fd.new and fd.old != fd.new:
                raise Fallback("rename")
            rel = fd.new or fd.old
            dest = WORK / rel
            if fd.old is None:
                old_text = ""
            else:
                try:
                    old_text = (self.src / fd.old).read_text()
                except OSError as e:
                    raise Fallback(f"missing {fd.old}") from e
            undo.append(rel)
            if fd.new is None:
                dest.unlink(missing_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(apply(old_text, fd) if fd.hunks else old_text)
        return undo

    def _restore(self, undo):
        for rel in undo:
            orig = self.src / rel
            dest = WORK / rel
            if orig.exists():
                shutil.copy2(orig, dest)  # keeps the mtime so .pyc stays valid
            else:
                dest.unlink(missing_ok=True)

    def run(self, patch_text: str, nodeids: list[str], timeout: float) -> dict[str, str] | None:
        """Return {nodeid: 'passed'|'failed'} for tests that reported, or None."""
        return self.run_fn(patch_text, lambda: _child(nodeids), timeout)

    def run_fn(self, patch_text: str, fn, timeout: float) -> dict[str, str] | None:
        """Run fn() in a forked child against the patched tree; fn returns a dict."""
        self.prepare()
        undo = self._apply(patch_text)
        try:
            return self._fork(fn, timeout)
        finally:
            self._restore(undo)

    def _fork(self, fn, timeout):
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:  # child
            os.close(r)
            code = 0

            def emit(res, _w=w):
                os.write(_w, json.dumps(res).encode())
                os.close(_w)
                os._exit(0)

            global _EMIT
            _EMIT = emit
            try:
                res = fn()
                os.write(w, json.dumps(res).encode())
            except BaseException as e:  # noqa: BLE001
                try:
                    os.write(w, json.dumps({"__error__": repr(e)}).encode())
                except Exception:
                    pass
                code = 1
            finally:
                os.close(w)
                os._exit(code)
        os.close(w)
        deadline = time.time() + timeout
        chunks = []
        os.set_blocking(r, False)
        import select

        while True:
            left = deadline - time.time()
            if left <= 0:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                os.close(r)
                log("  run timed out")
                return None
            ready, _, _ = select.select([r], [], [], min(left, 1.0))
            if ready:
                b = os.read(r, 1 << 20)
                if not b:
                    break
                chunks.append(b)
        os.close(r)
        os.waitpid(pid, 0)
        try:
            res = json.loads(b"".join(chunks) or b"{}")
        except ValueError:
            return None
        if "__error__" in res:
            log(f"  child error {res['__error__'][:200]}")
            return None
        return res


_EMIT = None  # in the child: send results to the parent and exit at once


def _child(nodeids):
    os.chdir(WORK)
    sys.path.insert(0, str(WORK))
    # Drop anything sqlglot-ish the parent might have imported.
    for m in list(sys.modules):
        if m == "sqlglot" or m.startswith(("sqlglot.", "tests")):
            del sys.modules[m]
    sys.stdout = sys.stderr  # stdout belongs to the answers
    import pytest

    results: dict[str, str] = {}

    class Collect:
        def pytest_runtest_logreport(self, report):
            if report.failed:
                results[report.nodeid] = "failed"
                if os.environ.get("BR_STOP_ON_FAIL") == "1":
                    # The caller keeps every test that did not report "passed",
                    # so the rest of the run cannot change its answer.
                    _EMIT(results)
            elif report.when == "call" and report.nodeid not in results:
                results[report.nodeid] = "passed" if report.outcome == "passed" else report.outcome
            elif report.skipped and report.nodeid not in results:
                results[report.nodeid] = "skipped"

        def pytest_collectreport(self, report):
            if report.failed:
                results["__collect_failed__:" + report.nodeid] = "failed"

        def pytest_pycollect_makeitem(self, collector, name, obj):
            # pytest reads setUpClass while collecting a unittest class, so
            # the executor tests must be restricted before that happens.
            if name == "TestExecutor" and isinstance(obj, type):
                _restrict_executor(obj, json.loads(os.environ.get("BR_SUBSET") or "{}"))
            return None

        def pytest_collection_finish(self, session):
            _restrict_generator(json.loads(os.environ.get("BR_SUBSET") or "{}"))

    args = [
        "-q", "-p", "no:cacheprovider", "-p", "no:randomly", "-o", "addopts=",
        "--rootdir", str(WORK), "-W", "ignore",
        "--timeout", os.environ.get("BR_TEST_TIMEOUT", "20"),
        "--timeout-method", "signal", *nodeids,
    ]
    with open(os.devnull, "w") as dn:
        old = sys.stderr
        sys.stdout = dn
        sys.stderr = dn
        try:
            pytest.main(args, plugins=[Collect()])
        finally:
            sys.stderr = old
    return results


def _restrict(spec):
    """Run only part of the heavy loop-over-cases tests, using their own code.

    spec["tpch"] / spec["tpcds"]: query indices to keep (None = all). Queries
      whose text the patch changed are always kept; if the patch changed how
      many queries a fixture holds, all are kept.
    spec["gen_modules"]: keep only the dialects whose generator's class
      hierarchy lives in one of these modules (None = all).
    """
    _restrict_generator(spec)


DUCK_CACHE = os.environ.get("BR_DUCK_CACHE", "/opt/cache/duck.pkl")


def _use_duck_cache(TE):
    """Reuse DuckDB's pinned-commit answer when the patched transpile is identical."""
    te = sys.modules[TE.__module__]
    try:
        import pickle

        with open(DUCK_CACHE, "rb") as f:
            duck = pickle.load(f)
    except Exception:  # noqa: BLE001
        return
    orig = TE.cached_execute

    def cached_execute(self, sql, tpch=True):
        if sql not in self.cache:
            hit = duck.get((tpch, sql))
            if hit is not None and te.transpile(sql, write="duckdb")[0] == hit[0]:
                self.cache[sql] = hit[1].copy()
        return orig(self, sql, tpch=tpch)

    TE.cached_execute = cached_execute


def _restrict_executor(TE, spec):
    if TE.__dict__.get("_br_restricted"):
        return
    TE._br_restricted = True
    _use_duck_cache(TE)
    if not ("tpch" in spec or "tpcds" in spec):
        return
    if True:
        import tests.helpers as helpers

        orig = TE.setUpClass.__func__
        src = os.environ.get("BR_SRC_FIXTURES")

        def setUpClass(cls):
            orig(cls)
            for kind, attr, fname in (("tpch", "tpch_sqls", "optimizer/tpc-h/tpc-h.sql"),
                                      ("tpcds", "tpcds_sqls", "optimizer/tpc-ds/tpc-ds.sql")):
                keep = spec.get(kind)
                if keep is None:
                    continue
                new = getattr(cls, attr)
                saved = helpers.FIXTURES_DIR
                try:
                    helpers.FIXTURES_DIR = src
                    old = list(helpers.load_sql_fixture_pairs(fname))
                finally:
                    helpers.FIXTURES_DIR = saved
                if len(old) != len(new):
                    continue  # the fixture's shape changed: run every query
                idx = set(keep) | {i for i, (a, b) in enumerate(zip(old, new)) if a != b}
                setattr(cls, attr, [new[i] for i in sorted(idx) if i < len(new)])

        TE.setUpClass = classmethod(setUpClass)


def _restrict_generator(spec):
    tg = sys.modules.get("tests.test_generator")
    mods = spec.get("gen_modules")
    if tg is not None and mods is not None:
        mods = set(mods)
        keep = []
        for name in tg.Dialects:
            try:
                d = tg.Dialect.get_or_raise(name)
                hier = {k.__module__ for k in type(d).__mro__} | {k.__module__ for k in d.generator_class.__mro__}
            except Exception:  # noqa: BLE001
                keep.append(name)  # cannot tell: keep it
                continue
            if hier & mods:
                keep.append(name)
        tg.Dialects = keep


def _install_inline_executor():
    """Replace ProcessPoolExecutor with an in-process one that still pickles.

    sqlglot's optimizer/executor tests spawn worker processes; under a 2-core
    CPU-billed run that is pure overhead, and os.cpu_count() may report the
    host's cores rather than ours.
    """
    import concurrent.futures as cf
    import concurrent.futures.process as cfp
    import pickle

    class LazyFuture(cf.Future):
        """Runs its work when result() is first asked for."""

        def __init__(self, work):
            super().__init__()
            self._work = work

        def result(self, timeout=None):
            if self._work is not None:
                work, self._work = self._work, None
                try:
                    self.set_result(work())
                except BaseException as e:  # noqa: BLE001
                    self.set_exception(e)
            return super().result(timeout)

    class InlineExecutor:
        def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_):
            # The executor tests (the ones with an initializer) read futures
            # one by one with result(), so their work can run lazily and a
            # failing query can stop the run early. The optimizer tests use
            # wait()/as_completed(), which need finished futures.
            self.lazy = initializer is not None
            if initializer is not None:
                initializer(*pickle.loads(pickle.dumps(initargs)))

        def submit(self, fn, *args, **kwargs):
            if self.lazy:
                payload = pickle.dumps((fn, args, kwargs))

                def work():
                    fn2, a, kw = pickle.loads(payload)
                    return pickle.loads(pickle.dumps(fn2(*a, **kw)))

                return LazyFuture(work)
            fut = cf.Future()
            try:
                fn2, a, kw = pickle.loads(pickle.dumps((fn, args, kwargs)))
                fut.set_result(pickle.loads(pickle.dumps(fn2(*a, **kw))))
            except BaseException as e:  # noqa: BLE001
                fut.set_exception(e)
            return fut

        def map(self, fn, *its, **_):
            return [self.submit(fn, *a).result() for a in zip(*its)]

        def shutdown(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.shutdown()

    cf.ProcessPoolExecutor = InlineExecutor
    cfp.ProcessPoolExecutor = InlineExecutor


MAX_NODES_ID = "tests/test_parser.py::TestParser::test_max_nodes"


def proxy_max_nodes():
    """test_max_nodes scaled down 100x: same code paths, well under a second.

    The real test builds a 10000-column, 100-deep query (~2M nodes) and checks
    the 1,000,000 and 50,000 node limits; parsing it takes ~29 s. Here the
    query is 100 columns wide (~20k nodes) and the limits scale with it, so
    every assertion keeps its meaning (over the limit by the same factor).
    The test module is imported too, as pytest would, so a change that breaks
    its imports fails here as it would there.
    """
    os.chdir(WORK)
    sys.path.insert(0, str(WORK))
    for m in list(sys.modules):
        if m == "sqlglot" or m.startswith(("sqlglot.", "tests")):
            del sys.modules[m]
    sys.stdout = sys.stderr = open(os.devnull, "w")
    ok = True
    try:
        import tests.test_parser  # noqa: F401
        from sqlglot import exp, parse_one
        from sqlglot.errors import ParseError

        assert isinstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select)
        cols = [f"col{i}" for i in range(500)]
        assert isinstance(parse_one(f"SELECT {','.join(cols)} FROM t", max_nodes=1_000_000), exp.Select)

        width, depth = 100, 100
        sql = "SELECT " + ",".join("x" + "=x" * depth for _ in range(width))
        for limit, needle in ((10_000, "Maximum number of AST nodes"), (500, "(500)")):
            try:
                parse_one(sql, max_nodes=limit)
                ok = False
            except ParseError as e:
                ok = ok and needle in str(e)
        try:
            parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
            ok = False
        except ParseError:
            pass
        assert isinstance(parse_one("SELECT 1, 2, 3"), exp.Select)
    except BaseException:  # noqa: BLE001
        ok = False
    return {MAX_NODES_ID: "passed" if ok else "failed"}
