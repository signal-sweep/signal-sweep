#!/usr/bin/env python3
"""Offline unit tests for mention_sweep. Stdlib unittest only — no pytest, no
network, no live gh. Every subprocess.run is patched to return fixture JSON.

Run: python -m unittest discover -s modules/mention_sweep -p 'test_*.py'
"""

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mention_sweep as ms  # noqa: E402


def _proc(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _issue_node(url, title, repo="acme/widgets", stars=500, body="", author="someone"):
    return {
        "title": title,
        "url": url,
        "createdAt": "2026-06-10T00:00:00Z",
        "bodyText": body,
        "state": "OPEN",
        "repository": {"nameWithOwner": repo, "stargazerCount": stars},
        "comments": {"totalCount": 1},
        "author": {"login": author},
    }


def _graphql_payload(nodes):
    return json.dumps({"data": {"search": {"nodes": nodes}}})


def _code_hit(url, path, repo="acme/awesome-list", fragment="signal-sweep"):
    return {
        "path": path,
        "repository": {"nameWithOwner": repo, "url": f"https://github.com/{repo}"},
        "sha": "deadbeef",
        "textMatches": [{"fragment": fragment}],
        "url": url,
    }


class FakeGh:
    """A subprocess.run replacement that routes by the gh subcommand. GraphQL
    calls return `graphql_out`; `gh search code` calls return `code_out`. Both
    record the commands they saw so query construction can be asserted."""

    def __init__(self, graphql_out="", code_out="[]"):
        self.graphql_out = graphql_out
        self.code_out = code_out
        self.graphql_cmds = []
        self.code_cmds = []

    def __call__(self, cmd, *a, **k):
        if "graphql" in cmd:
            self.graphql_cmds.append(cmd)
            return _proc(stdout=self.graphql_out)
        if cmd[:3] == ["gh", "search", "code"]:
            self.code_cmds.append(cmd)
            return _proc(stdout=self.code_out)
        return _proc(stdout="{}")


def _write_config(tmp, **overrides):
    cfg = {
        "display_name": "test-project",
        "match_strings": ["signal-sweep", "github.com/acme/signal-sweep"],
        "own_repos": ["acme/signal-sweep"],
        "min_stars": 0,
        "per_repo_cap": 4,
        "per_query": 20,
        "emit_cap": 100,
        "seen_retention_days": 180,
        "default_window_days": 30,
        "scan_code_lane": True,
        "state_dir": str(Path(tmp) / "state"),
        "candidates_file": str(Path(tmp) / "candidates.json"),
    }
    cfg.update(overrides)
    cfg_path = Path(tmp) / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    return cfg_path, cfg


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class ClassifyTests(unittest.TestCase):
    def test_favorable_mention_is_default(self):
        self.assertEqual(
            ms.classify("We use signal-sweep in our pipeline", "great tool"),
            "favorable-mention",
        )

    def test_question_detected(self):
        self.assertEqual(
            ms.classify("How do I configure signal-sweep?", ""), "question"
        )

    def test_misdescription_detected(self):
        self.assertEqual(
            ms.classify("signal-sweep is abandoned and unmaintained", ""),
            "possible-misdescription",
        )

    def test_misdescription_wins_over_question(self):
        # A hit that is both a question and a misread should flag the more
        # important class (a correction opportunity) for the human.
        self.assertEqual(
            ms.classify("Is signal-sweep deprecated now?", ""),
            "possible-misdescription",
        )


class OwnRepoTests(unittest.TestCase):
    def test_own_repo_excluded_case_insensitive(self):
        self.assertTrue(ms.is_own_repo("Acme/Signal-Sweep", ["acme/signal-sweep"]))

    def test_foreign_repo_kept(self):
        self.assertFalse(ms.is_own_repo("other/repo", ["acme/signal-sweep"]))


class QueryConstructionTests(unittest.TestCase):
    def test_thread_lane_excludes_own_repos_and_quotes_term(self):
        cfg = {
            "match_strings": ["signal-sweep"],
            "own_repos": ["acme/signal-sweep", "acme/two"],
            "per_query": 20,
        }
        fake = FakeGh(graphql_out=_graphql_payload([]))
        errors = []
        with mock.patch.object(ms.subprocess, "run", fake):
            ms.thread_lane(cfg, "2026-06-01", errors)
        # Two match-string queries x ISSUE+DISCUSSION = one term -> 2 graphql calls.
        self.assertEqual(len(fake.graphql_cmds), 2)
        joined = " ".join(fake.graphql_cmds[0])
        self.assertIn('"signal-sweep"', joined)
        self.assertIn("-repo:acme/signal-sweep", joined)
        self.assertIn("-repo:acme/two", joined)
        self.assertIn("created:>2026-06-01", joined)
        # Issue query carries is:issue; discussion query does not.
        issue_q = " ".join(fake.graphql_cmds[0])
        disc_q = " ".join(fake.graphql_cmds[1])
        self.assertIn("is:issue", issue_q)
        self.assertNotIn("is:issue", disc_q)

    def test_code_lane_can_be_disabled(self):
        cfg = {"match_strings": ["x"], "per_query": 10, "scan_code_lane": False}
        fake = FakeGh()
        errors = []
        with mock.patch.object(ms.subprocess, "run", fake):
            out = ms.code_lane(cfg, errors)
        self.assertEqual(out, [])
        self.assertEqual(len(fake.code_cmds), 0)

    def test_code_lane_calls_gh_search_code_per_term(self):
        cfg = {"match_strings": ["a", "b"], "per_query": 10, "scan_code_lane": True}
        fake = FakeGh(code_out=json.dumps([_code_hit("u", "README.md")]))
        errors = []
        with mock.patch.object(ms.subprocess, "run", fake):
            out = ms.code_lane(cfg, errors)
        self.assertEqual(len(fake.code_cmds), 2)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["lane"], "code")


