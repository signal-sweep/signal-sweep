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


def _se_question(**overrides):
    """A live-shape /search/excerpts question item (fields confirmed against
    the real API during build: tags, question_score, is_accepted,
    has_accepted_answer, answer_count, is_answered, question_id, item_type,
    score, last_activity_date, creation_date, body, excerpt, title)."""
    item = {
        "tags": ["python", "agents"],
        "question_score": 2,
        "is_accepted": False,
        "has_accepted_answer": False,
        "answer_count": 2,
        "is_answered": False,
        "question_id": 111,
        "item_type": "question",
        "score": 5,
        "last_activity_date": 1700000500,
        "creation_date": 1700000000,
        "body": "full body text",
        "excerpt": (
            'How do I keep <span class="highlight">agent</span> memory '
            "current&#39;s state?&hellip;"
        ),
        "title": "Agent memory isn&#39;t persisting",
    }
    item.update(overrides)
    return item


def _se_answer(**overrides):
    """A live-shape /search/excerpts answer item: no answer_count/
    has_accepted_answer (question-only fields), carries answer_id instead,
    and `title` still holds the parent question's title (confirmed live)."""
    item = {
        "tags": ["python", "agents"],
        "question_score": 2,
        "is_accepted": True,
        "answer_id": 222,
        "is_answered": True,
        "question_id": 111,
        "item_type": "answer",
        "score": 9,
        "last_activity_date": 1700000600,
        "creation_date": 1700000300,
        "body": "answer body text",
        "excerpt": "Store it in a vector store keyed by session",
        "title": "Agent memory isn't persisting",
    }
    item.update(overrides)
    return item


def _devto_article(**overrides):
    """A live-shape /api/articles item (fields confirmed against the real API
    during build)."""
    art = {
        "id": 1,
        "title": "Building durable agent memory",
        "description": "Patterns for keeping agent memory consistent across sessions",
        "url": "https://dev.to/someone/building-durable-agent-memory-1a2b",
        "path": "/someone/building-durable-agent-memory-1a2b",
        "published_at": "2026-07-20T00:00:00Z",
        "positive_reactions_count": 12,
        "comments_count": 3,
        "tag_list": ["ai", "agents"],
        "tags": "ai, agents",
    }
    art.update(overrides)
    return art


