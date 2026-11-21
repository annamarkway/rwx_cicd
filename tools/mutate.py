"""Generate sample-like mutant patches against the pinned sqlglot checkout.

Usage: python3 tools/mutate.py <sqlglot_dir> <out_dir> <count> [seed]

Kinds, roughly in the sample's proportions:
  code     one-line operator/literal flip in sqlglot/**/*.py
  comment  a comment line added in sqlglot/**/*.py
  fixture  a one-line edit to a .sql fixture under tests/fixtures/optimizer
  multi    two or three of the above across files
"""

import ast
import difflib
import json
import os
import random
import re
import sys

SRC, OUT, COUNT = sys.argv[1], sys.argv[2], int(sys.argv[3])
SEED = int(sys.argv[4]) if len(sys.argv) > 4 else 1
rng = random.Random(SEED)

PY = []
for d, _, fs in os.walk(os.path.join(SRC, "sqlglot")):
    for f in fs:
        if f.endswith(".py"):
            PY.append(os.path.relpath(os.path.join(d, f), SRC))
FIX = []
for d, _, fs in os.walk(os.path.join(SRC, "tests", "fixtures")):
    for f in fs:
        if f.endswith(".sql"):
            FIX.append(os.path.relpath(os.path.join(d, f), SRC))
PY.sort()
FIX.sort()

OPS = [
    (r" is not ", " is "), (r" is (?!not )", " == "), (r" == ", " != "), (r" != ", " == "),
    (r" and ", " or "), (r" or ", " and "), (r"\bTrue\b", "False"), (r"\bFalse\b", "True"),
    (r" < ", " <= "), (r" <= ", " < "), (r" > ", " >= "), (r" >= ", " > "),
    (r"\bnot ", ""), (r"(?<![\w.])(\d+)(?![\w.])", None), (r" \+ ", " - "), (r" - ", " + "),
    (r"\bif (?!not )", "if not "), (r"\bNone\b", "0"), (r'"([A-Z_]{2,})"', "STR"),
    (r"\breturn (\w+)$", "return None"), (r"\[0\]", "[-1]"), (r"\[-1\]", "[0]"),
    (r"\.upper\(\)", ".lower()"), (r"\.lower\(\)", ".upper()"),
]


def read(rel):
    with open(os.path.join(SRC, rel)) as f:
        return f.read()


def code_mutation(rel, text):
    lines = text.split("\n")
    for _ in range(200):
        i = rng.randrange(len(lines))
        ln = lines[i]
        s = ln.strip()
        if not s or s.startswith("#") or s.startswith(('"""', "'''")):
            continue
        pat, rep = rng.choice(OPS)
        ms = list(re.finditer(pat, ln))
        if not ms:
            continue
        m = rng.choice(ms)
        if rep is None:
            n = int(m.group(1))
            new = ln[: m.start(1)] + str(n + rng.choice([-1, 1])) + ln[m.end(1):]
        elif rep == "STR":
            g = m.group(1)
            new = ln[: m.start(1)] + g[:-1] + chr(ord(g[-1]) % 26 + 65 if g[-1] != "Z" else 65) + ln[m.end(1):]
        else:
            new = ln[: m.start()] + rep + ln[m.end():]
        if new == ln:
            continue
        cand = lines[:i] + [new] + lines[i + 1:]
        try:
            ast.parse("\n".join(cand))
        except SyntaxError:
            continue
        return "\n".join(cand)
    return None


COMMENTS = ["# Cheap check first", "# Keep in sync with the generator", "# TODO: revisit",
            "# Kept for backwards compatibility", "# noqa", "# see above"]


def comment_mutation(rel, text):
    lines = text.split("\n")
    for _ in range(100):
        i = rng.randrange(len(lines))
        s = lines[i].strip()
        if not s or s.startswith(("@", ")", "]", "}")):
            continue
        ind = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
        cand = lines[:i] + [ind + rng.choice(COMMENTS)] + lines[i:]
        try:
            ast.parse("\n".join(cand))
        except SyntaxError:
            continue
        return "\n".join(cand)
    return None


def fixture_mutation(rel, text):
    lines = text.split("\n")
    for _ in range(100):
        i = rng.randrange(len(lines))
        ln = lines[i]
        s = ln.strip()
        if not s or s.startswith(("#", "--")) or len(s) < 8:
            continue
        r = rng.random()
        toks = [m for m in re.finditer(r"[A-Za-z_]\w*", ln)]
        if r < 0.4 and toks:
            m = rng.choice(toks)
            new = ln[: m.start()] + m.group() + "a" + ln[m.end():]
        elif r < 0.7:
            j = rng.randrange(len(ln))
            new = ln[:j] + rng.choice(["x ", " 1", "(", ","]) + ln[j:]
        else:
            new = ln.replace(" = ", " <> ", 1) if " = " in ln else ln + " "
        if new != ln:
            return "\n".join(lines[:i] + [new] + lines[i + 1:])
    return None


def diff(rel, a, b):
    out = [f"diff --git a/{rel} b/{rel}\n", "index 0000000..0000000 100644\n"]
    d = list(difflib.unified_diff(a.splitlines(True), b.splitlines(True), f"a/{rel}", f"b/{rel}", n=3))
    if not d:
        return ""
    return "".join(out + d)


def one(kind, used):
    for _ in range(50):
        if kind == "fixture":
            rel = rng.choice([f for f in FIX if "/optimizer/" in f] if rng.random() < 0.85 else FIX)
            fn = fixture_mutation
        else:
            rel = rng.choice(PY)
            fn = code_mutation if kind == "code" else comment_mutation
        if rel in used:
            continue
        a = read(rel)
        b = fn(rel, a)
        if b is not None and b != a:
            used.add(rel)
            return diff(rel, a, b)
    return ""


os.makedirs(OUT, exist_ok=True)
kinds = {}
for n in range(COUNT):
    r = rng.random()
    kind = "code" if r < 0.6 else "comment" if r < 0.7 else "fixture" if r < 0.9 else "multi"
    used = set()
    if kind == "multi":
        parts = [one(rng.choice(["code", "code", "comment", "fixture"]), used) for _ in range(rng.choice([2, 3]))]
        text = "".join(parts)
    else:
        text = one(kind, used)
    if not text:
        continue
    cid = f"m{SEED:02d}{n:03d}"
    with open(os.path.join(OUT, f"{cid}.patch"), "w") as f:
        f.write(text)
    kinds[cid] = kind
with open(os.path.join(OUT, "kinds.json"), "w") as f:
    json.dump(kinds, f)
print(f"wrote {len(kinds)} mutants", file=sys.stderr)
