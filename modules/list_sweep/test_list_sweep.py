#!/usr/bin/env python3
"""Offline unit tests for list-sweep (stdlib unittest only).

Every gh / subprocess call and every network read is mocked. These tests make
NO live calls. Run: python -m unittest discover -s modules/list_sweep -p 'test_*.py'
"""

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

# Import the module by path so the test runs from any cwd.
_MOD_PATH = Path(__file__).resolve().parent / "list_sweep.py"
_spec = importlib.util.spec_from_file_location("list_sweep", _MOD_PATH)
ls = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ls)


CONFIG = {
    "own_repo": "me/my-project",
    "own_tagline": "a thing",
    "topics": ["claude-code", "ai-agents"],
    "search_keywords": ["awesome-claude-code", "awesome-ai-agents"],
    "watchlist": ["someone/awesome-claude-code"],
    "placements_path": None,
    "min_stars": 100,
    "per_query": 20,
    "emit_cap": 60,
    "seen_retention_days": 180,
    "default_window_days": 30,
    "fit_floor": 1,
    "state_dir": "state",
    "candidates_file": "candidates.json",
}


def _full_config():
    cfg = dict(CONFIG)
    for key, val in ls.DEFAULTS.items():
        cfg.setdefault(key, val)
    return cfg


def _fake_doc_resp(text):
    """Build a urlopen context-manager mock returning text with status 200."""
    resp = mock.MagicMock()
    resp.status = 200
    resp.read.return_value = text.encode("utf-8")
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


class QueryConstructionTests(unittest.TestCase):
    def test_query_per_keyword_with_time_scope(self):
        cfg = _full_config()
        queries = ls.build_queries(cfg, "2026-01-01")
        self.assertEqual(len(queries), len(cfg["search_keywords"]))
        for q in queries:
            self.assertIn("pushed:>2026-01-01", q)
            self.assertIn("in:name,description,readme", q)
            self.assertIn("sort:stars", q)
        self.assertTrue(any("awesome-claude-code" in q for q in queries))


class IntakeClassificationTests(unittest.TestCase):
    def _classify(self, doc_text):
        with mock.patch.object(ls, "fetch_raw", return_value=doc_text):
            return ls.classify_intake("owner/list", [])

    def test_pr_intake(self):
        path, doc, human = self._classify(
            "Add yourself with a pull request to this list."
        )
        self.assertEqual(path, "pr")
        self.assertFalse(human)

    def test_issue_form_intake(self):
        path, _doc, _h = self._classify(
            "To be added, open an issue using the template."
        )
        self.assertEqual(path, "issue-form")

    def test_web_form_intake(self):
        path, _doc, _h = self._classify(
            "Submissions go through our Google Form: forms.gle/x"
        )
        self.assertEqual(path, "web-form")

    def test_unknown_when_no_signal(self):
        path, doc, _h = self._classify("Welcome to the list. Here are the entries.")
        self.assertEqual(path, "unknown")
        # A doc was found, so 'doc' reflects that rather than a fetch miss.
        self.assertIsNotNone(doc)

    def test_unknown_when_no_doc(self):
        with mock.patch.object(ls, "fetch_raw", return_value=None):
            path, doc, human = ls.classify_intake("owner/list", [])
        self.assertEqual(path, "unknown")
        self.assertIsNone(doc)
        self.assertFalse(human)

    def test_human_only_flag(self):
        path, _doc, human = self._classify(
            "Open a pull request. Note: no bots, human submissions only."
        )
        self.assertEqual(path, "pr")
        self.assertTrue(human)

    def test_signal_in_later_doc_is_found(self):
        # A generic CONTRIBUTING.md must not stop the search: the intake
        # signal (and a human-only ban) may live further down the doc list.
        docs = {
            "CONTRIBUTING.md": "Thanks for contributing!",
            "README.md": (
                "Submissions go through our Google Form: forms.gle/x. "
                "No bots, human submissions only."
            ),
        }
        with mock.patch.object(
            ls, "fetch_raw", side_effect=lambda repo, doc: docs.get(doc)
        ):
            path, doc, human = ls.classify_intake("owner/list", [])
        self.assertEqual(path, "web-form")
        self.assertEqual(doc, "README.md")
        self.assertTrue(human)

    def test_human_only_accumulates_when_no_signal_anywhere(self):
        docs = {
            "CONTRIBUTING.md": "Thanks for contributing!",
            "README.md": "Curated by hand. No bots, human submissions only.",
        }
        with mock.patch.object(
            ls, "fetch_raw", side_effect=lambda repo, doc: docs.get(doc)
        ):
            path, doc, human = ls.classify_intake("owner/list", [])
        self.assertEqual(path, "unknown")
        self.assertEqual(doc, "CONTRIBUTING.md")  # first doc actually seen
        self.assertTrue(human)


