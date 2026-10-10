#!/usr/bin/env python3
"""Regression guard: every module names a config key it does not read.

A loader used to check its required keys, fill in its defaults and ignore
everything else. A misspelt optional key was therefore dropped without a word:
`exclude_author` for `exclude_authors` switched thread-sweep's author filter
off, and nothing said so. Every loader now passes the keys it reads to
sweepcore.warn_unknown_keys, which prints one WARN line per unknown top-level
key and never exits.

This lives in modules/ rather than inside one module's suite because the
invariant is cross-module, and because the opposite failure needs the same
table: a key a module does read, left out of the set it declares, would be
reported as ignored while it is in fact honoured. The last test here reads
each module's source for that.

Coverage is the other half. LOADERS names every module directory, and
CoverageTests fails if one is missing, so a new module cannot join the repo
without joining this guard.

Run: python -m unittest discover -s modules -p 'test_config_keys.py'
"""

import ast
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULES_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULES_DIR))
for _sub in sorted(p for p in MODULES_DIR.iterdir() if p.is_dir()):
    if (_sub / f"{_sub.name}.py").exists():
        sys.path.insert(0, str(_sub))

import benchmark_sweep  # noqa: E402
import cfp_sweep  # noqa: E402
import forum_sweep  # noqa: E402
import list_sweep  # noqa: E402
import mention_sweep  # noqa: E402
import newsletter_sweep  # noqa: E402
import placement_health  # noqa: E402
import release_sweep  # noqa: E402
import response_sweep  # noqa: E402
import teardown_sweep  # noqa: E402
import thread_sweep  # noqa: E402

# (module, the function that reads its config file, its shipped example).
LOADERS = (
    (benchmark_sweep, "load_config", "config.example.json"),
    (cfp_sweep, "load_config", "config.example.json"),
    (forum_sweep, "load_config", "config.example.json"),
    (list_sweep, "load_config", "config.example.json"),
    (mention_sweep, "load_config", "config.example.json"),
    (newsletter_sweep, "load_config", "config.example.json"),
    (placement_health, "load_config", "placements.example.json"),
    (release_sweep, "load_channels", "channels.example.json"),
    (response_sweep, "load_config", "config.example.json"),
    (teardown_sweep, "load_config", "config.example.json"),
    (thread_sweep, "load_config", "config.example.json"),
)

# A key no module reads and no known key resembles.
STRAY_KEY = "zz_stray_key_for_the_test"


def module_dir(mod):
    return Path(mod.__file__).resolve().parent


def example_config(mod, example_name):
    return json.loads((module_dir(mod) / example_name).read_text(encoding="utf-8"))


def load(mod, loader_name, cfg):
    """Run the module's loader over `cfg`; return what it wrote to stderr."""
    err = io.StringIO()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        with contextlib.redirect_stderr(err):
            getattr(mod, loader_name)(str(path))
    return err.getvalue()


def declared_keys(mod, loader_name, example_name):
    """The key set the module's loader hands to warn_unknown_keys."""
    seen = []

    def record(_cfg, known, _label):
        seen.append(set(known))
        return []

    with mock.patch.object(mod, "warn_unknown_keys", side_effect=record):
        load(mod, loader_name, example_config(mod, example_name))
    return seen[0] if seen else None


def keys_read_in_source(mod):
    """Every literal top-level key the module's source reads off `cfg`:
    cfg["k"], cfg.get("k"), cfg.setdefault("k") and `"k" in cfg`."""
    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    keys = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "cfg"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            keys.add(node.slice.value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("get", "setdefault")
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "cfg"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            keys.add(node.args[0].value)
        elif (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], (ast.In, ast.NotIn))
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str)
            and isinstance(node.comparators[0], ast.Name)
            and node.comparators[0].id == "cfg"
        ):
            keys.add(node.left.value)
    return keys


class CoverageTests(unittest.TestCase):
    def test_every_module_directory_is_in_the_table(self):
        on_disk = {
            p.name
            for p in MODULES_DIR.iterdir()
            if p.is_dir() and (p / f"{p.name}.py").exists()
        }
        self.assertEqual({mod.__name__ for mod, _fn, _ex in LOADERS}, on_disk)

    def test_every_loader_calls_warn_unknown_keys(self):
        for mod, loader_name, example_name in LOADERS:
            with self.subTest(module=mod.__name__):
                self.assertIsNotNone(declared_keys(mod, loader_name, example_name))


class UnknownKeyTests(unittest.TestCase):
    def test_the_shipped_example_is_clean(self):
        # The examples are meant to be copied, so a warning on an untouched
        # copy would teach people to ignore the line.
        for mod, loader_name, example_name in LOADERS:
            with self.subTest(module=mod.__name__):
                err = load(mod, loader_name, example_config(mod, example_name))
                self.assertNotIn("unknown config key", err)

    def test_a_stray_key_is_named_and_the_config_still_loads(self):
        for mod, loader_name, example_name in LOADERS:
            with self.subTest(module=mod.__name__):
                cfg = example_config(mod, example_name)
                cfg[STRAY_KEY] = True
                err = load(mod, loader_name, cfg)
                self.assertIn(f"unknown config key '{STRAY_KEY}' is ignored", err)
                self.assertEqual(err.count("unknown config key"), 1)
                self.assertTrue(err.startswith("WARN "))

    def test_the_misspelt_author_filter_is_caught(self):
        # The case that prompted the check: the filter was read with cfg.get,
        # so one missing letter switched it off in silence.
        cfg = example_config(thread_sweep, "config.example.json")
        cfg["exclude_author"] = cfg.pop("exclude_authors")
        err = load(thread_sweep, "load_config", cfg)
        self.assertIn(
            "WARN thread-sweep: unknown config key 'exclude_author' is ignored "
            "(did you mean 'exclude_authors'?)",
            err,
        )

    def test_a_misspelt_required_key_is_named_before_the_exit(self):
        cfg = example_config(thread_sweep, "config.example.json")
        cfg["own_repoo"] = cfg.pop("own_repo")
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
                thread_sweep.load_config(str(path))
        self.assertIn("unknown config key 'own_repoo'", err.getvalue())
        self.assertIn("did you mean 'own_repo'?", err.getvalue())
        self.assertIn("missing required keys", str(ctx.exception))


class DeclaredKeyTests(unittest.TestCase):
    def test_every_key_a_module_reads_is_one_it_declares(self):
        # The other direction. A key read with cfg.get but left out of
        # DEFAULTS and OPTIONAL_KEYS would be reported as ignored while the
        # module honours it.
        for mod, loader_name, example_name in LOADERS:
            with self.subTest(module=mod.__name__):
                declared = declared_keys(mod, loader_name, example_name)
                undeclared = keys_read_in_source(mod) - declared
                self.assertEqual(
                    undeclared,
                    set(),
                    f"{mod.__name__} reads config keys its loader does not "
                    "declare; add them to DEFAULTS or OPTIONAL_KEYS",
                )

    def test_every_example_key_is_declared_or_a_comment(self):
        for mod, loader_name, example_name in LOADERS:
            with self.subTest(module=mod.__name__):
                declared = declared_keys(mod, loader_name, example_name)
                extra = {
                    key
                    for key in example_config(mod, example_name)
                    if key not in declared and not key.startswith("_")
                }
                self.assertEqual(extra, set())


if __name__ == "__main__":
    unittest.main()
