"""pytest plugin used only while building maps (never at scoring time).

Records, for every test, which files under /src it opened (fixtures, data)
and which sqlglot modules were newly imported while it ran. Written as one
JSON file per xdist worker into $MAP_OUT.
"""

import json
import os
import sys

_current = None
_opens = {}
_SRC = "/src/"


def _hook(event, args):
    if event == "open":
        path = args[0]
        if isinstance(path, bytes):
            path = path.decode(errors="replace")
        if isinstance(path, str):
            path = os.path.abspath(path)
            if path.startswith(_SRC) and not path.endswith((".pyc", ".py")):
                # Opens outside any test (collection, module-level fixture
                # loads) are kept under "__collect__".
                _opens.setdefault(_current or "__collect__", set()).add(path[len(_SRC):])


sys.addaudithook(_hook)


# The optimizer and executor tests ship work to spawned worker processes,
# which per-test coverage cannot attribute. Run that work in-process instead,
# still round-tripping through pickle the way spawn does.
import concurrent.futures as _cf
import pickle as _pickle


class _InlineExecutor:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_):
        if initializer is not None:
            initializer(*_pickle.loads(_pickle.dumps(initargs)))

    def submit(self, fn, *args, **kwargs):
        fut = _cf.Future()
        try:
            fn2, a, kw = _pickle.loads(_pickle.dumps((fn, args, kwargs)))
            fut.set_result(_pickle.loads(_pickle.dumps(fn2(*a, **kw))))
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


_cf.ProcessPoolExecutor = _InlineExecutor
import concurrent.futures.process as _cfp  # noqa: E402

_cfp.ProcessPoolExecutor = _InlineExecutor


# Which sqlglot classes each test instantiates. A change to a class body
# (class attributes like TOKENS or arg_types) runs at import time, so line
# coverage cannot attribute it; instantiation can.
_types_seen = set()
_classes = {}


def _wrap_init(cls):
    orig = cls.__dict__.get("__init__")
    if orig is None or getattr(orig, "_mapwrapped", False):
        return

    def __init__(self, *a, **k):
        _types_seen.add(type(self))
        orig(self, *a, **k)
        d = getattr(self, "dialect", None)
        if d is not None:
            _types_seen.add(type(d))

    __init__._mapwrapped = True
    __init__.__wrapped__ = orig
    cls.__init__ = __init__


def _all_subclasses(c):
    out, todo = set(), [c]
    while todo:
        x = todo.pop()
        for s in x.__subclasses__():
            if s not in out:
                out.add(s)
                todo.append(s)
    return out


def pytest_configure(config):
    import importlib

    import sqlglot  # noqa: F401
    from sqlglot import expressions, generator, parser, tokens
    from sqlglot.dialects import dialect

    roots = [dialect.Dialect, tokens.Tokenizer, parser.Parser, generator.Generator]
    roots += [getattr(expressions, n) for n in ("Expr", "Expression") if hasattr(expressions, n)]
    for r in roots:
        _wrap_init(r)
        for s in _all_subclasses(r):
            _wrap_init(s)
    # Also patch subclasses created later (lazily imported dialects).
    for r in roots:
        prev = r.__dict__.get("__init_subclass__")

        def hook(cls, _prev=prev, _root=r, **kw):
            if _prev is not None:
                _prev.__func__(cls, **kw)
            else:
                super(_root, cls).__init_subclass__(**kw)
            _wrap_init(cls)

        r.__init_subclass__ = classmethod(hook)
    _ = importlib


def _flush_types(nodeid):
    names = set()
    for t in _types_seen:
        for k in t.__mro__:
            if k.__module__.startswith("sqlglot"):
                names.add(f"{k.__module__}:{k.__qualname__}")
    _types_seen.clear()
    if names:
        _classes[nodeid] = sorted(names)


COV = None  # set by tools/isomap.py: switch coverage context per phase


def pytest_runtest_setup(item):
    global _current
    _current = item.nodeid
    _types_seen.clear()
    if COV is not None:
        COV.switch_context("B")


def pytest_runtest_teardown(item):
    global _current
    _flush_types(item.nodeid)
    _current = None


def pytest_sessionfinish(session):
    out = os.environ.get("MAP_OUT")
    if not out:
        return
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with open(os.path.join(out, f"opens-{worker}.json"), "w") as f:
        json.dump({k: sorted(v) for k, v in _opens.items()}, f)
    with open(os.path.join(out, f"classes-{worker}.json"), "w") as f:
        json.dump(_classes, f)
