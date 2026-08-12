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
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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


def _issue_node(url="https://github.com/acme/widgets/issues/7"):
    """One well-formed, keepable search hit (foreign repo, over the star floor)."""
    return {
        "url": url,
        "title": "How do I wire memory?",
        "createdAt": "2026-07-10T00:00:00Z",
        "bodyText": "body text",
        "repository": {"nameWithOwner": "acme/widgets", "stargazerCount": 900},
        "comments": {"totalCount": 0},
        "isAnswered": False,
        "author": {"login": "someone"},
    }


class EarnedWindowTests(unittest.TestCase):
    """last_run is a claim about coverage, so a run may only advance it after
    proving it covered the window: at least one search came back and none
    failed. Both lanes read this window, so both feed the one report.

    The old guard asked only "did every request fail AND come back with
    nothing?", which let three unearned advances through — a partial failure
    that still returned items, a run that never issued a request at all, and a
    window narrowed to start after the stored marker. A fourth case did not
    advance so much as abort: an unparseable marker raised out of the scan.
    """

    OLD = "2026-07-01T00:00:00+00:00"

    def _setup(self, tmp, state_obj=None, **overrides):
        cfg = {
            "own_login": "me",
            "own_repo": "me/proj",
            "queries": {"memory": {"phrases": ["agent memory"]}},
            "watchlist": [],
            "state_dir": str(Path(tmp) / "state"),
            "candidates_file": str(Path(tmp) / "candidates.json"),
        }
        cfg.update(overrides)
        cfg_path = Path(tmp) / "config.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        state_file = Path(tmp) / "state" / "sweep_state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        if state_obj is not None:
            state_file.write_text(json.dumps(state_obj), encoding="utf-8")
        return cfg_path, state_file

    def _scan(self, cfg_path, graphql, days=None):
        args = argparse.Namespace(
            config=str(cfg_path), days=days, limit=None, dry_run=False
        )
        err = io.StringIO()
        with (
            mock.patch.object(ts, "gh_graphql", side_effect=graphql),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(err),
        ):
            ts.cmd_scan(args)
        return err.getvalue()

    @staticmethod
    def _marker(state_file):
        if not state_file.exists():
            return None
        return json.loads(state_file.read_text(encoding="utf-8")).get("last_run")

    @staticmethod
    def _digest(cfg_path):
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        return json.loads(Path(cfg["candidates_file"]).read_text(encoding="utf-8"))

    # --- fake GraphQL outcomes ---
    @staticmethod
    def _ok(query, **kw):
        """Every search comes back, holding nothing: a real, empty window."""
        return {"data": {"search": {"nodes": []}}}, None

    @staticmethod
    def _boom(query, **kw):
        return None, "HTTP 500 boom"

    @staticmethod
    def _never_called(query, **kw):  # pragma: no cover - asserts it is not run
        raise AssertionError("no search should have been issued")

    def _mixed(self):
        """The dangerous shape: one search lands, the next fails. Items come
        back, so a raw-count guard reads the run as a success."""
        calls = []

        def graphql(query, **kw):
            calls.append(query)
            if len(calls) == 1:
                return {"data": {"search": {"nodes": [_issue_node()]}}}, None
            return None, "HTTP 502 boom"

        return graphql

    def test_partial_failure_holds_the_marker_even_though_items_came_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(tmp, {"last_run": self.OLD, "seen": {}})
            self._scan(cfg_path, self._mixed())
            self.assertEqual(self._marker(state_file), self.OLD)

    def test_malformed_watchlist_entry_does_not_hold_the_marker(self):
        # A config typo is permanent, so gating the marker on it would freeze
        # the window on every future run. It is advisory, not a coverage gap:
        # the searches still came back, so the window WAS covered. list_sweep
        # already routes the identical case to advisory; this keeps the two
        # modules honest with each other.
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(
                tmp,
                {"last_run": self.OLD, "seen": {}},
                watchlist=["not-an-owner-name-pair"],
            )
            err = self._scan(cfg_path, self._ok)
            self.assertNotEqual(self._marker(state_file), self.OLD)
            # Still surfaced to the operator, just not marker-governing.
            self.assertIn("watchlist entry not in owner/name form", err)

    def test_watchlist_fetch_failure_still_holds_the_marker(self):
        # The control for the test above: a real fetch failure in the same lane
        # DOES mean ground went uncovered, so it must still hold.
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(
                tmp, {"last_run": self.OLD, "seen": {}}, queries={}, watchlist=["a/b"]
            )
            self._scan(cfg_path, self._boom)
            self.assertEqual(self._marker(state_file), self.OLD)

    def test_run_that_issued_no_search_holds_the_marker(self):
        # No error, no candidates, no request: the shape of a skipped run, and
        # the dangerous one — nothing about it reads as a failure.
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(
                tmp, {"last_run": self.OLD, "seen": {}}, queries={}, watchlist=[]
            )
            self._scan(cfg_path, self._never_called)
            self.assertEqual(self._marker(state_file), self.OLD)

    def test_total_failure_holds_the_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(tmp, {"last_run": self.OLD, "seen": {}})
            self._scan(cfg_path, self._boom)
            self.assertEqual(self._marker(state_file), self.OLD)

    def test_successful_empty_scan_still_advances(self):
        # The guard against over-correcting: a marker that never moves re-scans
        # a widening window forever. A search that came back holding nothing is
        # a covered window and must advance.
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(tmp, {"last_run": self.OLD, "seen": {}})
            self._scan(cfg_path, self._ok)
            self.assertNotEqual(self._marker(state_file), self.OLD)

    def test_failed_run_with_no_prior_marker_invents_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(tmp, {"seen": {}})
            self._scan(cfg_path, self._mixed())
            self.assertFalse(self._marker(state_file))

    def test_first_clean_run_with_no_prior_marker_lays_one_down(self):
        # The other direction of the same rule: absent is not unreadable.
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(tmp, {"seen": {}})
            self._scan(cfg_path, self._ok)
            stamp = self._marker(state_file)
            age = datetime.now(timezone.utc) - datetime.fromisoformat(stamp)
            self.assertAlmostEqual(age.total_seconds(), 0, delta=120)

    def test_narrow_days_override_does_not_swallow_the_uncovered_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            cfg_path, state_file = self._setup(tmp, {"last_run": old, "seen": {}})
            self._scan(cfg_path, self._ok, days=2)
            self.assertEqual(self._marker(state_file), old)

    def test_wide_days_override_covers_the_marker_and_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
            cfg_path, state_file = self._setup(tmp, {"last_run": old, "seen": {}})
            self._scan(cfg_path, self._ok, days=30)
            self.assertNotEqual(self._marker(state_file), old)

    def test_unreadable_marker_warns_and_re_windows_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, _state_file = self._setup(
                tmp, {"last_run": "last tuesday", "seen": {}}
            )
            err = self._scan(cfg_path, self._ok)
            self.assertIn("unreadable last_run", err)
            self.assertEqual(self._digest(cfg_path)["window_since"], _default_floor())

    def test_unreadable_marker_is_not_overwritten_by_an_unearned_stamp(self):
        # The re-windowed run cannot show it reached back to whatever the rotted
        # marker meant, so stamping `now` would bury the gap in front of the
        # default window AND destroy the evidence that the marker had rotted.
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(
                tmp, {"last_run": "last tuesday", "seen": {}}
            )
            self._scan(cfg_path, self._ok)
            self.assertEqual(self._marker(state_file), "last tuesday")

    def test_held_window_is_reported_in_the_digest_and_on_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, _state_file = self._setup(tmp, {"last_run": self.OLD, "seen": {}})
            err = self._scan(cfg_path, self._boom)
            self.assertTrue(self._digest(cfg_path)["window_held"])
            self.assertIn("keeping last_run", err)

    def test_clean_run_is_not_flagged_as_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, _state_file = self._setup(tmp, {"last_run": self.OLD, "seen": {}})
            self._scan(cfg_path, self._ok)
            self.assertFalse(self._digest(cfg_path)["window_held"])

    def test_held_window_is_re_covered_by_the_next_run(self):
        # The consequence that matters: whatever was published during the failed
        # run is still inside the window the next run asks for.
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, _state_file = self._setup(tmp, {"last_run": self.OLD, "seen": {}})
            self._scan(cfg_path, self._boom)
            self._scan(cfg_path, self._ok)
            self.assertEqual(self._digest(cfg_path)["window_since"], self.OLD[:10])


