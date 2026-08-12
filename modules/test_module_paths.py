#!/usr/bin/env python3
"""Regression guard: module paths anchor on the module, never on the CWD.

The paths a module owns — its state dir, its candidates file, its default
config — used to be read relative to the process CWD. Running two modules from
the repo root therefore pointed both at one shared ./candidates.json and one
shared ./state/: the second scan overwrote the first's candidates, and each
module opened a brand-new (empty) state file, so the window silently reset to
the first-run default instead of the module's real last_run. The tells were two
scans printing the same "candidates -> candidates.json" and a window date of
today-minus-the-default rather than the last real run.

Every module now resolves those paths from its own __file__ via
sweepcore.resolve_module_path. These tests pin that contract down.

This lives in modules/ rather than inside one module's suite because the
invariant is cross-module: the whole point is that six modules resolve to six
different places, which no single module's tests can see.

Run: python -m unittest discover -s modules -p 'test_module_paths.py'
"""

import argparse
import contextlib
import io
import json
import os
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

import forum_sweep  # noqa: E402
import list_sweep  # noqa: E402
import mention_sweep  # noqa: E402
import placement_health  # noqa: E402
import release_sweep  # noqa: E402
import sweepcore  # noqa: E402
import thread_sweep  # noqa: E402

# (module, the state filename that module writes) for every module that keeps
# a state dir and a candidates file.
STATEFUL = (
    (thread_sweep, "sweep_state.json"),
    (forum_sweep, "forum_sweep_state.json"),
    (mention_sweep, "mention_sweep_state.json"),
    (list_sweep, "list_state.json"),
)

# (module, subcommand, the cmd_* attribute that subcommand dispatches to).
# Used to read back the --config default the module's own parser installs.
CONFIG_DEFAULTS = (
    (thread_sweep, "density", "cmd_density", "config.json"),
    (forum_sweep, "density", "cmd_density", "config.json"),
    (mention_sweep, "density", "cmd_density", "config.json"),
    (list_sweep, "log", "cmd_log", "config.json"),
    (release_sweep, "log", "cmd_log", "channels.json"),
    (placement_health, "check", "cmd_check", "placements.json"),
)


@contextlib.contextmanager
def working_dir(path):
    """Run the body from `path` (Python 3.10 has no contextlib.chdir)."""
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield Path(path).resolve()
    finally:
        os.chdir(previous)


def module_dir(mod):
    return Path(mod.__file__).resolve().parent


class ForeignCwdTests(unittest.TestCase):
    """A module invoked from somewhere else still reads and writes its own
    canonical locations."""

    def test_state_paths_resolve_beside_the_module_not_the_cwd(self):
        for mod, state_name in STATEFUL:
            with self.subTest(module=mod.__name__):
                own = module_dir(mod)
                with tempfile.TemporaryDirectory() as tmp, working_dir(tmp) as here:
                    state_dir, state_file, ledger = mod.state_paths(dict(mod.DEFAULTS))
                self.assertEqual(Path(state_dir), own / "state")
                self.assertEqual(Path(state_file), own / "state" / state_name)
                self.assertEqual(Path(ledger).parent, own / "state")
                # The decisive assertion: nothing resolved under the CWD.
                self.assertNotIn(here, Path(state_file).parents)
                self.assertNotIn(here, Path(ledger).parents)

    def test_candidates_file_resolves_beside_the_module_not_the_cwd(self):
        for mod, _state_name in STATEFUL:
            with self.subTest(module=mod.__name__):
                own = module_dir(mod)
                with tempfile.TemporaryDirectory() as tmp, working_dir(tmp) as here:
                    out = sweepcore.resolve_module_path(
                        mod.__file__, mod.DEFAULTS["candidates_file"]
                    )
                self.assertEqual(out, own / "candidates.json")
                self.assertNotIn(here, out.parents)

    def test_module_level_path_constants_are_module_anchored(self):
        constants = (
            (placement_health, "LOG_PATH"),
            (release_sweep, "LEDGER_PATH"),
            (release_sweep, "BRIEF_PATH"),
        )
        for mod, attr in constants:
            with self.subTest(constant=f"{mod.__name__}.{attr}"):
                value = Path(getattr(mod, attr))
                self.assertTrue(value.is_absolute(), f"{attr} must not be CWD-relative")
                self.assertIn(module_dir(mod), value.parents)

    def test_default_config_path_is_module_anchored(self):
        """The --config default the parser installs points at the module's own
        config, so `python modules/<name>/<name>.py density` from the repo root
        does not fail (or, worse, silently read a stranger's config.json)."""
        for mod, subcommand, attr, filename in CONFIG_DEFAULTS:
            with self.subTest(module=mod.__name__):
                seen = {}

                def _capture(args, _seen=seen):
                    _seen["config"] = args.config
                    return 0

                argv = [f"{mod.__name__}.py", subcommand]
                with tempfile.TemporaryDirectory() as tmp, working_dir(tmp) as here:
                    with (
                        mock.patch.object(mod, attr, _capture),
                        mock.patch.object(sys, "argv", argv),
                    ):
                        with self.assertRaises(SystemExit) as exit_ctx:
                            mod.main()
                    self.assertEqual(exit_ctx.exception.code, 0)
                    resolved = Path(seen["config"])
                    self.assertEqual(resolved, module_dir(mod) / filename)
                    self.assertNotIn(here, resolved.parents)

    def test_an_explicit_config_override_is_still_honoured(self):
        """Only the DEFAULT is module-anchored. A --config the user typed is
        used verbatim, relative to their CWD, exactly as before."""
        for mod, subcommand, attr, _filename in CONFIG_DEFAULTS:
            with self.subTest(module=mod.__name__):
                seen = {}

                def _capture(args, _seen=seen):
                    _seen["config"] = args.config
                    return 0

                argv = [f"{mod.__name__}.py", "--config", "mine.json", subcommand]
                with (
                    mock.patch.object(mod, attr, _capture),
                    mock.patch.object(sys, "argv", argv),
                ):
                    with self.assertRaises(SystemExit):
                        mod.main()
                self.assertEqual(seen["config"], "mine.json")


