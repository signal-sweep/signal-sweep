#!/usr/bin/env python3
"""Offline unit tests for forum-sweep (stdlib unittest only).

No network: the HTTP fetch (sweepcore.http_get) is never called here — these
tests exercise forum-sweep's own pure helpers (make_candidate, _within_window,
load_config validation) and the load-bearing NoAutoPost gate guard. The shared
dedup/seen-store/density plumbing lives in sweepcore and is covered by
modules/test_sweepcore.py.

Run: python -m unittest discover -s modules/forum_sweep -p 'test_*.py'
"""

import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

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


class DiscourseSnippetTests(unittest.TestCase):
    def test_snippet_comes_from_posts_blurb_when_topics_carry_none(self):
        # The human-readable search blurb lives on posts[] keyed by topic_id;
        # topics[] only carry an excerpt on instances configured to include
        # one. The adapter must join the two or snippets go empty on many
        # instances (observed live on instances that omit topic excerpts).
        payload = {
            "topics": [
                {
                    "id": 42,
                    "slug": "agent-memory",
                    "title": "Agent memory question",
                    "created_at": "2026-07-01T00:00:00Z",
                    "posts_count": 1,
                    "like_count": 0,
                }
            ],
            "posts": [{"topic_id": 42, "blurb": "How do I keep agent memory current?"}],
        }
        cfg = {
            "sources": {"discourse": {"instances": ["forum.example.com"]}},
            "query_groups": {"memory": ["agent memory"]},
        }
        since = datetime(2026, 6, 1, tzinfo=timezone.utc)
        with mock.patch.object(fs, "http_get_json", return_value=payload):
            results = fs.discourse_adapter(cfg, since, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["snippet"], "How do I keep agent memory current?")


class RedditTimeParamTests(unittest.TestCase):
    def test_smallest_covering_bucket(self):
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)
        cases = [(3, "week"), (20, "month"), (90, "year"), (400, "all")]
        for days, expected in cases:
            since = now - timedelta(days=days)
            self.assertEqual(fs._reddit_time_param(since, now), expected)


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


class ScanHarness:
    """Shared scaffolding for the last_run tests: a temp config + state file, a
    fake adapter with a controllable outcome, and a one-call scan."""

    OLD = {
        "discourse": "2026-07-01T00:00:00+00:00",
        "hn": "2026-07-02T00:00:00+00:00",
        "lobsters": "2026-07-03T00:00:00+00:00",
        "reddit": "2026-07-04T00:00:00+00:00",
    }

    def _setup(self, tmp, state_obj, sources=None):
        cfg = {
            "subject": "my-project",
            "query_groups": {"memory": ["agent memory"]},
            "sources": sources or {"discourse": {"instances": []}},
            "state_dir": str(Path(tmp) / "state"),
            "candidates_file": str(Path(tmp) / "candidates.json"),
            "request_delay_seconds": 0,
        }
        cfg_path = Path(tmp) / "config.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        state_file = Path(tmp) / "state" / "forum_sweep_state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state_obj), encoding="utf-8")
        return cfg_path, state_file

    def _adapter(self, hits=1, error=None, seen_since=None, fetched=True):
        """Fake adapter: records the window it was handed, reports whether a
        request actually came back (`fetched`), optionally appends an error, and
        returns `hits` candidates. `fetched=False` is the lane that never made a
        request at all — a disabled source, or one with nothing configured."""

        def adapter(cfg, since_dt, report):
            if seen_since is not None:
                seen_since.append(since_dt)
            if fetched:
                report.fetch_ok()
            if error:
                report.append(error)
            return [
                fs.make_candidate(
                    f"https://example.test/{n}",
                    "t",
                    "2026-07-20T00:00:00Z",
                    "example.test",
                    1,
                    0,
                    "s",
                    "memory",
                    "hn",
                )
                for n in range(hits)
            ]

        return adapter

    def _scan(self, cfg_path, source, adapters, days=None):
        args = argparse.Namespace(
            config=str(cfg_path), source=source, days=days, limit=None, dry_run=False
        )
        with mock.patch.dict(fs.ADAPTERS, adapters):
            fs.cmd_scan(args)

    def _marks(self, state_file):
        return json.loads(state_file.read_text(encoding="utf-8"))["last_run_by_source"]


