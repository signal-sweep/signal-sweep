#!/usr/bin/env python3
"""Offline unit tests for list-sweep (stdlib unittest only).

Every gh / subprocess call and every network read is mocked. These tests make
NO live calls. Run: python -m unittest discover -s modules/list_sweep -p 'test_*.py'
"""

import importlib.util
import io
import json
import tempfile
import unittest
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

        # One query so each mocked hit is processed once (search_repos is mocked
        # to return the same list per query; collapsing to a single query keeps
        # the dedup-bucket counts deterministic).
        with (
            mock.patch.object(ls, "load_config", return_value=cfg),
            mock.patch.object(ls, "build_queries", return_value=["q"]),
            mock.patch.object(ls, "search_repos", return_value=search_hits),
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