class DistinctPathsTests(unittest.TestCase):
    """Two modules must never resolve to the same file. This is the collision
    the incident was made of."""

    def test_each_module_resolves_a_distinct_candidates_path(self):
        resolved = {
            mod.__name__: sweepcore.resolve_module_path(
                mod.__file__, mod.DEFAULTS["candidates_file"]
            )
            for mod, _state_name in STATEFUL
        }
        self.assertEqual(
            len(set(resolved.values())),
            len(resolved),
            f"modules share a candidates path: {resolved}",
        )
        # Named explicitly: these are the two that collided.
        self.assertNotEqual(resolved["thread_sweep"], resolved["forum_sweep"])

    def test_each_module_resolves_a_distinct_state_directory(self):
        # Distinct state *filenames* were never the problem — every module
        # already named its own. The collision was the directory: four modules
        # all pointing at one ./state/. Assert the directories differ.
        resolved = {
            mod.__name__: Path(mod.state_paths(dict(mod.DEFAULTS))[0])
            for mod, _state_name in STATEFUL
        }
        self.assertEqual(
            len(set(resolved.values())),
            len(resolved),
            f"modules share a state directory: {resolved}",
        )


class ResolveModulePathTests(unittest.TestCase):
    """The shared primitive itself."""

    def test_relative_value_anchors_on_the_module_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "pkg" / "widget.py"
            fake.parent.mkdir(parents=True)
            fake.write_text("", encoding="utf-8")
            with tempfile.TemporaryDirectory() as other, working_dir(other):
                got = sweepcore.resolve_module_path(str(fake), "state/x.json")
            self.assertEqual(got, fake.parent / "state" / "x.json")

    def test_absolute_value_passes_through_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            absolute = Path(tmp).resolve() / "elsewhere.json"
            got = sweepcore.resolve_module_path(__file__, str(absolute))
            self.assertEqual(got, absolute)