class WatchlistFitFloorTests(unittest.TestCase):
    def test_watchlist_entry_bypasses_fit_floor(self):
        # A watchlisted repo is hand-curated (and carries no description for
        # fit_score to read), so like the star floor the fit floor must not
        # silently drop it.
        cfg = _full_config()
        cfg["watchlist"] = ["someone/curated-list"]  # zero topic-term overlap
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            cfg["candidates_file"] = str(Path(tmp) / "candidates.json")
            args = mock.Mock(config="config.json", days=30, limit=None, dry_run=False)
            with (
                mock.patch.object(ls, "load_config", return_value=cfg),
                mock.patch.object(ls, "build_queries", return_value=[]),
                mock.patch.object(ls, "search_repos", return_value=[]),
                mock.patch.object(ls, "fetch_raw", return_value="Open a pull request."),
            ):
                ls.cmd_scan(args)
            payload = json.loads(
                Path(cfg["candidates_file"]).read_text(encoding="utf-8")
            )
        self.assertEqual(
            [c["repo"] for c in payload["candidates"]], ["someone/curated-list"]
        )
        self.assertEqual(payload["dropped"]["fit"], 0)


class FitScoreTests(unittest.TestCase):
    def test_scores_topic_and_keyword_overlap(self):
        cfg = _full_config()
        score = ls.fit_score(
            "x/awesome-claude-code", "tools for ai-agents and claude-code", cfg
        )
        self.assertGreaterEqual(score, 2)

    def test_zero_when_no_overlap(self):
        cfg = _full_config()
        self.assertEqual(ls.fit_score("x/cooking-recipes", "food stuff", cfg), 0)


class RepoFromUrlTests(unittest.TestCase):
    def test_github_url(self):
        self.assertEqual(
            ls.repo_from_url("https://github.com/owner/repo"), "owner/repo"
        )

    def test_raw_url(self):
        self.assertEqual(
            ls.repo_from_url(
                "https://raw.githubusercontent.com/owner/repo/main/README.md"
            ),
            "owner/repo",
        )

    def test_non_github(self):
        self.assertIsNone(ls.repo_from_url("https://example.com/foo"))


