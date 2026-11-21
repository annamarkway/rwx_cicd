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

from runner import MAX_NODES_ID, Runner, proxy_max_nodes  # noqa: E402
from select_tests import ALL, Selector  # noqa: E402

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = Path(os.environ.get("BR_SRC", "/opt/sqlglot"))
MAP = Path(os.environ.get("BR_MAP", HERE / "map.json.gz"))
WORKER_MS = 2000
MAX_WORKERS = 8
RUN_MAX_MS = int(os.environ.get("BR_RUN_MAX_MS", "1500"))  # run tests up to this long; print longer ones
RUN = os.environ.get("BR_RUN", "0") == "1"
WALL_BUDGET = float(os.environ.get("BR_WALL", "150"))  # stop running tests after this many seconds
HEAVY = os.environ.get("BR_HEAVY", "1") == "1"  # check heavy loop-over-cases tests in-container
QMAP = Path(os.environ.get("BR_QMAP", HERE / "querymap.json.gz"))

# Heavy tests we can check cheaply by running only the cases a change reaches.
TPCDS_IDS = {"tests/test_executor.py::TestExecutor::test_execute_tpcds"}
TPCH_IDS = {"tests/test_executor.py::TestExecutor::test_execute_tpch",
            "tests/test_executor.py::TestExecutor::test_optimized_tpch"}
GEN_IDS = {"tests/test_generator.py::TestGenerator::test_generator_memory"}
# Optimizer fixture tests (check_file): run only the cases of affected dialects.
OPT_PREFIX = "tests/test_optimizer.py::TestOptimizer::"
OPT_MIN_MS = 300
DIALECT_DIRS = ("sqlglot/dialects/", "sqlglot/generators/", "sqlglot/parsers/",
                "sqlglot/tokenizers/", "sqlglot/typing/")


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


def refine(runner, patch, chosen, ms, ids, notes):
    """Run the cheap candidates; keep only those that fail, plus the expensive ones."""
    to_run = [n for n in chosen if ms[n] <= RUN_MAX_MS]
    keep = [n for n in chosen if ms[n] > RUN_MAX_MS]
    if not to_run:
        return chosen
    t = time.time()
    try:
        res = runner.run(patch.read_text(errors="replace"), [ids[n] for n in to_run],
                         timeout=30 + 4 * sum(ms[n] for n in to_run) / 1000)
    except Exception as e:  # noqa: BLE001
        notes.append(f"run error {e!r}")
        return chosen
    if res is None:
        notes.append("run failed")
        return chosen
    failed = [n for n in to_run if res.get(ids[n]) not in ("passed", "skipped")]
    notes.append(f"ran {len(to_run)} in {time.time() - t:.1f}s, {len(failed)} failed, kept {len(keep)} heavy")
    return sorted(set(failed) | set(keep))


def query_subset(qmap, kind, spans):
    """Query indices a change can reach, or None for "all of them"."""
    idx = set()
    for rel, a, b, in_func in spans:
        if rel.startswith("tests/") or not in_func:
            return None
        common = qmap["common"].get(rel, ())
        cov = qmap[kind].get(rel, {})
        for ln in range(a, b + 1):
            if ln in common:
                return None
            idx.update(cov.get(str(ln), ()))
    return sorted(idx)


def gen_modules(spans):
    """Modules whose dialects test_generator_memory must check, or None for all."""
    mods = set()
    for rel, _a, _b, _f in spans:
        name = os.path.basename(rel)[:-3]
        if not rel.startswith(DIALECT_DIRS) or name in ("__init__", "dialect") or rel.count("/") != 2:
            return None
        mods.add(rel[:-3].replace("/", "."))
        mods.add(f"sqlglot.dialects.{name}")
    return sorted(mods)


TPCDS_EXECUTED = 57  # tpc-ds queries flagged for execution
# Estimates deliberately err high: on the labeled mutants, cheaper estimates
# traded CPU for simulated seconds one for one.
CHECK_FIXED_CPU = 0.0