class DryRunTests(unittest.TestCase):
    def test_dry_run_makes_no_subprocess_calls_and_no_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, cfg = _write_config(tmp)
            called = {"n": 0}

            def boom(*a, **k):
                called["n"] += 1
                raise AssertionError("dry-run must not call subprocess")

            args = _Args(config=str(cfg_path), days=7, limit=None, dry_run=True)
            buf = io.StringIO()
            with mock.patch.object(ms.subprocess, "run", boom):
                with redirect_stdout(buf):
                    rc = ms.cmd_scan(args)
            self.assertEqual(rc, 0)
            self.assertEqual(called["n"], 0)
            self.assertIn("MENTION_SWEEP_DRY-RUN", buf.getvalue())
            # No candidates file, no state dir written.
            self.assertFalse(Path(cfg["candidates_file"]).exists())
            self.assertFalse(Path(cfg["state_dir"]).exists())


class ScanIntegrationTests(unittest.TestCase):
    def _run_scan(self, tmp, graphql_nodes, code_hits, **cfg_overrides):
        cfg_path, cfg = _write_config(tmp, **cfg_overrides)
        fake = FakeGh(
            graphql_out=_graphql_payload(graphql_nodes),
            code_out=json.dumps(code_hits),
        )
        args = _Args(config=str(cfg_path), days=30, limit=None, dry_run=False)
        buf = io.StringIO()
        with mock.patch.object(ms.subprocess, "run", fake):
            with redirect_stdout(buf):
                rc = ms.cmd_scan(args)
        payload = json.loads(Path(cfg["candidates_file"]).read_text(encoding="utf-8"))
        return rc, payload, cfg, buf.getvalue()

    def test_candidates_json_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            nodes = [_issue_node("https://x/1", "Loving signal-sweep", body="works")]
            hits = [_code_hit("https://x/code1", "awesome/README.md")]
            rc, payload, cfg, out = self._run_scan(tmp, nodes, hits)
            self.assertEqual(rc, 0)
            for key in (
                "scanned_at",
                "window_since",
                "by_kind",
                "by_match_type",
                "posting_density",
                "dropped",
                "errors",
                "candidates",
            ):
                self.assertIn(key, payload)
            self.assertIn("MENTION_SWEEP_OK", out)
            cand = payload["candidates"][0]
            for key in (
                "url",
                "title",
                "repo",
                "stars",
                "snippet",
                "match",
                "match_type",
                "kind",
                "lane",
            ):
                self.assertIn(key, cand)
            # Both lanes contributed.
            lanes = {c["lane"] for c in payload["candidates"]}
            self.assertEqual(lanes, {"thread", "code"})

    def test_own_repo_hit_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            nodes = [
                _issue_node("https://x/own", "self", repo="acme/signal-sweep"),
                _issue_node("https://x/ext", "external", repo="other/repo"),
            ]
            rc, payload, cfg, out = self._run_scan(tmp, nodes, [])
            urls = {c["url"] for c in payload["candidates"]}
            self.assertIn("https://x/ext", urls)
            self.assertNotIn("https://x/own", urls)
            self.assertGreaterEqual(payload["dropped"]["own"], 1)

    def test_dedup_against_seen_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            nodes = [_issue_node("https://x/seen", "again")]
            # First scan records the URL in the seen-store.
            rc1, p1, cfg, _ = self._run_scan(tmp, nodes, [])
            self.assertEqual(len(p1["candidates"]), 1)
            # Second scan with the same hit drops it as seen.
            rc2, p2, _cfg2, _ = self._run_scan(tmp, nodes, [])
            urls = {c["url"] for c in p2["candidates"]}
            self.assertNotIn("https://x/seen", urls)
            self.assertGreaterEqual(p2["dropped"]["seen"], 1)

    def test_dedup_against_posted_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, cfg = _write_config(tmp)
            # Pre-seed the ledger with a posted URL.
            state_dir = Path(cfg["state_dir"])
            state_dir.mkdir(parents=True, exist_ok=True)
            ledger = state_dir / "mention_sweep_log.jsonl"
            ledger.write_text(
                json.dumps(
                    {"date": "2026-06-15T00:00:00+00:00", "url": "https://x/posted"}
                )
                + "\n",
                encoding="utf-8",
            )
            nodes = [_issue_node("https://x/posted", "engaged already")]
            fake = FakeGh(graphql_out=_graphql_payload(nodes), code_out="[]")
            args = _Args(config=str(cfg_path), days=30, limit=None, dry_run=False)
            buf = io.StringIO()
            with mock.patch.object(ms.subprocess, "run", fake):
                with redirect_stdout(buf):
                    ms.cmd_scan(args)
            payload = json.loads(
                Path(cfg["candidates_file"]).read_text(encoding="utf-8")
            )
            urls = {c["url"] for c in payload["candidates"]}
            self.assertNotIn("https://x/posted", urls)
            self.assertGreaterEqual(payload["dropped"]["posted"], 1)

    def test_within_batch_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Same URL twice in one scan -> counted once, the rest as dup.
            nodes = [
                _issue_node("https://x/dup", "one"),
                _issue_node("https://x/dup", "two"),
            ]
            rc, payload, cfg, out = self._run_scan(tmp, nodes, [])
            urls = [c["url"] for c in payload["candidates"]]
            self.assertEqual(urls.count("https://x/dup"), 1)
            self.assertGreaterEqual(payload["dropped"]["dup"], 1)

    def test_min_stars_floor_on_thread_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            nodes = [
                _issue_node("https://x/low", "small repo", stars=5),
                _issue_node("https://x/high", "big repo", stars=900),
            ]
            rc, payload, cfg, out = self._run_scan(tmp, nodes, [], min_stars=100)
            urls = {c["url"] for c in payload["candidates"]}
            self.assertIn("https://x/high", urls)
            self.assertNotIn("https://x/low", urls)
            self.assertGreaterEqual(payload["dropped"]["stars"], 1)


