# Trace multiprocessing children spawned by a traced test (see br_tracer.py).
import os, sys
if os.environ.get("BR_OUT") and any("multiprocessing" in a for a in getattr(sys, "orig_argv", [])):
    import br_tracer
    import atexit, json
    from multiprocessing import util as _u

    def _dump():
        if getattr(_dump, "done", False):
            return
        _dump.done = True
        br_tracer.mon.set_events(br_tracer.TOOL, 0)
        with open(br_tracer.OUT + ".child.%d" % os.getpid(), "w") as f:
            json.dump(br_tracer.snapshot(), f)

    _u.Finalize(None, _dump, exitpriority=-100)
    atexit.register(_dump)
