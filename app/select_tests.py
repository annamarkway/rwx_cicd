"""Static test selection for one sqlglot patch.

Given a unified diff against the pinned sqlglot commit, return the set of test
numbers that could fail, or ALL when we cannot reason about the change.

Inputs built ahead of time from the pinned commit (see tools/):
  lines    file -> line -> tests that executed that line during the test
  opens    non-.py file -> tests that opened it
  classes  "module:qualname" -> tests that instantiated it (or a subclass)

Rules, by where a changed line sits in the *old* file's AST:
  * inside a function: tests that executed any line of the innermost
    enclosing statement (a whole multi-line `if` header, not just the line,
    so short-circuited operands are covered).
  * in a class body: tests that instantiated the class, plus line coverage.
  * at module level: tests that run code referring to the names it binds.
  * a change whose AST is unchanged (comments, whitespace): nothing.
  * anything we cannot place: ALL.
"""

from __future__ import annotations

import ast
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


class Fallback(Exception):
    """We cannot reason about this change: run everything."""


ALL = "ALL"


# --------------------------------------------------------------------------- diff


@dataclass
class FileDiff:
    old: str | None  # repo-relative path, None for a new file
    new: str | None  # None for a deleted file
    hunks: list = field(default_factory=list)  # (old_start, [(tag, text)])
    binary: bool = False


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _strip(path: str) -> str | None:
    path = path.split("\t")[0].strip()
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def parse_patch(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk = None
    for raw in text.split("\n"):
        if raw.startswith("diff --git "):
            parts = raw.split(" ")
            cur = FileDiff(old=_strip(parts[2]) if len(parts) > 2 else None,
                           new=_strip(parts[3]) if len(parts) > 3 else None)
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue
        if raw.startswith("--- ") and hunk is None:
            cur.old = _strip(raw[4:])
            continue
        if raw.startswith("+++ ") and hunk is None:
            cur.new = _strip(raw[4:])
            continue
        if raw.startswith("new file mode"):
            cur.old = None
            continue
        if raw.startswith("deleted file mode"):
            cur.new = None
            continue
        if raw.startswith("rename from "):
            cur.old = raw[len("rename from "):]
            continue
        if raw.startswith("rename to "):
            cur.new = raw[len("rename to "):]
            continue
        if raw.startswith("Binary files") or raw.startswith("GIT binary patch"):
            cur.binary = True
            continue
        m = _HUNK.match(raw)
        if m:
            hunk = (int(m.group(1)), [])
            left = [int(m.group(2) or 1), int(m.group(4) or 1)]  # old, new lines still expected
            cur.hunks.append(hunk)
            continue
        if hunk is None:
            continue
        if raw.startswith("\\"):
            hunk[1].append(("\\", raw))
            continue
        if left[0] <= 0 and left[1] <= 0:
            continue  # hunk complete; junk between hunks/files
        tag = raw[:1] if raw else " "  # a bare empty line is blank context
        if tag not in (" ", "-", "+"):
            continue
        hunk[1].append((tag, raw[1:]))
        if tag in (" ", "-"):
            left[0] -= 1
        if tag in (" ", "+"):
            left[1] -= 1
    return files


def apply(old_text: str, fd: FileDiff) -> str:
    """Apply one file's hunks to its old text. Raises Fallback on mismatch."""
    old = old_text.split("\n")
    out: list[str] = []
    pos = 0  # index into old
    for start, body in fd.hunks:
        idx = max(start - 1, 0)
        if start == 0:
            idx = 0
        if idx < pos:
            raise Fallback("overlapping hunks")
        out.extend(old[pos:idx])
        pos = idx
        for tag, text in body:
            if tag == " ":
                if pos >= len(old) or old[pos] != text:
                    raise Fallback("context mismatch")
                out.append(text)
                pos += 1
            elif tag == "-":
                if pos >= len(old) or old[pos] != text:
                    raise Fallback("removal mismatch")
                pos += 1
            elif tag == "+":
                out.append(text)
            # "\ No newline at end of file" is ignored: trailing newline does
            # not change Python semantics or fixture parsing in practice.
    out.extend(old[pos:])
    return "\n".join(out)


def changed_points(fd: FileDiff):
    """Yield old-file line numbers touched by the diff.

    Removed lines yield their own number. An insertion yields the old lines
    on either side of it.
    """
    for start, body in fd.hunks:
        o = start
        for tag, _text in body:
            if tag == " ":
                o += 1
            elif tag == "-":
                yield o
                o += 1
            elif tag == "+":
                yield o - 1
                yield o


# --------------------------------------------------------------------------- AST


def _start(node) -> int:
    s = node.lineno
    for d in getattr(node, "decorator_list", ()) or ():
        s = min(s, d.lineno)
    return s


def _children(node):
    for name in ("body", "orelse", "finalbody", "handlers", "cases"):
        for c in getattr(node, name, ()) or ():
            if hasattr(c, "lineno"):
                yield c


def locate(tree: ast.AST, line: int) -> list:
    """Chain of statements from outermost to innermost that contain line."""
    chain = []
    node = tree
    while True:
        nxt = None
        for c in _children(node):
            if _start(c) <= line <= (c.end_lineno or c.lineno):
                nxt = c
                break
        if nxt is None:
            return chain
        chain.append(nxt)
        node = nxt


def _bound_names(stmt) -> set[str]:
    names: set[str] = set()
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(stmt.name)
    elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
        for a in stmt.names:
            names.add((a.asname or a.name).split(".")[0])
    else:
        targets = []
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
        elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
            targets = [stmt.target]
        for t in targets:
            for n in ast.walk(t):
                if isinstance(n, ast.Name):
                    names.add(n.id)
                elif isinstance(n, ast.Attribute):
                    names.add(n.attr)
        # Also names defined anywhere inside compound module-level blocks
        # (if TYPE_CHECKING:, try/except imports, ...).
        if isinstance(stmt, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
            for sub in ast.walk(stmt):
                if sub is not stmt and isinstance(sub, ast.stmt):
                    names |= _bound_names(sub) if not isinstance(sub, (ast.If, ast.Try, ast.With, ast.For, ast.While)) else set()
    return names


def _identity_safe(node) -> bool:
    """Operands where `is` and `==` agree for every sqlglot type.

    None (Expression.__eq__ and _Dialect.__eq__ return False for it) and
    enum-style members like NormalizationStrategy.UPPERCASE / TokenType.SELECT
    (Enum equality is identity).
    """
    if isinstance(node, ast.Constant) and node.value is None:
        return True
    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id[:1].isupper() and not node.value.id.isupper()
            and node.attr.isupper() and len(node.attr) > 1):
        return True
    return False


class _Normalize(ast.NodeTransformer):
    def __init__(self, strip_annotations=False):
        self.strip = strip_annotations

    def _fn(self, node):
        self.generic_visit(node)
        if self.strip:
            # With `from __future__ import annotations` a function's argument
            # and return annotations are never evaluated, and nothing in
            # sqlglot introspects them (only class-level ones, for dataclasses
            # and TypedDicts, which are left alone).
            node.returns = None
            a = node.args
            for arg in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]:
                if arg is not None:
                    arg.annotation = None
        return node

    visit_FunctionDef = visit_AsyncFunctionDef = _fn

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) == 1 and (_identity_safe(node.left) or _identity_safe(node.comparators[0])):
            op = node.ops[0]
            if isinstance(op, ast.Eq):
                node.ops = [ast.Is()]
            elif isinstance(op, ast.NotEq):
                node.ops = [ast.IsNot()]
        return node


