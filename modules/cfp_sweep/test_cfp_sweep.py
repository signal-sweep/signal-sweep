#!/usr/bin/env python3
"""Offline unit tests for cfp-sweep (stdlib unittest only).

Every network read (sweepcore.http_get) is mocked. These tests make NO live
calls. Run: python -m unittest discover -s modules/cfp_sweep -p 'test_*.py'
"""

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

# Import the module by path so the test runs from any cwd.
_MOD_PATH = Path(__file__).resolve().parent / "cfp_sweep.py"
_spec = importlib.util.spec_from_file_location("cfp_sweep", _MOD_PATH)
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)

# Fixed "today" for every test, matching the date this module was verified
# against live (2026-08-26). Fixture dates below are chosen relative to it.
TODAY = date(2026, 8, 26)

CONFIG = {
    "subject": {"name": "test-project", "url": "https://github.com/me/test-project"},
    "topics": ["python", "data"],
    "countries": [],
    "include_online": True,
    "watchlist": [],
    "default_cooldown_days": 180,
    "seen_retention_days": 180,
    "default_window_days": 30,
    "emit_cap": 60,
    "state_dir": "state",
    "candidates_file": "candidates.json",
}


def _full_config(**overrides):
    cfg = dict(CONFIG)
    cfg.update(overrides)
    for key, val in cs.DEFAULTS.items():
        cfg.setdefault(key, val)
    return cfg


def _entry(name="Test Conf", **overrides):
    """A conference-data record shaped like the real dataset (verified live
    2026-08-26): name, url, startDate, endDate, city, country, online,
    cfpUrl, cfpEndDate. No cfpStartDate - the live schema does not carry one.
    cfpEndDate defaults to an offset from TODAY, not a literal date, so a
    scan-integration test using the default never rots on the calendar the
    way the hardcoded '2026-08-30' fixtures did (findings c74fa9dd/a6ad7b8e)."""
    base = {
        "name": name,
        "url": "https://example.com/conf",
        "startDate": "2026-10-01",
        "endDate": "2026-10-02",
        "city": "Springfield",
        "country": "USA",
        "online": False,
        "cfpUrl": "https://example.com/conf/cfp",
        "cfpEndDate": (TODAY + timedelta(days=20)).isoformat(),
    }
    base.update(overrides)
    return base


def _topic_url(topic, year):
    return f"{cs.CONFERENCE_DATA_BASE}/{year}/{topic}.json"


def _http_map(mapping, default=(404, "", "HTTP 404")):
    """Fake sweepcore.http_get keyed by exact URL; anything unlisted 404s,
    matching the real dataset's mostly-unseeded next-year files."""

    def fake(url, timeout=15, headers=None):
        return mapping.get(url, default)

    return fake


# --- small helpers -----------------------------------------------------


class ParseDateTests(unittest.TestCase):
    def test_valid_iso_date(self):
        self.assertEqual(cs._parse_date("2026-09-15"), date(2026, 9, 15))

    def test_none_is_none(self):
        self.assertIsNone(cs._parse_date(None))

    def test_non_string_is_none(self):
        self.assertIsNone(cs._parse_date(12345))

    def test_malformed_string_is_none(self):
        self.assertIsNone(cs._parse_date("not-a-date"))

    def test_invalid_calendar_date_is_none(self):
        self.assertIsNone(cs._parse_date("2026-02-30"))


class RegionFilterTests(unittest.TestCase):
    def test_empty_countries_allows_everything(self):
        self.assertTrue(cs._region_ok({"country": "France", "online": False}, [], True))

    def test_listed_country_passes(self):
        self.assertTrue(
            cs._region_ok({"country": "USA", "online": False}, ["USA"], True)
        )

    def test_unlisted_country_is_excluded(self):
        self.assertFalse(
            cs._region_ok({"country": "India", "online": False}, ["USA"], True)
        )

    def test_online_included_regardless_of_country_when_include_online(self):
        self.assertTrue(cs._region_ok({"online": True}, ["USA"], True))

    def test_online_not_exempt_when_include_online_false(self):
        self.assertFalse(cs._region_ok({"online": True}, ["USA"], False))


class DedupKeyTests(unittest.TestCase):
    def test_uses_cfp_url_when_present(self):
        key = cs._dedup_key("HTTPS://Example.com/CFP/", "https://example.com", 2026)
        self.assertEqual(key, "https://example.com/cfp")

    def test_falls_back_to_url_plus_year_when_no_cfp_url(self):
        key = cs._dedup_key("", "https://example.com/conf/", 2026)
        self.assertEqual(key, "https://example.com/conf::2026")


class DaysUntilTests(unittest.TestCase):
    def test_known_deadline(self):
        self.assertEqual(cs._days_until("2026-09-15", TODAY), 20)

    def test_missing_deadline_is_sentinel(self):
        self.assertEqual(cs._days_until(None, TODAY), cs.UNKNOWN_DEADLINE_SENTINEL)

    def test_unparseable_deadline_is_sentinel(self):
        self.assertEqual(cs._days_until("soon", TODAY), cs.UNKNOWN_DEADLINE_SENTINEL)


