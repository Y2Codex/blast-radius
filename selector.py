"""Blast Radius selector.

For every patch, work out which tests can observe the change, using maps recorded
from the pinned sqlglot commit (every test traced alone in a fresh process):

  lines    file -> line -> tests that executed the line (import time included)
  starts   file -> first line of a function/lambda -> tests that entered it
  classes  class -> tests that instantiated it or a subclass
  opened   data file -> tests that opened it
  refs     name -> tests that ran code reading that name

A change whose patched file has the same AST as the original selects nothing.
"""

import ast
import json
import os
import pickle
import sys
import zlib
from difflib import SequenceMatcher

DATA = os.environ.get("BR_DATA", "/data")
REPO = os.environ.get("BR_REPO", "/opt/sqlglot")
MAP = os.environ.get("BR_MAP", os.path.join(os.path.dirname(os.path.abspath(__file__)), "map.bin"))
WORKER_MS = 2000
MAX_WORKERS = 8
DEBUG = bool(os.environ.get("BR_DEBUG"))

FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


def log(*a):
    print(*a, file=sys.stderr)


# --------------------------------------------------------------------------- map


class Map:
    def __init__(self, path):
        d = pickle.loads(zlib.decompress(open(path, "rb").read()))
        self.tests = d["tests"]
        self._masks = d["masks"]
        self._ints = {}
        self.lines = d["lines"]
        self.starts = d["starts"]
        self.classes = d["classes"]
        self.opened = d["opened"]
        self.refs = d["refs"]
        self._any = {}
        self.all = (1 << len(self.tests)) - 1

    def m(self, i):
        v = self._ints.get(i)
        if v is None:
            v = self._ints[i] = int.from_bytes(self._masks[i], "little")
        return v

    def line(self, f, a, b=None):
        d = self.lines.get(f)
        if not d:
            return 0
        r = 0
        for l in range(a, (b or a) + 1):
            i = d.get(l)
            if i is not None:
                r |= self.m(i)
        return r

    def start(self, f, a, b=None):
        d = self.starts.get(f)
        if not d:
            return 0
        r = 0
        for l in range(a, (b or a) + 1):
            i = d.get(l)
            if i is not None:
                r |= self.m(i)
        return r

    def file_any(self, f):
        v = self._any.get(f)
        if v is None:
            v = 0
            for i in set(self.lines.get(f, {}).values()):
                v |= self.m(i)
            self._any[f] = v
        return v

    def cls(self, key):
        i = self.classes.get(key)
        return None if i is None else self.m(i)

    def ref(self, name):
        i = self.refs.get(name)
        return None if i is None else self.m(i)

    def open(self, path):
        i = self.opened.get(path)
        return 0 if i is None else self.m(i)


# ------------------------------------------------------------------------- patch


def parse_patch(text):
    """-> [(old_path_or_None, new_path_or_None, [hunks], flags)]; hunk = (old_start, [(tag, line, has_newline)]).

    Reads `diff --git` headers as well as ---/+++ pairs, so entries without hunks (pure renames,
    binary files, deleted or added empty files, mode changes) are not lost."""
    entries = []
    cur = None
    lines = text.split("\n")
    i, n = 0, len(lines)

    def entry(o, nw):
        e = {"old": o, "new": nw, "hunks": [], "flags": set(), "hdr": False}
        entries.append(e)
        return e

    while i < n:
        ln = lines[i]
        if ln.startswith("diff --git "):
            o, nw = _git_paths(ln[11:])
            cur = entry(o, nw)
            i += 1
            continue
        if ln.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            if cur is None or cur["hdr"]:
                cur = entry(None, None)
            cur["old"] = _path(ln[4:])
            cur["new"] = _path(lines[i + 1][4:])
            cur["hdr"] = True
            i += 2
            continue
        if cur is not None and not cur["hunks"]:
            if ln.startswith("deleted file mode"):
                cur["new"] = None
                cur["flags"].add("deleted")
            elif ln.startswith("new file mode"):
                cur["old"] = None
                cur["flags"].add("added")
            elif ln.startswith("rename from "):
                cur["old"] = _path(ln[12:])
            elif ln.startswith("rename to "):
                cur["new"] = _path(ln[10:])
            elif ln.startswith(("copy from ", "copy to ")):
                cur["flags"].add("copy")
            elif ln.startswith(("GIT binary patch", "Binary files ")):
                cur["flags"].add("binary")
            elif ln.startswith(("old mode ", "new mode ")):
                cur["flags"].add("mode")
        if ln.startswith("@@") and cur is not None:
            parts = ln.split()
            o = parts[1][1:].split(",")
            ostart = int(o[0])
            ocount = int(o[1]) if len(o) > 1 else 1
            nn = parts[2][1:].split(",")
            ncount = int(nn[1]) if len(nn) > 1 else 1
            body = []
            i += 1
            while i < n and (ocount > 0 or ncount > 0):
                h = lines[i]
                tag = h[:1]
                if tag == "\\":
                    if body:
                        body[-1] = (body[-1][0], body[-1][1], False)
                    i += 1
                    continue
                if tag == "-":
                    ocount -= 1
                elif tag == "+":
                    ncount -= 1
                else:
                    tag = " "
                    ocount -= 1
                    ncount -= 1
                body.append((tag, h[1:], True))
                i += 1
            if i < n and lines[i].startswith("\\"):
                if body:
                    body[-1] = (body[-1][0], body[-1][1], False)
                i += 1
            cur["hunks"].append((ostart, body))
            continue
        i += 1
    return [(e["old"], e["new"], e["hunks"], e["flags"]) for e in entries if e["old"] or e["new"]]


