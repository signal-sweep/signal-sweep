#!/usr/bin/env python3
"""Offline unit tests for placement-health (stdlib unittest only).

No network: the fetch (sweepcore.http_get, wrapped by placement_health.fetch) is
patched to return fixture (status, body, err) tuples. These tests cover the pure
state-decision helper check_one across its LIVE / DROPPED / BROKEN / PENDING
branches, load_config validation, and a NoAutoPost source guard.

placement-health is read-only (it has no outbound path at all), but the guard is
kept so a future edit that adds one fails this test loudly.

Run: python -m unittest discover -s modules/placement_health -p 'test_*.py'
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# placement_health adds modules/ to sys.path for sweepcore; replicate first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import placement_health as ph  # noqa: E402


def _patch_fetch(status, body, err):
    return mock.patch.object(ph, "fetch", return_value=(status, body, err))


class CheckOneTests(unittest.TestCase):
    def test_live_when_expect_string_present(self):
        with _patch_fetch(200, "... my-project is here ...", None):
            state, _detail = ph.check_one(
                {"url": "https://x", "expect": "my-project"}, 10
            )
        self.assertEqual(state, "LIVE")

    def test_dropped_when_200_but_expect_absent(self):
        with _patch_fetch(200, "nothing here", None):
            state, detail = ph.check_one(
                {"url": "https://x", "expect": "my-project"}, 10
            )
        self.assertEqual(state, "DROPPED")
        self.assertIn("pruned", detail)

    def test_broken_on_fetch_error_for_live_entry(self):
        with _patch_fetch(None, "", "HTTP 404"):
            state, detail = ph.check_one(
                {"url": "https://x", "expect": "my-project"}, 10
            )
        self.assertEqual(state, "BROKEN")
        self.assertEqual(detail, "HTTP 404")

    def test_pending_404_is_pending_ok_not_a_finding(self):
        with _patch_fetch(None, "", "HTTP 404"):
            state, _detail = ph.check_one(
                {"url": "https://x", "expect": "my-project", "status": "pending"}, 10
            )
        self.assertEqual(state, "PENDING_OK")

    def test_pending_now_present_flags_for_promotion(self):
        with _patch_fetch(200, "my-project landed", None):
            state, detail = ph.check_one(
                {"url": "https://x", "expect": "my-project", "status": "pending"}, 10
            )
        self.assertEqual(state, "PENDING_MERGED")
        self.assertIn("promote", detail)

    def test_pending_still_absent_stays_pending_ok(self):
        with _patch_fetch(200, "not there yet", None):
            state, _detail = ph.check_one(
                {"url": "https://x", "expect": "my-project", "status": "pending"}, 10
            )
        self.assertEqual(state, "PENDING_OK")

    def test_ok_states_classification(self):
        # The three OK states are not findings; DROPPED/BROKEN are.
        self.assertIn("LIVE", ph.OK_STATES)
        self.assertIn("PENDING_OK", ph.OK_STATES)
        self.assertIn("PENDING_MERGED", ph.OK_STATES)
        self.assertNotIn("DROPPED", ph.OK_STATES)
        self.assertNotIn("BROKEN", ph.OK_STATES)


class LoadConfigTests(unittest.TestCase):
    def _write(self, tmp, cfg):
        p = Path(tmp) / "placements.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")
        return str(p)

    def test_valid_config_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"placements": [{"url": "https://x", "expect": "me"}]}
            loaded = ph.load_config(self._write(tmp, cfg))
            self.assertEqual(len(loaded["placements"]), 1)

    def test_empty_placements_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                ph.load_config(self._write(tmp, {"placements": []}))

    def test_placement_missing_required_field_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"placements": [{"url": "https://x"}]}  # no 'expect'
            with self.assertRaises(SystemExit):
                ph.load_config(self._write(tmp, cfg))

    def test_missing_config_file_exits(self):
        with self.assertRaises(SystemExit):
            ph.load_config("does/not/exist.json")


class NoAutoPostTests(unittest.TestCase):
    """placement-health is read-only intelligence with no outbound path. This
    guard freezes that property: an edit that introduces a post/submit/scheduler
    affordance must fail here."""

    def test_no_outbound_or_scheduler_token_in_source(self):
        src = Path(ph.__file__).read_text(encoding="utf-8")
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

    def test_only_subcommand_is_check(self):
        src = Path(ph.__file__).read_text(encoding="utf-8")
        self.assertIn('add_parser("check"', src)
        for banned in ('add_parser("submit', 'add_parser("post', 'add_parser("comment'):
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()