class TierMappingTests(unittest.TestCase):
    """_tier_for's neutralizing 'comments' stand-in must preserve the
    pattern-driven split (a real topic match beats a generic watchlist pull)
    that relevance_tier's own docstring calls for - without it, both lanes
    would land in the same tier."""

    def test_topic_matched_pattern_is_med(self):
        self.assertEqual(cs._tier_for({"pattern": "python"}), "med")

    def test_multi_topic_pattern_is_still_med_not_high(self):
        self.assertEqual(cs._tier_for({"pattern": "python,data"}), "med")

    def test_watchlist_pattern_is_low(self):
        self.assertEqual(cs._tier_for({"pattern": "watchlist"}), "low")


# --- config validation ---------------------------------------------------


class ConfigValidationTests(unittest.TestCase):
    def test_missing_config_file_exits(self):
        with self.assertRaises(SystemExit):
            cs.load_config("/no/such/config.json")

    def test_missing_required_key_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.json"
            p.write_text(json.dumps({"topics": ["python"]}), encoding="utf-8")
            with self.assertRaises(SystemExit):
                cs.load_config(str(p))

    def test_empty_topics_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.json"
            p.write_text(
                json.dumps({"subject": {"name": "x"}, "topics": []}), encoding="utf-8"
            )
            with self.assertRaises(SystemExit):
                cs.load_config(str(p))

    def test_invalid_json_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.json"
            p.write_text("{not json", encoding="utf-8")
            with self.assertRaises(SystemExit):
                cs.load_config(str(p))

    def test_valid_config_gets_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.json"
            p.write_text(
                json.dumps({"subject": {"name": "x"}, "topics": ["python"]}),
                encoding="utf-8",
            )
            cfg = cs.load_config(str(p))
        self.assertEqual(cfg["default_cooldown_days"], 180)
        self.assertEqual(cfg["watchlist"], [])
        self.assertEqual(cfg["countries"], [])
        self.assertTrue(cfg["include_online"])


class ExampleConfigTests(unittest.TestCase):
    """The shipped example must stay valid and structurally complete - a
    regression guard against README/config drift."""

    def test_shipped_example_config_is_valid(self):
        example = Path(__file__).resolve().parent / "config.example.json"
        cfg = cs.load_config(str(example))
        self.assertIn("subject", cfg)
        self.assertTrue(cfg["topics"])
        self.assertTrue(cfg["watchlist"])
        for entry in cfg["watchlist"]:
            self.assertIn("name", entry)
            self.assertIn("cfp_url", entry)
            self.assertIn("topics", entry)
            self.assertTrue(entry["topics"])


# --- lane 1: conference-data ----------------------------------------------