def _git_paths(rest):
    """Both paths of a `diff --git a/X b/Y` line."""
    rest = rest.strip()
    mid = len(rest) // 2
    if len(rest) > 4 and rest[mid] == " " and rest[:mid][2:] == rest[mid + 1:][2:]:
        return _path(rest[:mid]), _path(rest[mid + 1:])
    for sep in (' "b/', " b/"):
        k = rest.find(sep)
        if k > 0:
            return _path(rest[:k]), _path(rest[k + 1:])
    return None, None


def _path(s):
    s = s.split("\t")[0].strip()
    if len(s) > 1 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    if s == "/dev/null":
        return None
    if s.startswith(("a/", "b/")):
        s = s[2:]
    return s


def apply_hunks(old_text, hunks):
    old = old_text.split("\n")
    out = []
    pos = 0
    for ostart, body in hunks:
        start = max(ostart - 1, 0) if any(t != "+" for t, _, _ in body) else ostart
        if not old_text and ostart == 0:
            start = 0
        out.extend(old[pos:start])
        pos = start
        for tag, txt, _ in body:
            if tag == " ":
                out.append(old[pos] if pos < len(old) else txt)
                pos += 1
            elif tag == "-":
                pos += 1
            else:
                out.append(txt)
    out.extend(old[pos:])
    return "\n".join(out)


# ---------------------------------------------------------------------- AST diff


class Change:
    __slots__ = ("node", "chain", "kind", "new")

    def __init__(self, node, chain, kind="mod", new=None):
        self.new = new
        self.node = node      # old node that differs (or container for inserts)
        self.chain = chain    # [(ancestor, field)] outermost first
        self.kind = kind


def _dump(n):
    return ast.dump(n) if isinstance(n, ast.AST) else repr(n)


_PLAIN = (str, bytes, int, bool, type(None))


def node_eq(a, b):
    """Same tree, positions aside: what comparing ast.dump() of both would say, without building the strings."""
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.AST):
        for f in a._fields:
            if not node_eq(getattr(a, f, None), getattr(b, f, None)):
                return False
        return True
    if isinstance(a, list):
        if len(a) != len(b):
            return False
        for x, y in zip(a, b):
            if not node_eq(x, y):
                return False
        return True
    if isinstance(a, _PLAIN):
        return a == b
    return repr(a) == repr(b)


def diff_nodes(o, n, chain, out):
    if type(o) is not type(n):
        out.append(Change(o, chain, new=n))
        return
    for field in o._fields:
        vo, vn = getattr(o, field, None), getattr(n, field, None)
        if isinstance(vo, list) and isinstance(vn, list):
            diff_lists(o, field, vo, vn, chain, out)
        elif isinstance(vo, ast.AST) and isinstance(vn, ast.AST):
            if not node_eq(vo, vn):
                diff_nodes(vo, vn, chain + [(o, field)], out)
        elif isinstance(vo, ast.AST) or isinstance(vn, ast.AST):
            out.append(Change(vo if isinstance(vo, ast.AST) else o, chain + ([(o, field)] if isinstance(vo, ast.AST) else [])))
        elif not node_eq(vo, vn):
            out.append(Change(o, chain, new=n))