class PerSourceLastRunTests(ScanHarness, unittest.TestCase):
    """last_run is tracked per source. One shared marker meant a single-source
    scan advanced the window for all four lanes, so the three that never ran
    silently skipped everything published in the gap."""

    def test_single_source_scan_leaves_other_sources_last_run_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(
                tmp, {"last_run_by_source": dict(self.OLD), "seen": {}}
            )
            self._scan(cfg_path, "hn", {"hn": self._adapter(hits=1)})
            after = json.loads(state_file.read_text(encoding="utf-8"))
            marks = after["last_run_by_source"]
            self.assertNotEqual(marks["hn"], self.OLD["hn"])
            for other in ("discourse", "lobsters", "reddit"):
                self.assertEqual(marks[other], self.OLD[other])

    def test_unscanned_source_keeps_its_window_on_the_next_scan(self):
        # The damage the shared marker did: after scanning HN, Lobsters must
        # still be scanned from ITS old stamp, not from the advanced one.
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, _state_file = self._setup(
                tmp, {"last_run_by_source": dict(self.OLD), "seen": {}}
            )
            self._scan(cfg_path, "hn", {"hn": self._adapter(hits=1)})
            windows = []
            self._scan(
                cfg_path,
                "lobsters",
                {"lobsters": self._adapter(hits=0, seen_since=windows)},
            )
            self.assertEqual(len(windows), 1)
            self.assertEqual(windows[0], datetime.fromisoformat(self.OLD["lobsters"]))

    def test_migrate_state_seeds_every_source_from_the_shared_value(self):
        legacy = "2026-06-15T00:00:00+00:00"
        migrated = fs.migrate_state(
            {"last_run": legacy, "seen": {"https://example.test/1": "2026-06-15"}}
        )
        self.assertEqual(
            migrated["last_run_by_source"], {name: legacy for name in fs.SOURCES}
        )
        # No data loss: the seen-store survives and the ambiguous legacy key is
        # gone, so there is only ever one authoritative marker per source.
        self.assertEqual(migrated["seen"], {"https://example.test/1": "2026-06-15"})
        self.assertNotIn("last_run", migrated)

    def test_migrate_state_is_idempotent_and_keeps_existing_per_source_marks(self):
        state = {
            "last_run": "2026-06-15T00:00:00+00:00",
            "last_run_by_source": {"hn": "2026-07-09T00:00:00+00:00"},
            "seen": {},
        }
        once = fs.migrate_state(state)
        twice = fs.migrate_state(json.loads(json.dumps(once)))
        self.assertEqual(once["last_run_by_source"]["hn"], "2026-07-09T00:00:00+00:00")
        self.assertEqual(
            once["last_run_by_source"]["discourse"], "2026-06-15T00:00:00+00:00"
        )
        self.assertEqual(twice["last_run_by_source"], once["last_run_by_source"])

    def test_first_run_with_no_state_uses_default_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, _state_file = self._setup(tmp, {"seen": {}})
            windows = []
            self._scan(
                cfg_path, "hn", {"hn": self._adapter(hits=0, seen_since=windows)}
            )
            age = datetime.now(timezone.utc) - windows[0]
            self.assertAlmostEqual(age.total_seconds(), 14 * 86400, delta=120)

    def test_legacy_shared_state_migrates_through_a_scan_without_data_loss(self):
        legacy = "2026-06-15T00:00:00+00:00"
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(
                tmp,
                {
                    "last_run": legacy,
                    "seen": {"https://example.test/old": "2026-06-15"},
                },
            )
            windows = []
            self._scan(
                cfg_path, "hn", {"hn": self._adapter(hits=1, seen_since=windows)}
            )
            # The scanned lane inherited the shared value as its window rather
            # than resetting to the 14-day default...
            self.assertEqual(windows[0], datetime.fromisoformat(legacy))
            after = json.loads(state_file.read_text(encoding="utf-8"))
            marks = after["last_run_by_source"]
            # ...the three unscanned lanes are seeded with it, not reset...
            for other in ("discourse", "lobsters", "reddit"):
                self.assertEqual(marks[other], legacy)
            self.assertNotEqual(marks["hn"], legacy)
            # ...and the seen-store came through intact.
            self.assertIn("https://example.test/old", after["seen"])
            self.assertNotIn("last_run", after)

    def test_blacked_out_source_holds_its_window_while_a_clean_one_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(
                tmp, {"last_run_by_source": dict(self.OLD), "seen": {}}
            )
            self._scan(
                cfg_path,
                "all",
                {
                    "discourse": self._adapter(
                        hits=0, error="discourse: HTTP 503", fetched=False
                    ),
                    "hn": self._adapter(hits=1),
                    "lobsters": self._adapter(hits=0),
                    "reddit": self._adapter(hits=0),
                },
            )
            marks = json.loads(state_file.read_text(encoding="utf-8"))[
                "last_run_by_source"
            ]
            self.assertEqual(marks["discourse"], self.OLD["discourse"])
            for advanced in ("hn", "lobsters", "reddit"):
                self.assertNotEqual(marks[advanced], self.OLD[advanced])

    def test_total_blackout_keeps_every_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(
                tmp, {"last_run_by_source": dict(self.OLD), "seen": {}}
            )
            self._scan(
                cfg_path,
                "all",
                {
                    name: self._adapter(
                        hits=0, error=f"{name}: HTTP 503", fetched=False
                    )
                    for name in fs.SOURCES
                },
            )
            marks = json.loads(state_file.read_text(encoding="utf-8"))[
                "last_run_by_source"
            ]
            self.assertEqual(marks, self.OLD)

    def test_dry_run_does_not_advance_any_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(
                tmp, {"last_run_by_source": dict(self.OLD), "seen": {}}
            )
            args = argparse.Namespace(
                config=str(cfg_path), source="hn", days=None, limit=None, dry_run=True
            )
            with mock.patch.dict(fs.ADAPTERS, {"hn": self._adapter(hits=1)}):
                fs.cmd_scan(args)
            marks = json.loads(state_file.read_text(encoding="utf-8"))[
                "last_run_by_source"
            ]
            self.assertEqual(marks, self.OLD)