class StackExchangeAdapterTests(unittest.TestCase):
    SINCE = datetime(2026, 6, 1, tzinfo=timezone.utc)

    def _cfg(
        self, sites=("stackoverflow",), min_score=0, enabled=True, query_groups=None
    ):
        return {
            "sources": {
                "stackexchange": {
                    "enabled": enabled,
                    "sites": list(sites),
                    "min_score": min_score,
                }
            },
            "query_groups": query_groups or {"memory": ["agent memory"]},
        }

    def test_disabled_by_default_makes_no_call(self):
        cfg = self._cfg(enabled=False)
        with mock.patch.object(fs, "http_get_json") as mocked:
            results = fs.stackexchange_adapter(cfg, self.SINCE, [])
        mocked.assert_not_called()
        self.assertEqual(results, [])

    def test_no_sites_configured_makes_no_call(self):
        cfg = self._cfg(sites=())
        with mock.patch.object(fs, "http_get_json") as mocked:
            results = fs.stackexchange_adapter(cfg, self.SINCE, [])
        mocked.assert_not_called()
        self.assertEqual(results, [])

    def test_parses_question_and_answer_items_with_correct_urls(self):
        payload = {"items": [_se_question(), _se_answer()]}
        with mock.patch.object(fs, "http_get_json", return_value=payload):
            results = fs.stackexchange_adapter(self._cfg(), self.SINCE, [])
        self.assertEqual(len(results), 2)
        q, a = results
        self.assertEqual(q["url"], "https://stackoverflow.com/q/111")
        self.assertEqual(a["url"], "https://stackoverflow.com/a/222")
        self.assertEqual(q["lane"], "stackexchange")
        self.assertEqual(q["source"], "stackoverflow")
        self.assertEqual(q["pattern"], "memory")

    def test_html_entities_and_highlight_span_stripped(self):
        payload = {"items": [_se_question()]}
        with mock.patch.object(fs, "http_get_json", return_value=payload):
            results = fs.stackexchange_adapter(self._cfg(), self.SINCE, [])
        cand = results[0]
        self.assertEqual(cand["title"], "Agent memory isn't persisting")
        self.assertNotIn("&#39;", cand["title"])
        self.assertNotIn("<span", cand["snippet"])
        self.assertNotIn("&#39;", cand["snippet"])
        self.assertNotIn("&hellip;", cand["snippet"])
        self.assertIn("agent", cand["snippet"].lower())

    def test_comments_field_uses_answer_count_for_questions_zero_for_answers(self):
        payload = {"items": [_se_question(answer_count=4), _se_answer()]}
        with mock.patch.object(fs, "http_get_json", return_value=payload):
            results = fs.stackexchange_adapter(self._cfg(), self.SINCE, [])
        self.assertEqual(results[0]["comments"], 4)
        self.assertEqual(results[1]["comments"], 0)

    def test_is_answered_flows_through_and_boosts_relevance_tier(self):
        # Same answer_count (0) on both so comments contributes equally to
        # each side, isolating is_answered as the only scoring difference.
        payload = {
            "items": [
                _se_question(is_answered=False, answer_count=0),
                _se_question(is_answered=True, answer_count=0, question_id=112),
            ]
        }
        with mock.patch.object(fs, "http_get_json", return_value=payload):
            results = fs.stackexchange_adapter(self._cfg(), self.SINCE, [])
        unanswered, answered = results
        self.assertIs(unanswered["is_answered"], False)
        self.assertIs(answered["is_answered"], True)
        # relevance_tier (sweepcore) reads is_answered as an answer-gap
        # signal for free -- no adapter-side ranking needed.
        self.assertGreater(
            fs.TIER_RANK[fs.relevance_tier(unanswered)],
            fs.TIER_RANK[fs.relevance_tier(answered)],
        )

    def test_min_score_floor_drops_low_score_items(self):
        payload = {"items": [_se_question(score=-1), _se_answer(score=5)]}
        with mock.patch.object(fs, "http_get_json", return_value=payload):
            results = fs.stackexchange_adapter(self._cfg(min_score=0), self.SINCE, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://stackoverflow.com/a/222")

    def test_malformed_rows_skipped_without_killing_the_lane(self):
        payload = {
            "items": [
                "not-a-dict",
                {"item_type": "question"},  # no question_id and no answer_id
                {"item_type": "answer"},  # no answer_id and no question_id
                _se_question(),
            ]
        }
        with mock.patch.object(fs, "http_get_json", return_value=payload):
            results = fs.stackexchange_adapter(self._cfg(), self.SINCE, [])
        self.assertEqual(len(results), 1)

    def test_answer_missing_its_own_id_falls_back_to_the_question_link(self):
        # Real SE responses always carry answer_id on an answer row (confirmed
        # live). If one somehow doesn't, degrading to the parent question's
        # link is the fail-open choice -- a possibly-relevant candidate stays
        # in the digest rather than being silently dropped.
        payload = {"items": [{"item_type": "answer", "question_id": 1, "score": 1}]}
        with mock.patch.object(fs, "http_get_json", return_value=payload):
            results = fs.stackexchange_adapter(self._cfg(), self.SINCE, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://stackoverflow.com/q/1")

    def test_non_list_items_payload_yields_no_candidates(self):
        payload = {"items": "not-a-list"}
        with mock.patch.object(fs, "http_get_json", return_value=payload):
            results = fs.stackexchange_adapter(self._cfg(), self.SINCE, [])
        self.assertEqual(results, [])

    def test_failed_fetch_for_one_site_does_not_raise(self):
        with mock.patch.object(fs, "http_get_json", return_value=None):
            results = fs.stackexchange_adapter(self._cfg(), self.SINCE, [])
        self.assertEqual(results, [])


class StackExchangeBackoffTests(unittest.TestCase):
    """The `backoff` field is a JSON-body throttle hint, distinct from the
    429/503 Retry-After sweepcore.http_get already backs off on. It must be
    honoured (slept on) without holding the lane's earned marker, because the
    fetch that carries it still succeeded (PR #21's rule is about coverage,
    not about a perfectly quiet run)."""

    SINCE = datetime(2026, 6, 1, tzinfo=timezone.utc)
    CFG = {
        "sources": {"stackexchange": {"enabled": True, "sites": ["stackoverflow"]}},
        "query_groups": {"memory": ["agent memory"]},
    }

    def test_backoff_field_triggers_a_stubbed_sleep(self):
        payload = {"items": [], "backoff": 7}
        with mock.patch.object(fs, "http_get_json", return_value=payload):
            with mock.patch.object(fs.time, "sleep") as mock_sleep:
                fs.stackexchange_adapter(self.CFG, self.SINCE, fs.LaneReport())
        mock_sleep.assert_called_once_with(7.0)

    def test_backoff_alone_does_not_mark_the_lane_unclean(self):
        payload = {"items": [], "backoff": 3}
        report = fs.LaneReport()
        report.fetch_ok()  # what the real http_get_json records on a 200
        with mock.patch.object(fs, "http_get_json", return_value=payload):
            with mock.patch.object(fs.time, "sleep"):
                fs.stackexchange_adapter(self.CFG, self.SINCE, report)
        self.assertEqual(len(report), 0)
        self.assertTrue(report.clean)

    def test_non_numeric_backoff_is_ignored_without_crashing(self):
        payload = {"items": [], "backoff": "soon"}
        with mock.patch.object(fs, "http_get_json", return_value=payload):
            with mock.patch.object(fs.time, "sleep") as mock_sleep:
                fs.stackexchange_adapter(self.CFG, self.SINCE, fs.LaneReport())
        mock_sleep.assert_not_called()


class SeSiteUrlTests(unittest.TestCase):
    def test_vanity_domain(self):
        self.assertEqual(fs._se_site_url("stackoverflow"), "https://stackoverflow.com")

    def test_non_vanity_slug_uses_stackexchange_dot_com(self):
        self.assertEqual(fs._se_site_url("ai"), "https://ai.stackexchange.com")
        self.assertEqual(
            fs._se_site_url("some-new-site"), "https://some-new-site.stackexchange.com"
        )


class SeExcerptCleanTests(unittest.TestCase):
    def test_unescapes_entities_and_strips_highlight_span(self):
        raw = 'It doesn&#39;t <span class="highlight">persist</span>&hellip;'
        self.assertEqual(fs._clean_se_excerpt(raw), "It doesn't persist…")

    def test_none_and_empty_are_safe(self):
        self.assertEqual(fs._clean_se_excerpt(""), "")
        self.assertEqual(fs._clean_se_excerpt(None), "")


class TokenOverlapPatternTests(unittest.TestCase):
    GROUPS = {
        "memory": ["agent memory"],
        "hooks": ["destructive command hook guard"],
    }

    def test_returns_the_matching_pattern_at_the_floor(self):
        text = "A deep dive into agent memory architectures"
        self.assertEqual(fs._token_overlap_pattern(text, self.GROUPS), "memory")

    def test_returns_none_below_the_floor(self):
        text = "A single mention of agent tooling, nothing else relevant"
        self.assertIsNone(fs._token_overlap_pattern(text, self.GROUPS))

    def test_case_insensitive(self):
        text = "AGENT MEMORY at scale"
        self.assertEqual(fs._token_overlap_pattern(text, self.GROUPS), "memory")

    def test_picks_the_higher_overlap_group(self):
        text = "A destructive command hook guard also touches agent memory briefly"
        self.assertEqual(fs._token_overlap_pattern(text, self.GROUPS), "hooks")


class DevToAdapterTests(unittest.TestCase):
    SINCE = datetime(2026, 6, 1, tzinfo=timezone.utc)

    def _cfg(self, tags=("ai",), min_reactions=3, enabled=True, query_groups=None):
        return {
            "sources": {
                "devto": {
                    "enabled": enabled,
                    "tags": list(tags),
                    "min_reactions": min_reactions,
                }
            },
            "query_groups": query_groups or {"memory": ["agent memory"]},
        }

    def test_disabled_by_default_makes_no_call(self):
        cfg = self._cfg(enabled=False)
        with mock.patch.object(fs, "http_get_json") as mocked:
            results = fs.devto_adapter(cfg, self.SINCE, [])
        mocked.assert_not_called()
        self.assertEqual(results, [])

    def test_no_tags_configured_makes_no_call(self):
        cfg = self._cfg(tags=())
        with mock.patch.object(fs, "http_get_json") as mocked:
            results = fs.devto_adapter(cfg, self.SINCE, [])
        mocked.assert_not_called()
        self.assertEqual(results, [])

    def test_parses_matching_article_with_shared_candidate_schema(self):
        payload = [_devto_article()]
        with mock.patch.object(fs, "http_get_json", return_value=payload):
            results = fs.devto_adapter(self._cfg(), self.SINCE, [])
        self.assertEqual(len(results), 1)
        cand = results[0]
        self.assertEqual(
            cand["url"], "https://dev.to/someone/building-durable-agent-memory-1a2b"
        )
        self.assertEqual(cand["source"], "dev.to")
        self.assertEqual(cand["lane"], "devto")
        self.assertEqual(cand["score_or_stars"], 12)
        self.assertEqual(cand["comments"], 3)
        self.assertEqual(cand["pattern"], "memory")

    def test_min_reactions_floor_drops_low_reaction_articles(self):
        payload = [_devto_article(positive_reactions_count=1)]
        with mock.patch.object(fs, "http_get_json", return_value=payload):
            results = fs.devto_adapter(self._cfg(min_reactions=3), self.SINCE, [])
        self.assertEqual(results, [])

    def test_window_filters_articles_published_before_since(self):
        old = _devto_article(published_at="2026-01-01T00:00:00Z")
        with mock.patch.object(fs, "http_get_json", return_value=[old]):
            results = fs.devto_adapter(self._cfg(), self.SINCE, [])
        self.assertEqual(results, [])

    def test_token_overlap_floor_drops_unrelated_articles(self):
        unrelated = _devto_article(
            title="A guide to CSS grid layouts",
            description="Learn flexbox and grid for responsive design",
        )
        with mock.patch.object(fs, "http_get_json", return_value=[unrelated]):
            results = fs.devto_adapter(self._cfg(), self.SINCE, [])
        self.assertEqual(results, [])

    def test_malformed_rows_skipped_without_killing_the_lane(self):
        payload = ["not-a-dict", {"title": "no url field"}, _devto_article()]
        with mock.patch.object(fs, "http_get_json", return_value=payload):
            results = fs.devto_adapter(self._cfg(), self.SINCE, [])
        self.assertEqual(len(results), 1)

    def test_non_list_payload_yields_no_candidates(self):
        payload = {"error": "not found"}
        with mock.patch.object(fs, "http_get_json", return_value=payload):
            results = fs.devto_adapter(self._cfg(), self.SINCE, [])
        self.assertEqual(results, [])


class NewSourceEarnedStampTests(ScanHarness, unittest.TestCase):
    """The PR #21 earned-marker rule, plus the shared seen-store/per-source-cap
    pipeline, proven end-to-end for the two new sources through cmd_scan with
    only the HTTP layer faked -- the same shape as FetchAccountingTests uses
    for discourse, extended to stackexchange/devto so the SOURCES/ADAPTERS
    wiring is proven, not just each adapter's own parsing."""

    def _se_body(self, question_id=901, score=5):
        now_epoch = int(datetime.now(timezone.utc).timestamp())
        return json.dumps(
            {
                "items": [
                    {
                        "tags": ["python"],
                        "question_score": 1,
                        "is_accepted": False,
                        "has_accepted_answer": False,
                        "answer_count": 0,
                        "is_answered": False,
                        "question_id": question_id,
                        "item_type": "question",
                        "score": score,
                        "last_activity_date": now_epoch,
                        "creation_date": now_epoch,
                        "body": "b",
                        "excerpt": "agent memory question",
                        "title": "Agent memory",
                    }
                ],
                "has_more": False,
                "quota_max": 300,
                "quota_remaining": 299,
            }
        )

    def _devto_body(self, article_id=1):
        return json.dumps(
            [
                {
                    "id": article_id,
                    "title": "Agent memory patterns",
                    "description": "notes on agent memory",
                    "url": f"https://dev.to/x/agent-memory-{article_id}",
                    "path": f"/x/agent-memory-{article_id}",
                    "published_at": datetime.now(timezone.utc).isoformat(),
                    "positive_reactions_count": 10,
                    "comments_count": 2,
                    "tag_list": ["ai"],
                    "tags": "ai",
                }
            ]
        )

    def _run(self, tmp, source, sources_cfg, http_get_fn, extra_last_run=None):
        last_run = dict(self.OLD)
        if extra_last_run:
            last_run.update(extra_last_run)
        cfg_path, state_file = self._setup(
            tmp, {"last_run_by_source": last_run, "seen": {}}, sources=sources_cfg
        )
        args = argparse.Namespace(
            config=str(cfg_path), source=source, days=None, limit=None, dry_run=False
        )
        with mock.patch.object(fs, "http_get", http_get_fn):
            fs.cmd_scan(args)
        return cfg_path, state_file

    def test_stackexchange_disabled_holds_and_makes_no_network_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(fs, "http_get") as mocked_http:
                _cfg_path, state_file = self._run(
                    tmp,
                    "stackexchange",
                    {"stackexchange": {"sites": ["stackoverflow"]}},  # enabled omitted
                    mocked_http,
                )
            mocked_http.assert_not_called()
            self.assertNotIn("stackexchange", self._marks(state_file))

    def test_stackexchange_clean_fetch_earns_a_fresh_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = self._se_body()

            def ok(url, **kwargs):
                return 200, body, None

            _cfg_path, state_file = self._run(
                tmp,
                "stackexchange",
                {"stackexchange": {"enabled": True, "sites": ["stackoverflow"]}},
                ok,
            )
            marks = self._marks(state_file)
            self.assertIn("stackexchange", marks)
            age = datetime.now(timezone.utc) - datetime.fromisoformat(
                marks["stackexchange"]
            )
            self.assertAlmostEqual(age.total_seconds(), 0, delta=120)

    def test_stackexchange_failed_fetch_holds_the_marker(self):
        with tempfile.TemporaryDirectory() as tmp:

            def failing(url, **kwargs):
                return 503, "", "HTTP 503"

            _cfg_path, state_file = self._run(
                tmp,
                "stackexchange",
                {"stackexchange": {"enabled": True, "sites": ["stackoverflow"]}},
                failing,
                extra_last_run={"stackexchange": "2026-07-05T00:00:00+00:00"},
            )
            marks = self._marks(state_file)
            self.assertEqual(marks["stackexchange"], "2026-07-05T00:00:00+00:00")

    def test_devto_disabled_holds_and_makes_no_network_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(fs, "http_get") as mocked_http:
                _cfg_path, state_file = self._run(
                    tmp, "devto", {"devto": {"tags": ["ai"]}}, mocked_http
                )
            mocked_http.assert_not_called()
            self.assertNotIn("devto", self._marks(state_file))

    def test_devto_clean_fetch_earns_a_fresh_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = self._devto_body()

            def ok(url, **kwargs):
                return 200, body, None

            _cfg_path, state_file = self._run(
                tmp, "devto", {"devto": {"enabled": True, "tags": ["ai"]}}, ok
            )
            marks = self._marks(state_file)
            self.assertIn("devto", marks)
            age = datetime.now(timezone.utc) - datetime.fromisoformat(marks["devto"])
            self.assertAlmostEqual(age.total_seconds(), 0, delta=120)

    def test_devto_failed_fetch_holds_the_marker(self):
        with tempfile.TemporaryDirectory() as tmp:

            def failing(url, **kwargs):
                return 503, "", "HTTP 503"

            _cfg_path, state_file = self._run(
                tmp,
                "devto",
                {"devto": {"enabled": True, "tags": ["ai"]}},
                failing,
                extra_last_run={"devto": "2026-07-06T00:00:00+00:00"},
            )
            marks = self._marks(state_file)
            self.assertEqual(marks["devto"], "2026-07-06T00:00:00+00:00")

    def test_seen_store_dedups_stackexchange_candidates_across_scans(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = self._se_body(question_id=555)

            def ok(url, **kwargs):
                return 200, body, None

            cfg_path, _state_file = self._run(
                tmp,
                "stackexchange",
                {"stackexchange": {"enabled": True, "sites": ["stackoverflow"]}},
                ok,
            )
            cand_path = Path(json.loads(cfg_path.read_text())["candidates_file"])
            first = json.loads(cand_path.read_text())
            self.assertEqual(len(first["candidates"]), 1)

            args = argparse.Namespace(
                config=str(cfg_path),
                source="stackexchange",
                days=None,
                limit=None,
                dry_run=False,
            )
            with mock.patch.object(fs, "http_get", ok):
                fs.cmd_scan(args)
            second = json.loads(cand_path.read_text())
            self.assertEqual(len(second["candidates"]), 0)
            self.assertEqual(second["dropped"]["seen"], 1)

    def test_per_source_cap_applies_to_a_stackexchange_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            now_epoch = int(datetime.now(timezone.utc).timestamp())
            items = [
                {
                    "tags": ["python"],
                    "question_score": 1,
                    "is_accepted": False,
                    "has_accepted_answer": False,
                    "answer_count": 0,
                    "is_answered": False,
                    "question_id": 1000 + n,
                    "item_type": "question",
                    "score": n,
                    "last_activity_date": now_epoch,
                    "creation_date": now_epoch,
                    "body": "b",
                    "excerpt": "agent memory question",
                    "title": f"Agent memory {n}",
                }
                for n in range(10)
            ]
            body = json.dumps({"items": items, "has_more": False})

            def ok(url, **kwargs):
                return 200, body, None

            cfg_path, _state_file = self._run(
                tmp,
                "stackexchange",
                {"stackexchange": {"enabled": True, "sites": ["stackoverflow"]}},
                ok,
            )
            cand_path = Path(json.loads(cfg_path.read_text())["candidates_file"])
            payload = json.loads(cand_path.read_text())
            # default per_source_cap (DEFAULTS) is 4; one phrase * one site ->
            # a single request returning 10 items, capped to 4 kept.
            self.assertEqual(len(payload["candidates"]), 4)
            self.assertEqual(payload["dropped"]["source_cap"], 6)


if __name__ == "__main__":
    unittest.main()