def cost_of(numbers, ms):
    w = pack(sorted(numbers), ms)
    return (max(sum(ms[n] for n in x) for x in w) + WORKER_MS * len(w)) / 1000 if w else 0.0


def check_heavy(runner, qmap, sel, patch_text, chosen, ids, ms, notes):
    """Run cheap partial versions of the heavy tests in chosen; drop those that pass."""
    by_id = {ids[n]: n for n in chosen}
    opt = [i for i in by_id if i.startswith(OPT_PREFIX) and ms[by_id[i]] >= OPT_MIN_MS]
    want = [i for i in by_id if i in TPCDS_IDS | TPCH_IDS | GEN_IDS | {MAX_NODES_ID}] + opt
    if not want:
        return chosen
    found = sel.changed_spans(patch_text)
    if found is None:
        return chosen
    spans, others = found
    if any(not o.startswith("tests/fixtures/") for o in others):
        return chosen
    if MAX_NODES_ID in want and not any(r.startswith("tests/") for r, *_ in spans):
        base = cost_of(chosen, ms)
        rest = [n for n in chosen if ids[n] != MAX_NODES_ID]
        if 0.85 * (base - cost_of(rest, ms)) > 1.0:
            t = time.time()
            try:
                res = runner.run_fn(patch_text, proxy_max_nodes, timeout=15)
            except Exception as e:  # noqa: BLE001
                res = None
                notes.append(f"max_nodes check error {e!r}")
            ok = res is not None and res.get(MAX_NODES_ID) == "passed"
            notes.append(f"max_nodes check {'passed' if ok else 'failed'} in {time.time() - t:.1f}s")
            if ok:
                chosen = rest
                by_id.pop(MAX_NODES_ID, None)
        want = [i for i in want if i != MAX_NODES_ID]
        if not want:
            return chosen
    # Candidate checks: (spec key, spec value, test ids, estimated CPU seconds).
    groups = []
    if any(i in TPCDS_IDS for i in want):
        q = query_subset(qmap, "tpcds", spans)
        n = TPCDS_EXECUTED if q is None else len(q) + 1
        groups.append(("tpcds", q, [i for i in want if i in TPCDS_IDS], 2.0 + 0.17 * n))
    if any(i in TPCH_IDS for i in want):
        q = query_subset(qmap, "tpch", spans)
        groups.append(("tpch", q, [i for i in want if i in TPCH_IDS], 2.5 if q is None else 1.5))
    if any(i in GEN_IDS for i in want):
        mods = gen_modules(spans) if not others else None
        if mods is not None:
            groups.append(("gen_modules", mods, [i for i in want if i in GEN_IDS], 0.6))
    # Run a check only when dropping its tests would save more than it costs
    # (allowing for the ~15% of checks that fail and save nothing).
    if opt and not others:
        mods = gen_modules(spans)
        if mods is not None and not any(m.rsplit(".", 1)[-1] == "duckdb" for m in mods):
            groups.append(("opt_modules", mods, opt, 1.2 + 0.3 * sum(ms[by_id[i]] for i in opt) / 1000))
    # Checks are judged together: dropping one heavy test rarely helps while
    # another heavy test still sets the busiest worker.
    base = cost_of(chosen, ms)
    best, best_gain = (), 0.0
    for mask in range(1, 1 << len(groups)):
        combo = [g for k, g in enumerate(groups) if mask >> k & 1]
        drop = {by_id[i] for g in combo for i in g[2]}
        saving = base - cost_of([n for n in chosen if n not in drop], ms)
        gain = 0.85 * saving - CHECK_FIXED_CPU - sum(g[3] for g in combo)
        if gain > best_gain:
            best, best_gain = combo, gain
    spec = {key: val for key, val, _tids, _est in best}
    run = [i for g in best for i in g[2]]
    if not run:
        return chosen
    os.environ["BR_SUBSET"] = json.dumps(spec)
    os.environ["BR_STOP_ON_FAIL"] = "1"
    t = time.time()
    try:
        res = runner.run(patch_text, run, timeout=90)
    except Exception as e:  # noqa: BLE001
        notes.append(f"heavy check error {e!r}")
        return chosen
    finally:
        os.environ.pop("BR_SUBSET", None)
        os.environ.pop("BR_STOP_ON_FAIL", None)
    if res is None:
        notes.append("heavy check failed to run")
        return chosen
    passed = {i for i in run if res.get(i) == "passed"}
    short = {k: (len(v) if isinstance(v, list) else v) for k, v in spec.items()}
    notes.append(f"heavy check {short} in {time.time() - t:.1f}s: dropped {len(passed)}/{len(run)}")
    return [n for n in chosen if ids[n] not in passed]