def equivalent(old_tree: ast.AST, new_tree: ast.AST) -> bool:
    import copy

    def future_annotations(tree):
        return any(isinstance(n, ast.ImportFrom) and n.module == "__future__"
                   and any(a.name == "annotations" for a in n.names) for n in tree.body)

    strip = future_annotations(old_tree) and future_annotations(new_tree)
    a = _Normalize(strip).visit(copy.deepcopy(old_tree))
    b = _Normalize(strip).visit(copy.deepcopy(new_tree))
    return ast.dump(a) == ast.dump(b)


# --------------------------------------------------------------------------- selector


def module_name(rel: str) -> str:
    m = rel[:-3] if rel.endswith(".py") else rel
    if m.endswith("/__init__"):
        m = m[: -len("/__init__")]
    return m.replace("/", ".")


class Selector:
    def __init__(self, src: Path, mapdata: dict, tests: list[dict]):
        self.src = src
        self.lines = mapdata["lines"]
        self.opens = mapdata["opens"]
        self.classes = mapdata["classes"]
        self.imports = mapdata.get("imports", {})
        self.tests = tests
        self.by_file: dict[str, set[int]] = {}
        for t in tests:
            self.by_file.setdefault(t["file"], set()).add(t["number"])
        self._text: dict[str, str | None] = {}
        self._tree: dict[str, ast.AST] = {}
        self._pyfiles: list[str] | None = None
        self.log: list[str] = []

    # --- source access
    def text(self, rel: str) -> str | None:
        if rel not in self._text:
            p = self.src / rel
            try:
                self._text[rel] = p.read_text()
            except (OSError, UnicodeDecodeError):
                self._text[rel] = None
        return self._text[rel]

    def tree(self, rel: str) -> ast.AST:
        if rel not in self._tree:
            self._tree[rel] = ast.parse(self.text(rel) or "")
        return self._tree[rel]

    def pyfiles(self) -> list[str]:
        if self._pyfiles is None:
            out = []
            for top in ("sqlglot", "tests", "sqlglotc"):
                for dirpath, _dirs, files in os.walk(self.src / top):
                    for f in files:
                        if f.endswith(".py"):
                            out.append(os.path.relpath(os.path.join(dirpath, f), self.src))
            out.append("setup.py")
            self._pyfiles = sorted(out)
        return self._pyfiles

    # --- coverage helpers
    def cov(self, rel: str, a: int, b: int) -> set[int]:
        fl = self.lines.get(rel)
        out: set[int] = set()
        if not fl:
            return out
        for ln in range(a, b + 1):
            v = fl.get(str(ln))
            if v:
                out.update(v)
        return out

    def import_cov(self, rel: str, node) -> set[int]:
        """Tests in files whose import (collection) ran any line of node.

        A broken function that runs at import time breaks every test in the
        importing test file, e.g. a lazy module __getattr__ used by a
        module-level `from sqlglot.optimizer import x`.
        """
        fl = self.imports.get(rel)
        out: set[int] = set()
        if not fl:
            return out
        for ln in range(_start(node), (node.end_lineno or node.lineno) + 1):
            for f in fl.get(str(ln), ()):
                out |= self.by_file.get(f, set())
        return out

    def cov_node(self, rel: str, node) -> set[int]:
        return self.cov(rel, _start(node), node.end_lineno or node.lineno)

    def class_users(self, rel: str, chain: list) -> set[int] | None:
        """Instantiators of the innermost class in chain (None if untracked)."""
        quals = [n.name for n in chain if isinstance(n, ast.ClassDef)]
        if not quals:
            return None
        key = f"{module_name(rel)}:{'.'.join(quals)}"
        users = self.classes.get(key)
        return set(users) if users is not None else None

    # --- the core rule
    def for_line(self, rel: str, line: int, depth: int = 2) -> set[int] | str:
        tree = self.tree(rel)
        chain = locate(tree, line)
        if not chain:
            return set()  # blank/comment line between top-level statements
        inner = chain[-1]
        sel = self.cov_node(rel, inner)
        in_func = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in chain)
        if in_func:
            return sel | self.import_cov(rel, inner)
        classes = [n for n in chain if isinstance(n, ast.ClassDef)]
        if classes:
            users = self.class_users(rel, chain)
            # Coverage of the whole outermost class (its methods run when used).
            sel |= self.cov_node(rel, classes[0])
            if users is not None:
                sel |= users
            else:
                # An untracked class: whoever refers to the class name.
                r = self.name_users(rel, {classes[0].name}, depth - 1, exclude=classes[0])
                if r == ALL:
                    return ALL
                sel |= r
            return sel
        # Module level.
        names = _bound_names(inner)
        if not names:
            if isinstance(inner, ast.Expr) and isinstance(getattr(inner, "value", None), ast.Constant):
                return sel  # a bare string (module docstring)
            raise Fallback(f"module-level statement with no names {rel}:{line}")
        if rel.startswith("tests/") and rel in self.by_file:
            sel |= self.by_file[rel]
        r = self.name_users(rel, names, depth - 1, exclude=inner)
        if r == ALL:
            return ALL
        return sel | r

    def name_users(self, rel: str, names: set[str], depth: int, exclude=None) -> set[int] | str:
        """Tests that execute code referring to any of names."""
        if depth < 0:
            raise Fallback("name usage too deep")
        pat = re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted(names)) + r")\b")
        out: set[int] = set()
        ex = (_start(exclude), exclude.end_lineno) if exclude is not None else (0, -1)
        for f in self.pyfiles():
            t = self.text(f)
            if not t or not pat.search(t):
                continue
            for i, ln in enumerate(t.split("\n"), start=1):
                if f == rel and ex[0] <= i <= ex[1]:
                    continue
                if pat.search(ln):
                    r = self.for_line(f, i, depth)
                    if r == ALL:
                        return ALL
                    out |= r
        return out

    # --- one file / one patch
    def for_file(self, fd: FileDiff) -> set[int] | str:
        if fd.binary:
            raise Fallback("binary")
        path = fd.new or fd.old
        if fd.old is None:
            # New file. Nothing in the pinned tree imports or opens it, unless
            # it is something pytest or Python picks up on its own.
            name = os.path.basename(path)
            if name in ("conftest.py", "sitecustomize.py", "usercustomize.py", "pytest.ini", "tox.ini") or name.endswith(".pth"):
                raise Fallback(f"new special file {path}")
            if path.startswith("tests/fixtures/"):
                return self.dir_openers(path)
            if path.endswith(".py") and path.startswith("sqlglot/"):
                # A new module only matters if something imports it; it can
                # shadow nothing in a package that already exists except a
                # same-named package dir. Be conservative for __init__.py.
                if name == "__init__.py":
                    raise Fallback("new package")
            return set()
        if fd.new is None or fd.new != fd.old:
            raise Fallback(f"delete/rename {fd.old}")
        if not path.endswith(".py"):
            return self.for_data_file(path)
        old = self.text(path)
        if old is None:
            raise Fallback(f"unknown file {path}")
        new = apply(old, fd)
        if (path.startswith("sqlglot/") and path not in self.lines and path not in self.imports
                and os.path.basename(path) != "__init__.py"):
            # No test ever imports or runs this module (e.g. sqlglot/__main__.py,
            # the CLI): nothing a test runs can change.
            self.log.append(f"{path}: never executed by any test")
            return set()
        try:
            new_tree = ast.parse(new)
            compile(new_tree, path, "exec")
        except SyntaxError:
            return self.import_breakers(path)
        if ast.dump(self.tree(path)) == ast.dump(new_tree):
            self.log.append(f"{path}: AST unchanged")
            return set()
        if equivalent(self.tree(path), new_tree):
            self.log.append(f"{path}: equivalent (is/== on None or enum, or annotations only)")
            return set()
        entry = self.table_entry_rule(path, fd)
        if entry is not None:
            return entry
        sel: set[int] = set()
        for ln in sorted(set(changed_points(fd))):
            if ln < 1:
                continue
            r = self.for_line(path, ln)
            if r == ALL:
                return ALL
            sel |= r
        return sel

    # --- lookup-table entries in tokenizer/parser/generator/dialect classes

    TABLE_FILES = ("sqlglot/dialects/", "sqlglot/generators/", "sqlglot/parsers/", "sqlglot/tokenizers/")
    TABLE_CORE = ("sqlglot/tokens.py", "sqlglot/parser.py", "sqlglot/generator.py")

    def table_entry_rule(self, path: str, fd: FileDiff) -> set[int] | None:
        """A change confined to entries of class-level dict literals.

        Tables like KEYWORDS, FUNCTIONS, TRANSFORMS or TIME_MAPPING are looked
        up by SQL text or by expression class, so an entry only matters to the
        class's users whose input mentions the entry's text (case-insensitive),
        or that build the expression class it is keyed by. Returns None when
        the rule does not apply (then the general rules run).
        """
        if not (path.startswith(self.TABLE_FILES) or path in self.TABLE_CORE):
            return None
        tree = self.tree(path)
        points = sorted({ln for ln in changed_points(fd) if ln >= 1})
        if not points:
            return None
        entries, users = [], set()
        for ln in points:
            chain = locate(tree, ln)
            if not chain or any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in chain):
                return None
            if not any(isinstance(n, ast.ClassDef) for n in chain):
                return None
            inner = chain[-1]
            value = getattr(inner, "value", None)
            if not isinstance(inner, (ast.Assign, ast.AnnAssign)) or not isinstance(value, ast.Dict):
                return None
            hit = None
            for k, v in zip(value.keys, value.values):
                if k is None:
                    continue  # **spread: its own line is never "inside" an entry
                a, b = min(k.lineno, v.lineno), max(k.end_lineno, v.end_lineno)
                if a <= ln <= b:
                    hit = (k, v)
                    break
            if hit is None:
                # Between entries (an inserted line): use its neighbours.
                near = [(k, v) for k, v in zip(value.keys, value.values)
                        if k is not None and (k.lineno in (ln, ln + 1) or v.end_lineno in (ln, ln - 1))]
                if not near:
                    return None
                entries.extend(near)
            else:
                entries.append(hit)
            u = self.class_users(path, chain)
            if u is None:
                return None
            users |= u | self.cov_node(path, [n for n in chain if isinstance(n, ast.ClassDef)][0])
        # The text and expression classes an entry is about: from the old
        # entries and from the lines the patch adds.
        needles, exp_classes = set(), set()
        texts = [ast.get_source_segment(self.text(path), n) or "" for kv in entries for n in kv]
        texts += [t for _s, body in fd.hunks for tag, t in body if tag == "+"]
        for t in texts:
            for m in re.finditer(r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'', t):
                s = (m.group(1) if m.group(1) is not None else m.group(2)).lower()
                if s:
                    needles.add(s)
                    needles.update(w for w in re.split(r"\s+", s) if len(w) >= 3)
            exp_classes.update(re.findall(r"\bexp\.([A-Z]\w*)", t))
        if not needles and not exp_classes:
            return None
        built = set()
        for name in exp_classes:
            for key, ts in self.classes.items():
                if key.endswith(":" + name):
                    built.update(ts)
        out = {n for n in users if n in built or self.test_mentions(n, needles)}
        self.log.append(f"{path}: table entry rule, {len(out)} of {len(users)} users")
        return out

    def test_mentions(self, number: int, needles: set[str]) -> bool:
        if not needles:
            return False
        text = self.test_text(number)
        return any(nd in text for nd in needles)

    def test_text(self, number: int) -> str:
        """Lowercased text a test feeds to sqlglot: its own source, the
        non-test code of its file (helpers, shared constants), and the
        fixture files it opens."""
        cache = self.__dict__.setdefault("_ttext", {})
        if number in cache:
            return cache[number]
        t = self.tests[number]
        rel, parts = t["file"], t["id"].split("::")[1:]
        src = self.text(rel) or ""
        shared, own = self._file_parts(rel)
        body = own.get(tuple(parts))
        if body is None:
            body = src.lower()  # cannot locate: the whole file
        fix = self.__dict__.setdefault("_test_fixtures", None)
        if fix is None:
            fix = {}
            for f, ts in self.opens.items():
                for n in ts:
                    fix.setdefault(n, []).append(f)
            self._test_fixtures = fix
        extra = []
        for f in fix.get(number, ()):
            ft = self.text(f)
            if ft:
                extra.append(ft.lower())
        cache[number] = "\n".join([shared, body] + extra)
        return cache[number]

    def _file_parts(self, rel: str):
        """(file text outside test methods, {(Class, test): method text})."""
        cache = self.__dict__.setdefault("_fparts", {})
        if rel in cache:
            return cache[rel]
        src = self.text(rel) or ""
        lines = src.split("\n")
        own, cut = {}, set()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            cache[rel] = (src.lower(), {})
            return cache[rel]
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                for m in node.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name.startswith("test"):
                        a, b = _start(m), m.end_lineno
                        own[(node.name, m.name)] = "\n".join(lines[a - 1:b]).lower()
                        cut.update(range(a, b + 1))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                a, b = _start(node), node.end_lineno
                own[(node.name,)] = "\n".join(lines[a - 1:b]).lower()
                cut.update(range(a, b + 1))
        shared = "\n".join(ln for i, ln in enumerate(lines, start=1) if i not in cut).lower()
        cache[rel] = (shared, own)
        return cache[rel]

    def import_breakers(self, path: str) -> set[int] | str:
        """A file that no longer compiles: every test that imports it fails."""
        if path.startswith("tests/") and path in self.by_file:
            # Only that test module breaks (unless others import from it).
            r = self.name_users(path, {os.path.basename(path)[:-3]}, 0)
            return ALL if r == ALL else (self.by_file[path] | r)
        raise Fallback(f"syntax error in {path}")

    def dir_openers(self, path: str) -> set[int]:
        d = os.path.dirname(path)
        out: set[int] = set()
        for f, ts in self.opens.items():
            if os.path.dirname(f) == d or f == d:
                out.update(ts)
        return out

    def for_data_file(self, path: str) -> set[int] | str:
        if path in self.opens:
            return set(self.opens[path])
        if path.startswith("tests/"):
            return self.dir_openers(path)
        if path.startswith(("sqlglot/", "sqlglotc/")):
            # Package data nobody opened during tests.
            out = self.dir_openers(path)
            return out
        # Top-level docs/config (README, CHANGELOG, setup.cfg, pyproject ...).
        if path in ("pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini") or path.endswith(".pth"):
            raise Fallback(f"config {path}")
        return set()

    def changed_spans(self, patch_text: str):
        """Where a patch changes behaviour, for the heavy-test checks.

        Returns (spans, other_files): spans are (relpath, first, last, in_func)
        for the innermost statement around each changed line of every .py
        file whose AST really changed; other_files are non-.py paths. Returns
        None when the patch cannot be read this way.
        """
        try:
            spans, others = [], []
            for fd in parse_patch(patch_text):
                if fd.binary or fd.old is None or fd.new is None or fd.old != fd.new:
                    return None
                path = fd.new
                if not path.endswith(".py"):
                    others.append(path)
                    continue
                old = self.text(path)
                if old is None:
                    return None
                new_tree = ast.parse(apply(old, fd))
                if equivalent(self.tree(path), new_tree):
                    continue
                for ln in sorted(set(changed_points(fd))):
                    if ln < 1:
                        continue
                    chain = locate(self.tree(path), ln)
                    if not chain:
                        continue
                    inner = chain[-1]
                    in_func = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in chain)
                    spans.append((path, _start(inner), inner.end_lineno or inner.lineno, in_func))
            return spans, others
        except (Fallback, SyntaxError, RecursionError):
            return None

    def select(self, patch_text: str) -> set[int] | str:
        self.log = []
        try:
            files = parse_patch(patch_text)
            if not files:
                raise Fallback("empty patch")
            sel: set[int] = set()
            for fd in files:
                r = self.for_file(fd)
                if r == ALL:
                    return ALL
                sel |= r
            return sel
        except Fallback as e:
            self.log.append(f"fallback: {e}")
            return ALL
        except RecursionError:
            self.log.append("fallback: recursion")
            return ALL