class ScanWritesBesideTheModuleTests(unittest.TestCase):
    """End-to-end reproduction of the incident: two modules run back-to-back
    from one shared CWD must write to two different places, and each must read
    its OWN state rather than starting a fresh one in the CWD."""

    def _thread_cfg(self):
        cfg = dict(thread_sweep.DEFAULTS)
        cfg.update({"own_login": "me", "own_repo": "me/proj", "queries": {}})
        return cfg

    def _mention_cfg(self):
        cfg = dict(mention_sweep.DEFAULTS)
        cfg.update({"display_name": "proj", "match_strings": ["proj"], "own_repos": []})
        return cfg

    def _run_thread_scan(self, fake_dir, *, days=None):
        args = argparse.Namespace(
            config="config.json", days=days, limit=None, dry_run=False
        )
        buf = io.StringIO()
        with (
            mock.patch.object(
                thread_sweep, "__file__", str(Path(fake_dir) / "thread_sweep.py")
            ),
            mock.patch.object(
                thread_sweep, "load_config", return_value=self._thread_cfg()
            ),
            mock.patch.object(thread_sweep, "search_lane", return_value=[]),
            mock.patch.object(thread_sweep, "watchlist_lane", return_value=[]),
            mock.patch("sys.stdout", buf),
        ):
            rc = thread_sweep.cmd_scan(args)
        return rc, buf.getvalue()

    def _run_mention_scan(self, fake_dir):
        args = argparse.Namespace(
            config="config.json", days=None, limit=None, dry_run=False
        )
        buf = io.StringIO()
        with (
            mock.patch.object(
                mention_sweep, "__file__", str(Path(fake_dir) / "mention_sweep.py")
            ),
            mock.patch.object(
                mention_sweep, "load_config", return_value=self._mention_cfg()
            ),
            mock.patch.object(mention_sweep, "thread_lane", return_value=[]),
            mock.patch.object(mention_sweep, "code_lane", return_value=[]),
            mock.patch("sys.stdout", buf),
        ):
            rc = mention_sweep.cmd_scan(args)
        return rc, buf.getvalue()

    def test_two_modules_from_one_cwd_do_not_clobber_each_other(self):
        with (
            tempfile.TemporaryDirectory() as thread_home,
            tempfile.TemporaryDirectory() as mention_home,
            tempfile.TemporaryDirectory() as shared_cwd,
        ):
            with working_dir(shared_cwd) as here:
                thread_rc, _ = self._run_thread_scan(thread_home)
                mention_rc, _ = self._run_mention_scan(mention_home)
            self.assertEqual((thread_rc, mention_rc), (0, 0))

            thread_out = Path(thread_home) / "candidates.json"
            mention_out = Path(mention_home) / "candidates.json"
            self.assertTrue(thread_out.exists(), "thread candidates missed its module")
            self.assertTrue(
                mention_out.exists(), "mention candidates missed its module"
            )
            self.assertNotEqual(thread_out, mention_out)
            self.assertTrue((Path(thread_home) / "state" / "sweep_state.json").exists())
            self.assertTrue(
                (Path(mention_home) / "state" / "mention_sweep_state.json").exists()
            )
            # Nothing at all landed in the directory the process was started in.
            self.assertFalse((here / "candidates.json").exists())
            self.assertFalse((here / "state").exists())

    def test_posted_ledger_still_excludes_a_thread_from_another_cwd(self):
        """Gate-adjacent: the posted ledger is the record of what a human already
        approved and answered, and it is what keeps an answered thread out of the
        next digest. A CWD-anchored ledger meant a scan started elsewhere read an
        empty one and re-surfaced work already done. Record from one directory,
        scan from another, and the exclusion must still hold."""
        answered = "https://github.com/acme/widgets/issues/7"
        candidate = {
            "url": answered,
            "title": "already answered",
            "created": "2026-08-01T00:00:00Z",
            "repo": "acme/widgets",
            "stars": 5000,
            "comments": 0,
            "is_answered": False,
            "author": "someone",
            "snippet": "",
            "pattern": "memory",
            "answers_with": "",
            "lane": "query",
        }
        with (
            tempfile.TemporaryDirectory() as thread_home,
            tempfile.TemporaryDirectory() as record_cwd,
            tempfile.TemporaryDirectory() as scan_cwd,
        ):
            patched_file = str(Path(thread_home) / "thread_sweep.py")
            mark_args = argparse.Namespace(
                config="config.json",
                url=answered,
                pattern="memory",
                comment_file=None,
            )
            with working_dir(record_cwd):
                with (
                    mock.patch.object(thread_sweep, "__file__", patched_file),
                    mock.patch.object(
                        thread_sweep, "load_config", return_value=self._thread_cfg()
                    ),
                    mock.patch("sys.stdout", io.StringIO()),
                ):
                    thread_sweep.cmd_mark_posted(mark_args)

            args = argparse.Namespace(
                config="config.json", days=30, limit=None, dry_run=False
            )
            buf = io.StringIO()
            with working_dir(scan_cwd):
                with (
                    mock.patch.object(thread_sweep, "__file__", patched_file),
                    mock.patch.object(
                        thread_sweep, "load_config", return_value=self._thread_cfg()
                    ),
                    mock.patch.object(
                        thread_sweep, "search_lane", return_value=[dict(candidate)]
                    ),
                    mock.patch.object(thread_sweep, "watchlist_lane", return_value=[]),
                    mock.patch("sys.stdout", buf),
                ):
                    thread_sweep.cmd_scan(args)

            payload = json.loads(
                (Path(thread_home) / "candidates.json").read_text(encoding="utf-8")
            )
        self.assertEqual(payload["dropped"]["posted"], 1)
        self.assertEqual(payload["candidates"], [])

    def test_scan_reads_its_own_last_run_instead_of_re_windowing(self):
        """The second half of the incident: a fresh state file in the CWD made
        the window fall back to the first-run default. With the state read from
        the module's own dir, the printed window follows the real last_run."""
        from datetime import datetime, timedelta, timezone

        last_run = datetime.now(timezone.utc) - timedelta(days=3)
        with (
            tempfile.TemporaryDirectory() as thread_home,
            tempfile.TemporaryDirectory() as shared_cwd,
        ):
            state_file = Path(thread_home) / "state" / "sweep_state.json"
            state_file.parent.mkdir(parents=True)
            state_file.write_text(
                f'{{"last_run": "{last_run.isoformat()}", "seen": {{}}}}',
                encoding="utf-8",
            )
            with working_dir(shared_cwd):
                rc, out = self._run_thread_scan(thread_home)
            self.assertEqual(rc, 0)
            self.assertIn(f"window>{last_run.strftime('%Y-%m-%d')}", out)
            first_run_default = (
                datetime.now(timezone.utc)
                - timedelta(days=thread_sweep.DEFAULTS["default_window_days"])
            ).strftime("%Y-%m-%d")
            self.assertNotIn(f"window>{first_run_default}", out)


if __name__ == "__main__":
    unittest.main()
