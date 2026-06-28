#!/usr/bin/env python3
"""Run every module's offline test suite, gating the whole repo in one command.

Why this exists rather than a bare `python -m unittest discover -s modules`:
each module is a standalone directory (no package __init__.py, by design — every
script runs as `cd modules/<name> && python <name>.py`). Its test imports the
module by top-level name, which only resolves when that module's own directory is
on sys.path[0]. A single top-level `discover` does NOT recurse into the
non-package subdirectories, so it would silently run only the shared
modules/test_sweepcore.py and skip all six per-module suites — green CI that
gates almost nothing. Loading them in one process instead collides on the
top-level module names (every suite does `import <module>`).

So each directory's suite is run in its own `python -m unittest discover`
subprocess (a fresh interpreter, the standalone-run contract each suite already
relies on). Exit code is non-zero if any suite fails, so CI fails on a
regression.

Usage: python modules/run_tests.py
"""

import subprocess
import sys
from pathlib import Path

MODULES_DIR = Path(__file__).resolve().parent


def _dirs_with_tests():
    """The modules/ dir itself (test_sweepcore.py) plus each subdirectory that
    has at least one test_*.py."""
    dirs = []
    if list(MODULES_DIR.glob("test_*.py")):
        dirs.append(MODULES_DIR)
    for sub in sorted(p for p in MODULES_DIR.iterdir() if p.is_dir()):
        if list(sub.glob("test_*.py")):
            dirs.append(sub)
    return dirs


def main():
    failures = []
    for d in _dirs_with_tests():
        print(f"\n=== {d.relative_to(MODULES_DIR.parent)} ===", flush=True)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                str(d),
                "-p",
                "test_*.py",
            ],
        )
        if proc.returncode != 0:
            failures.append(str(d))
    if failures:
        print(f"\nFAILED suites: {failures}")
        return 1
    print("\nAll module suites passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