def _default_floor():
    """The date a 14-day default window starts on, as the digest formats it."""
    return (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d")


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
            "own_login": "me",
            "own_repo": "me/proj",
            "queries": {"memory": {"phrases": ["agent memory"]}},
            "state_dir": str(Path(tmp) / "state"),
            "candidates_file": str(Path(tmp) / "candidates.json"),
            "watchlist": [],
        }
        cfg_path = Path(tmp) / "config.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        cand_path = Path(cfg["candidates_file"])
        cand_path.write_text(self.SENTINEL, encoding="utf-8")
        state_file = Path(tmp) / "state" / "sweep_state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            json.dumps({"last_run": "2026-07-01T00:00:00+00:00", "seen": {}}),
            encoding="utf-8",
        )
        return cfg_path, cand_path, state_file

    @staticmethod
    def _one_hit(query, **kw):
        return {
            "data": {
                "search": {
                    "nodes": [
                        {
                            "url": "https://github.com/acme/widgets/issues/7",
                            "title": "How do I wire memory?",
                            "createdAt": "2026-07-10T00:00:00Z",
                            "bodyText": "body text",
                            "repository": {
                                "nameWithOwner": "acme/widgets",
                                "stargazerCount": 900,
                            },
                            "comments": {"totalCount": 0},
                            "isAnswered": False,
                            "author": {"login": "someone"},
                        }
                    ]
                }
            }
        }, None

    def _scan(self, cfg_path, dry_run):
        args = argparse.Namespace(
            config=str(cfg_path), days=30, limit=None, dry_run=dry_run
        )
        buf = io.StringIO()
        patched = mock.patch.object(ts, "gh_graphql", side_effect=self._one_hit)
        with patched, contextlib.redirect_stdout(buf):
            ts.cmd_scan(args)
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
