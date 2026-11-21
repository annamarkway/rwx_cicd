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
        self.prepare()
        undo = self._apply(patch_text)
        try:
            return self._fork(nodeids, timeout)
        finally:
            self._restore(undo)

    def _fork(self, nodeids, timeout):
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:  # child
            os.close(r)
            code = 0
            try:
                res = _child(nodeids)
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
            elif report.when == "call" and report.nodeid not in results:
                results[report.nodeid] = "passed" if report.outcome == "passed" else report.outcome
            elif report.skipped and report.nodeid not in results:
                results[report.nodeid] = "skipped"

        def pytest_collectreport(self, report):
            if report.failed:
                results["__collect_failed__:" + report.nodeid] = "failed"

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


def _install_inline_executor():
    """Replace ProcessPoolExecutor with an in-process one that still pickles.

    sqlglot's optimizer/executor tests spawn worker processes; under a 2-core
    CPU-billed run that is pure overhead, and os.cpu_count() may report the
    host's cores rather than ours.
    """
    import concurrent.futures as cf
    import concurrent.futures.process as cfp
    import pickle

    class InlineExecutor:
        def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_):
            if initializer is not None:
                initializer(*pickle.loads(pickle.dumps(initargs)))

        def submit(self, fn, *args, **kwargs):
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
