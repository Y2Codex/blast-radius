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
    """-> {path: (old_path_or_None, new_path_or_None, [hunks])}; hunk = (old_start, [(tag, line)])"""
    files = {}
    cur = None
    old = new = None
    lines = text.split("\n")
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]
        if ln.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            old = _path(ln[4:])
            new = _path(lines[i + 1][4:])
            cur = []
            files[new or old] = (old, new, cur)
            i += 2
            continue
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
            cur.append((ostart, body))
            continue
        i += 1
    return files


def _path(s):
    s = s.split("\t")[0].strip()
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


def diff_nodes(o, n, chain, out):
    if type(o) is not type(n):
        out.append(Change(o, chain, new=n))
        return
    for field in o._fields:
        vo, vn = getattr(o, field, None), getattr(n, field, None)
        if isinstance(vo, list) and isinstance(vn, list):
            diff_lists(o, field, vo, vn, chain, out)
        elif isinstance(vo, ast.AST) and isinstance(vn, ast.AST):
            if _dump(vo) != _dump(vn):
                diff_nodes(vo, vn, chain + [(o, field)], out)
        elif isinstance(vo, ast.AST) or isinstance(vn, ast.AST):
            out.append(Change(vo if isinstance(vo, ast.AST) else o, chain + ([(o, field)] if isinstance(vo, ast.AST) else [])))
        elif vo != vn or type(vo) is not type(vn):
            out.append(Change(o, chain))


def diff_lists(o, field, vo, vn, chain, out):
    do = [_dump(x) for x in vo]
    dn = [_dump(x) for x in vn]
    if do == dn:
        return
    sub = chain + [(o, field)]
    if len(vo) == len(vn):
        for a, b, x, y in zip(vo, vn, do, dn):
            if x != y:
                if isinstance(a, ast.AST) and isinstance(b, ast.AST):
                    diff_nodes(a, b, sub, out)
                else:
                    out.append(Change(a if isinstance(a, ast.AST) else o, sub if isinstance(a, ast.AST) else chain))
        return
    is_stmts = all(isinstance(x, ast.stmt) for x in vo + vn)
    if not is_stmts:
        out.append(Change(o, chain))
        return
    sm = SequenceMatcher(None, do, dn, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
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


def import_time_tests(mp, path, stmt, chain, changed_node, why):
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
    entry = dict_entry_key(stmt, changed_node, chain)
    if entry is not None:
        u = expr_class_users(mp, entry)
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


def dict_entry_key(stmt, changed_node, chain):
    """Key node of the dict entry containing changed_node, when stmt assigns a dict literal."""
    value = getattr(stmt, "value", None)
    if not isinstance(value, ast.Dict):
        return None
    line = getattr(changed_node, "lineno", None)
    if line is None:
        return None
    end = getattr(changed_node, "end_lineno", line)
    for k, v in zip(value.keys, value.values):
        if k is None:
            continue
        if k.lineno <= line and end <= (v.end_lineno or v.lineno):
            return k
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
    return import_time_tests(mp, path, stmt, outer, node, why)


_SWAPS = {(ast.Is, ast.Eq), (ast.Eq, ast.Is), (ast.IsNot, ast.NotEq), (ast.NotEq, ast.IsNot)}


def identity_swap(ch):
    """`x is None` <-> `x == None` (also True/False and enum members such as Kind.NAME) behave the same."""
    if (type(ch.node), type(ch.new)) not in _SWAPS or not ch.chain:
        return False
    cmp_, field = ch.chain[-1]
    if not isinstance(cmp_, ast.Compare) or field != "ops" or len(cmp_.ops) != 1:
        return False
    for side in (cmp_.comparators[0], cmp_.left):
        if isinstance(side, ast.Constant) and (side.value is None or side.value is True or side.value is False):
            return True
        if (isinstance(side, ast.Attribute) and isinstance(side.value, (ast.Name, ast.Attribute))
                and side.attr.isupper() and (side.value.id if isinstance(side.value, ast.Name) else side.value.attr)[:1].isupper()):
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
    if ast.dump(old_tree) == ast.dump(new_tree):
        why.append("ast-equal")
        return 0
    changes = []
    diff_nodes(old_tree, new_tree, [], changes)
    m = 0
    for ch in changes:
        m |= change_tests(mp, path, ch, why)
    return m


def patch_tests(mp, text, why):
    try:
        files = parse_patch(text)
    except Exception as e:  # malformed patch: be safe
        why.append(f"parse-error:{e!r}")
        return mp.all
    if not files:
        why.append("no-files")
        return 0
    total = 0
    for key, (old, new, hunks) in files.items():
        path = old or new
        w = []
        try:
            old_text = read_repo(old) if old else ""
            if old and old_text is None:
                # unknown file: anything that opened it
                m = mp.open(path) | mp.file_any(path)
                w.append("missing-old")
            else:
                new_text = apply_hunks(old_text, hunks) if new else None
                if path.endswith(".py"):
                    m = py_file_tests(mp, path, old_text, new_text, w)
                    if new and old and new != old:
                        m |= mp.file_any(old)
                        w.append("rename")
                    m |= mp.open(path)
                else:
                    m = mp.open(path)
                    if new_text == old_text:
                        m = 0
                    w.append("data-file")
        except Exception as e:
            w.append(f"error:{e!r}")
            m = mp.file_any(path) | mp.open(path)
            if not m and path.startswith(("sqlglot/", "tests/")):
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