class MarkPostedAndDensityTests(unittest.TestCase):
    def test_mark_posted_then_density(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, cfg = _write_config(tmp)
            comment_file = Path(tmp) / "reply.md"
            comment_file.write_text("I maintain this; here is the mechanism.", "utf-8")
            mark_args = _Args(
                config=str(cfg_path),
                url="https://x/engaged",
                kind="correct",
                comment_file=str(comment_file),
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = ms.cmd_mark_posted(mark_args)
            self.assertEqual(rc, 0)
            self.assertIn("LEDGER_OK", buf.getvalue())
            ledger = Path(cfg["state_dir"]) / "mention_sweep_log.jsonl"
            entry = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(entry["url"], "https://x/engaged")
            self.assertEqual(entry["kind"], "correct")
            # Density reads the ledger back.
            dbuf = io.StringIO()
            with redirect_stdout(dbuf):
                ms.cmd_density(_Args(config=str(cfg_path)))
            self.assertIn("posted replies:", dbuf.getvalue())


class NoAutoPostTests(unittest.TestCase):
    """The gate is the project's identity: discovery + drafting only. There must
    be NO code path that posts a comment or batch-approves. mark-posted only
    RECORDS a post the human already made."""

    def test_no_outbound_post_path_in_source(self):
        src = Path(ms.__file__).read_text(encoding="utf-8")
        banned = [
            "issues/{",  # gh issue-comment REST path
            "addDiscussionComment",  # discussion-comment GraphQL mutation
            "mutation",  # any GraphQL mutation
            "--auto",
            "auto_post",
            "auto-post",
            "batch_approve",
            "batch-approve",
            "schedule",
            "cron",
        ]
        for token in banned:
            self.assertNotIn(
                token,
                src,
                f"outbound/auto-post/scheduler token {token!r} must not appear",
            )

    def test_gh_helpers_never_use_a_post_verb(self):
        # The only gh calls are read-only: `gh api graphql` (query, asserted
        # below) and `gh search code`. No `gh issue comment`, no `gh pr`, no
        # `-X POST`/`--method POST`.
        captured = []

        def capture(cmd, *a, **k):
            captured.append(cmd)
            if "graphql" in cmd:
                return _proc(stdout=_graphql_payload([]))
            return _proc(stdout="[]")

        cfg = {
            "match_strings": ["x"],
            "own_repos": [],
            "per_query": 5,
            "scan_code_lane": True,
        }
        with mock.patch.object(ms.subprocess, "run", capture):
            ms.thread_lane(cfg, "2026-06-01", [])
            ms.code_lane(cfg, [])
        for cmd in captured:
            joined = " ".join(cmd)
            self.assertNotIn("issue comment", joined)
            self.assertNotIn("pr ", joined)
            self.assertNotIn("-X POST", joined)
            self.assertNotIn("--method POST", joined)
            # GraphQL bodies are query (read), never mutation (write).
            self.assertNotIn("mutation", joined)


class MatchTypeTests(unittest.TestCase):
    def test_url_terms_are_high_confidence(self):
        self.assertEqual(ms.match_kind("github.com/acme/widgets"), "url")
        self.assertEqual(ms.match_kind("https://github.com/acme/widgets"), "url")
        self.assertEqual(ms.match_kind("acme/widgets"), "url")

    def test_bare_name_is_low_confidence(self):
        self.assertEqual(ms.match_kind("signal-sweep"), "name")
        self.assertEqual(ms.match_kind("agent-workspace-architecture"), "name")

    def test_url_matches_rank_before_name_matches(self):
        # A bare-name hit with many stars and a url hit with few. The url hit
        # must sort first: match confidence outranks star count.
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, cfg = _write_config(
                tmp,
                match_strings=["signal-sweep", "github.com/acme/signal-sweep"],
                own_repos=[],
                min_stars=0,
                scan_code_lane=False,
            )

            class PerTermGh:
                def __call__(self, cmd, *a, **k):
                    joined = " ".join(cmd)
                    if "graphql" in cmd:
                        if "github.com/acme/signal-sweep" in joined:
                            return _proc(
                                stdout=_graphql_payload(
                                    [_issue_node("https://x/url", "url hit", stars=10)]
                                )
                            )
                        return _proc(
                            stdout=_graphql_payload(
                                [_issue_node("https://x/name", "name hit", stars=9000)]
                            )
                        )
                    return _proc(stdout="[]")

            args = _Args(config=str(cfg_path), days=30, limit=None, dry_run=False)
            buf = io.StringIO()
            with mock.patch.object(ms.subprocess, "run", PerTermGh()):
                with redirect_stdout(buf):
                    ms.cmd_scan(args)
            payload = json.loads(
                Path(cfg["candidates_file"]).read_text(encoding="utf-8")
            )
            cands = payload["candidates"]
            self.assertEqual(cands[0]["url"], "https://x/url")
            self.assertEqual(cands[0]["match_type"], "url")
            self.assertEqual(payload["by_match_type"].get("url"), 1)


if __name__ == "__main__":
    unittest.main()