def diff_lists(o, field, vo, vn, chain, out):
    sub = chain + [(o, field)]
    if len(vo) == len(vn):
        for a, b in zip(vo, vn):
            if not node_eq(a, b):
                if isinstance(a, ast.AST) and isinstance(b, ast.AST):
                    diff_nodes(a, b, sub, out)
                else:
                    out.append(Change(a if isinstance(a, ast.AST) else o, sub if isinstance(a, ast.AST) else chain))
        return
    is_stmts = all(isinstance(x, ast.stmt) for x in vo + vn)
    if not is_stmts:
        out.append(Change(o, chain))
        return
    # only the statements between the common head and tail need aligning
    p = 0
    top = min(len(vo), len(vn))
    while p < top and node_eq(vo[p], vn[p]):
        p += 1
    q = 0
    while q < top - p and node_eq(vo[-1 - q], vn[-1 - q]):
        q += 1
    do = [_dump(x) for x in vo[p:len(vo) - q]]
    dn = [_dump(x) for x in vn[p:len(vn) - q]]
    sm = SequenceMatcher(None, do, dn, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        i1, i2, j1, j2 = i1 + p, i2 + p, j1 + p, j2 + p
        if tag == "replace" and i2 - i1 == j2 - j1:
            for a, b in zip(vo[i1:i2], vn[j1:j2]):
                diff_nodes(a, b, sub, out)
            continue
        for a in vo[i1:i2]:
            out.append(Change(a, sub))
        if j2 > j1:
            # inserted statements: anchor on neighbours in the old list
            anchors = [x for x in (vo[i1 - 1] if i1 > 0 else None, vo[i2] if i2 < len(vo) else None) if x is not None]
            c = Change(o, chain, "insert")
            c.node = (o, field, anchors, vn[j1:j2])
            out.append(c)


# ------------------------------------------------------------------ change -> tests


def own_lines(stmt):
    """Lines of a statement excluding nested statement bodies."""
    a = stmt.lineno
    b = getattr(stmt, "end_lineno", a) or a
    first = None
    for field in ("body", "handlers", "cases"):
        v = getattr(stmt, field, None)
        if isinstance(v, list) and v and hasattr(v[0], "lineno"):
            first = v[0].lineno if first is None else min(first, v[0].lineno)
    if first is not None:
        b = max(a, first - 1)
    decs = getattr(stmt, "decorator_list", None)
    if decs:
        a = min([a] + [d.lineno for d in decs])
    return a, b


def func_start_mask(mp, path, fn):
    m = mp.start(path, fn.lineno)
    for d in getattr(fn, "decorator_list", ()):
        m |= mp.start(path, d.lineno)
    return m


def module_name(path):
    p = path[:-3]
    if p.endswith("/__init__"):
        p = p[: -len("/__init__")]
    return p.replace("/", ".")


def assigned_names(stmt):
    names = []
    if isinstance(stmt, ast.Assign):
        tg = stmt.targets
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        tg = [stmt.target]
    elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [stmt.name]
    elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return [(a.asname or a.name).split(".")[0] for a in stmt.names]
    else:
        return names
    for t in tg:
        for x in ast.walk(t):
            if isinstance(x, ast.Name):
                names.append(x.id)
            elif isinstance(x, ast.Attribute):
                names.append(x.attr)
    return names


def expr_class_users(mp, node):
    """If node is `exp.Foo`/`Foo` naming a traced class, tests that instantiated it."""
    name = None
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in ("exp", "expressions"):
        name = node.attr
    if not name or not name[:1].isupper():
        return None
    r = None
    for key in mp.classes:
        mod, _, qn = key.partition(":")
        if qn == name and mod.startswith("sqlglot.expressions"):
            r = (r or 0) | mp.cls(key)
    return r


def import_time_tests(mp, path, stmt, chain, changed_node, why, new_node=None):
    """Tests affected by a change to code that runs at import (module / class level)."""
    a, b = own_lines(stmt)
    loaders = mp.line(path, a, b)
    if not loaders:
        # e.g. inside `if TYPE_CHECKING:` or never-imported module
        return 0
    tests = loaders
    classes = [n for n, f in chain if isinstance(n, ast.ClassDef)]
    if isinstance(stmt, ast.ClassDef):
        classes = classes + [stmt]
    in_func = any(isinstance(n, FUNCS) and f == "body" for n, f in chain)
    if classes and not in_func:
        key = module_name(path) + ":" + ".".join(c.name for c in classes)
        users = mp.cls(key)
        if users is not None:
            c = classes[-1]
            users |= mp.start(path, c.lineno + 1, c.end_lineno)
            tests &= users
            why.append("class-users")
    # dict entry keyed by an expression class
    entry = dict_entry_key(stmt, changed_node)
    if entry is not None:
        key, in_key = entry
        u = expr_class_users(mp, key)
        if u is not None and in_key:
            # the key itself changed: the entry now answers for another class as well
            nu = expr_class_users(mp, new_node) if new_node is not None else None
            u = None if nu is None else u | nu
        if u is not None:
            tests &= u
            why.append("dict-key")
    names = assigned_names(stmt)
    if names and not isinstance(stmt, ast.ClassDef):
        rm = 0
        known = True
        for nm in names:
            r = mp.ref(nm)
            if r is None:
                r = 0
            rm |= r
        if known and not any(nm.startswith("__") for nm in names):
            tests &= rm
            why.append("refs")
    return tests


def dict_entry_key(stmt, changed_node):
    """(key node, change is inside the key) for the dict entry holding changed_node, when stmt
    assigns a dict literal. None when the change is not inside exactly one entry."""
    value = getattr(stmt, "value", None)
    if not isinstance(value, ast.Dict) or not isinstance(changed_node, ast.AST):
        return None
    for k, v in zip(value.keys, value.values):
        if k is None:
            continue
        if any(x is changed_node for x in ast.walk(k)):
            return k, True
        if any(x is changed_node for x in ast.walk(v)):
            return k, False
    return None


def is_docstring(stmt, chain):
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)):
        return False
    if not chain:
        return False
    parent, field = chain[-1]
    return field == "body" and getattr(parent, "body", [None])[0] is stmt


