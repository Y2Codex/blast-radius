"""Run every test alone in a fresh process under the tracer; write raw/<n>.json."""
import json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor

root, tests_json, outdir, jobs = os.path.abspath(sys.argv[1]), sys.argv[2], os.path.abspath(sys.argv[3]), int(sys.argv[4])
here = os.path.dirname(os.path.abspath(__file__))
tests = json.load(open(tests_json))["tests"]
os.makedirs(outdir, exist_ok=True)


def run(t):
    out = os.path.join(outdir, f"{t['number']}.json")
    if os.path.exists(out):
        return
    env = dict(os.environ, BR_ROOT=root, BR_OUT=out + ".tmp", SKIP_INTEGRATION="0",
               PYTHONPATH=root + os.pathsep + here, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "br_tracer", "-p", "no:cacheprovider",
                        t["id"]], cwd=root, env=env, capture_output=True, text=True)
    if os.path.exists(out + ".tmp"):
        os.replace(out + ".tmp", out)
    else:
        print("FAILED", t["id"], p.stdout[-500:], p.stderr[-500:], flush=True)


t0 = time.time()
with ThreadPoolExecutor(jobs) as ex:
    for i, _ in enumerate(ex.map(run, tests)):
        if i % 100 == 0:
            print(i, round(time.time() - t0), flush=True)
