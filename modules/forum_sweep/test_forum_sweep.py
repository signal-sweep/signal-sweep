#!/usr/bin/env python3
"""Offline unit tests for forum-sweep (stdlib unittest only).

No network: the HTTP fetch (sweepcore.http_get) is never called here — these
tests exercise forum-sweep's own pure helpers (make_candidate, _within_window,
load_config validation) and the load-bearing NoAutoPost gate guard. The shared
dedup/seen-store/density plumbing lives in sweepcore and is covered by
modules/test_sweepcore.py.

Run: python -m unittest discover -s modules/forum_sweep -p 'test_*.py'
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# forum_sweep adds modules/ to sys.path for sweepcore; replicate before import.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import forum_sweep as fs  # noqa: E402


class MakeCandidateTests(unittest.TestCase):
    def test_shared_schema_fields(self):
        cand = fs.make_candidate(
            url="https://news.ycombinator.com/item?id=1",
            title="Ask HN: agent memory?",
            created="2026-06-10T00:00:00Z",
            source="news.ycombinator.com",
            score_or_stars=42,
            comments=7,
            snippet="some body text",
            pattern="memory",
            lane="hn",
        )
        for key in (
            "url",
            "title",
            "created",
            "source",
            "score_or_stars",
            "comments",
            "snippet",
            "pattern",
            "lane",
        ):
            self.assertIn(key, cand)
        self.assertEqual(cand["score_or_stars"], 42)
        self.assertEqual(cand["lane"], "hn")

    def test_snippet_flattened_and_truncated(self):
        cand = fs.make_candidate(
            "u", "t", "", "src", 0, 0, "line1\nline2\r line3" + "y" * 1000, "p", "l"
        )
        self.assertNotIn("\n", cand["snippet"])
        self.assertNotIn("\r", cand["snippet"])
        self.assertEqual(len(cand["snippet"]), 500)

    def test_none_fields_coerced_to_safe_defaults(self):
        cand = fs.make_candidate("u", None, None, None, None, None, None, None, None)
        self.assertEqual(cand["title"], "")
        self.assertEqual(cand["source"], "")
        self.assertEqual(cand["score_or_stars"], 0)
        self.assertEqual(cand["comments"], 0)
        self.assertEqual(cand["snippet"], "")


class WithinWindowTests(unittest.TestCase):
    def setUp(self):
        self.since = datetime(2026, 6, 1, tzinfo=timezone.utc)

    def test_after_window_kept(self):
        self.assertTrue(fs._within_window("2026-06-15T00:00:00Z", self.since))

    def test_before_window_dropped(self):
        self.assertFalse(fs._within_window("2026-05-01T00:00:00Z", self.since))

    def test_missing_timestamp_kept_failopen(self):
        self.assertTrue(fs._within_window("", self.since))
        self.assertTrue(fs._within_window(None or "", self.since))

    def test_unparseable_timestamp_kept_failopen(self):
        self.assertTrue(fs._within_window("not-a-date", self.since))

    def test_naive_timestamp_treated_as_utc(self):
        # A timestamp without tz that is clearly after the window stays kept.
        later = (self.since + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S")
        self.assertTrue(fs._within_window(later, self.since))


class LoadConfigTests(unittest.TestCase):
    def _write(self, tmp, cfg):
        p = Path(tmp) / "config.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")
        return str(p)

    def _valid(self):
        return {
            "subject": "my-project",
            "query_groups": {"memory": ["agent memory"]},
            "sources": {"discourse": {"instances": []}},
        }

    def test_valid_config_gets_defaults_and_flattens_thresholds(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._valid()
            cfg["thresholds"] = {"per_source_cap": 9, "hn_min_points": 11}
            loaded = fs.load_config(self._write(tmp, cfg))
            self.assertEqual(loaded["per_source_cap"], 9)
            self.assertEqual(loaded["hn_min_points"], 11)
            self.assertEqual(loaded["state_dir"], "state")

    def test_missing_required_key_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = self._valid()
            del bad["sources"]
            with self.assertRaises(SystemExit):
                fs.load_config(self._write(tmp, bad))

    def test_empty_query_group_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = self._valid()
            bad["query_groups"] = {"memory": []}
            with self.assertRaises(SystemExit):
                fs.load_config(self._write(tmp, bad))

    def test_missing_config_file_exits(self):
        with self.assertRaises(SystemExit):
            fs.load_config("does/not/exist.json")


class StatePathsTests(unittest.TestCase):
    def test_state_paths_derive_from_state_dir(self):
        _sdir, state_file, ledger = fs.state_paths({"state_dir": "st"})
        self.assertEqual(Path(state_file).name, "forum_sweep_state.json")
        self.assertEqual(Path(ledger).name, "forum_sweep_log.jsonl")


class RedditOptInTests(unittest.TestCase):
    def test_reddit_adapter_disabled_by_default_makes_no_call(self):
        # No sources.reddit.enabled flag -> the adapter returns [] without ever
        # touching the network (discovery-only, opt-in). If it tried to fetch,
        # http_get is unmocked and would attempt a real call; returning [] first
        # proves the opt-in gate short-circuits.
        cfg = {"sources": {"reddit": {"subs": ["test"]}}, "query_groups": {"p": ["x"]}}
        since = datetime(2026, 6, 1, tzinfo=timezone.utc)
        self.assertEqual(fs.reddit_adapter(cfg, since, []), [])

    def test_hn_adapter_disabled_by_default_makes_no_call(self):
        cfg = {"sources": {"hn": {}}, "query_groups": {"p": ["x"]}}
        since = datetime(2026, 6, 1, tzinfo=timezone.utc)
        self.assertEqual(fs.hn_adapter(cfg, since, []), [])


class NoAutoPostTests(unittest.TestCase):
    """Discovery + drafting only. No outbound-post, batch-approve, or scheduler
    path. mark-posted only RECORDS a post the human already made, and the Reddit
    lane is explicitly discovery-only."""

    def test_no_outbound_or_scheduler_token_in_source(self):
        src = Path(fs.__file__).read_text(encoding="utf-8")
        banned = [
            "mutation",
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
        src = Path(fs.__file__).read_text(encoding="utf-8")
        self.assertIn('add_parser("scan"', src)
        self.assertIn('add_parser("density"', src)
        self.assertIn('add_parser("mark-posted"', src)
        for banned in ('add_parser("submit', 'add_parser("post', 'add_parser("comment'):
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()