def change_tests(mp, path, ch, why):
    if ch.kind == "insert":
        container, field, anchors, new_stmts = ch.node
        chain = ch.chain + [(container, field)]
        fn = innermost_func(chain)
        if fn is not None:
            why.append("insert-in-func")
            return func_start_mask(mp, path, fn)
        m = 0
        for a in anchors:
            x, y = own_lines(a)
            m |= mp.line(path, x, y)
        if not anchors:
            m = mp.file_any(path)
        classes = [n for n, f in chain if isinstance(n, ast.ClassDef)]
        if classes:
            key = module_name(path) + ":" + ".".join(c.name for c in classes)
            users = mp.cls(key)
            if users is not None:
                c = classes[-1]
                m &= users | mp.start(path, c.lineno + 1, c.end_lineno)
        names = [nm for s in new_stmts for nm in assigned_names(s)]
        if names and all(not isinstance(s, (ast.Expr, ast.If, ast.For, ast.While, ast.Try, ast.With, ast.Delete)) for s in new_stmts):
            rm = 0
            for nm in names:
                rm |= mp.ref(nm) or 0
            if not any(nm.startswith("__") for nm in names):
                m &= rm
        why.append("insert-toplevel")
        return m

    node, chain = ch.node, ch.chain
    if identity_swap(ch):
        why.append("is-eq-equivalent")
        return 0
    # innermost statement
    stmt = node if isinstance(node, (ast.stmt, ast.ExceptHandler)) else None
    stmt_idx = len(chain)
    if stmt is None:
        for i in range(len(chain) - 1, -1, -1):
            if isinstance(chain[i][0], (ast.stmt, ast.ExceptHandler)):
                stmt, stmt_idx = chain[i][0], i
                break
    if stmt is None:
        why.append("no-stmt")
        return mp.file_any(path)
    if isinstance(stmt, ast.ExceptHandler) or isinstance(stmt, ast.match_case if hasattr(ast, "match_case") else ()):
        pass
    outer = chain[:stmt_idx]

    # is the change in a compound statement's body-less part? (stmt is node itself or header expr)
    # lambda / nested function scope inside the statement
    inner_fn = None
    for i in range(len(chain) - 1, stmt_idx - 1, -1):
        n, f = chain[i]
        if isinstance(n, ast.Lambda) and f == "body":
            inner_fn = n
            break
    if inner_fn is not None:
        why.append("lambda")
        m = mp.start(path, inner_fn.lineno)
        if inner_fn.end_lineno and inner_fn.end_lineno > inner_fn.lineno:
            pass
        return m

    if isinstance(stmt, DEFS) and node is not stmt:
        # change in decorators / signature of a def
        m = func_start_mask(mp, path, stmt)
        via = [f for n, f in chain[stmt_idx:stmt_idx + 1]]
        if via and via[0] == "decorator_list" or not m:
            fn = innermost_func(outer)
            if fn is not None:
                m |= func_start_mask(mp, path, fn)
            else:
                m |= import_time_tests(mp, path, stmt, outer, node, why)
        why.append("def-header")
        return m

    if is_docstring(stmt, outer):
        why.append("docstring")
        return 0

    fn = innermost_func(outer)
    a, b = own_lines(stmt)
    if isinstance(stmt, (ast.ClassDef,) + DEFS) and node is stmt:
        # whole definition replaced/removed
        b = stmt.end_lineno or b
    if fn is not None:
        why.append("func")
        entered = func_start_mask(mp, path, fn)
        lm = mp.line(path, a, b)
        return entered & lm if entered else lm
    return import_time_tests(mp, path, stmt, outer, node, why, ch.new)