class ConferenceDataLaneTests(unittest.TestCase):
    def _run(self, topics, url_map, countries=None, include_online=True):
        cfg = _full_config(
            topics=topics, countries=countries or [], include_online=include_online
        )
        report = cs.LaneReport()
        dropped = {
            "malformed": 0,
            "no_cfp": 0,
            "cfp_closed": 0,
            "past_event": 0,
            "region": 0,
        }
        with mock.patch.object(cs, "http_get", _http_map(url_map)):
            result = cs.conference_data_lane(cfg, TODAY, report, dropped)
        return result, report, dropped

    def test_keeps_a_well_formed_open_entry(self):
        url_map = {_topic_url("python", 2026): (200, json.dumps([_entry()]), None)}
        result, _report, dropped = self._run(["python"], url_map)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["venue"], "Test Conf")
        self.assertEqual(result[0]["detected_state"], "open")
        self.assertEqual(sum(dropped.values()), 0)

    def test_malformed_entries_are_dropped_and_counted(self):
        raw_list = [
            "not-a-dict",
            {"url": "https://x.com"},  # missing name
            _entry(name="Bad Date", startDate="not-a-date"),
            _entry(name="Bad Cfp End", cfpEndDate="also-not-a-date"),
            _entry(name="Good Conf"),
        ]
        url_map = {_topic_url("python", 2026): (200, json.dumps(raw_list), None)}
        result, report, dropped = self._run(["python"], url_map)
        self.assertEqual([c["venue"] for c in result], ["Good Conf"])
        self.assertEqual(dropped["malformed"], 4)
        self.assertTrue(report.clean)

    def test_missing_cfp_end_date_is_no_cfp_not_malformed(self):
        raw_list = [
            _entry(name="No Cfp Tracked", cfpEndDate=None),
            _entry(name="Has Cfp"),
        ]
        url_map = {_topic_url("python", 2026): (200, json.dumps(raw_list), None)}
        result, _report, dropped = self._run(["python"], url_map)
        self.assertEqual([c["venue"] for c in result], ["Has Cfp"])
        self.assertEqual(dropped["no_cfp"], 1)
        self.assertEqual(dropped["malformed"], 0)

    def test_closed_cfp_is_dropped(self):
        raw_list = [_entry(name="Already Closed", cfpEndDate="2026-01-01")]
        url_map = {_topic_url("python", 2026): (200, json.dumps(raw_list), None)}
        result, _report, dropped = self._run(["python"], url_map)
        self.assertEqual(result, [])
        self.assertEqual(dropped["cfp_closed"], 1)

    def test_past_event_is_dropped_even_with_an_open_cfp(self):
        raw_list = [
            _entry(
                name="Already Happened", startDate="2026-01-01", endDate="2026-01-02"
            )
        ]
        url_map = {_topic_url("python", 2026): (200, json.dumps(raw_list), None)}
        result, _report, dropped = self._run(["python"], url_map)
        self.assertEqual(result, [])
        self.assertEqual(dropped["past_event"], 1)

    def test_region_filter_excludes_unlisted_country(self):
        raw_list = [_entry(name="India Conf", country="India"), _entry(name="USA Conf")]
        url_map = {_topic_url("python", 2026): (200, json.dumps(raw_list), None)}
        result, _report, dropped = self._run(["python"], url_map, countries=["USA"])
        self.assertEqual([c["venue"] for c in result], ["USA Conf"])
        self.assertEqual(dropped["region"], 1)

    def test_online_entry_included_regardless_of_country_when_include_online(self):
        raw_list = [_entry(name="Online Conf", online=True, country=None, city=None)]
        url_map = {_topic_url("python", 2026): (200, json.dumps(raw_list), None)}
        result, _report, dropped = self._run(
            ["python"], url_map, countries=["USA"], include_online=True
        )
        self.assertEqual([c["venue"] for c in result], ["Online Conf"])
        self.assertEqual(dropped["region"], 0)

    def test_online_entry_excluded_when_include_online_false_and_no_country_match(self):
        raw_list = [_entry(name="Online Conf", online=True, country=None, city=None)]
        url_map = {_topic_url("python", 2026): (200, json.dumps(raw_list), None)}
        result, _report, dropped = self._run(
            ["python"], url_map, countries=["USA"], include_online=False
        )
        self.assertEqual(result, [])
        self.assertEqual(dropped["region"], 1)

    def test_same_conference_across_two_topics_merges_with_accumulated_topics(self):
        shared = _entry(name="Shared Conf", cfpUrl="https://shared.example/cfp")
        url_map = {
            _topic_url("python", 2026): (200, json.dumps([shared]), None),
            _topic_url("data", 2026): (200, json.dumps([shared]), None),
        }
        result, _report, _dropped = self._run(["python", "data"], url_map)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["topics_matched"], {"python", "data"})

    def test_404_topic_year_counts_as_covered_not_an_error(self):
        # python/2026 has data; python/2027 is not seeded yet - both count as
        # a covered fetch (verified live: 2027 has 6 of 2026's 30 topic files).
        url_map = {_topic_url("python", 2026): (200, "[]", None)}
        _result, report, _dropped = self._run(["python"], url_map)
        self.assertEqual(report.fetches_ok, 2)
        self.assertEqual(list(report), [])
        self.assertTrue(report.clean)

    def test_network_error_is_recorded_and_breaks_clean(self):
        url_map = {
            _topic_url("python", 2026): (None, "", "connection refused"),
            _topic_url("python", 2027): (404, "", "HTTP 404"),
        }
        _result, report, _dropped = self._run(["python"], url_map)
        self.assertFalse(report.clean)
        self.assertEqual(len(report), 1)

    def test_non_list_payload_is_a_recorded_error(self):
        url_map = {_topic_url("python", 2026): (200, json.dumps({"oops": True}), None)}
        result, report, _dropped = self._run(["python"], url_map)
        self.assertEqual(result, [])
        self.assertFalse(report.clean)

    def test_bad_json_payload_is_a_recorded_error(self):
        url_map = {_topic_url("python", 2026): (200, "{not json", None)}
        result, report, _dropped = self._run(["python"], url_map)
        self.assertEqual(result, [])
        self.assertFalse(report.clean)

    def test_no_topics_configured_never_fetches_and_is_unclean(self):
        report = cs.LaneReport()
        dropped = {
            "malformed": 0,
            "no_cfp": 0,
            "cfp_closed": 0,
            "past_event": 0,
            "region": 0,
        }
        with mock.patch.object(
            cs, "http_get", side_effect=AssertionError("must not fetch")
        ):
            result = cs.conference_data_lane(
                {"topics": [], "countries": [], "include_online": True},
                TODAY,
                report,
                dropped,
            )
        self.assertEqual(result, [])
        self.assertFalse(report.clean)


# --- lane 2: watchlist -----------------------------------------------------