class AdvanceOnlyOnCleanFetchTests(ScanHarness, unittest.TestCase):
    """Being selected is a request to scan, not proof the scan happened.

    Deriving the advance set from what was ASKED for meant a lane that errored,
    crashed, or never made a request still had its marker moved over a window it
    had not covered — the same silent window loss as the old shared marker, one
    lane at a time. A marker is earned by a fetch that came back.
    """

    def test_errored_lane_holds_its_window_even_though_it_returned_items(self):
        # The instance that failed is exactly the one whose items are missing.
        # Partial retrieval is not coverage, so the marker must not move.
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(
                tmp, {"last_run_by_source": dict(self.OLD), "seen": {}}
            )
            self._scan(
                cfg_path,
                "discourse",
                {"discourse": self._adapter(hits=2, error="discourse two: HTTP 503")},
            )
            marks = self._marks(state_file)
            self.assertEqual(marks["discourse"], self.OLD["discourse"])

    def test_crashed_lane_holds_its_window(self):
        def boom(cfg, since_dt, report):
            raise ValueError("unexpected shape")

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(
                tmp, {"last_run_by_source": dict(self.OLD), "seen": {}}
            )
            self._scan(cfg_path, "hn", {"hn": boom})
            self.assertEqual(self._marks(state_file)["hn"], self.OLD["hn"])

    def test_lane_that_never_made_a_request_holds_its_window(self):
        # No error, no candidates, no fetch: the shape of a skipped lane. Silent
        # emptiness is the dangerous case — nothing in the run reads as failure.
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(
                tmp, {"last_run_by_source": dict(self.OLD), "seen": {}}
            )
            self._scan(cfg_path, "hn", {"hn": self._adapter(hits=0, fetched=False)})
            self.assertEqual(self._marks(state_file)["hn"], self.OLD["hn"])

    def test_disabled_and_unconfigured_lanes_hold_while_a_clean_one_advances(self):
        # End-to-end through the real adapters: hn and reddit are opt-in and off,
        # lobsters has no tags, so three of the four lanes return [] without ever
        # reaching the network. Only the stubbed discourse lane earns a marker.
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(
                tmp, {"last_run_by_source": dict(self.OLD), "seen": {}}
            )
            self._scan(cfg_path, "all", {"discourse": self._adapter(hits=1)})
            marks = self._marks(state_file)
            self.assertNotEqual(marks["discourse"], self.OLD["discourse"])
            for skipped in ("hn", "lobsters", "reddit"):
                self.assertEqual(marks[skipped], self.OLD[skipped])

    def test_successful_empty_result_still_advances(self):
        # The other side of the fix: a fetch that came back holding nothing is a
        # real, covered, empty window. Holding it would re-scan forever.
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(
                tmp, {"last_run_by_source": dict(self.OLD), "seen": {}}
            )
            self._scan(cfg_path, "hn", {"hn": self._adapter(hits=0, fetched=True)})
            self.assertNotEqual(self._marks(state_file)["hn"], self.OLD["hn"])

    def test_failed_lane_next_run_re_covers_the_missed_window(self):
        # The consequence that matters: whatever was published during the failed
        # run is still inside the window the next run asks for.
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, _state_file = self._setup(
                tmp, {"last_run_by_source": dict(self.OLD), "seen": {}}
            )
            self._scan(
                cfg_path, "hn", {"hn": self._adapter(hits=3, error="hn: HTTP 429")}
            )
            windows = []
            self._scan(
                cfg_path, "hn", {"hn": self._adapter(hits=0, seen_since=windows)}
            )
            self.assertEqual(windows[0], datetime.fromisoformat(self.OLD["hn"]))

    def test_held_lane_is_named_in_the_digest_and_on_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, _state_file = self._setup(
                tmp, {"last_run_by_source": dict(self.OLD), "seen": {}}
            )
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self._scan(
                    cfg_path,
                    "hn",
                    {"hn": self._adapter(hits=0, error="hn: HTTP 503", fetched=False)},
                )
            self.assertIn("hn", err.getvalue())
            payload = json.loads(
                (Path(tmp) / "candidates.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["sources_held"], ["hn"])


class FetchAccountingTests(ScanHarness, unittest.TestCase):
    """The same rule end-to-end through the real Discourse adapter, with only
    the HTTP layer faked. The fake-adapter tests above report their own outcome;
    these prove the accounting is actually wired into the fetch path, so a lane
    cannot be held forever (or advanced wrongly) by a miscount."""

    HOSTS = ["good.example", "bad.example"]

    def _payload(self):
        return json.dumps(
            {
                "topics": [
                    {
                        "id": 42,
                        "slug": "agent-memory",
                        "title": "Agent memory question",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "posts_count": 1,
                        "like_count": 0,
                    }
                ],
                "posts": [{"topic_id": 42, "blurb": "how do I keep it current?"}],
            }
        )

    def _http(self, failing_hosts=()):
        def http_get(url, **kwargs):
            if any(host in url for host in failing_hosts):
                return 503, "", "HTTP 503"
            return 200, self._payload(), None

        return http_get

    def _run(self, tmp, hosts, failing_hosts=()):
        cfg_path, state_file = self._setup(
            tmp,
            {"last_run_by_source": dict(self.OLD), "seen": {}},
            sources={"discourse": {"instances": hosts}},
        )
        args = argparse.Namespace(
            config=str(cfg_path),
            source="discourse",
            days=None,
            limit=None,
            dry_run=False,
        )
        with mock.patch.object(fs, "http_get", self._http(failing_hosts)):
            fs.cmd_scan(args)
        return state_file

    def test_completed_fetch_advances_the_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = self._run(tmp, ["good.example"])
            self.assertNotEqual(
                self._marks(state_file)["discourse"], self.OLD["discourse"]
            )

    def test_failed_fetch_holds_the_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = self._run(tmp, ["bad.example"], failing_hosts=["bad.example"])
            self.assertEqual(
                self._marks(state_file)["discourse"], self.OLD["discourse"]
            )

    def test_one_failed_instance_holds_the_lane_the_others_retrieved(self):
        # The reported defect, end to end: two instances, one 503s. Candidates
        # come back from the healthy one, so the run looks productive — and the
        # window belonging to the instance that failed is exactly what would be
        # lost if the marker moved on the strength of that.
        with tempfile.TemporaryDirectory() as tmp:
            state_file = self._run(tmp, self.HOSTS, failing_hosts=["bad.example"])
            payload = json.loads(
                (Path(tmp) / "candidates.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(payload["candidates"]), 1)
            self.assertEqual(payload["sources_held"], ["discourse"])
            self.assertEqual(
                self._marks(state_file)["discourse"], self.OLD["discourse"]
            )


class NarrowedWindowTests(ScanHarness, unittest.TestCase):
    """A window that starts after the stored marker leaves a gap in front of it.
    Stamping `now` after such a run would swallow that gap silently and
    permanently, so a narrowed run does not earn the marker.

    Two routes narrow a window: an explicit `--days N`, and a marker that no
    longer parses (the lane drops back to the default window). Same shape, same
    rule — the stored marker stands.
    """

    def test_narrow_days_override_does_not_swallow_the_uncovered_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            cfg_path, state_file = self._setup(
                tmp, {"last_run_by_source": {"hn": old}, "seen": {}}
            )
            self._scan(cfg_path, "hn", {"hn": self._adapter(hits=1)}, days=2)
            self.assertEqual(self._marks(state_file)["hn"], old)

    def test_the_gap_is_still_covered_by_the_next_default_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            cfg_path, _state_file = self._setup(
                tmp, {"last_run_by_source": {"hn": old}, "seen": {}}
            )
            self._scan(cfg_path, "hn", {"hn": self._adapter(hits=1)}, days=2)
            windows = []
            self._scan(
                cfg_path, "hn", {"hn": self._adapter(hits=0, seen_since=windows)}
            )
            self.assertEqual(windows[0], datetime.fromisoformat(old))

    def test_wide_days_override_covers_the_marker_and_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
            cfg_path, state_file = self._setup(
                tmp, {"last_run_by_source": {"hn": old}, "seen": {}}
            )
            self._scan(cfg_path, "hn", {"hn": self._adapter(hits=1)}, days=30)
            self.assertNotEqual(self._marks(state_file)["hn"], old)

    def test_unreadable_marker_warns_before_falling_back_to_the_default_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, _state_file = self._setup(
                tmp, {"last_run_by_source": {"hn": "last tuesday"}, "seen": {}}
            )
            windows, err = [], io.StringIO()
            with contextlib.redirect_stderr(err):
                self._scan(
                    cfg_path, "hn", {"hn": self._adapter(hits=0, seen_since=windows)}
                )
            self.assertIn("unreadable last_run for hn", err.getvalue())
            age = datetime.now(timezone.utc) - windows[0]
            self.assertAlmostEqual(age.total_seconds(), 14 * 86400, delta=120)

    def test_unreadable_marker_is_not_overwritten_by_an_unearned_now_stamp(self):
        # The other narrowing route, and the same loss: the lane re-windowed to
        # the default because the marker would not parse, so a clean fetch
        # through that window cannot show it reached back to whatever the marker
        # meant. Stamping `now` would bury however much of the gap sat in front
        # of the window AND destroy the evidence that the marker had rotted.
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(
                tmp, {"last_run_by_source": {"hn": "last tuesday"}, "seen": {}}
            )
            with contextlib.redirect_stderr(io.StringIO()):
                self._scan(cfg_path, "hn", {"hn": self._adapter(hits=1)})
            self.assertEqual(self._marks(state_file)["hn"], "last tuesday")

    def test_lane_with_no_stored_marker_still_earns_one(self):
        # The other direction, so the fix above cannot be satisfied by never
        # advancing: absent is not unreadable. A first run has no earlier marker
        # to fall short of, so a fetch that came back — here holding nothing,
        # the real empty window — must still lay a stamp down, or the lane
        # re-scans the default window forever.
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(tmp, {"seen": {}})
            self._scan(cfg_path, "hn", {"hn": self._adapter(hits=0, fetched=True)})
            stamp = self._marks(state_file)["hn"]
            age = datetime.now(timezone.utc) - datetime.fromisoformat(stamp)
            self.assertAlmostEqual(age.total_seconds(), 0, delta=120)


class DryRunWritesNothingTests(unittest.TestCase):
    """A --dry-run scan must be side-effect-free on disk.

    candidates.json is the human's working digest — the file they are part-way
    through triaging. A preview that silently overwrites it (while printing
    "state untouched") destroys the run it was meant to preview, so the file
    must come out byte-identical and the printed summary must say so.
    """

    SENTINEL = '{"candidates": ["hand-triaged, do not clobber"]}'

    def _setup(self, tmp):
        cfg = {
            "subject": "my-project",
            "query_groups": {"memory": ["agent memory"]},
            "sources": {"hn": {"enabled": True}},
            "state_dir": str(Path(tmp) / "state"),
            "candidates_file": str(Path(tmp) / "candidates.json"),
            "request_delay_seconds": 0,
        }
        cfg_path = Path(tmp) / "config.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        cand_path = Path(cfg["candidates_file"])
        cand_path.write_text(self.SENTINEL, encoding="utf-8")
        state_file = Path(tmp) / "state" / "forum_sweep_state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            json.dumps(
                {
                    "last_run_by_source": {"hn": "2026-07-02T00:00:00+00:00"},
                    "seen": {},
                }
            ),
            encoding="utf-8",
        )
        return cfg_path, cand_path, state_file

    @staticmethod
    def _one_hit(cfg, since_dt, errors):
        return [
            fs.make_candidate(
                "https://news.ycombinator.com/item?id=1",
                "Ask HN: agent memory?",
                "2026-07-20T00:00:00Z",
                "news.ycombinator.com",
                12,
                0,
                "body text",
                "memory",
                "hn",
            )
        ]

    def _scan(self, cfg_path, dry_run):
        args = argparse.Namespace(
            config=str(cfg_path), source="hn", days=30, limit=None, dry_run=dry_run
        )
        buf = io.StringIO()
        patched = mock.patch.dict(fs.ADAPTERS, {"hn": self._one_hit})
        with patched, contextlib.redirect_stdout(buf):
            fs.cmd_scan(args)
        return buf.getvalue()

    def test_dry_run_leaves_candidates_file_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, cand_path, state_file = self._setup(tmp)
            before, state_before = cand_path.read_bytes(), state_file.read_bytes()
            out = self._scan(cfg_path, dry_run=True)
            self.assertEqual(cand_path.read_bytes(), before)
            self.assertEqual(state_file.read_bytes(), state_before)
            # A candidate really did survive the filters, so the write path was
            # exercised — the file is intact because the dry run declined to
            # write, not because there was nothing to write.
            self.assertIn("kept=1", out)

    def test_dry_run_output_does_not_claim_a_write_it_did_not_make(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, cand_path, _state = self._setup(tmp)
            out = self._scan(cfg_path, dry_run=True)
            self.assertIn("DRY-RUN", out)
            self.assertNotIn(f"candidates -> {cand_path}", out)
            self.assertIn("would be written to", out)

    def test_real_scan_still_writes_the_candidates_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, cand_path, _state = self._setup(tmp)
            out = self._scan(cfg_path, dry_run=False)
            written = cand_path.read_text(encoding="utf-8")
            self.assertNotEqual(written, self.SENTINEL)
            self.assertEqual(len(json.loads(written)["candidates"]), 1)
            self.assertIn(f"candidates -> {cand_path}", out)


if __name__ == "__main__":
    unittest.main()
