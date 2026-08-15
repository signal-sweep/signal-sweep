#!/usr/bin/env python3
"""Offline unit tests for release-sweep (stdlib unittest only).

No network, no live gh: sweepcore.gh is patched where a helper calls it. The
star helper here is bucket_commits — a pure Conventional-Commit classifier with
no I/O at all. load_channels validation, resolve_repo and previous_tag (gh
mocked), plus the NoAutoPost source guard round it out.

Run: python -m unittest discover -s modules/release_sweep -p 'test_*.py'
"""

import contextlib
import io
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


class BriefEmptyMessageTests(unittest.TestCase):
    def test_empty_commit_message_does_not_crash_brief(self):
        # git permits empty commit messages (--allow-empty-message); the
        # subject extraction must not IndexError on "".splitlines() == [].
        rel = {
            "tagName": "v2.0.0",
            "name": "v2.0.0",
            "publishedAt": "2026-07-01T00:00:00Z",
            "body": "notes",
            "url": "https://github.com/o/r/releases/tag/v2.0.0",
        }
        cmp_payload = {
            "total_commits": 2,
            "files": [],
            "commits": [
                {"commit": {"message": ""}},
                {"commit": {"message": "feat: add thing\n\nbody"}},
            ],
        }
        gh_seq = [(rel, None), (cmp_payload, None)]
        with tempfile.TemporaryDirectory() as tmp:
            chan = Path(tmp) / "channels.json"
            chan.write_text(
                json.dumps({"channels": [{"name": "blog"}]}), encoding="utf-8"
            )
            args = mock.Mock(
                config=str(chan), repo="o/r", tag="v2.0.0", since="v1.0.0", force=False
            )
            with (
                mock.patch.object(rs, "gh", side_effect=gh_seq),
                mock.patch.object(rs, "BRIEF_PATH", Path(tmp) / "brief.json"),
                mock.patch.object(rs, "LEDGER_PATH", Path(tmp) / "announced_log.jsonl"),
            ):
                rc = rs.cmd_brief(args)
            brief = json.loads((Path(tmp) / "brief.json").read_text(encoding="utf-8"))
        self.assertEqual(rc, 0)
        self.assertIn("add thing", brief["highlights"]["feat"])


class BriefHarness:
    """Shared cmd_brief driver for the guard suites below. A plain mixin, not a
    TestCase, so the suites that reuse it don't re-run each other's tests."""

    RELEASE = {
        "name": "release",
        "publishedAt": "2026-07-01T00:00:00Z",
        "body": "notes",
        "url": "https://github.com/o/r/releases/tag/v2.0.0",
    }
    COMPARE = {
        "total_commits": 1,
        "files": [],
        "commits": [{"commit": {"message": "feat: add thing"}}],
    }

    def _brief(
        self,
        tmp,
        recorded,
        tag="v2.0.0",
        force=False,
        channels=("hn", "blog"),
        repo="o/r",
    ):
        """Run cmd_brief against a ledger seeded with `recorded` entries;
        return (rc, brief-dict, stdout).

        Each recorded item is a (version, channel) pair - written against
        `repo`, the shape mark-announced now records - or a
        (entry_repo, version, channel) triple to name a different repo.
        entry_repo None writes a legacy line carrying no repo field at all.
        """
        ledger = Path(tmp) / "announced_log.jsonl"
        if recorded:
            lines = []
            for item in recorded:
                entry_repo, version, channel = (
                    item if len(item) == 3 else (repo, item[0], item[1])
                )
                entry = {
                    "date": "2026-07-01T00:00:00+00:00",
                    "version": version,
                    "channel": channel,
                }
                if entry_repo is not None:
                    entry["repo"] = entry_repo
                lines.append(json.dumps(entry) + "\n")
            ledger.write_text("".join(lines), encoding="utf-8")
        cfg = Path(tmp) / "channels.json"
        cfg.write_text(
            json.dumps({"channels": [{"name": n} for n in channels]}), encoding="utf-8"
        )
        rel = dict(self.RELEASE, tagName=tag)
        args = mock.Mock(
            config=str(cfg), repo=repo, tag=tag, since="v1.0.0", force=force
        )
        out = io.StringIO()
        with (
            mock.patch.object(
                rs, "gh", side_effect=[(rel, None), (self.COMPARE, None)]
            ),
            mock.patch.object(rs, "BRIEF_PATH", Path(tmp) / "brief.json"),
            mock.patch.object(rs, "LEDGER_PATH", ledger),
            contextlib.redirect_stdout(out),
        ):
            rc = rs.cmd_brief(args)
        brief = json.loads((Path(tmp) / "brief.json").read_text(encoding="utf-8"))
        return rc, brief, out.getvalue()