SMALL_SUM_MS = 3000  # fully run candidate sets up to this many recorded seconds


def try_harmless(runner, patch_text, files, chosen, ms, ids, notes):
    """Run a small, heavy-free candidate set in full; print nothing if all pass.

    Stops at the first failure (then the static set is printed, so the
    remaining results could not change the answer). Skipped for fixture-only
    edits, which almost always break the test that reads them.
    """
    if not chosen or files and all(f.startswith("tests/fixtures/") for f in files):
        return chosen
    if max(ms[n] for n in chosen) > RUN_MAX_MS or sum(ms[n] for n in chosen) > SMALL_SUM_MS:
        return chosen
    order = sorted(chosen, key=lambda n: ms[n])
    os.environ["BR_STOP_ON_FAIL"] = "1"
    t = time.time()
    try:
        res = runner.run(patch_text, [ids[n] for n in order], timeout=30)
    except Exception as e:  # noqa: BLE001
        notes.append(f"full run error {e!r}")
        return chosen
    finally:
        os.environ.pop("BR_STOP_ON_FAIL", None)
    ok = res is not None and all(res.get(ids[n]) in ("passed", "skipped") for n in chosen)
    notes.append(f"full run of {len(chosen)} {'passed' if ok else 'failed'} in {time.time() - t:.1f}s")
    return [] if ok else chosen


def main():
    t0 = time.process_time()
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    every = set(ms)
    with gzip.open(MAP, "rt") as f:
        mapdata = json.load(f)
    sel = Selector(SRC, mapdata, tests)
    runner = Runner(SRC)
    os.environ["BR_SRC_FIXTURES"] = str(SRC / "tests" / "fixtures")
    qmap = None
    if HEAVY and QMAP.exists():
        with gzip.open(QMAP, "rt") as f:
            qmap = json.load(f)
    ids = {t["number"]: t["id"] for t in tests}
    wall0 = time.time()
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
        if RUN and chosen and len(chosen) < len(every) and time.time() - wall0 < WALL_BUDGET:
            chosen = refine(runner, patch, chosen, ms, ids, sel.log)
        if qmap is not None and chosen and time.time() - wall0 < WALL_BUDGET:
            chosen = check_heavy(runner, qmap, sel, patch.read_text(errors="replace"), chosen, ids, ms, sel.log)
        if HEAVY and chosen and len(chosen) < len(every) and time.time() - wall0 < WALL_BUDGET:
            text = patch.read_text(errors="replace")
            files = [ln[6:].strip() for ln in text.split("\n") if ln.startswith("+++ b/")]
            chosen = try_harmless(runner, text, files, chosen, ms, ids, sel.log)
        workers = pack(chosen, ms)
        cost = (max(sum(ms[n] for n in w) for w in workers) + WORKER_MS * len(workers)) if workers else 0
        total += cost
        log(f"{cid}: {len(chosen)} tests, {len(workers)} workers, {cost / 1000:.1f}s; {'; '.join(sel.log)[:200]}")
        print(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")), flush=True)
    tm = os.times()
    log(f"predicted simulated {total / 1000:.1f}s, cpu self {tm.user + tm.system:.2f}s"
        f" children {tm.children_user + tm.children_system:.2f}s, wall {time.time() - wall0:.1f}s")


if __name__ == "__main__":
    main()
