#!/usr/bin/env python3
"""Offline unit tests for release-sweep (stdlib unittest only).

No network, no live gh: sweepcore.gh is patched where a helper calls it. The
star helper here is bucket_commits — a pure Conventional-Commit classifier with
no I/O at all. load_channels validation, resolve_repo and previous_tag (gh
mocked), plus the NoAutoPost source guard round it out.

Run: python -m unittest discover -s modules/release_sweep -p 'test_*.py'
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# release_sweep adds modules/ to sys.path for sweepcore; replicate first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_sweep as rs  # noqa: E402


class BucketCommitsTests(unittest.TestCase):
    def test_groups_by_conventional_prefix_and_strips_it(self):
        buckets = rs.bucket_commits(
            [
                "feat: add memory module",
                "fix: handle empty config",
                "docs: update README",
                "random no-prefix subject",
            ]
        )
        self.assertEqual(buckets["feat"], ["add memory module"])
        self.assertEqual(buckets["fix"], ["handle empty config"])
        # docs + bare subject both fall through to 'other'.
        self.assertIn("update README", buckets["other"])
        self.assertIn("random no-prefix subject", buckets["other"])

    def test_scoped_prefix_is_stripped(self):
        buckets = rs.bucket_commits(["feat(api): new endpoint"])
        self.assertEqual(buckets["feat"], ["new endpoint"])

    def test_bang_marks_breaking_over_feat(self):
        buckets = rs.bucket_commits(["feat!: drop python 3.9"])
        self.assertEqual(buckets["breaking"], ["drop python 3.9"])
        self.assertEqual(buckets["feat"], [])

    def test_breaking_change_footer_token_marks_breaking(self):
        buckets = rs.bucket_commits(["refactor: rework BREAKING CHANGE api"])
        self.assertEqual(len(buckets["breaking"]), 1)

    def test_empty_and_falsy_subjects_skipped(self):
        buckets = rs.bucket_commits(["", None, "feat: real one"])
        self.assertEqual(buckets["feat"], ["real one"])
        total = sum(len(v) for v in buckets.values())
        self.assertEqual(total, 1)

    def test_all_four_buckets_always_present(self):
        buckets = rs.bucket_commits([])
        self.assertEqual(set(buckets), {"breaking", "feat", "fix", "other"})
        for v in buckets.values():
            self.assertEqual(v, [])


class LoadChannelsTests(unittest.TestCase):
    def _write(self, tmp, cfg):
        p = Path(tmp) / "channels.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")
        return str(p)

    def test_valid_returns_channel_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            chans = rs.load_channels(self._write(tmp, {"channels": [{"name": "hn"}]}))
            self.assertEqual(chans, [{"name": "hn"}])

    def test_empty_channels_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                rs.load_channels(self._write(tmp, {"channels": []}))

    def test_missing_config_file_exits(self):
        with self.assertRaises(SystemExit):
            rs.load_channels("does/not/exist.json")


class ResolveRepoTests(unittest.TestCase):
    def test_explicit_arg_wins_without_calling_gh(self):
        # No gh patch: if it tried to call gh, the real binary/network would be
        # hit. An explicit arg must short-circuit before that.
        self.assertEqual(rs.resolve_repo("owner/name"), "owner/name")

    def test_detects_from_gh_when_no_arg(self):
        with mock.patch.object(rs, "gh", return_value=("owner/detected", None)):
            self.assertEqual(rs.resolve_repo(None), "owner/detected")

    def test_gh_error_exits(self):
        with mock.patch.object(rs, "gh", return_value=(None, "no repo")):
            with self.assertRaises(SystemExit):
                rs.resolve_repo(None)


class PreviousTagTests(unittest.TestCase):
    def test_returns_tag_after_current_in_release_list(self):
        releases = [{"tagName": "v3"}, {"tagName": "v2"}, {"tagName": "v1"}]
        with mock.patch.object(rs, "gh", return_value=(releases, None)):
            self.assertEqual(rs.previous_tag("o/r", "v2"), "v1")

    def test_returns_none_for_oldest_tag(self):
        releases = [{"tagName": "v2"}, {"tagName": "v1"}]
        with mock.patch.object(rs, "gh", return_value=(releases, None)):
            self.assertIsNone(rs.previous_tag("o/r", "v1"))

    def test_returns_none_on_gh_error(self):
        with mock.patch.object(rs, "gh", return_value=(None, "boom")):
            self.assertIsNone(rs.previous_tag("o/r", "v1"))


class NoAutoPostTests(unittest.TestCase):
    """release-sweep assembles material and records announcements; it never
    posts. mark-announced only RECORDS an announcement the human already made.
    No outbound-post, batch-approve, or scheduler path may appear."""

    def test_no_outbound_or_scheduler_token_in_source(self):
        src = Path(rs.__file__).read_text(encoding="utf-8")
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

    def test_subcommands_are_only_brief_mark_announced_log(self):
        src = Path(rs.__file__).read_text(encoding="utf-8")
        # The brief parser may be wrapped across lines by the formatter, so match
        # the subcommand name token rather than the exact add_parser call shape.
        self.assertIn('"brief"', src)
        self.assertIn('add_parser("mark-announced"', src)
        self.assertIn('add_parser("log"', src)
        for banned in ('add_parser("submit', 'add_parser("post', 'add_parser("publish'):
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()