_SWAPS = {(ast.Is, ast.Eq), (ast.Eq, ast.Is), (ast.IsNot, ast.NotEq), (ast.NotEq, ast.IsNot)}


# Enum classes of the pinned commit. A member is a singleton, so comparing a value with one by
# `is` or by `==` agrees whenever the value is itself a member, which is how sqlglot uses them.
ENUMS = {"TokenType", "DType", "Type", "ErrorLevel", "IsPredicatePrecedence", "PropertiesLocation", "ScopeType",
         "TrieResult", "Dialects", "NormalizationStrategy", "AliasPrecedence", "UsingColumnOrder"}


def identity_swap(ch):
    """`x is None` <-> `x == None`, and the same against an enum member, behave the same.
    True and False do not (0 == False), nor do other upper-case attributes, which may be
    string constants such as Drill.DATE_FORMAT."""
    if (type(ch.node), type(ch.new)) not in _SWAPS or not ch.chain:
        return False
    cmp_, field = ch.chain[-1]
    if not isinstance(cmp_, ast.Compare) or field != "ops" or len(cmp_.ops) != 1:
        return False
    for side in (cmp_.comparators[0], cmp_.left):
        if isinstance(side, ast.Constant) and side.value is None:
            return True
        if isinstance(side, ast.Attribute) and isinstance(side.value, (ast.Name, ast.Attribute)) and side.attr.isupper():
            owner = side.value.id if isinstance(side.value, ast.Name) else side.value.attr
            if owner in ENUMS:
                return True
    return False


def innermost_func(chain):
    for n, f in reversed(chain):
        if isinstance(n, FUNCS) and f == "body":
            return n
    return None


# ------------------------------------------------------------------- per file


def read_repo(path):
    try:
        with open(os.path.join(REPO, path), encoding="utf-8", newline="") as f:
            return f.read().replace(chr(13) + chr(10), chr(10))
    except (OSError, UnicodeDecodeError):
        return None


def tests_where(mp, pred):
    m = 0
    for i, tid in enumerate(mp.tests):
        if pred(tid):
            m |= 1 << i
    return m


def tests_under(mp, d):
    """Tests whose file lives in directory d ("" is the repository root)."""
    return mp.all if not d else tests_where(mp, lambda t: t.startswith(d + "/"))


# Names a new top-level module or package would hide from every test process: the checkout is
# on PYTHONPATH ahead of the standard library and of the installed packages.
STARTUP = {"sitecustomize", "usercustomize", "pytest", "_pytest", "pluggy", "iniconfig", "packaging",
           "sqlglot", "tests", "duckdb", "pandas", "numpy", "dateutil", "pytz", "setuptools", "mypyc"}
PYTEST_FILES = {"pytest.ini": None, ".pytest.ini": None, "tox.ini": ("[pytest]",),
                "setup.cfg": ("[tool:pytest]",), "pyproject.toml": ("[tool.pytest",)}


def pytest_config(base, text):
    """The part of a config file pytest reads, comments and blank lines dropped."""
    heads = PYTEST_FILES[base]
    keep, on = [], heads is None
    for ln in (text or "").split("\n"):
        t = ln.strip()
        if not t or t[0] in "#;":
            continue
        if t.startswith("["):
            on = heads is None or t.startswith(heads)
        if on:
            keep.append(t)
    return keep