class PlacementsDedupTests(unittest.TestCase):
    def test_loads_repos_from_placements_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "placements.json"
            p.write_text(
                json.dumps(
                    {
                        "placements": [
                            {
                                "url": "https://raw.githubusercontent.com/already/listed/main/README.md",
                                "expect": "me",
                            },
                            {"url": "https://github.com/another/dir", "expect": "me"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            listed = ls.load_placements(str(p), [])
        self.assertIn("already/listed", listed)
        self.assertIn("another/dir", listed)

    def test_missing_path_is_a_soft_error_not_crash(self):
        errors = []
        listed = ls.load_placements("does/not/exist.json", errors)
        self.assertEqual(listed, set())
        self.assertEqual(len(errors), 1)


class SubmittedLedgerTests(unittest.TestCase):
    def test_submitted_repos_from_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = Path(tmp) / "submitted_log.jsonl"
            led.write_text(
                json.dumps({"url": "https://github.com/owner/done", "list": "Done"})
                + "\n",
                encoding="utf-8",
            )
            repos = ls.submitted_repos(led)
        self.assertIn("owner/done", repos)


class ScanIntegrationTests(unittest.TestCase):
    """End-to-end scan with gh + network mocked, verifying candidates.json shape,
    dedup against the seen-store + placements registry, and flagging."""

    def _run_scan(self, tmp, search_hits, doc_text, *, placements=None, seen=None):
        cfg = _full_config()
        cfg["state_dir"] = str(Path(tmp) / "state")
        cfg["candidates_file"] = str(Path(tmp) / "candidates.json")
        cfg["watchlist"] = []  # isolate lane 1 for a clean count
        if placements is not None:
            pp = Path(tmp) / "placements.json"
            pp.write_text(json.dumps({"placements": placements}), encoding="utf-8")
            cfg["placements_path"] = str(pp)

        if seen is not None:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "list_state.json").write_text(
                json.dumps({"last_run": None, "seen": seen}), encoding="utf-8"
            )

        args = mock.Mock(config="config.json", days=30, limit=None, dry_run=False)

        def _search(query, limit, errors):
            # A search that comes back is a completed fetch, so the stand-in
            # records one: without it the run reads as "never searched" and the
            # scan correctly declines to advance its window, which is a
            # different code path from the one these tests are about.
            ls.note_fetch_ok(errors)
            return search_hits

        # One query so each mocked hit is processed once (search_repos is mocked
        # to return the same list per query; collapsing to a single query keeps
        # the dedup-bucket counts deterministic).
        with (
            mock.patch.object(ls, "load_config", return_value=cfg),
            mock.patch.object(ls, "build_queries", return_value=["q"]),
            mock.patch.object(ls, "search_repos", side_effect=_search),
            mock.patch.object(ls, "fetch_raw", return_value=doc_text),
        ):
            rc = ls.cmd_scan(args)
        payload = json.loads(Path(cfg["candidates_file"]).read_text(encoding="utf-8"))
        return rc, payload

    def test_candidates_shape_and_flagging(self):
        hits = [
            {
                "fullName": "good/awesome-claude-code",
                "description": "ai-agents and claude-code tools",
                "stargazersCount": 500,
                "url": "https://github.com/good/awesome-claude-code",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            rc, payload = self._run_scan(
                tmp, hits, "Submit via our Google Form: forms.gle/abc"
            )
        self.assertEqual(rc, 0)
        self.assertEqual(len(payload["candidates"]), 1)
        cand = payload["candidates"][0]
        for field in (
            "repo",
            "url",
            "stars",
            "lane",
            "fit_score",
            "intake_path",
            "flagged",
            "submission_draft",
        ):
            self.assertIn(field, cand)
        self.assertEqual(cand["intake_path"], "web-form")
        self.assertTrue(cand["flagged"])  # web-form -> flagged
        self.assertEqual(payload["flagged_count"], 1)
        self.assertIn("project", cand["submission_draft"])

    def test_dedup_against_placements_and_seen(self):
        hits = [
            {
                "fullName": "placed/awesome-claude-code",
                "description": "claude-code",
                "stargazersCount": 500,
                "url": "https://github.com/placed/awesome-claude-code",
            },
            {
                "fullName": "seenrepo/awesome-ai-agents",
                "description": "ai-agents",
                "stargazersCount": 500,
                "url": "https://github.com/seenrepo/awesome-ai-agents",
            },
            {
                "fullName": "fresh/awesome-claude-code",
                "description": "claude-code",
                "stargazersCount": 500,
                "url": "https://github.com/fresh/awesome-claude-code",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            rc, payload = self._run_scan(
                tmp,
                hits,
                "Open a pull request.",
                placements=[
                    {
                        "url": "https://github.com/placed/awesome-claude-code",
                        "expect": "x",
                    }
                ],
                seen={"seenrepo/awesome-ai-agents": "2099-01-01"},
            )
        repos = {c["repo"] for c in payload["candidates"]}
        self.assertEqual(repos, {"fresh/awesome-claude-code"})
        self.assertEqual(payload["dropped"]["placed"], 1)
        self.assertEqual(payload["dropped"]["seen"], 1)

    def test_star_floor_and_own_repo_dropped(self):
        hits = [
            {
                "fullName": "me/my-project",  # own repo
                "description": "claude-code",
                "stargazersCount": 9999,
                "url": "https://github.com/me/my-project",
            },
            {
                "fullName": "tiny/awesome-claude-code",
                "description": "claude-code",
                "stargazersCount": 5,  # below min_stars
                "url": "https://github.com/tiny/awesome-claude-code",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            _rc, payload = self._run_scan(tmp, hits, "Open a pull request.")
        self.assertEqual(payload["candidates"], [])
        self.assertEqual(payload["dropped"]["own"], 1)
        self.assertEqual(payload["dropped"]["stars"], 1)


class DryRunTests(unittest.TestCase):
    def test_dry_run_makes_no_calls_and_writes_nothing(self):
        cfg = _full_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            cfg["candidates_file"] = str(Path(tmp) / "candidates.json")
            args = mock.Mock(config="config.json", days=7, limit=None, dry_run=True)
            buf = io.StringIO()
            with (
                mock.patch.object(ls, "load_config", return_value=cfg),
                mock.patch.object(
                    ls, "search_repos", side_effect=AssertionError("no gh in dry-run")
                ),
                mock.patch.object(
                    ls, "fetch_raw", side_effect=AssertionError("no network in dry-run")
                ),
                # search_repos + fetch_raw are the only list-sweep I/O paths; the
                # raw subprocess/urllib calls now live in sweepcore (imported), so
                # guarding those two functions is the dry-run no-I/O assertion.
                mock.patch("sys.stdout", buf),
            ):
                rc = ls.cmd_scan(args)
        self.assertEqual(rc, 0)
        self.assertIn("DRY-RUN", buf.getvalue())
        # No candidates file written.
        self.assertFalse(Path(cfg["candidates_file"]).exists())
        # No state directory created.
        self.assertFalse(Path(cfg["state_dir"]).exists())


class EarnedWindowTests(unittest.TestCase):
    """last_run is a claim about coverage, so a run may only advance it after
    proving it covered the window: at least one lane-1 search came back and none
    failed. Lane 1 is the only time-scoped lane (pushed:>floor) — the watchlist
    is a fixed seed list and the placements registry is a dedup source — so
    lane 1 alone earns or holds the marker.

    The old guard asked only "did every request fail AND come back with
    nothing?", which let three unearned advances through: a failed search whose
    watchlist entries still produced candidates, a run whose search lane never
    issued a query, and a window narrowed to start after the stored marker. A
    fourth case did not advance so much as abort — an unparseable marker raised
    out of the scan.
    """

    OLD = "2026-07-01T00:00:00+00:00"

    def _setup(self, tmp, state_obj=None, **overrides):
        cfg = _full_config()
        cfg["state_dir"] = str(Path(tmp) / "state")
        cfg["candidates_file"] = str(Path(tmp) / "candidates.json")
        cfg["watchlist"] = []
        cfg["search_keywords"] = ["awesome-claude-code"]  # one query, one fetch
        cfg.update(overrides)
        state_file = Path(cfg["state_dir"]) / "list_state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        if state_obj is not None:
            state_file.write_text(json.dumps(state_obj), encoding="utf-8")
        return cfg, state_file

    def _scan(self, cfg, gh, days=None, queries=None):
        args = mock.Mock(config="config.json", days=days, limit=None, dry_run=False)
        err = io.StringIO()
        patches = [
            mock.patch.object(ls, "load_config", return_value=cfg),
            # `gh` (not search_repos) is patched, so the real lane-1 helper runs
            # and does its own fetch accounting.
            mock.patch.object(ls, "gh", side_effect=gh),
            mock.patch.object(ls, "fetch_raw", return_value="Open a pull request."),
        ]
        if queries is not None:
            patches.append(mock.patch.object(ls, "build_queries", return_value=queries))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(err))
            ls.cmd_scan(args)
        return err.getvalue()

    @staticmethod
    def _marker(state_file):
        if not state_file.exists():
            return None
        return json.loads(state_file.read_text(encoding="utf-8")).get("last_run")

    @staticmethod
    def _digest(cfg):
        return json.loads(Path(cfg["candidates_file"]).read_text(encoding="utf-8"))

    # --- fake gh outcomes ---
    @staticmethod
    def _ok(args):
        """The search comes back matching nothing: a real, empty window."""
        return [], None

    @staticmethod
    def _boom(args):
        return None, "HTTP 500 boom"

    @staticmethod
    def _silent_failure(args):
        """gh() yields ("", "") when a non-zero exit writes nothing to stderr.
        The `if err` guard is falsy here, so this is the shape that slipped
        past as a covered window."""
        return None, ""

    def test_search_failing_with_empty_stderr_holds_the_marker(self):
        # No usable payload means the search did not come back, whatever the
        # stderr channel says. Counting it as covered advanced the marker over
        # ground the query never returned.
        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(tmp, {"last_run": self.OLD, "seen": {}})
            self._scan(cfg, self._silent_failure)
            self.assertEqual(self._marker(state_file), self.OLD)

    def test_failed_search_holds_the_marker_even_though_lists_came_back(self):
        # The dangerous shape: lane 1 fails, the hand-curated watchlist still
        # yields candidates, so a raw-count guard reads the run as a success.
        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(
                tmp,
                {"last_run": self.OLD, "seen": {}},
                watchlist=["someone/curated-list"],
            )
            self._scan(cfg, self._boom)
            self.assertEqual(len(self._digest(cfg)["candidates"]), 1)
            self.assertEqual(self._marker(state_file), self.OLD)

    def test_run_that_issued_no_search_holds_the_marker(self):
        # No error, no search: the shape of a skipped lane, and the dangerous
        # one — nothing about it reads as a failure.
        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(
                tmp,
                {"last_run": self.OLD, "seen": {}},
                watchlist=["someone/curated-list"],
            )
            self._scan(cfg, self._ok, queries=[])
            self.assertEqual(self._marker(state_file), self.OLD)

    def test_total_failure_holds_the_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(tmp, {"last_run": self.OLD, "seen": {}})
            self._scan(cfg, self._boom)
            self.assertEqual(self._marker(state_file), self.OLD)

    def test_successful_empty_scan_still_advances(self):
        # The guard against over-correcting: a marker that never moves re-scans
        # a widening window forever. A search that came back matching nothing is
        # a covered window and must advance.
        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(tmp, {"last_run": self.OLD, "seen": {}})
            self._scan(cfg, self._ok)
            self.assertNotEqual(self._marker(state_file), self.OLD)

    def test_missing_placements_file_does_not_hold_the_marker(self):
        # The dedup registry is not a coverage source. Holding on it would
        # freeze the marker permanently the moment placements.json went missing,
        # for no coverage gain at all.
        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(
                tmp,
                {"last_run": self.OLD, "seen": {}},
                placements_path=str(Path(tmp) / "nope.json"),
            )
            self._scan(cfg, self._ok)
            self.assertNotEqual(self._marker(state_file), self.OLD)
            # The problem is still reported, just not treated as lost coverage.
            self.assertTrue(
                any("placements_path" in e for e in self._digest(cfg)["errors"]),
                "a missing dedup registry must still reach the digest",
            )

    def test_malformed_watchlist_entry_does_not_hold_the_marker(self):
        # Same reasoning: the watchlist is untimed, so a bad entry costs no
        # window. It stays a reported problem, not a permanent marker freeze.
        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(
                tmp, {"last_run": self.OLD, "seen": {}}, watchlist=["not-owner-slash"]
            )
            self._scan(cfg, self._ok)
            self.assertNotEqual(self._marker(state_file), self.OLD)
            self.assertTrue(any("owner/name" in e for e in self._digest(cfg)["errors"]))

    def test_failed_run_with_no_prior_marker_invents_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(
                tmp, {"seen": {}}, watchlist=["someone/curated-list"]
            )
            self._scan(cfg, self._boom)
            self.assertFalse(self._marker(state_file))

    def test_first_clean_run_with_no_prior_marker_lays_one_down(self):
        # The other direction of the same rule: absent is not unreadable.
        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(tmp, {"seen": {}})
            self._scan(cfg, self._ok)
            age = datetime.now(timezone.utc) - datetime.fromisoformat(
                self._marker(state_file)
            )
            self.assertAlmostEqual(age.total_seconds(), 0, delta=120)

    def test_narrow_days_override_does_not_swallow_the_uncovered_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
            cfg, state_file = self._setup(tmp, {"last_run": old, "seen": {}})
            self._scan(cfg, self._ok, days=2)
            self.assertEqual(self._marker(state_file), old)

    def test_wide_days_override_covers_the_marker_and_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
            cfg, state_file = self._setup(tmp, {"last_run": old, "seen": {}})
            self._scan(cfg, self._ok, days=30)
            self.assertNotEqual(self._marker(state_file), old)

    def test_unreadable_marker_warns_and_re_windows_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _state_file = self._setup(
                tmp, {"last_run": "last tuesday", "seen": {}}
            )
            err = self._scan(cfg, self._ok)
            self.assertIn("unreadable last_run", err)
            floor = (datetime.now(timezone.utc) - timedelta(days=30)).strftime(
                "%Y-%m-%d"
            )
            self.assertEqual(self._digest(cfg)["pushed_since"], floor)

    def test_unreadable_marker_is_not_overwritten_by_an_unearned_stamp(self):
        # The re-windowed run cannot show it reached back to whatever the rotted
        # marker meant, so stamping `now` would bury the gap in front of the
        # default window AND destroy the evidence that the marker had rotted.
        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(tmp, {"last_run": "last tuesday", "seen": {}})
            self._scan(cfg, self._ok)
            self.assertEqual(self._marker(state_file), "last tuesday")

    def test_held_window_is_reported_in_the_digest_and_on_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _state_file = self._setup(tmp, {"last_run": self.OLD, "seen": {}})
            err = self._scan(cfg, self._boom)
            self.assertTrue(self._digest(cfg)["window_held"])
            self.assertIn("keeping last_run", err)

    def test_clean_run_is_not_flagged_as_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _state_file = self._setup(tmp, {"last_run": self.OLD, "seen": {}})
            self._scan(cfg, self._ok)
            self.assertFalse(self._digest(cfg)["window_held"])

    def test_held_window_is_re_covered_by_the_next_run(self):
        # The consequence that matters: whatever was pushed during the failed
        # run is still inside the window the next run asks for.
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _state_file = self._setup(tmp, {"last_run": self.OLD, "seen": {}})
            self._scan(cfg, self._boom)
            self._scan(cfg, self._ok)
            self.assertEqual(self._digest(cfg)["pushed_since"], self.OLD[:10])


class GateInvariantTests(unittest.TestCase):
    """The gate is sacred: there is no auto-submit / batch-approve / scheduler path."""

    def test_no_submit_subcommand_or_outbound_helper(self):
        source = _MOD_PATH.read_text(encoding="utf-8").lower()
        # No subcommand that performs an outbound submission.
        for banned in ('add_parser("submit', "add_parser('submit", "auto-submit"):
            self.assertNotIn(banned, source)
        # No batch-approve / scheduler affordance.
        for banned in ("batch_approve", "batch-approve", "--yes", "schedule"):
            self.assertNotIn(banned, source)
        # The only gh helper writes nothing outbound: assert the module never
        # issues a comment/PR-creating gh call.
        for banned in (
            "issues/{",
            "/comments",
            "adddiscussioncomment",
            "pr create",
            "pull-request",
        ):
            self.assertNotIn(banned, source)

    def test_subcommands_are_only_scan_mark_submitted_log(self):
        parser_src = _MOD_PATH.read_text(encoding="utf-8")
        self.assertIn('add_parser("scan"', parser_src)
        self.assertIn('add_parser("mark-submitted"', parser_src)
        self.assertIn('add_parser("log"', parser_src)


if __name__ == "__main__":
    unittest.main()
