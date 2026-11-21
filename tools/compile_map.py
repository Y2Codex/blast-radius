"""Compile raw per-test traces into one compact map.

usage: compile_map.py <sqlglot checkout> <tests.json> <raw dir> <out file>
"""
import ast, json, os, pickle, sys, zlib

root, tests_json, rawdir, out = sys.argv[1:5]
tests = json.load(open(tests_json))["tests"]
ids = [t["id"] for t in tests]
NB = (len(ids) + 7) // 8

lines, starts, classes, opened = {}, {}, {}, {}
bad = []
for i, t in enumerate(tests):
    p = os.path.join(rawdir, f"{t['number']}.json")
    d = json.load(open(p))
    if d["outcomes"].get(t["id"] + ":call") != "passed":
        bad.append((t["id"], d["outcomes"]))
    bit = 1 << i
    for f, ls in d["lines"].items():
        m = lines.setdefault(f, {})
        for l in ls:
            m[l] = m.get(l, 0) | bit
    for f, ls in d["starts"].items():
        m = starts.setdefault(f, {})
        for l in ls:
            m[l] = m.get(l, 0) | bit
    for c in d["classes"]:
        classes[c] = classes.get(c, 0) | bit
    for o in d["opened"]:
        opened[o] = opened.get(o, 0) | bit
print("tests not passing in isolation:", len(bad), bad[:20], file=sys.stderr)

# name -> tests that execute code which reads that name (functions entered, or import-time lines)
refs = {}
FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def scan(rel, tree):
    fl, fs = lines.get(rel, {}), starts.get(rel, {})

    def visit(node, fmask, stmt):
        if isinstance(node, FUNCS):
            m = fs.get(node.lineno, 0)
            for dec in getattr(node, "decorator_list", ()):
                m |= fs.get(dec.lineno, 0)
            # header parts (decorators, defaults) run in the enclosing scope
            for field, value in ast.iter_fields(node):
                kids = value if isinstance(value, list) else [value]
                for k in kids:
                    if isinstance(k, ast.AST):
                        inner = field == "body"
                        visit(k, m if inner else fmask, k if isinstance(k, ast.stmt) else stmt)
            return
        if isinstance(node, ast.stmt):
            stmt = node
        name = None
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            name = node.id
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            name = node.attr
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isidentifier():
            name = node.value  # getattr(x, "NAME") style access
        if name is not None:
            if fmask is None:
                m = 0
                if stmt is not None:
                    for l in range(stmt.lineno, (stmt.end_lineno or stmt.lineno) + 1):
                        m |= fl.get(l, 0)
            else:
                m = fmask
            if m:
                refs[name] = refs.get(name, 0) | m
        for k in ast.iter_child_nodes(node):
            visit(k, fmask, stmt)

    visit(tree, None, None)


for base in ("sqlglot", "tests"):
    for dp, dn, fn in os.walk(os.path.join(root, base)):
        for f in fn:
            if f.endswith(".py"):
                full = os.path.join(dp, f)
                rel = os.path.relpath(full, root).replace("\\", "/")
                try:
                    scan(rel, ast.parse(open(full, encoding="utf-8").read()))
                except SyntaxError:
                    pass

masks, index = [], {}


def mi(m):
    b = m.to_bytes(NB, "little")
    i = index.get(b)
    if i is None:
        i = index[b] = len(masks)
        masks.append(b)
    return i


data = {
    "tests": ids,
    "lines": {f: {l: mi(m) for l, m in d.items()} for f, d in lines.items()},
    "starts": {f: {l: mi(m) for l, m in d.items()} for f, d in starts.items()},
    "classes": {c: mi(m) for c, m in classes.items()},
    "opened": {o: mi(m) for o, m in opened.items()},
    "refs": {n: mi(m) for n, m in refs.items()},
}
data["masks"] = masks
blob = zlib.compress(pickle.dumps(data, protocol=4), 9)
open(out, "wb").write(blob)
print("masks", len(masks), "bytes", len(blob), file=sys.stderr)