def coding_cookie(text):
    import re
    for ln in (text or "").split("\n")[:2]:
        m = re.match(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)", ln)
        if m:
            return m.group(1)
    return None


def structural_tests(mp, old, new, old_text, new_text, semantic, w):
    """Tests tied to a file by something other than executed lines. These links are read off
    the test sources and the layout of the pinned commit, where the tracer cannot see them:

    - tests/test_typing.py parses every sqlglot/typing/*.py as text and imports each module;
    - tests/test_sqlglotc.py runs sqlglotc/setup.py with runpy;
    - pytest reads conftest.py files and its own sections of the config files at the root;
    - a package stops importing when its __init__.py goes, and a new package or top-level
      module can hide an existing module of the same name.
    """
    m = 0
    gone = old if (new is None or (old and new and old != new)) else None
    born = new if (old is None or (old and new and old != new)) else None
    for p in {x for x in (old, new) if x}:
        d, _, base = p.rpartition("/")
        if semantic and p.endswith(".py") and p.startswith("sqlglot/typing/"):
            m |= tests_where(mp, lambda t: t.startswith("tests/test_typing.py::"))
            w.append("typing-source")
        if semantic and (p.startswith("sqlglotc/") or p == "tests/test_sqlglotc.py"):
            m |= tests_where(mp, lambda t: t.startswith("tests/test_sqlglotc.py::"))
            w.append("sqlglotc")
        if semantic and base == "conftest.py":
            m |= tests_under(mp, d)
            w.append("conftest")
        if not d and base in PYTEST_FILES:
            if pytest_config(base, old_text if p == old else None) != pytest_config(base, new_text if p == new else None):
                m |= mp.all
                w.append("pytest-config")
    if gone and gone.endswith("/__init__.py"):
        d = gone.rpartition("/")[0]
        if d == "tests" or d.startswith("tests/"):
            m |= tests_under(mp, d)
        for f in mp.lines:
            if f.startswith(d + "/"):
                m |= mp.file_any(f)
        w.append("package-gone")
    if born and born.endswith(".py"):
        parts = born.split("/")
        if parts[-1] == "__init__.py" and len(parts) > 1:
            shadowed = "/".join(parts[:-1]) + ".py"
            if shadowed in mp.lines:
                m |= mp.file_any(shadowed)
                w.append("shadows-module")
            parts = parts[:-1]
        if len(parts) == 1:
            stem = parts[0][:-3] if parts[0].endswith(".py") else parts[0]
            if stem in STARTUP or stem in getattr(sys, "stdlib_module_names", ()):
                m |= mp.all
                w.append("shadows-import")
    if new and new.endswith(".py") and new_text is not None and coding_cookie(new_text) != coding_cookie(old_text):
        m |= mp.file_any(old or new) | (mp.all if (old or new) not in mp.lines and new.startswith(("sqlglot/", "tests/")) else 0)
        w.append("coding-cookie")
    return m


def own_tests(mp, path, changes):
    """A change inside `class C: def test_x` of a test file can fail C::test_x whether or not
    the map saw that test run the changed line (it may have errored during setup when traced)."""
    want = set()
    for ch in changes:
        nodes = [n for n, _ in ch.chain]
        nodes.append(ch.node[0] if ch.kind == "insert" else ch.node)
        cls = None
        for n in nodes:
            if isinstance(n, ast.ClassDef) and cls is None:
                cls = n.name
            elif isinstance(n, DEFS) and cls is not None:
                want.add(f"{path}::{cls}::{n.name}")
                break
    return tests_where(mp, lambda t: t in want) if want else 0


def py_file_tests(mp, path, old_text, new_text, why):
    if path not in mp.lines:
        why.append("unexecuted-file")
        return 0
    if new_text is None:
        why.append("deleted")
        return mp.file_any(path)
    try:
        old_tree = ast.parse(old_text)
    except (SyntaxError, ValueError):
        why.append("old-unparsable")
        return mp.file_any(path)
    try:
        new_tree = ast.parse(new_text)
    except (SyntaxError, ValueError):
        why.append("syntax-error")
        return mp.file_any(path)
    if node_eq(old_tree, new_tree):
        why.append("ast-equal")
        return 0
    changes = []
    diff_nodes(old_tree, new_tree, [], changes)
    m = 0
    for ch in changes:
        m |= change_tests(mp, path, ch, why)
    if path.startswith("tests/"):
        m |= own_tests(mp, path, changes)
    return m


