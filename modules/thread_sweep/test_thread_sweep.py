#!/usr/bin/env python3
"""Offline unit tests for thread-sweep (stdlib unittest only).

No network, no live gh: gh_graphql is patched to return fixture payloads. The
shared dedup/seen-store/density plumbing lives in sweepcore and is covered by
modules/test_sweepcore.py; these tests cover thread-sweep's own pure helpers
(node_to_candidate, load_config validation) plus the load-bearing NoAutoPost
gate guard that is this project's identity.

Run: python -m unittest discover -s modules/thread_sweep -p 'test_*.py'
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

# Import the module by path so the test runs from any cwd. thread_sweep itself
# adds modules/ to sys.path to find sweepcore; replicate that here first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import thread_sweep as ts  # noqa: E402


class NodeToCandidateTests(unittest.TestCase):
    def _node(self, **over):
        node = {
            "url": "https://github.com/acme/widgets/issues/7",
            "title": "How do I wire memory?",
            "createdAt": "2026-06-10T00:00:00Z",
            "bodyText": "first line\nsecond line\r third",
            "state": "OPEN",
            "repository": {"nameWithOwner": "acme/widgets", "stargazerCount": 800},
            "comments": {"totalCount": 3},
            "isAnswered": False,
            "author": {"login": "someone"},
        }
        node.update(over)
        return node

    def test_maps_fields_into_candidate_schema(self):
        cand = ts.node_to_candidate(self._node(), "memory", "query", "MEMORY.md")
        self.assertEqual(cand["url"], "https://github.com/acme/widgets/issues/7")
        self.assertEqual(cand["repo"], "acme/widgets")
        self.assertEqual(cand["stars"], 800)
        self.assertEqual(cand["comments"], 3)
        self.assertIs(cand["is_answered"], False)
        self.assertEqual(cand["author"], "someone")
        self.assertEqual(cand["pattern"], "memory")
        self.assertEqual(cand["lane"], "query")
        self.assertEqual(cand["answers_with"], "MEMORY.md")

    def test_snippet_is_newline_flattened_and_truncated(self):
        long_body = "x" * 1000
        cand = ts.node_to_candidate(self._node(bodyText=long_body), "p", "query")
        self.assertEqual(len(cand["snippet"]), 500)
        # CR/LF in the body are flattened to spaces (kept on one line for digest).
        cand2 = ts.node_to_candidate(self._node(), "p", "query")
        self.assertNotIn("\n", cand2["snippet"])
        self.assertNotIn("\r", cand2["snippet"])

    def test_missing_url_returns_none(self):
        self.assertIsNone(ts.node_to_candidate({"title": "no url"}, "p", "query"))
        self.assertIsNone(ts.node_to_candidate(None, "p", "query"))

    def test_missing_repo_and_author_default_safely(self):
        cand = ts.node_to_candidate(
            {"url": "https://x/1", "repository": None, "author": None}, "p", "query"
        )
        self.assertEqual(cand["repo"], "")
        self.assertEqual(cand["author"], "")
        self.assertEqual(cand["stars"], 0)


class LoadConfigTests(unittest.TestCase):
    def _write(self, tmp, cfg):
        p = Path(tmp) / "config.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")
        return str(p)

    def _valid(self):
        return {
            "own_login": "me",
            "own_repo": "me/proj",
            "queries": {"memory": {"phrases": ["agent memory"]}},
        }

    def test_valid_config_gets_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ts.load_config(self._write(tmp, self._valid()))
            self.assertEqual(cfg["min_stars"], ts.DEFAULTS["min_stars"])
            self.assertEqual(cfg["state_dir"], "state")

    def test_missing_required_key_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = self._valid()
            del bad["own_repo"]
            with self.assertRaises(SystemExit):
                ts.load_config(self._write(tmp, bad))

    def test_queries_must_have_nonempty_phrases(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = self._valid()
            bad["queries"] = {"memory": {"phrases": []}}
            with self.assertRaises(SystemExit):
                ts.load_config(self._write(tmp, bad))

    def test_missing_config_file_exits(self):
        with self.assertRaises(SystemExit):
            ts.load_config("does/not/exist.json")


class StatePathsTests(unittest.TestCase):
    def test_state_paths_derive_from_state_dir(self):
        sdir, state_file, ledger = ts.state_paths({"state_dir": "st"})
        self.assertEqual(Path(state_file).name, "sweep_state.json")
        self.assertEqual(Path(ledger).name, "posted_ledger.jsonl")
        self.assertEqual(Path(sdir).name, "st")


class NoAutoPostTests(unittest.TestCase):
    """The gate is the project's identity: discovery + drafting only. There must
    be NO code path that posts a comment, batch-approves, or schedules. The
    mark-posted subcommand only RECORDS a post the human already made."""

    def test_no_outbound_or_scheduler_token_in_source(self):
        src = Path(ts.__file__).read_text(encoding="utf-8")
        banned = [
            "issues/{",  # gh issue-comment REST path
            "addDiscussionComment",  # discussion-comment GraphQL mutation
            "mutation",  # any GraphQL mutation (this module only queries)
            "--auto",
            "auto_post",
            "auto-post",
            "batch_approve",
            "batch-approve",
            "schedule",
            "cron",
            "issue comment",
            "pr create",
            "-X POST",
            "--method POST",
        ]
        for token in banned:
            self.assertNotIn(
                token,
                src,
                f"outbound/auto-post/scheduler token {token!r} must not appear",
            )

    def test_subcommands_are_only_scan_density_mark_posted(self):
        src = Path(ts.__file__).read_text(encoding="utf-8")
        self.assertIn('add_parser("scan"', src)
        self.assertIn('add_parser("density"', src)
        self.assertIn('add_parser("mark-posted"', src)
        # No submit/post/comment outbound subcommand.
        for banned in ('add_parser("submit', 'add_parser("post', 'add_parser("comment'):
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()
