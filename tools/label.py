"""Label mutant patches with the full suite (inside the lab image).

Usage: python /tools/label.py <patch_dir> <tests.json> <labels.json>

For each patch: copy /src to /tmp/w, apply it, run the whole suite with
xdist, and record every test that did not pass (failure, error, timeout,
subtest failure, or never reported because its file would not import).
Labels are appended incrementally so the run can be interrupted.
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, "/app")
from select_tests import apply, parse_patch  # noqa: E402

patch_dir, tests_json, dest = sys.argv[1:4]
ids = {t["id"]: t["number"] for t in json.load(open(tests_json))["tests"]}
labels = json.load(open(dest)) if os.path.exists(dest) else {}
NPROC = os.environ.get("LABEL_N", "8")

PLUGIN = r'''
import json, os
_res = {}
def pytest_runtest_logreport(report):
    if report.failed:
        _res[report.nodeid] = "failed"
    elif report.when == "call" and report.nodeid not in _res:
        _res[report.nodeid] = report.outcome
    elif report.skipped and report.nodeid not in _res:
        _res[report.nodeid] = "skipped"
def pytest_sessionfinish(session):
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return
    json.dump(_res, open(os.environ["LABEL_OUT"], "w"))
'''

os.makedirs("/tmp/plug", exist_ok=True)
open("/tmp/plug/labelplug.py", "w").write(PLUGIN)

for p in sorted(glob.glob(os.path.join(patch_dir, "*.patch"))):
    cid = os.path.basename(p)[:-6]
    if cid in labels:
        continue
    t = time.time()
    if os.path.exists("/tmp/w"):
        shutil.rmtree("/tmp/w")
    shutil.copytree("/src", "/tmp/w", symlinks=True, ignore=shutil.ignore_patterns(".git"))
    try:
        for fd in parse_patch(open(p).read()):
            rel = fd.new or fd.old
            old = open(f"/src/{fd.old}").read() if fd.old else ""
            if fd.new is None:
                os.unlink(f"/tmp/w/{rel}")
            else:
                open(f"/tmp/w/{rel}", "w").write(apply(old, fd))
    except Exception as e:  # noqa: BLE001
        print(cid, "apply failed", e, file=sys.stderr)
        continue
    env = dict(os.environ, PYTHONPATH="/tmp/w:/tmp/plug", LABEL_OUT="/tmp/res.json",
               SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
    if os.path.exists("/tmp/res.json"):
        os.unlink("/tmp/res.json")
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "labelplug",
                    "-n", NPROC, "--timeout", "300", "tests"], cwd="/tmp/w", env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        res = json.load(open("/tmp/res.json"))
    except Exception:  # noqa: BLE001
        res = {}
    failing = sorted(n for i, n in ids.items() if res.get(i) not in ("passed", "skipped"))
    labels[cid] = {"failing": failing}
    json.dump(labels, open(dest, "w"))
    print(f"{cid}: {len(failing)} failing, {time.time() - t:.0f}s", file=sys.stderr, flush=True)
