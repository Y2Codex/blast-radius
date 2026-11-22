# Y2Code — Blast Radius

Picks which sqlglot tests to run for each patch, and how to split them across workers.

## How it works

`map.bin` is a map recorded once from sqlglot at the pinned commit
(`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`). Every test was run alone in a fresh
process, spawned child processes included, while recording:

- which lines it executed and which functions and lambdas it entered,
- which dialect, parser, generator and expression classes it instantiated,
- which data files (fixtures) it opened,
- which names the code it ran reads.

For each patch, `selector.py` applies the diff to the original file and compares the two
syntax trees:

- Same tree (comments, whitespace): run nothing.
- A statement inside a function: tests that entered the function and executed that statement.
- A lambda: tests that called it.
- Class or module level code: tests that loaded the module, used the class, and read the changed name.
- A data file: tests that opened it.
- A statement inserted into a function: tests that ran both of its neighbours (the only ones that can reach it).
- `is` / `==` swaps against `None` or an enum member: treated as no change.

When the selection is cheap to run (estimated CPU under 80% of what the scorer would charge for
it) and the patch changes Python code but no fixture, the selector runs it against the patched
checkout: a symlink farm over `/opt/sqlglot` in `/tmp` with the patched files written in, tests
run with `unittest` in a forked child (every sqlglot test is a `unittest.TestCase`), cheapest and
closest-named tests first, stopping at the first failure. All pass: nothing is selected. A
failure: the selection stands, less the tests seen passing. A timeout or any doubt keeps the
selection. Process pools in the tests get one forked worker instead of spawned ones.

Selected tests are packed longest-first onto the worker count that minimises
busiest worker + 2 s per worker.

Nothing is looked up by patch.

## Files

| | |
| --- | --- |
| `Dockerfile` | Base image from the kit; installs `requirements.lock`, fetches and byte-compiles sqlglot at the pinned commit. |
| `requirements.lock` | The kit's package set, so tests run here as they did when the commits were labeled. |
| `selector.py` | The selector. |
| `map.bin` | The recorded map (zlib-compressed pickle). |
| `fetch_sqlglot.py` | Build step that unpacks the pinned sqlglot into `/opt/sqlglot`. |
| `tools/` | Scripts that made the map: `build_map.py` traces each test with `br_tracer.py`, `compile_map.py` writes `map.bin`. |