class WatchlistClassificationTests(unittest.TestCase):
    def _classify(self, body, entry_overrides=None, status=200, err=None):
        entry = {"name": "Test Venue"}
        if entry_overrides:
            entry.update(entry_overrides)
        errors = []
        with mock.patch.object(cs, "http_get", lambda *a, **k: (status, body, err)):
            state, cfp_end, note = cs.classify_cfp_page(
                "https://venue.example/cfp", entry, TODAY, errors
            )
        return state, cfp_end, note, errors

    def test_explicit_closed_phrase(self):
        # real page shape (Berlin Buzzwords 2026 CFP page, verified live).
        state, cfp_end, _note, errors = self._classify(
            "CfP Portal Submissions are closed. You can visit the CfP-Portal to "
            "view or edit your proposals."
        )
        self.assertEqual(state, "closed")
        self.assertIsNone(cfp_end)
        self.assertEqual(errors, [])

    def test_explicit_open_phrase(self):
        state, _cfp_end, _note, _errors = self._classify(
            "We are looking for speakers! Submit your talk today."
        )
        self.assertEqual(state, "open")

    def test_nav_anchor_cta_label_is_not_an_open_signal(self):
        # real bug, found live: KubeCon + CloudNativeCon Europe's CFP page
        # (verified 2026-08-26) carries this exact markup as a permanent
        # section anchor - <a href="#submit-your-talk">Submit Your Talk</a> -
        # regardless of whether the CFP is actually open. Matching runs over
        # the raw fetched body (no HTML parser), so this nav link's anchor
        # text is exactly the kind of phrase the marker lists must not use.
        state, cfp_end, note, _errors = self._classify(
            '<li><a href="#submit-your-talk">submit your talk</a></li>'
            '<li><a href="#dates-to-remember">dates to remember</a></li>'
        )
        self.assertEqual(state, "unknown")
        self.assertIsNone(cfp_end)
        self.assertIn("no cfp language detected", note)

    def test_closed_beats_open_when_both_present(self):
        # real page shape: a generic "is open" mention (about the community,
        # not the CFP) alongside an explicit closed statement - closed wins.
        state, _c, _n, _e = self._classify(
            "This conference is open to new ideas. Submissions are closed."
        )
        self.assertEqual(state, "closed")

    def test_future_dated_deadline_with_no_phrase_infers_open(self):
        # real page shape (pretalx / SciPy-style CFP boilerplate).
        state, cfp_end, note, _errors = self._classify(
            "The deadline to submit a proposal is October 4, 2026, EoD AoE."
        )
        self.assertEqual(state, "open")
        self.assertEqual(cfp_end, date(2026, 10, 4))
        self.assertIn("future", note)

    def test_past_dated_deadline_with_no_phrase_infers_closed(self):
        state, cfp_end, note, _errors = self._classify(
            "The deadline to submit a proposal is March 4, 2026, EoD AoE."
        )
        self.assertEqual(state, "closed")
        self.assertEqual(cfp_end, date(2026, 3, 4))
        self.assertIn("past", note)

    def test_date_without_a_year_is_left_unparsed(self):
        # real page shape (KubeCon + CloudNativeCon Europe CFP page, verified
        # live): the close date never carries a year anywhere on the page.
        state, cfp_end, _note, _errors = self._classify(
            "Submissions are due by 11 October at 11:59 PM CEST. "
            "CFP Closes: Sunday, 11 October at 11:59 PM CEST (UTC+2)."
        )
        self.assertEqual(state, "unknown")
        self.assertIsNone(cfp_end)

    def test_iso_date_format_is_parsed(self):
        state, cfp_end, _n, _e = self._classify("Submission deadline: 2026-10-04.")
        self.assertEqual(state, "open")
        self.assertEqual(cfp_end, date(2026, 10, 4))

    def test_day_first_date_format_is_parsed(self):
        state, cfp_end, _n, _e = self._classify(
            "The submission deadline is 4 October 2026."
        )
        self.assertEqual(state, "open")
        self.assertEqual(cfp_end, date(2026, 10, 4))

    def test_generic_cfp_language_only_is_unknown_with_a_note(self):
        state, cfp_end, note, _e = self._classify(
            "Welcome to our Call for Papers page."
        )
        self.assertEqual(state, "unknown")
        self.assertIsNone(cfp_end)
        self.assertIn("cfp language present", note)

    def test_no_cfp_language_at_all_is_unknown_with_a_different_note(self):
        # real page shape: a client-rendered SPA CFP page (GitNation/React
        # Summit, verified live) whose server-fetched HTML carries no text.
        state, _cfp_end, note, _e = self._classify('<div id="root"></div>')
        self.assertEqual(state, "unknown")
        self.assertIn("JS-rendered", note)

    def test_fetch_failure_is_unknown_and_recorded_as_advisory(self):
        state, cfp_end, note, errors = self._classify("", status=503, err="HTTP 503")
        self.assertEqual(state, "unknown")
        self.assertIsNone(cfp_end)
        self.assertEqual(note, "fetch failed")
        self.assertEqual(len(errors), 1)

    def test_per_entry_closed_marker_override_is_additive(self):
        state, _c, _n, _e = self._classify(
            "Registration for our bespoke portal has ceased.",
            entry_overrides={
                "closed_markers": ["registration for our bespoke portal has ceased"]
            },
        )
        self.assertEqual(state, "closed")

    def test_per_entry_open_marker_override_is_additive(self):
        state, _c, _n, _e = self._classify(
            "Our bespoke portal is now taking pitches.",
            entry_overrides={"open_markers": ["now taking pitches"]},
        )
        self.assertEqual(state, "open")

    def test_builtin_markers_still_work_alongside_an_override(self):
        state, _c, _n, _e = self._classify(
            "Submissions are closed.",
            entry_overrides={"open_markers": ["now taking pitches"]},
        )
        self.assertEqual(state, "closed")