def file_tests(mp, old, new, hunks, flags, w):
    if "copy" in flags:
        old_text, old = read_repo(old) if old else "", None  # a copy leaves its source in place
        old_text = old_text or ""
    else:
        old_text = read_repo(old) if old else ""
    path = old or new
    renamed = bool(old and new and old != new)
    if old and old_text is None:
        # not a text file of the pinned commit: anything that touched it
        w.append("missing-old")
        new_text = None
        changed = semantic = True
        m = mp.open(path) | mp.file_any(path)
    elif "binary" in flags:
        w.append("binary")
        new_text = None
        changed = semantic = True
        m = mp.open(path) | mp.file_any(path)
    else:
        new_text = apply_hunks(old_text, hunks) if new else None
        changed = new is None or old is None or renamed or new_text != old_text
        m = 0
        if path.endswith(".py"):
            if old is None:
                w.append("new-file")
            else:
                m = py_file_tests(mp, path, old_text, new_text, w)
            if changed:
                m |= mp.open(path)
            semantic = changed and "ast-equal" not in w
        else:
            if changed:
                m = mp.open(path)
            w.append("data-file")
            semantic = changed
    if renamed:
        m |= mp.file_any(old) | mp.open(old)
        w.append("rename")
    if changed:
        m |= structural_tests(mp, old, new, old_text, new_text, semantic, w)
    return m


def patch_tests(mp, text, why):
    try:
        files = parse_patch(text)
    except Exception as e:  # malformed patch: be safe
        why.append(f"parse-error:{e!r}")
        return mp.all
    if not files:
        why.append("no-files")
        return mp.all if text.strip() else 0
    total = 0
    for old, new, hunks, flags in files:
        path = old or new
        w = []
        try:
            m = file_tests(mp, old, new, hunks, flags, w)
        except Exception as e:
            w.append(f"error:{e!r}")
            m = mp.file_any(path) | mp.open(path)
            if not m and path.startswith(("sqlglot/", "tests/", "sqlglotc/")):
                m = mp.all
        why.append(f"{path}[{','.join(w)}]={bin(m).count('1')}")
        total |= m
    return total


# -------------------------------------------------------------------- packing


def pack(items):
    """items: [(ms, number)] -> workers minimising max load + 2s per worker."""
    if not items:
        return []
    items = sorted(items, reverse=True)
    best = None
    for k in range(1, min(MAX_WORKERS, len(items)) + 1):
        loads = [0] * k
        bins = [[] for _ in range(k)]
        for ms, num in items:
            j = loads.index(min(loads))
            loads[j] += ms
            bins[j].append(num)
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, bins)
    return best[1]


def main():
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    mp = Map(MAP)
    by_id = {t["id"]: t for t in tests}
    # map bit -> (ms, number)
    bit_info = []
    for tid in mp.tests:
        t = by_id.get(tid)
        bit_info.append(None if t is None else (round(t["seconds"] * 1000), t["number"]))
    known = set(mp.tests)
    unknown = [t for t in tests if t["id"] not in known]
    if unknown:
        log(f"{len(unknown)} tests not in map; they are always selected with any non-empty selection of their file")
    unknown_by_file = {}
    for t in unknown:
        unknown_by_file.setdefault(t["file"], []).append((round(t["seconds"] * 1000), t["number"]))

    cdir = os.path.join(DATA, "commits")
    out = []
    for name in sorted(os.listdir(cdir)):
        if not name.endswith(".patch"):
            continue
        cid = name[: -len(".patch")]
        why = []
        try:
            with open(os.path.join(cdir, name), encoding="utf-8", errors="replace", newline="") as f:
                text = f.read()
            mask = patch_tests(mp, text, why)
        except Exception as e:
            why.append(f"fatal:{e!r}")
            mask = mp.all
        items = []
        files = set()
        i = 0
        mm = mask
        while mm:
            if mm & 1:
                info = bit_info[i]
                if info is not None:
                    items.append(info)
                    files.add(mp.tests[i].split("::")[0])
            mm >>= 1
            i += 1
        if unknown and mask:
            for f in files:
                items.extend(unknown_by_file.get(f, ()))
        workers = pack(items)
        out.append(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
        if DEBUG:
            log(cid, len(items), "tests", round(sum(x[0] for x in items) / 1000, 2), "s", len(workers), "w", " ".join(why))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))


if __name__ == "__main__":
    main()