class DoubleAnnounceGuardTests(BriefHarness, unittest.TestCase):
    """The documented never-double-announce guard: `brief` reads its own
    announced ledger, so a channel already recorded for this repo + version
    drops out of the drafting material. A genuinely new release must still
    surface."""

    def test_announced_channel_is_dropped_from_second_brief(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, brief, out = self._brief(tmp, [("v2.0.0", "hn")])
        self.assertEqual(rc, 0)
        names = [c.get("name") for c in brief["channels"]]
        self.assertNotIn("hn", names)
        self.assertEqual(names, ["blog"])
        self.assertEqual(brief["already_announced"], ["hn"])
        self.assertIn("already announced", out)
        self.assertIn("hn", out)

    def test_every_channel_announced_marks_the_release_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, brief, out = self._brief(tmp, [("v2.0.0", "hn"), ("v2.0.0", "blog")])
        self.assertEqual(rc, 0)
        self.assertEqual(brief["channels"], [])
        self.assertEqual(brief["already_announced"], ["blog", "hn"])
        self.assertIn("RELEASE_BRIEF_SKIP", out)
        self.assertNotIn("RELEASE_BRIEF_OK", out)

    def test_new_release_still_surfaces_every_channel(self):
        # Same channels announced for the PREVIOUS version only: the new tag is
        # unannounced, so nothing is suppressed.
        with tempfile.TemporaryDirectory() as tmp:
            rc, brief, out = self._brief(
                tmp, [("v1.0.0", "hn"), ("v1.0.0", "blog")], tag="v2.0.0"
            )
        self.assertEqual(rc, 0)
        self.assertEqual([c.get("name") for c in brief["channels"]], ["hn", "blog"])
        self.assertEqual(brief["already_announced"], [])
        self.assertIn("RELEASE_BRIEF_OK", out)

    def test_empty_ledger_leaves_every_channel_in_the_brief(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, brief, out = self._brief(tmp, [])
        self.assertEqual(rc, 0)
        self.assertEqual([c.get("name") for c in brief["channels"]], ["hn", "blog"])
        self.assertIn("RELEASE_BRIEF_OK", out)

    def test_force_readmits_an_announced_channel_but_marks_it(self):
        # The escape hatch for a redraft (a post that got deleted). It only
        # re-assembles material; posting stays a per-channel human action.
        with tempfile.TemporaryDirectory() as tmp:
            rc, brief, out = self._brief(tmp, [("v2.0.0", "hn")], force=True)
        self.assertEqual(rc, 0)
        by_name = {c.get("name"): c for c in brief["channels"]}
        self.assertEqual(set(by_name), {"hn", "blog"})
        self.assertIs(by_name["hn"].get("already_announced"), True)
        self.assertNotIn("already_announced", by_name["blog"])
        self.assertIn("--force", out)

    def test_corrupt_ledger_line_does_not_break_the_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "announced_log.jsonl"
            ledger.write_text(
                'not json\n{"version": "v2.0.0", "channel": "hn"}\n\n', encoding="utf-8"
            )
            cfg = Path(tmp) / "channels.json"
            cfg.write_text(
                json.dumps({"channels": [{"name": "hn"}, {"name": "blog"}]}),
                encoding="utf-8",
            )
            rel = dict(self.RELEASE, tagName="v2.0.0")
            args = mock.Mock(
                config=str(cfg), repo="o/r", tag="v2.0.0", since="v1.0.0", force=False
            )
            with (
                mock.patch.object(
                    rs, "gh", side_effect=[(rel, None), (self.COMPARE, None)]
                ),
                mock.patch.object(rs, "BRIEF_PATH", Path(tmp) / "brief.json"),
                mock.patch.object(rs, "LEDGER_PATH", ledger),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                rc = rs.cmd_brief(args)
            brief = json.loads((Path(tmp) / "brief.json").read_text(encoding="utf-8"))
        self.assertEqual(rc, 0)
        self.assertEqual([c.get("name") for c in brief["channels"]], ["blog"])

    def test_announced_channels_reads_the_ledger_by_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "announced_log.jsonl"
            ledger.write_text(
                '{"repo": "o/r", "version": "v1", "channel": "hn"}\n'
                '{"repo": "o/r", "version": "v2", "channel": "x"}\n'
                '{"repo": "o/r", "version": "v2", "channel": "hn"}\n',
                encoding="utf-8",
            )
            self.assertEqual(
                set(rs.announced_channels("v2", ledger, repo="o/r")), {"x", "hn"}
            )
            self.assertEqual(
                set(rs.announced_channels("v1", ledger, repo="o/r")), {"hn"}
            )
            self.assertEqual(
                set(rs.announced_channels("v3", ledger, repo="o/r")), set()
            )

    def test_brief_never_writes_to_the_ledger(self):
        # Gate guard: the guard makes `brief` a ledger READER. Recording an
        # announcement stays the human's explicit mark-announced step, so no
        # brief path (including --force) may append to the ledger.
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "announced_log.jsonl"
            for force in (False, True):
                self._brief(tmp, [("v2.0.0", "hn")], force=force)
                self.assertEqual(
                    len(
                        [
                            ln
                            for ln in ledger.read_text(encoding="utf-8").splitlines()
                            if ln.strip()
                        ]
                    ),
                    1,
                )

    def test_announced_channels_on_missing_ledger_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.jsonl"
            self.assertEqual(
                set(rs.announced_channels("v1", missing, repo="o/r")), set()
            )


class CrossRepoLedgerTests(BriefHarness, unittest.TestCase):
    """One checkout, one ledger, many repos: the dedup key is
    (repo, version, channel). A v1.0.0 announced for one repo must not suppress
    a genuinely new v1.0.0 of another - and must still suppress its own."""

    def test_same_tag_in_another_repo_is_not_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, brief, out = self._brief(
                tmp,
                [("org/a", "v1.0.0", "hn"), ("org/a", "v1.0.0", "blog")],
                tag="v1.0.0",
                repo="org/b",
            )
        self.assertEqual(rc, 0)
        self.assertEqual([c.get("name") for c in brief["channels"]], ["hn", "blog"])
        self.assertEqual(brief["already_announced"], [])
        self.assertIn("RELEASE_BRIEF_OK", out)
        self.assertNotIn("RELEASE_BRIEF_SKIP", out)

    def test_same_repo_tag_and_channel_is_still_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, brief, out = self._brief(
                tmp, [("org/a", "v1.0.0", "hn")], tag="v1.0.0", repo="org/a"
            )
        self.assertEqual(rc, 0)
        self.assertEqual([c.get("name") for c in brief["channels"]], ["blog"])
        self.assertEqual(brief["already_announced"], ["hn"])

    def test_every_channel_announced_for_this_repo_only_skips_this_repo(self):
        recorded = [
            ("org/a", "v1.0.0", "hn"),
            ("org/a", "v1.0.0", "blog"),
            ("org/b", "v1.0.0", "hn"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            rc, _, out = self._brief(tmp, recorded, tag="v1.0.0", repo="org/a")
            self.assertIn("RELEASE_BRIEF_SKIP", out)
            rc_b, brief_b, out_b = self._brief(
                tmp, recorded, tag="v1.0.0", repo="org/b"
            )
        self.assertEqual((rc, rc_b), (0, 0))
        self.assertEqual([c.get("name") for c in brief_b["channels"]], ["blog"])
        self.assertIn("RELEASE_BRIEF_OK", out_b)

    def test_announced_channels_keys_on_repo_as_well_as_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "announced_log.jsonl"
            ledger.write_text(
                '{"repo": "org/a", "version": "v1", "channel": "hn"}\n'
                '{"repo": "org/b", "version": "v1", "channel": "blog"}\n',
                encoding="utf-8",
            )
            self.assertEqual(set(rs.announced_channels("v1", ledger, "org/a")), {"hn"})
            self.assertEqual(
                set(rs.announced_channels("v1", ledger, "org/b")), {"blog"}
            )
            self.assertEqual(set(rs.announced_channels("v1", ledger, "org/c")), set())


class LegacyLedgerMigrationTests(BriefHarness, unittest.TestCase):
    """Documented migration: a ledger line written before the repo field
    existed carries no repo, so it applies to EVERY repo. That fails closed
    (the repo that wrote it is still guarded) at the cost of over-matching, and
    the over-match is reported rather than silent - `--force` and a one-line
    ledger edit both clear it."""

    def test_legacy_entry_applies_to_every_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, brief, out = self._brief(
                tmp, [(None, "v1.0.0", "hn")], tag="v1.0.0", repo="org/b"
            )
        self.assertEqual(rc, 0)
        self.assertEqual([c.get("name") for c in brief["channels"]], ["blog"])
        self.assertEqual(brief["already_announced"], ["hn"])
        self.assertEqual(brief["already_announced_legacy"], ["hn"])

    def test_legacy_suppression_is_reported_not_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, out = self._brief(
                tmp, [(None, "v1.0.0", "hn")], tag="v1.0.0", repo="org/b"
            )
        self.assertIn("pre-repo ledger line", out)
        self.assertIn("hn", out)

    def test_legacy_reported_on_the_skip_path_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, brief, out = self._brief(
                tmp,
                [(None, "v1.0.0", "hn"), (None, "v1.0.0", "blog")],
                tag="v1.0.0",
                repo="org/b",
            )
        self.assertEqual(rc, 0)
        self.assertIn("RELEASE_BRIEF_SKIP", out)
        self.assertIn("pre-repo ledger line", out)
        self.assertEqual(brief["already_announced_legacy"], ["blog", "hn"])

    def test_a_repo_scoped_entry_is_never_reported_as_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, brief, out = self._brief(
                tmp, [("org/b", "v1.0.0", "hn")], tag="v1.0.0", repo="org/b"
            )
        self.assertEqual(brief["already_announced"], ["hn"])
        self.assertEqual(brief["already_announced_legacy"], [])
        self.assertNotIn("pre-repo ledger line", out)

    def test_force_still_readmits_a_legacy_matched_channel(self):
        # The documented escape hatch has to work for the over-matching case,
        # or a legacy line would be an unclearable block on another repo.
        with tempfile.TemporaryDirectory() as tmp:
            rc, brief, out = self._brief(
                tmp, [(None, "v1.0.0", "hn")], tag="v1.0.0", repo="org/b", force=True
            )
        by_name = {c.get("name"): c for c in brief["channels"]}
        self.assertEqual(set(by_name), {"hn", "blog"})
        self.assertIs(by_name["hn"].get("already_announced"), True)


class LedgerKeyNormalisationTests(BriefHarness, unittest.TestCase):
    """The key fields are operator-typed free text, so they are normalised on
    both write and read. Drift in case or whitespace must fail CLOSED (still
    match, still suppress), never open into a second announcement."""

    def test_normalise_key_folds_case_and_collapses_whitespace(self):
        self.assertEqual(rs.normalise_key("  Show   HN "), "show hn")
        self.assertEqual(rs.normalise_key("V1.0.0"), "v1.0.0")
        self.assertEqual(rs.normalise_key("Org/A"), "org/a")
        self.assertEqual(rs.normalise_key(""), "")
        self.assertEqual(rs.normalise_key(None), "")
        self.assertEqual(rs.normalise_key(17), "")

    def test_case_and_whitespace_drift_still_suppresses(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, brief, out = self._brief(
                tmp,
                [("Org/A", " V1.0.0 ", "  Show   HN ")],
                tag="v1.0.0",
                repo="org/a",
                channels=("show hn", "blog"),
            )
        self.assertEqual(rc, 0)
        self.assertEqual([c.get("name") for c in brief["channels"]], ["blog"])
        self.assertEqual(brief["already_announced"], ["show hn"])

    def test_drift_in_the_config_channel_name_still_suppresses(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, brief, _ = self._brief(
                tmp,
                [("org/a", "v1.0.0", "show hn")],
                tag="v1.0.0",
                repo="org/a",
                channels=(" Show  HN", "blog"),
            )
        self.assertEqual(rc, 0)
        self.assertEqual([c.get("name") for c in brief["channels"]], ["blog"])
        self.assertEqual(brief["already_announced"], [" Show  HN"])

    def test_a_different_channel_is_not_swallowed_by_normalisation(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, brief, _ = self._brief(
                tmp,
                [("org/a", "v1.0.0", "show-hn")],
                tag="v1.0.0",
                repo="org/a",
                channels=("show hn", "blog"),
            )
        self.assertEqual(
            [c.get("name") for c in brief["channels"]], ["show hn", "blog"]
        )


class MarkAnnouncedTests(unittest.TestCase):
    """mark-announced is the human's record-what-I-posted step. It writes the
    repo it was recorded against so the guard can scope to it."""

    def _mark(self, tmp, version, channel, repo="org/a", note=None):
        ledger = Path(tmp) / "announced_log.jsonl"
        args = mock.Mock(version=version, channel=channel, repo=repo, note=note)
        out = io.StringIO()
        with (
            mock.patch.object(rs, "LEDGER_PATH", ledger),
            contextlib.redirect_stdout(out),
        ):
            rc = rs.cmd_mark_announced(args)
        lines = [
            json.loads(ln)
            for ln in ledger.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        return rc, lines, out.getvalue()

    def test_entry_records_the_repo_normalised(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, lines, out = self._mark(tmp, " V1.0.0 ", "  Show   HN ", repo="Org/A")
        self.assertEqual(rc, 0)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["repo"], "org/a")
        self.assertEqual(lines[0]["version"], "v1.0.0")
        self.assertEqual(lines[0]["channel"], "show hn")
        # The confirmation echoes the stored key, so a fold is visible at
        # record time rather than only when a later brief acts on it.
        self.assertIn("LEDGER_OK org/a v1.0.0 -> show hn", out)

    def test_repo_is_detected_when_not_passed(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = mock.Mock(version="v1.0.0", channel="hn", repo=None, note=None)
            with (
                mock.patch.object(rs, "LEDGER_PATH", Path(tmp) / "announced_log.jsonl"),
                mock.patch.object(rs, "gh", return_value=("Org/Detected", None)),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                rs.cmd_mark_announced(args)
            entry = json.loads(
                (Path(tmp) / "announced_log.jsonl").read_text(encoding="utf-8").strip()
            )
        self.assertEqual(entry["repo"], "org/detected")

    def test_recorded_announcement_suppresses_only_its_own_repo(self):
        # End-to-end over the real write path: mark, then brief both repos.
        with tempfile.TemporaryDirectory() as tmp:
            self._mark(tmp, "v1.0.0", "hn", repo="org/a")
            rel = dict(BriefHarness.RELEASE, tagName="v1.0.0")
            cfg = Path(tmp) / "channels.json"
            cfg.write_text(
                json.dumps({"channels": [{"name": "hn"}, {"name": "blog"}]}),
                encoding="utf-8",
            )
            briefs = {}
            for repo in ("org/a", "org/b"):
                args = mock.Mock(
                    config=str(cfg),
                    repo=repo,
                    tag="v1.0.0",
                    since="v0.9.0",
                    force=False,
                )
                with (
                    mock.patch.object(
                        rs,
                        "gh",
                        side_effect=[(rel, None), (BriefHarness.COMPARE, None)],
                    ),
                    mock.patch.object(rs, "BRIEF_PATH", Path(tmp) / "brief.json"),
                    mock.patch.object(
                        rs, "LEDGER_PATH", Path(tmp) / "announced_log.jsonl"
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    rs.cmd_brief(args)
                briefs[repo] = json.loads(
                    (Path(tmp) / "brief.json").read_text(encoding="utf-8")
                )
        self.assertEqual([c["name"] for c in briefs["org/a"]["channels"]], ["blog"])
        self.assertEqual(
            [c["name"] for c in briefs["org/b"]["channels"]], ["hn", "blog"]
        )

    def test_log_shows_the_repo_and_flags_a_legacy_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "announced_log.jsonl"
            ledger.write_text(
                '{"date": "2026-07-01T00:00:00+00:00", "repo": "org/a",'
                ' "version": "v1.0.0", "channel": "hn"}\n'
                '{"date": "2026-07-01T00:00:00+00:00", "version": "v0.9.0",'
                ' "channel": "blog"}\n',
                encoding="utf-8",
            )
            out = io.StringIO()
            with (
                mock.patch.object(rs, "LEDGER_PATH", ledger),
                contextlib.redirect_stdout(out),
            ):
                rs.cmd_log(mock.Mock())
        printed = out.getvalue()
        self.assertIn("org/a", printed)
        self.assertIn("(any repo)", printed)


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
