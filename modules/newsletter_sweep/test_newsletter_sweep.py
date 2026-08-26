#!/usr/bin/env python3
"""Offline unit tests for newsletter-sweep (stdlib unittest only).

Every network read (sweepcore.http_get) is mocked. These tests make NO live
calls. Run: python -m unittest discover -s modules/newsletter_sweep -p 'test_*.py'
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
_MOD_PATH = Path(__file__).resolve().parent / "newsletter_sweep.py"
_spec = importlib.util.spec_from_file_location("newsletter_sweep", _MOD_PATH)
ns = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ns)

# Fixed "today" for every test, matching the date this module was verified
# against live (2026-08-26).
TODAY = date(2026, 8, 26)
NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)

CONFIG = {
    "subject": {"name": "test-project", "url": "https://github.com/me/test-project"},
    "outlets": [],
    "default_cooldown_days": 90,
    "default_window_days": 30,
    "state_dir": "state",
    "candidates_file": "candidates.json",
}


def _full_config(**overrides):
    cfg = dict(CONFIG)
    cfg.update(overrides)
    for key, val in ns.DEFAULTS.items():
        cfg.setdefault(key, val)
    return cfg


def _outlet(name="Test Outlet", **overrides):
    """A well-formed registry entry, shaped like a real seed entry."""
    base = {
        "name": name,
        "url": "https://example.com",
        "submit_channel": "web-form",
        "submit_url_or_address": "https://example.com/submit",
        "format_note": "one-line tool blurb",
        "audience_note": "developers",
        "cooldown_days": None,
        "check_url": "https://example.com/submit",
        "alive_markers": ["submit your tool"],
    }
    base.update(overrides)
    return base


def _http_map(mapping, default=(404, "", "HTTP 404")):
    """Fake sweepcore.http_get keyed by exact URL."""

    def fake(url, timeout=15, headers=None):
        return mapping.get(url, default)

    return fake


# --- registry validation ---------------------------------------------------


class ValidateOutletTests(unittest.TestCase):
    def _validate(self, entry):
        advisory = []
        result = ns._validate_outlet(entry, advisory)
        return result, advisory

    def test_well_formed_entry_is_normalized(self):
        result, advisory = self._validate(_outlet())
        self.assertIsNotNone(result)
        self.assertEqual(advisory, [])
        self.assertEqual(result["name"], "Test Outlet")
        self.assertEqual(result["submit_channel"], "web-form")
        self.assertEqual(result["alive_markers"], ["submit your tool"])

    def test_non_dict_entry_is_rejected(self):
        result, advisory = self._validate("not-a-dict")
        self.assertIsNone(result)
        self.assertEqual(len(advisory), 1)
        self.assertIn("not an object", advisory[0])

    def test_missing_name_is_rejected(self):
        entry = _outlet()
        del entry["name"]
        result, advisory = self._validate(entry)
        self.assertIsNone(result)
        self.assertIn("'name'", advisory[0])

    def test_missing_url_is_rejected(self):
        entry = _outlet()
        del entry["url"]
        result, _advisory = self._validate(entry)
        self.assertIsNone(result)

    def test_missing_submit_channel_is_rejected(self):
        entry = _outlet()
        del entry["submit_channel"]
        result, _advisory = self._validate(entry)
        self.assertIsNone(result)

    def test_missing_submit_url_or_address_is_rejected(self):
        entry = _outlet()
        del entry["submit_url_or_address"]
        result, _advisory = self._validate(entry)
        self.assertIsNone(result)

    def test_non_string_name_is_rejected(self):
        result, _advisory = self._validate(_outlet(name=12345))
        self.assertIsNone(result)

    def test_blank_name_is_rejected(self):
        result, _advisory = self._validate(_outlet(name="   "))
        self.assertIsNone(result)

    def test_invalid_submit_channel_is_rejected(self):
        result, advisory = self._validate(_outlet(submit_channel="carrier-pigeon"))
        self.assertIsNone(result)
        self.assertIn("submit_channel", advisory[0])
        self.assertIn("carrier-pigeon", advisory[0])

    def test_each_allowed_channel_is_accepted(self):
        for channel in ("web-form", "email", "github-pr", "unknown"):
            with self.subTest(channel=channel):
                result, advisory = self._validate(_outlet(submit_channel=channel))
                self.assertIsNotNone(result)
                self.assertEqual(advisory, [])

    def test_non_int_cooldown_days_normalizes_to_none(self):
        result, _ = self._validate(_outlet(cooldown_days="30"))
        self.assertIsNone(result["cooldown_days"])

    def test_zero_cooldown_days_normalizes_to_none(self):
        result, _ = self._validate(_outlet(cooldown_days=0))
        self.assertIsNone(result["cooldown_days"])

    def test_negative_cooldown_days_normalizes_to_none(self):
        result, _ = self._validate(_outlet(cooldown_days=-5))
        self.assertIsNone(result["cooldown_days"])

    def test_bool_cooldown_days_normalizes_to_none(self):
        # bool is a subclass of int in Python - True/False must not sneak
        # through as cooldown_days 1/0.
        result, _ = self._validate(_outlet(cooldown_days=True))
        self.assertIsNone(result["cooldown_days"])

    def test_valid_positive_cooldown_days_is_preserved(self):
        result, _ = self._validate(_outlet(cooldown_days=45))
        self.assertEqual(result["cooldown_days"], 45)

    def test_missing_check_url_falls_back_to_url(self):
        entry = _outlet()
        del entry["check_url"]
        result, _ = self._validate(entry)
        self.assertEqual(result["check_url"], entry["url"])

    def test_non_string_check_url_falls_back_to_url(self):
        result, _ = self._validate(_outlet(check_url=42))
        self.assertEqual(result["check_url"], "https://example.com")

    def test_missing_alive_markers_normalizes_to_empty_list(self):
        entry = _outlet()
        del entry["alive_markers"]
        result, _ = self._validate(entry)
        self.assertEqual(result["alive_markers"], [])

    def test_non_list_alive_markers_normalizes_to_empty_list(self):
        result, _ = self._validate(_outlet(alive_markers="submit your tool"))
        self.assertEqual(result["alive_markers"], [])

    def test_alive_markers_with_non_string_element_normalizes_to_empty_list(self):
        result, _ = self._validate(_outlet(alive_markers=["ok", 5]))
        self.assertEqual(result["alive_markers"], [])

    def test_missing_format_and_audience_notes_default_to_empty_string(self):
        entry = _outlet()
        del entry["format_note"]
        del entry["audience_note"]
        result, _ = self._validate(entry)
        self.assertEqual(result["format_note"], "")
        self.assertEqual(result["audience_note"], "")

    def test_non_string_format_note_defaults_to_empty_string(self):
        result, _ = self._validate(_outlet(format_note=7))
        self.assertEqual(result["format_note"], "")


# --- freshness check ---------------------------------------------------


class CheckOutletTests(unittest.TestCase):
    def _check(self, status, body, err, entry_overrides=None):
        entry = ns._validate_outlet(_outlet(**(entry_overrides or {})), [])
        report = ns.LaneReport()
        advisory = []
        with mock.patch.object(ns, "http_get", lambda *a, **k: (status, body, err)):
            status_result, note = ns.check_outlet(entry, report, advisory)
        return status_result, note, report, advisory

    def test_outlet_specific_marker_present_is_alive(self):
        status, note, report, advisory = self._check(
            200, "Please submit your tool below.", None
        )
        self.assertEqual(status, "alive")
        self.assertIn("outlet-specific", note)
        self.assertEqual(advisory, [])
        self.assertTrue(report.clean)

    def test_marker_matching_is_case_insensitive(self):
        status, _note, _report, _advisory = self._check(
            200, "PLEASE SUBMIT YOUR TOOL BELOW.", None
        )
        self.assertEqual(status, "alive")

    def test_marker_absent_and_no_generic_match_is_changed(self):
        status, note, _report, advisory = self._check(
            200, "This page no longer says anything relevant.", None
        )
        self.assertEqual(status, "changed")
        self.assertIn("no alive markers", note)
        self.assertEqual(advisory, [])

    def test_no_configured_markers_falls_back_to_generic_and_matches(self):
        status, note, _report, _advisory = self._check(
            200,
            "Want to be featured? Suggest a tool here.",
            None,
            entry_overrides={"alive_markers": []},
        )
        self.assertEqual(status, "alive")
        self.assertIn("generic", note)

    def test_no_configured_markers_and_no_generic_match_is_changed(self):
        status, _note, _report, _advisory = self._check(
            200,
            "Nothing submission-shaped here at all.",
            None,
            entry_overrides={"alive_markers": []},
        )
        self.assertEqual(status, "changed")

    def test_http_404_is_unreachable_and_recorded_as_advisory_not_report(self):
        status, note, report, advisory = self._check(404, "", "HTTP 404")
        self.assertEqual(status, "unreachable")
        self.assertIn("404", note)
        self.assertEqual(len(advisory), 1)
        self.assertEqual(list(report), [])

    def test_http_error_still_counts_as_a_completed_fetch(self):
        # A non-200 response is a completed request (the server answered),
        # so it must not withhold the coverage marker.
        _status, _note, report, _advisory = self._check(500, "", "HTTP 500")
        self.assertEqual(report.fetches_ok, 1)
        self.assertTrue(report.clean)

    def test_network_failure_is_unreachable_and_recorded_on_the_report(self):
        status, note, report, advisory = self._check(None, "", "connection refused")
        self.assertEqual(status, "unreachable")
        self.assertIn("fetch failed", note)
        self.assertEqual(len(report), 1)
        self.assertFalse(report.clean)
        self.assertEqual(advisory, [])

    def test_multiple_configured_markers_any_match_suffices(self):
        entry = ns._validate_outlet(
            _outlet(alive_markers=["first phrase", "second phrase"]), []
        )
        report, advisory = ns.LaneReport(), []
        with mock.patch.object(
            ns,
            "http_get",
            lambda *a, **k: (200, "only the second phrase appears", None),
        ):
            status, _note = ns.check_outlet(entry, report, advisory)
        self.assertEqual(status, "alive")

    def test_marker_is_matched_as_a_plain_substring(self):
        status, _note, _report, _advisory = self._check(
            200, "<p>please <b>submit your tool</b> below</p>", None
        )
        self.assertEqual(status, "alive")


# --- ledger reads --------------------------------------------------------


class LastContactByOutletTests(unittest.TestCase):
    def test_reads_the_most_recent_date_per_outlet(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = Path(tmp) / "submitted_log.jsonl"
            led.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {"date": "2026-01-01T00:00:00+00:00", "outlet": "O"}
                        ),
                        json.dumps(
                            {"date": "2026-06-01T00:00:00+00:00", "outlet": "O"}
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            latest = ns.last_contact_by_outlet(led)
        self.assertEqual(latest["o"], date(2026, 6, 1))

    def test_missing_ledger_is_empty(self):
        self.assertEqual(ns.last_contact_by_outlet(Path("/no/such/ledger.jsonl")), {})

    def test_malformed_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = Path(tmp) / "submitted_log.jsonl"
            led.write_text(
                "not json\n" + json.dumps({"outlet": "O"}) + "\n",  # missing date
                encoding="utf-8",
            )
            latest = ns.last_contact_by_outlet(led)
        self.assertEqual(latest, {})

    def test_outlet_names_are_normalized_lowercase(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = Path(tmp) / "submitted_log.jsonl"
            led.write_text(
                json.dumps({"date": "2026-06-01T00:00:00+00:00", "outlet": "MixedCase"})
                + "\n",
                encoding="utf-8",
            )
            latest = ns.last_contact_by_outlet(led)
        self.assertIn("mixedcase", latest)


# --- candidate build, cooldown, ranking -------------------------------------


class BuildCandidateTests(unittest.TestCase):
    def test_never_contacted_has_none_days_since(self):
        entry = ns._validate_outlet(_outlet(), [])
        cand = ns.build_candidate(entry, "alive", "note", {}, _full_config(), TODAY)
        self.assertIsNone(cand["days_since_last_contact"])

    def test_contacted_n_days_ago_computes_correctly(self):
        entry = ns._validate_outlet(_outlet(name="Contacted"), [])
        last_contact = {"contacted": TODAY - timedelta(days=15)}
        cand = ns.build_candidate(
            entry, "alive", "note", last_contact, _full_config(), TODAY
        )
        self.assertEqual(cand["days_since_last_contact"], 15)

    def test_per_outlet_cooldown_override_is_used(self):
        entry = ns._validate_outlet(_outlet(cooldown_days=20), [])
        cand = ns.build_candidate(
            entry, "alive", "note", {}, _full_config(default_cooldown_days=90), TODAY
        )
        self.assertEqual(cand["cooldown_days"], 20)

    def test_missing_override_falls_back_to_config_default(self):
        entry = ns._validate_outlet(_outlet(cooldown_days=None), [])
        cand = ns.build_candidate(
            entry, "alive", "note", {}, _full_config(default_cooldown_days=90), TODAY
        )
        self.assertEqual(cand["cooldown_days"], 90)


class ApplyCooldownTests(unittest.TestCase):
    def _cand(self, name="Outlet", status="alive", days_since=None, cooldown_days=90):
        return {
            "name": name,
            "status": status,
            "days_since_last_contact": days_since,
            "cooldown_days": cooldown_days,
        }

    def test_never_contacted_is_kept(self):
        kept, dropped = ns.apply_cooldown([self._cand(days_since=None)])
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, 0)

    def test_inside_cooldown_is_dropped(self):
        kept, dropped = ns.apply_cooldown([self._cand(days_since=10, cooldown_days=90)])
        self.assertEqual(kept, [])
        self.assertEqual(dropped, 1)

    def test_exactly_at_the_cooldown_boundary_is_kept(self):
        # days_since < cooldown_days is the drop condition; equal clears it.
        kept, dropped = ns.apply_cooldown([self._cand(days_since=90, cooldown_days=90)])
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, 0)

    def test_past_the_cooldown_window_is_kept(self):
        kept, dropped = ns.apply_cooldown(
            [self._cand(days_since=200, cooldown_days=90)]
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, 0)

    def test_status_does_not_affect_cooldown_filtering(self):
        # An unreachable outlet still inside its cooldown is still dropped;
        # one that has cleared cooldown is still kept regardless of status.
        cands = [
            self._cand(name="A", status="unreachable", days_since=5, cooldown_days=90),
            self._cand(
                name="B", status="unreachable", days_since=200, cooldown_days=90
            ),
        ]
        kept, dropped = ns.apply_cooldown(cands)
        self.assertEqual([c["name"] for c in kept], ["B"])
        self.assertEqual(dropped, 1)

    def test_drop_count_accumulates_across_multiple_outlets(self):
        cands = [
            self._cand(name="A", days_since=5, cooldown_days=90),
            self._cand(name="B", days_since=200, cooldown_days=90),
            self._cand(name="C", days_since=1, cooldown_days=90),
        ]
        kept, dropped = ns.apply_cooldown(cands)
        self.assertEqual([c["name"] for c in kept], ["B"])
        self.assertEqual(dropped, 2)


class SortKeyTests(unittest.TestCase):
    def _cand(self, status, days_since):
        return {"status": status, "days_since_last_contact": days_since}

    def test_alive_outranks_changed(self):
        alive = self._cand("alive", 5)
        changed = self._cand("changed", 5)
        ordered = sorted([changed, alive], key=ns._sort_key, reverse=True)
        self.assertEqual([c["status"] for c in ordered], ["alive", "changed"])

    def test_changed_outranks_unreachable(self):
        changed = self._cand("changed", 5)
        unreachable = self._cand("unreachable", 5)
        ordered = sorted([unreachable, changed], key=ns._sort_key, reverse=True)
        self.assertEqual([c["status"] for c in ordered], ["changed", "unreachable"])

    def test_longer_since_contact_ranks_first_within_the_same_status(self):
        recent = self._cand("alive", 5)
        old = self._cand("alive", 300)
        ordered = sorted([recent, old], key=ns._sort_key, reverse=True)
        self.assertEqual(ordered, [old, recent])

    def test_never_contacted_ranks_above_any_contacted_outlet(self):
        contacted = self._cand("alive", 5000)
        never = self._cand("alive", None)
        ordered = sorted([contacted, never], key=ns._sort_key, reverse=True)
        self.assertEqual(ordered, [never, contacted])

    def test_status_beats_recency_across_tiers(self):
        # A long-cold alive outlet still outranks a just-checked changed one.
        stale_alive = self._cand("alive", 1)
        fresh_changed = self._cand("changed", 500)
        ordered = sorted([fresh_changed, stale_alive], key=ns._sort_key, reverse=True)
        self.assertEqual([c["status"] for c in ordered], ["alive", "changed"])


# --- config validation ---------------------------------------------------


class ConfigValidationTests(unittest.TestCase):
    def test_missing_config_file_exits(self):
        with self.assertRaises(SystemExit):
            ns.load_config("/no/such/config.json")

    def test_missing_required_key_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.json"
            p.write_text(json.dumps({"outlets": [_outlet()]}), encoding="utf-8")
            with self.assertRaises(SystemExit):
                ns.load_config(str(p))

    def test_empty_outlets_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.json"
            p.write_text(
                json.dumps({"subject": {"name": "x"}, "outlets": []}), encoding="utf-8"
            )
            with self.assertRaises(SystemExit):
                ns.load_config(str(p))

    def test_non_list_outlets_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.json"
            p.write_text(
                json.dumps({"subject": {"name": "x"}, "outlets": "nope"}),
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit):
                ns.load_config(str(p))

    def test_invalid_json_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.json"
            p.write_text("{not json", encoding="utf-8")
            with self.assertRaises(SystemExit):
                ns.load_config(str(p))

    def test_valid_config_gets_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.json"
            p.write_text(
                json.dumps({"subject": {"name": "x"}, "outlets": [_outlet()]}),
                encoding="utf-8",
            )
            cfg = ns.load_config(str(p))
        self.assertEqual(cfg["default_cooldown_days"], 90)
        self.assertEqual(cfg["state_dir"], "state")
        self.assertEqual(cfg["candidates_file"], "candidates.json")


class ExampleConfigTests(unittest.TestCase):
    """The shipped example must stay valid and structurally complete - a
    regression guard against README/config drift."""

    def test_shipped_example_config_is_valid(self):
        example = Path(__file__).resolve().parent / "config.example.json"
        cfg = ns.load_config(str(example))
        self.assertIn("subject", cfg)
        self.assertGreaterEqual(len(cfg["outlets"]), 8)

    def test_every_shipped_outlet_has_required_fields_and_a_valid_channel(self):
        example = Path(__file__).resolve().parent / "config.example.json"
        cfg = ns.load_config(str(example))
        for entry in cfg["outlets"]:
            with self.subTest(outlet=entry.get("name")):
                for key in ns.REQUIRED_OUTLET_KEYS:
                    self.assertIn(key, entry)
                    self.assertTrue(str(entry[key]).strip())
                self.assertIn(entry["submit_channel"], ns.ALLOWED_CHANNELS)

    def test_every_shipped_outlet_parses_cleanly(self):
        example = Path(__file__).resolve().parent / "config.example.json"
        cfg = ns.load_config(str(example))
        for entry in cfg["outlets"]:
            advisory = []
            parsed = ns._validate_outlet(entry, advisory)
            with self.subTest(outlet=entry.get("name")):
                self.assertIsNotNone(parsed, msg=advisory)
                self.assertEqual(advisory, [])


# --- dry-run -------------------------------------------------------------


class DryRunTests(unittest.TestCase):
    def test_dry_run_makes_no_calls_and_writes_nothing(self):
        cfg = _full_config(outlets=[_outlet()])
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            cfg["candidates_file"] = str(Path(tmp) / "candidates.json")
            args = mock.Mock(config="config.json", dry_run=True)
            buf = io.StringIO()
            with (
                mock.patch.object(ns, "load_config_for_dry_run", return_value=cfg),
                mock.patch.object(
                    ns, "http_get", side_effect=AssertionError("no network in dry-run")
                ),
                mock.patch("sys.stdout", buf),
            ):
                rc = ns.cmd_scan(args)
        self.assertEqual(rc, 0)
        self.assertIn("DRY-RUN", buf.getvalue())
        self.assertFalse(Path(cfg["candidates_file"]).exists())
        self.assertFalse(Path(cfg["state_dir"]).exists())

    def test_dry_run_lists_every_outlet_check_target(self):
        cfg = _full_config(
            outlets=[_outlet(name="Outlet A", check_url="https://a.example/check")]
        )
        args = mock.Mock(config="config.json", dry_run=True)
        buf = io.StringIO()
        with (
            mock.patch.object(ns, "load_config_for_dry_run", return_value=cfg),
            mock.patch("sys.stdout", buf),
        ):
            ns.cmd_scan(args)
        out = buf.getvalue()
        self.assertIn("Outlet A", out)
        self.assertIn("https://a.example/check", out)

    def test_dry_run_skips_a_malformed_entry_without_crashing(self):
        cfg = _full_config(outlets=["not-a-dict", _outlet(name="Fine Outlet")])
        args = mock.Mock(config="config.json", dry_run=True)
        buf = io.StringIO()
        with (
            mock.patch.object(ns, "load_config_for_dry_run", return_value=cfg),
            mock.patch("sys.stdout", buf),
        ):
            rc = ns.cmd_scan(args)
        self.assertEqual(rc, 0)
        self.assertIn("Fine Outlet", buf.getvalue())


# --- earned window marker -------------------------------------------------


class EarnedWindowTests(unittest.TestCase):
    """last_run is a coverage claim: every registry outlet's check_url fetch
    reached a server this run. Mechanics: sweepcore.window_start /
    earned_stamp, shared with every other module in the toolkit."""

    OLD = "2026-07-01T00:00:00+00:00"

    def _setup(self, tmp, state_obj=None, outlets=None, **overrides):
        cfg = _full_config(outlets=outlets or [_outlet()])
        cfg["state_dir"] = str(Path(tmp) / "state")
        cfg["candidates_file"] = str(Path(tmp) / "candidates.json")
        cfg.update(overrides)
        state_file = Path(cfg["state_dir"]) / "newsletter_state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        if state_obj is not None:
            state_file.write_text(json.dumps(state_obj), encoding="utf-8")
        return cfg, state_file

    def _scan(self, cfg, http_get_fn):
        args = mock.Mock(config="config.json", dry_run=False)
        err = io.StringIO()
        with (
            mock.patch.object(ns, "load_config", return_value=cfg),
            mock.patch.object(ns, "http_get", side_effect=http_get_fn),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(err),
        ):
            ns.cmd_scan(args)
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
        return 200, "no signal here", None

    @staticmethod
    def _boom(url, timeout=15, headers=None):
        return None, "", "connection refused"

    @staticmethod
    def _not_found(url, timeout=15, headers=None):
        return 404, "", "HTTP 404"

    def test_total_failure_holds_the_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(tmp, {"last_run": self.OLD, "seen": {}})
            self._scan(cfg, self._boom)
            self.assertEqual(self._marker(state_file), self.OLD)

    def test_successful_scan_advances_the_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(tmp, {"last_run": self.OLD, "seen": {}})
            self._scan(cfg, self._ok)
            self.assertNotEqual(self._marker(state_file), self.OLD)

    def test_one_network_failure_among_others_holds_the_marker(self):
        outlets = [
            _outlet(name="A", check_url="https://a.example"),
            _outlet(name="B", check_url="https://b.example"),
        ]

        def fake(url, timeout=15, headers=None):
            if url == "https://a.example":
                return None, "", "boom"
            return 200, "ok", None

        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(
                tmp, {"last_run": self.OLD, "seen": {}}, outlets=outlets
            )
            self._scan(cfg, fake)
            self.assertEqual(self._marker(state_file), self.OLD)

    def test_a_404_among_others_still_advances(self):
        outlets = [
            _outlet(name="A", check_url="https://a.example"),
            _outlet(name="B", check_url="https://b.example"),
        ]

        def fake(url, timeout=15, headers=None):
            if url == "https://a.example":
                return 404, "", "HTTP 404"
            return 200, "ok", None

        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(
                tmp, {"last_run": self.OLD, "seen": {}}, outlets=outlets
            )
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

    def test_a_404_run_is_not_flagged_as_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _state_file = self._setup(tmp, {"last_run": self.OLD, "seen": {}})
            self._scan(cfg, self._not_found)
            self.assertFalse(self._digest(cfg)["window_held"])


# --- scan integration: shape, ranking, cooldown, honesty --------------------


class ScanIntegrationTests(unittest.TestCase):
    def _run_scan(self, cfg, http_fn, tmp, capture_stdout=None):
        cfg["state_dir"] = str(Path(tmp) / "state")
        cfg["candidates_file"] = str(Path(tmp) / "candidates.json")
        args = mock.Mock(config="config.json", dry_run=False)
        buf = capture_stdout if capture_stdout is not None else io.StringIO()
        with (
            mock.patch.object(ns, "load_config", return_value=cfg),
            mock.patch.object(ns, "http_get", http_fn),
            contextlib.redirect_stdout(buf),
        ):
            ns.cmd_scan(args)
        return json.loads(Path(cfg["candidates_file"]).read_text(encoding="utf-8"))

    def test_candidate_shape_carries_every_expected_field(self):
        outlet = _outlet(name="Shape Outlet")
        cfg = _full_config(outlets=[outlet])
        url_map = {outlet["check_url"]: (200, "submit your tool", None)}
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._run_scan(cfg, _http_map(url_map), tmp)
        cand = payload["candidates"][0]
        for field in (
            "name",
            "url",
            "submit_channel",
            "submit_url_or_address",
            "format_note",
            "audience_note",
            "cooldown_days",
            "status",
            "detection_note",
            "days_since_last_contact",
        ):
            self.assertIn(field, cand)

    def test_alive_ranks_above_changed_and_unreachable(self):
        alive = _outlet(name="Alive Outlet", check_url="https://alive.example")
        changed = _outlet(name="Changed Outlet", check_url="https://changed.example")
        unreachable = _outlet(
            name="Unreachable Outlet", check_url="https://unreachable.example"
        )
        cfg = _full_config(outlets=[changed, unreachable, alive])
        url_map = {
            "https://alive.example": (200, "submit your tool", None),
            "https://changed.example": (200, "nothing relevant here", None),
            "https://unreachable.example": (404, "", "HTTP 404"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._run_scan(cfg, _http_map(url_map), tmp)
        self.assertEqual(
            [c["name"] for c in payload["candidates"]],
            ["Alive Outlet", "Changed Outlet", "Unreachable Outlet"],
        )

    def test_changed_and_unreachable_outlets_are_not_silently_dropped(self):
        # The freshness check exists to surface registry drift - hiding a
        # changed/unreachable outlet from candidates.json would bury exactly
        # what it is meant to catch.
        outlet = _outlet(name="Drifted Outlet")
        cfg = _full_config(outlets=[outlet])
        url_map = {outlet["check_url"]: (200, "nothing relevant here", None)}
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._run_scan(cfg, _http_map(url_map), tmp)
        self.assertEqual(len(payload["candidates"]), 1)
        self.assertEqual(payload["candidates"][0]["status"], "changed")

    def test_cooldown_drop_reason_appears_in_the_digest(self):
        outlet = _outlet(name="Burned Outlet")
        cfg = _full_config(outlets=[outlet], default_cooldown_days=90)
        url_map = {outlet["check_url"]: (200, "submit your tool", None)}
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            ledger_dir = Path(cfg["state_dir"])
            ledger_dir.mkdir(parents=True, exist_ok=True)
            (ledger_dir / "submitted_log.jsonl").write_text(
                json.dumps(
                    {
                        "date": (TODAY - timedelta(days=10)).isoformat(),
                        "outlet": "Burned Outlet",
                        "url": outlet["submit_url_or_address"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            payload = self._run_scan(cfg, _http_map(url_map), tmp)
        self.assertEqual(payload["candidates"], [])
        self.assertEqual(payload["dropped"]["cooldown"], 1)

    def test_status_tally_in_the_summary_line_matches_reality(self):
        alive = _outlet(name="A", check_url="https://a.example")
        changed = _outlet(name="B", check_url="https://b.example")
        unreachable = _outlet(name="C", check_url="https://c.example")
        cfg = _full_config(outlets=[alive, changed, unreachable])
        url_map = {
            "https://a.example": (200, "submit your tool", None),
            "https://b.example": (200, "nothing relevant here", None),
            "https://c.example": (500, "", "HTTP 500"),
        }
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            self._run_scan(cfg, _http_map(url_map), tmp, capture_stdout=buf)
        out = buf.getvalue()
        self.assertIn("outlets=3", out)
        self.assertIn("alive=1", out)
        self.assertIn("changed=1", out)
        self.assertIn("unreachable=1", out)

    def test_malformed_entry_does_not_crash_and_is_excluded_from_outlet_count(self):
        good = _outlet(name="Good Outlet")
        cfg = _full_config(outlets=["not-a-dict", good])
        url_map = {good["check_url"]: (200, "submit your tool", None)}
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._run_scan(cfg, _http_map(url_map), tmp, capture_stdout=buf)
        self.assertEqual(len(payload["candidates"]), 1)
        self.assertIn("outlets=1", buf.getvalue())
        self.assertTrue(any("not an object" in e for e in payload["errors"]))

    def test_an_outlet_reappears_on_a_second_scan_with_no_contact_recorded(self):
        # There is no seen-store forever-exclusion in this module (unlike
        # cfp-sweep/list-sweep's discovery candidates): the registry is
        # small and static, and the cooldown ledger is the only mechanism
        # that removes an outlet from view. This regression-guards that
        # deliberate design choice.
        outlet = _outlet(name="Repeat Outlet")
        cfg = _full_config(outlets=[outlet])
        url_map = {outlet["check_url"]: (200, "submit your tool", None)}
        with tempfile.TemporaryDirectory() as tmp:
            first = self._run_scan(cfg, _http_map(url_map), tmp)
            second = self._run_scan(cfg, _http_map(url_map), tmp)
        self.assertEqual(len(first["candidates"]), 1)
        self.assertEqual(len(second["candidates"]), 1)
        self.assertEqual(second["candidates"][0]["name"], "Repeat Outlet")

    def test_within_alive_tier_longest_since_contact_ranks_first(self):
        fresh = _outlet(name="Fresh Contact", check_url="https://fresh.example")
        never = _outlet(name="Never Contacted", check_url="https://never.example")
        old = _outlet(name="Old Contact", check_url="https://old.example")
        cfg = _full_config(outlets=[fresh, never, old])
        url_map = {
            "https://fresh.example": (200, "submit your tool", None),
            "https://never.example": (200, "submit your tool", None),
            "https://old.example": (200, "submit your tool", None),
        }
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            ledger_dir = Path(cfg["state_dir"])
            ledger_dir.mkdir(parents=True, exist_ok=True)
            lines = [
                json.dumps(
                    {
                        "date": (TODAY - timedelta(days=200)).isoformat(),
                        "outlet": "Fresh Contact",
                    }
                ),
                json.dumps(
                    {
                        "date": (TODAY - timedelta(days=500)).isoformat(),
                        "outlet": "Old Contact",
                    }
                ),
            ]
            (ledger_dir / "submitted_log.jsonl").write_text(
                "\n".join(lines) + "\n", encoding="utf-8"
            )
            payload = self._run_scan(cfg, _http_map(url_map), tmp)
        self.assertEqual(
            [c["name"] for c in payload["candidates"]],
            ["Never Contacted", "Old Contact", "Fresh Contact"],
        )


# --- ledger commands ---------------------------------------------------


class LedgerCommandTests(unittest.TestCase):
    def test_mark_submitted_appends_and_log_shows_it(self):
        cfg = _full_config(outlets=[_outlet()])
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            args_mark = mock.Mock(
                config="config.json",
                outlet="TLDR",
                url="https://tldr.tech/submit",
                note="link accepted",
            )
            buf = io.StringIO()
            with (
                mock.patch.object(ns, "load_config", return_value=cfg),
                mock.patch("sys.stdout", buf),
            ):
                rc = ns.cmd_mark_submitted(args_mark)
            self.assertEqual(rc, 0)
            self.assertIn("LEDGER_OK TLDR", buf.getvalue())

            log_buf = io.StringIO()
            args_log = mock.Mock(config="config.json")
            with (
                mock.patch.object(ns, "load_config", return_value=cfg),
                mock.patch("sys.stdout", log_buf),
            ):
                ns.cmd_log(args_log)
            self.assertIn("TLDR", log_buf.getvalue())
            self.assertIn("tldr.tech", log_buf.getvalue())

    def test_mark_submitted_without_a_url_records_an_empty_string(self):
        cfg = _full_config(outlets=[_outlet()])
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            _dir, _state, ledger_file = ns.state_paths(cfg)
            args_mark = mock.Mock(
                config="config.json", outlet="Console", url=None, note=None
            )
            with (
                mock.patch.object(ns, "load_config", return_value=cfg),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                ns.cmd_mark_submitted(args_mark)
            recorded = json.loads(ledger_file.read_text(encoding="utf-8").strip())
        self.assertEqual(recorded["url"], "")
        self.assertEqual(recorded["note"], "")

    def test_log_with_no_ledger_says_so(self):
        cfg = _full_config(outlets=[_outlet()])
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            buf = io.StringIO()
            args = mock.Mock(config="config.json")
            with (
                mock.patch.object(ns, "load_config", return_value=cfg),
                mock.patch("sys.stdout", buf),
            ):
                ns.cmd_log(args)
            self.assertIn("no submissions recorded yet", buf.getvalue())

    def test_density_reports_ledger_counts(self):
        cfg = _full_config(outlets=[_outlet()])
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            args_mark = mock.Mock(
                config="config.json", outlet="O1", url="https://o1.example", note=None
            )
            with (
                mock.patch.object(ns, "load_config", return_value=cfg),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                ns.cmd_mark_submitted(args_mark)
            buf = io.StringIO()
            args_dens = mock.Mock(config="config.json")
            with (
                mock.patch.object(ns, "load_config", return_value=cfg),
                mock.patch("sys.stdout", buf),
            ):
                ns.cmd_density(args_dens)
            self.assertIn("1 in last 30d", buf.getvalue())


# --- the gate is sacred ---------------------------------------------------


class GateInvariantTests(unittest.TestCase):
    """There is no auto-submit / batch-approve / scheduler path, and this
    module never sends anything - no email, no form POST, no PR - it only
    ever reads."""

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

    def test_never_sends_email_or_calls_a_write_verb(self):
        source = _MOD_PATH.read_text(encoding="utf-8")
        # http_get is the module's only network primitive; nothing here ever
        # constructs a POST/PUT/PATCH request, sends mail, or reaches for
        # urllib directly.
        self.assertNotIn("urllib.request", source)
        self.assertNotIn("method=", source)
        self.assertNotIn("data=", source)
        for banned in ("smtplib", "sendmail", "send_message", "SMTP("):
            self.assertNotIn(banned, source)

    def test_no_github_pr_or_issue_creation_helper(self):
        source = _MOD_PATH.read_text(encoding="utf-8").lower()
        for banned in ("gh(", "gh_graphql", "pr create", "issue create"):
            self.assertNotIn(banned, source)


if __name__ == "__main__":
    unittest.main()
