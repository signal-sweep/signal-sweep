#!/usr/bin/env python3
"""Offline unit tests for thread-sweep (stdlib unittest only).

No network, no live gh: gh_graphql is patched to return fixture payloads. The
shared dedup/seen-store/density plumbing lives in sweepcore and is covered by
modules/test_sweepcore.py; these tests cover thread-sweep's own pure helpers
(node_to_candidate, load_config validation) plus the load-bearing NoAutoPost
gate guard that is this project's identity.

Run: python -m unittest discover -s modules/thread_sweep -p 'test_*.py'
"""

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


class FilterCandidatesTests(unittest.TestCase):
    def _cfg(self, **over):
        cfg = {"own_login": "me", "own_repo": "me/proj", "min_stars": 300}
        cfg.update(over)
        return cfg

    def _cand(self, **over):
        cand = {
            "url": "https://github.com/acme/widgets/issues/1",
            "author": "someone",
            "repo": "acme/widgets",
            "stars": 800,
            "lane": "query",
        }
        cand.update(over)
        return cand

    def test_each_drop_reason_is_counted(self):
        raw = [
            self._cand(),  # kept
            self._cand(),  # same url again -> dup
            self._cand(url="https://x/posted"),  # in posted ledger
            self._cand(url="https://x/seen"),  # in seen store
            self._cand(url="https://x/own-author", author="me"),  # own login
            self._cand(url="https://x/own-repo", repo="me/proj"),  # own repo
            self._cand(url="https://x/small", stars=10),  # under star floor
        ]
        kept, dropped = ts.filter_candidates(
            raw, {"https://x/seen": "2026-01-01"}, {"https://x/posted"}, self._cfg()
        )
        self.assertEqual([c["url"] for c in kept], [raw[0]["url"]])
        self.assertEqual(
            dropped, {"seen": 1, "posted": 1, "stars": 1, "own": 2, "dup": 1}
        )

    def test_watchlist_lane_bypasses_star_floor(self):
        raw = [self._cand(stars=1, lane="watchlist")]
        kept, dropped = ts.filter_candidates(raw, {}, set(), self._cfg())
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped["stars"], 0)

    def test_pure_no_input_mutation(self):
        seen = {"https://x/seen": "2026-01-01"}
        ts.filter_candidates([self._cand()], seen, set(), self._cfg())
        self.assertEqual(seen, {"https://x/seen": "2026-01-01"})


class CapPerRepoTests(unittest.TestCase):
    def test_overflow_is_capped_and_counted(self):
        kept = [{"repo": "acme/widgets", "url": f"https://x/{i}"} for i in range(3)] + [
            {"repo": "other/repo", "url": "https://x/o"}
        ]
        dropped = {}
        capped = ts.cap_per_repo(kept, 2, dropped)
        self.assertEqual(len(capped), 3)  # 2 from acme + 1 from other
        self.assertEqual(dropped["repo_cap"], 1)


class SearchWindowTests(unittest.TestCase):
    def test_query_uses_inclusive_date_boundary(self):
        """GitHub date qualifiers are day-granular: '>' would skip everything
        created later on the last-run day itself, so the query must use '>='."""
        captured = []

        def fake_graphql(query, **variables):
            captured.append(variables)
            return {"data": {"search": {"nodes": []}}}, None

        cfg = {
            "own_repo": "me/proj",
            "queries": {"memory": {"phrases": ["agent memory"]}},
            "per_query": 5,
        }
        with mock.patch.object(ts, "gh_graphql", side_effect=fake_graphql):
            ts.search_lane(cfg, "2026-07-01", [])
        self.assertTrue(captured)
        for variables in captured:
            self.assertIn("created:>=2026-07-01", variables["q"])
            self.assertNotIn("created:>2026-07-01", variables["q"])


class BlackoutHoldsWindowTests(unittest.TestCase):
    """A scan where every request fails must not advance last_run — advancing
    would silently skip the failed window forever."""

    def _run_scan(self, tmp, graphql):
        cfg = {
            "own_login": "me",
            "own_repo": "me/proj",
            "queries": {"memory": {"phrases": ["agent memory"]}},
            "state_dir": str(Path(tmp) / "state"),
            "candidates_file": str(Path(tmp) / "candidates.json"),
        }
        cfg_path = Path(tmp) / "config.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        state_file = Path(tmp) / "state" / "sweep_state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        old_stamp = "2026-07-01T00:00:00+00:00"
        state_file.write_text(
            json.dumps({"last_run": old_stamp, "seen": {}}), encoding="utf-8"
        )
        args = argparse.Namespace(
            config=str(cfg_path), days=None, limit=None, dry_run=False
        )
        with mock.patch.object(ts, "gh_graphql", side_effect=graphql):
            ts.cmd_scan(args)
        return old_stamp, json.loads(state_file.read_text(encoding="utf-8"))

    def test_total_failure_keeps_last_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, state = self._run_scan(
                tmp, lambda query, **kw: (None, "HTTP 500 boom")
            )
            self.assertEqual(state["last_run"], old)

    def test_clean_empty_scan_advances_last_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, state = self._run_scan(
                tmp, lambda query, **kw: ({"data": {"search": {"nodes": []}}}, None)
            )
            self.assertNotEqual(state["last_run"], old)


if __name__ == "__main__":
    unittest.main()
