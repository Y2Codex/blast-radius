"""pytest plugin: record, for this process, every repo line executed, every repo code object
entered, every repo data file opened, and the sqlglot classes instantiated (with their bases)."""
import json, os, sys

ROOT = os.path.realpath(os.environ["BR_ROOT"]) + os.sep
OUT = os.environ["BR_OUT"]
lines = {}    # file -> set(line)
starts = {}   # file -> set(co_firstlineno)
opened = set()
types = set()
_cache = {}
mon = sys.monitoring
TOOL, TOOL2 = 3, 4
DISABLE = mon.DISABLE
INIT_QUALNAMES = {"Expr.__init__", "Dialect.__init__", "Tokenizer.__init__", "Parser.__init__",
                  "Generator.__init__", "TypeAnnotator.__init__"}
_getframe = sys._getframe


def _rel(fn):
    r = _cache.get(fn)
    if r is None:
        if fn.startswith("<"):
            r = ""
        else:
            p = os.path.realpath(fn)
            r = p[len(ROOT):].replace("\\", "/") if p.startswith(ROOT) else ""
        _cache[fn] = r
    return r


def _line(code, line):
    r = _rel(code.co_filename)
    if r:
        s = lines.get(r)
        if s is None:
            s = lines[r] = set()
        s.add(line)
    return DISABLE


def _init_start(code, offset):
    try:
        types.add(type(_getframe(1).f_locals["self"]))
    except Exception:
        pass


def _start(code, offset):
    r = _rel(code.co_filename)
    if r:
        s = starts.get(r)
        if s is None:
            s = starts[r] = set()
        s.add(code.co_firstlineno)
        if code.co_qualname in INIT_QUALNAMES and r.startswith("sqlglot/"):
            mon.set_local_events(TOOL2, code, mon.events.PY_START)
            _init_start(code, offset)
    return DISABLE


def _audit(event, args):
    if event == "open":
        p = args[0]
        if isinstance(p, (str, bytes, os.PathLike)):
            try:
                p = os.fsdecode(p)
            except Exception:
                return
            if p.endswith((".pyc", ".py")):
                return
            r = _rel(os.path.abspath(p))
            if r and "__pycache__" not in r:
                opened.add(r)


mon.use_tool_id(TOOL, "br")
mon.use_tool_id(TOOL2, "br2")
mon.register_callback(TOOL, mon.events.LINE, _line)
mon.register_callback(TOOL, mon.events.PY_START, _start)
mon.register_callback(TOOL2, mon.events.PY_START, _init_start)
mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START)
sys.addaudithook(_audit)

outcomes = {}


def pytest_runtest_logreport(report):
    if report.outcome != "passed" or report.when == "call":
        outcomes[report.nodeid + ":" + report.when] = report.outcome


def snapshot():
    cls = set()
    for tp in types:
        for b in tp.__mro__:
            if b.__module__.startswith("sqlglot"):
                cls.add(b.__module__ + ":" + b.__qualname__)
    return {"lines": {k: sorted(v) for k, v in lines.items()},
            "starts": {k: sorted(v) for k, v in starts.items()},
            "classes": sorted(cls), "opened": sorted(opened)}


def pytest_sessionfinish(session, exitstatus):
    import glob
    mon.set_events(TOOL, 0)
    d = snapshot()
    kids = glob.glob(glob.escape(OUT) + ".child.*")
    for k in kids:
        c = json.load(open(k))
        for key in ("lines", "starts"):
            for f, ls in c[key].items():
                d[key][f] = sorted(set(d[key].get(f, [])) | set(ls))
        d["classes"] = sorted(set(d["classes"]) | set(c["classes"]))
        d["opened"] = sorted(set(d["opened"]) | set(c["opened"]))
        os.remove(k)
    d.update(outcomes=outcomes, exit=int(exitstatus), children=len(kids))
    with open(OUT, "w") as f:
        json.dump(d, f)
