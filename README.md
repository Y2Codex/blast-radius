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
- A function or method renamed, removed or re-decorated: also every test that called it.
- A class renamed or removed: also every test that ran code naming the class.
- A method added to a class: also the tests that reached any method of the same name.
- `is` / `==` swaps against `None` or an enum member: treated as no change.

Selected tests are packed longest-first onto the worker count that minimises
busiest worker + 2 s per worker.

No test is run at decision time, and nothing is looked up by patch.

## Files

| | |
| --- | --- |
| `Dockerfile` | Base image from the kit; fetches sqlglot at the pinned commit. Nothing else is installed. |
| `selector.py` | The selector. |
| `map.bin` | The recorded map (zlib-compressed pickle). |
| `fetch_sqlglot.py` | Build step that unpacks the pinned sqlglot into `/opt/sqlglot`. |
| `tools/` | Scripts that made the map: `build_map.py` traces each test with `br_tracer.py`, `compile_map.py` writes `map.bin`. |

## Switched off

`selector.py` still holds an experiment that runs a cheap selection against the patched
checkout and keeps only what fails (`BR_VERIFY=1`), and one that then leaves out the two
slowest tests (`BR_HEAVY_SKIP=1`). Both are off. On the practice board the first saved 2.0 s
of test time for 2.4 s of CPU, and the second can miss a change that only makes code slower.
Running them needs the packages in `requirements.lock`, which the image no longer installs.