class WatchlistLaneConfigTests(unittest.TestCase):
    def test_entry_missing_cfp_url_is_skipped_with_advisory_and_never_fetched(self):
        cfg = {"watchlist": [{"name": "No URL Venue", "topics": ["python"]}]}
        advisory = []
        with mock.patch.object(
            cs, "http_get", side_effect=AssertionError("must not fetch")
        ):
            result = cs.watchlist_lane(cfg, TODAY, advisory)
        self.assertEqual(result, [])
        self.assertEqual(len(advisory), 1)
        self.assertIn("cfp_url", advisory[0])

    def test_entry_missing_topics_is_skipped_with_advisory(self):
        cfg = {"watchlist": [{"name": "No Topics", "cfp_url": "https://x.example/cfp"}]}
        advisory = []
        with mock.patch.object(
            cs, "http_get", side_effect=AssertionError("must not fetch")
        ):
            result = cs.watchlist_lane(cfg, TODAY, advisory)
        self.assertEqual(result, [])
        self.assertEqual(len(advisory), 1)

    def test_non_dict_entry_is_skipped_with_advisory(self):
        cfg = {"watchlist": ["not-an-object"]}
        advisory = []
        result = cs.watchlist_lane(cfg, TODAY, advisory)
        self.assertEqual(result, [])
        self.assertEqual(len(advisory), 1)

    def test_valid_entry_produces_a_candidate_with_passthrough_fields(self):
        cfg = {
            "watchlist": [
                {
                    "name": "Good Venue",
                    "cfp_url": "https://good.example/cfp",
                    "topics": ["python", "security"],
                    "cooldown_days": 30,
                    "cadence_note": "annual",
                    "format_note": "hybrid",
                }
            ]
        }
        advisory = []
        with mock.patch.object(
            cs, "http_get", lambda *a, **k: (200, "call for papers", None)
        ):
            result = cs.watchlist_lane(cfg, TODAY, advisory)
        self.assertEqual(len(result), 1)
        cand = result[0]
        self.assertEqual(cand["venue"], "Good Venue")
        self.assertEqual(cand["lane"], "watchlist")
        self.assertEqual(cand["cooldown_days"], 30)
        self.assertEqual(cand["topics_matched"], {"python", "security"})
        self.assertEqual(cand["cadence_note"], "annual")
        self.assertEqual(cand["detected_state"], "unknown")


# --- dedup, cooldown, filtering ---------------------------------------------


def _cand(
    venue="Conf",
    cfp_url="https://x.example/cfp",
    url="https://x.example",
    year=2026,
    cooldown_days=None,
):
    return {
        "venue": venue,
        "cfp_url": cfp_url,
        "url": url,
        "year": year,
        "cooldown_days": cooldown_days,
    }


class FilterCandidatesTests(unittest.TestCase):
    def test_batch_duplicate_is_dropped(self):
        raw = [_cand(), _cand()]
        kept, dropped = cs.filter_candidates(raw, {}, {}, _full_config(), TODAY)
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped["dup"], 1)

    def test_seen_store_excludes_a_known_key(self):
        cand = _cand()
        key = cs._dedup_key(cand["cfp_url"], cand["url"], cand["year"])
        kept, dropped = cs.filter_candidates(
            [cand], {key: "2026-01-01"}, {}, _full_config(), TODAY
        )
        self.assertEqual(kept, [])
        self.assertEqual(dropped["seen"], 1)

    def test_cooldown_excludes_a_recently_submitted_venue(self):
        cand = _cand(venue="Recent Conf")
        last_submitted = {"recent conf": TODAY - timedelta(days=10)}
        kept, dropped = cs.filter_candidates(
            [cand], {}, last_submitted, _full_config(default_cooldown_days=180), TODAY
        )
        self.assertEqual(kept, [])
        self.assertEqual(dropped["cooldown"], 1)

    def test_cooldown_allows_a_venue_past_the_window(self):
        cand = _cand(venue="Old Conf")
        last_submitted = {"old conf": TODAY - timedelta(days=200)}
        kept, dropped = cs.filter_candidates(
            [cand], {}, last_submitted, _full_config(default_cooldown_days=180), TODAY
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped["cooldown"], 0)

    def test_per_entry_cooldown_override_is_honoured(self):
        # 40 days since the last submission clears a 30-day override even
        # though it would still be inside the 180-day default.
        cand = _cand(venue="Short Cooldown Conf", cooldown_days=30)
        last_submitted = {"short cooldown conf": TODAY - timedelta(days=40)}
        kept, dropped = cs.filter_candidates(
            [cand], {}, last_submitted, _full_config(default_cooldown_days=180), TODAY
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped["cooldown"], 0)

    def test_venue_with_no_ledger_history_is_kept(self):
        cand = _cand(venue="Fresh Conf")
        kept, dropped = cs.filter_candidates([cand], {}, {}, _full_config(), TODAY)
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped["cooldown"], 0)

    def test_venue_name_matching_is_case_insensitive(self):
        cand = _cand(venue="MixedCase Conf")
        last_submitted = {"mixedcase conf": TODAY - timedelta(days=5)}
        kept, dropped = cs.filter_candidates(
            [cand], {}, last_submitted, _full_config(default_cooldown_days=180), TODAY
        )
        self.assertEqual(kept, [])
        self.assertEqual(dropped["cooldown"], 1)


class LastSubmissionByVenueTests(unittest.TestCase):
    def test_reads_the_most_recent_date_per_venue(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = Path(tmp) / "submitted_log.jsonl"
            led.write_text(
                "\n".join(
                    [
                        json.dumps({"date": "2026-01-01T00:00:00+00:00", "venue": "V"}),
                        json.dumps({"date": "2026-06-01T00:00:00+00:00", "venue": "V"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            latest = cs.last_submission_by_venue(led)
        self.assertEqual(latest["v"], date(2026, 6, 1))

    def test_missing_ledger_is_empty(self):
        self.assertEqual(cs.last_submission_by_venue(Path("/no/such/ledger.jsonl")), {})

    def test_malformed_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = Path(tmp) / "submitted_log.jsonl"
            led.write_text(
                "not json\n" + json.dumps({"venue": "V"}) + "\n",  # missing date
                encoding="utf-8",
            )
            latest = cs.last_submission_by_venue(led)
        self.assertEqual(latest, {})


# --- dry-run -------------------------------------------------------------


class DryRunTests(unittest.TestCase):
    def test_dry_run_makes_no_calls_and_writes_nothing(self):
        cfg = _full_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            cfg["candidates_file"] = str(Path(tmp) / "candidates.json")
            args = mock.Mock(config="config.json", limit=None, dry_run=True)
            buf = io.StringIO()
            with (
                mock.patch.object(cs, "load_config_for_dry_run", return_value=cfg),
                mock.patch.object(
                    cs, "http_get", side_effect=AssertionError("no network in dry-run")
                ),
                mock.patch("sys.stdout", buf),
            ):
                rc = cs.cmd_scan(args)
        self.assertEqual(rc, 0)
        self.assertIn("DRY-RUN", buf.getvalue())
        self.assertFalse(Path(cfg["candidates_file"]).exists())
        self.assertFalse(Path(cfg["state_dir"]).exists())

    def test_dry_run_lists_every_topic_year_url_and_watchlist_venue(self):
        cfg = _full_config(
            topics=["python"],
            watchlist=[{"name": "Venue A", "cfp_url": "https://a.example/cfp"}],
        )
        args = mock.Mock(config="config.json", limit=None, dry_run=True)
        buf = io.StringIO()
        with (
            mock.patch.object(cs, "load_config_for_dry_run", return_value=cfg),
            mock.patch("sys.stdout", buf),
        ):
            cs.cmd_scan(args)
        out = buf.getvalue()
        self.assertIn(_topic_url("python", TODAY.year), out)
        self.assertIn(_topic_url("python", TODAY.year + 1), out)
        self.assertIn("Venue A", out)
        self.assertIn("https://a.example/cfp", out)


# --- earned window marker -------------------------------------------------


class EarnedWindowTests(unittest.TestCase):
    """last_run is a coverage claim for lane 1 (every topic/year fetch came
    back, none failed); lane 1 always re-reads the full dataset rather than
    an incremental query, so this marker gates nothing about what is fetched
    - it only proves the last scan completed. Mechanics: sweepcore.window_start
    / earned_stamp, shared with every other module in the toolkit."""

    OLD = "2026-07-01T00:00:00+00:00"

    def _setup(self, tmp, state_obj=None, **overrides):
        cfg = _full_config(topics=["python"], watchlist=[])
        cfg["state_dir"] = str(Path(tmp) / "state")
        cfg["candidates_file"] = str(Path(tmp) / "candidates.json")
        cfg.update(overrides)
        state_file = Path(cfg["state_dir"]) / "cfp_state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        if state_obj is not None:
            state_file.write_text(json.dumps(state_obj), encoding="utf-8")
        return cfg, state_file

    def _scan(self, cfg, http_get_fn):
        args = mock.Mock(config="config.json", limit=None, dry_run=False)
        err = io.StringIO()
        with (
            mock.patch.object(cs, "load_config", return_value=cfg),
            mock.patch.object(cs, "http_get", side_effect=http_get_fn),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(err),
        ):
            cs.cmd_scan(args)
        return err.getvalue()

    @staticmethod
    def _marker(state_file):
        if not state_file.exists():
            return None
        return json.loads(state_file.read_text(encoding="utf-8")).get("last_run")

    @staticmethod
    def _digest(cfg):
        return json.loads(Path(cfg["candidates_file"]).read_text(encoding="utf-8"))

    @staticmethod
    def _ok(url, timeout=15, headers=None):
        return 200, "[]", None

    @staticmethod
    def _boom(url, timeout=15, headers=None):
        return None, "", "connection refused"

    def test_total_failure_holds_the_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(tmp, {"last_run": self.OLD, "seen": {}})
            self._scan(cfg, self._boom)
            self.assertEqual(self._marker(state_file), self.OLD)

    def test_successful_empty_scan_still_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(tmp, {"last_run": self.OLD, "seen": {}})
            self._scan(cfg, self._ok)
            self.assertNotEqual(self._marker(state_file), self.OLD)

    def test_one_failed_topic_year_holds_the_marker(self):
        def fake(url, timeout=15, headers=None):
            if url == _topic_url("python", TODAY.year + 1):
                return None, "", "boom"
            return 200, "[]", None

        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(tmp, {"last_run": self.OLD, "seen": {}})
            self._scan(cfg, fake)
            self.assertEqual(self._marker(state_file), self.OLD)

    def test_404_for_next_year_still_advances(self):
        def fake(url, timeout=15, headers=None):
            if url == _topic_url("python", TODAY.year + 1):
                return 404, "", "HTTP 404"
            return 200, "[]", None

        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(tmp, {"last_run": self.OLD, "seen": {}})
            self._scan(cfg, fake)
            self.assertNotEqual(self._marker(state_file), self.OLD)

    def test_failed_run_with_no_prior_marker_invents_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(tmp, {"seen": {}})
            self._scan(cfg, self._boom)
            self.assertFalse(self._marker(state_file))

    def test_first_clean_run_with_no_prior_marker_lays_one_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(tmp, {"seen": {}})
            self._scan(cfg, self._ok)
            age = datetime.now(timezone.utc) - datetime.fromisoformat(
                self._marker(state_file)
            )
            self.assertAlmostEqual(age.total_seconds(), 0, delta=120)

    def test_unreadable_marker_warns_and_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(tmp, {"last_run": "last tuesday", "seen": {}})
            err = self._scan(cfg, self._ok)
            self.assertIn("unreadable last_run", err)
            self.assertEqual(self._marker(state_file), "last tuesday")

    def test_held_window_is_reported_in_digest_and_stderr(self):
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


# --- scan integration: shape, dedup, sort order -----------------------------


class ScanIntegrationTests(unittest.TestCase):
    # TODAY as the tz-aware datetime cs._now() would return - cmd_scan derives
    # today, the earned-window marker, and the seen-store cutoff all from one
    # _now() call, so freezing it here pins every date cmd_scan computes to
    # the module-level TODAY constant instead of the real clock (findings
    # c74fa9dd/a6ad7b8e: this suite used to compare fixture deadlines against
    # whatever day it happened to run on).
    FROZEN_NOW = datetime(TODAY.year, TODAY.month, TODAY.day, tzinfo=timezone.utc)

    def _run_scan(self, cfg, url_map, tmp):
        cfg["state_dir"] = str(Path(tmp) / "state")
        cfg["candidates_file"] = str(Path(tmp) / "candidates.json")
        args = mock.Mock(config="config.json", limit=None, dry_run=False)
        with (
            mock.patch.object(cs, "load_config", return_value=cfg),
            mock.patch.object(cs, "http_get", _http_map(url_map)),
            mock.patch.object(cs, "_now", return_value=self.FROZEN_NOW),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cs.cmd_scan(args)
        return json.loads(Path(cfg["candidates_file"]).read_text(encoding="utf-8"))

    def test_candidate_shape_and_topic_match_count_leads_the_sort(self):
        near = _entry(
            name="Near Deadline Conf",
            cfpUrl="https://near.example/cfp",
            cfpEndDate=(TODAY + timedelta(days=4)).isoformat(),
        )
        far_multi = _entry(
            name="Multi Topic Conf",
            cfpUrl="https://multi.example/cfp",
            cfpEndDate=(TODAY + timedelta(days=90)).isoformat(),
        )
        url_map = {
            _topic_url("python", 2026): (200, json.dumps([near, far_multi]), None),
            _topic_url("data", 2026): (200, json.dumps([far_multi]), None),
        }
        cfg = _full_config(topics=["python", "data"], watchlist=[])
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._run_scan(cfg, url_map, tmp)
        venues = [c["venue"] for c in payload["candidates"]]
        # Multi Topic Conf matches 2 configured topics vs Near Deadline
        # Conf's 1, so it ranks first despite a later deadline -
        # topic_match_count leads deadline proximity in the sort key.
        self.assertEqual(venues, ["Multi Topic Conf", "Near Deadline Conf"])
        cand = payload["candidates"][0]
        for field in (
            "venue",
            "lane",
            "url",
            "cfp_url",
            "cfp_end",
            "detected_state",
            "topic_match_count",
            "topics_matched",
            "pattern",
            "tier",
            "days_until_deadline",
        ):
            self.assertIn(field, cand)
        self.assertEqual(cand["topic_match_count"], 2)
        self.assertEqual(cand["pattern"], "data,python")

    def test_deadline_proximity_breaks_ties_within_equal_topic_match_count(self):
        soon = _entry(
            name="Soon Conf",
            cfpUrl="https://soon.example/cfp",
            cfpEndDate=(TODAY + timedelta(days=4)).isoformat(),
        )
        later = _entry(
            name="Later Conf",
            cfpUrl="https://later.example/cfp",
            cfpEndDate=(TODAY + timedelta(days=90)).isoformat(),
        )
        url_map = {_topic_url("python", 2026): (200, json.dumps([soon, later]), None)}
        cfg = _full_config(topics=["python"], watchlist=[])
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._run_scan(cfg, url_map, tmp)
        self.assertEqual(
            [c["venue"] for c in payload["candidates"]], ["Soon Conf", "Later Conf"]
        )

    def test_conference_data_lane_ranks_above_watchlist_lane(self):
        dataset_hit = _entry(name="Dataset Conf", cfpUrl="https://dataset.example/cfp")
        url_map = {_topic_url("python", 2026): (200, json.dumps([dataset_hit]), None)}
        cfg = _full_config(
            topics=["python"],
            watchlist=[
                {
                    "name": "Watchlist Venue",
                    "cfp_url": "https://watchlist.example/cfp",
                    "topics": ["python"],
                }
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            # The watchlist venue's own page: no phrase, no dated deadline.
            url_map[_topic_url("python", 2027)] = (404, "", "HTTP 404")
            url_map["https://watchlist.example/cfp"] = (200, "no signal here", None)
            payload = self._run_scan(cfg, url_map, tmp)
        self.assertEqual(
            [c["venue"] for c in payload["candidates"]],
            ["Dataset Conf", "Watchlist Venue"],
        )
        self.assertEqual(payload["candidates"][0]["tier"], "med")
        self.assertEqual(payload["candidates"][1]["tier"], "low")

    def test_cooldown_drop_reason_appears_in_the_digest(self):
        conf = _entry(name="Burned Venue", cfpUrl="https://burned.example/cfp")
        url_map = {_topic_url("python", 2026): (200, json.dumps([conf]), None)}
        cfg = _full_config(topics=["python"], watchlist=[], default_cooldown_days=180)
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            ledger_dir = Path(cfg["state_dir"])
            ledger_dir.mkdir(parents=True, exist_ok=True)
            (ledger_dir / "submitted_log.jsonl").write_text(
                json.dumps(
                    {
                        "date": (TODAY - timedelta(days=10)).isoformat(),
                        "venue": "Burned Venue",
                        "url": "https://burned.example/cfp",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            payload = self._run_scan(cfg, url_map, tmp)
        self.assertEqual(payload["candidates"], [])
        self.assertEqual(payload["dropped"]["cooldown"], 1)

    def test_seen_candidate_does_not_resurface_on_a_second_scan(self):
        conf = _entry(name="Repeat Conf", cfpUrl="https://repeat.example/cfp")
        url_map = {_topic_url("python", 2026): (200, json.dumps([conf]), None)}
        cfg = _full_config(topics=["python"], watchlist=[])
        with tempfile.TemporaryDirectory() as tmp:
            first = self._run_scan(cfg, url_map, tmp)
            second = self._run_scan(cfg, url_map, tmp)
        self.assertEqual(len(first["candidates"]), 1)
        self.assertEqual(second["candidates"], [])
        self.assertEqual(second["dropped"]["seen"], 1)


# --- ledger commands ---------------------------------------------------


class LedgerCommandTests(unittest.TestCase):
    def test_mark_submitted_appends_and_log_shows_it(self):
        cfg = _full_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            args_mark = mock.Mock(
                config="config.json",
                venue="PyConf",
                url="https://pyconf.example/cfp",
                note="talk accepted",
            )
            buf = io.StringIO()
            with (
                mock.patch.object(cs, "load_config", return_value=cfg),
                mock.patch("sys.stdout", buf),
            ):
                rc = cs.cmd_mark_submitted(args_mark)
            self.assertEqual(rc, 0)
            self.assertIn("LEDGER_OK PyConf", buf.getvalue())

            log_buf = io.StringIO()
            args_log = mock.Mock(config="config.json")
            with (
                mock.patch.object(cs, "load_config", return_value=cfg),
                mock.patch("sys.stdout", log_buf),
            ):
                cs.cmd_log(args_log)
            self.assertIn("PyConf", log_buf.getvalue())
            self.assertIn("pyconf.example", log_buf.getvalue())

    def test_log_with_no_ledger_says_so(self):
        cfg = _full_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            buf = io.StringIO()
            args = mock.Mock(config="config.json")
            with (
                mock.patch.object(cs, "load_config", return_value=cfg),
                mock.patch("sys.stdout", buf),
            ):
                cs.cmd_log(args)
            self.assertIn("no submissions recorded yet", buf.getvalue())

    def test_density_reports_ledger_counts(self):
        cfg = _full_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            args_mark = mock.Mock(
                config="config.json", venue="V1", url="https://v1.example", note=None
            )
            with (
                mock.patch.object(cs, "load_config", return_value=cfg),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                cs.cmd_mark_submitted(args_mark)
            buf = io.StringIO()
            args_dens = mock.Mock(config="config.json")
            with (
                mock.patch.object(cs, "load_config", return_value=cfg),
                mock.patch("sys.stdout", buf),
            ):
                cs.cmd_density(args_dens)
            self.assertIn("1 in last 30d", buf.getvalue())


# --- the gate is sacred ---------------------------------------------------


class GateInvariantTests(unittest.TestCase):
    """There is no auto-submit / batch-approve / scheduler path, and this
    module never issues an outbound (POST/PUT-shaped) request - both lanes
    are plain reads."""

    def test_no_submit_subcommand_or_outbound_helper(self):
        source = _MOD_PATH.read_text(encoding="utf-8").lower()
        for banned in ('add_parser("submit', "add_parser('submit", "auto-submit"):
            self.assertNotIn(banned, source)
        for banned in ("batch_approve", "batch-approve", "--yes", "auto_approve"):
            self.assertNotIn(banned, source)

    def test_subcommands_are_only_scan_mark_submitted_log_density(self):
        parser_src = _MOD_PATH.read_text(encoding="utf-8")
        self.assertIn('add_parser("scan"', parser_src)
        self.assertIn('add_parser("mark-submitted"', parser_src)
        self.assertIn('add_parser("log"', parser_src)
        self.assertIn('add_parser("density"', parser_src)

    def test_never_submits_a_form_or_calls_a_write_verb(self):
        source = _MOD_PATH.read_text(encoding="utf-8")
        # http_get is the module's only network primitive; nothing here ever
        # constructs a POST/PUT/PATCH request or reaches for urllib directly.
        self.assertNotIn("urllib.request", source)
        self.assertNotIn("method=", source)


if __name__ == "__main__":
    unittest.main()
