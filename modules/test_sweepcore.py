#!/usr/bin/env python3
"""Offline unit tests for sweepcore (stdlib unittest only).

sweepcore holds the shared primitives every module now imports, so these tests
are the regression guard for the four modules that have no tests of their own.
Every subprocess / network call is mocked. No live calls.
Run: python -m unittest discover -s modules -p 'test_sweepcore.py'
"""

import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sweepcore as sc  # noqa: E402


def _proc(stdout="", returncode=0, stderr=""):
    return mock.Mock(stdout=stdout, returncode=returncode, stderr=stderr)


class StateTests(unittest.TestCase):
    def test_load_state_missing_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                sc.load_state(Path(tmp) / "nope.json"),
                {"last_run": None, "seen": {}},
            )

    def test_load_state_corrupt_resets(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "s.json"
            p.write_text("{not json", encoding="utf-8")
            with mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(sc.load_state(p), {"last_run": None, "seen": {}})

    def test_load_state_valid_roundtrips(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "s.json"
            state = {
                "last_run": "2026-06-28T00:00:00+00:00",
                "seen": {"u": "2026-06-28"},
            }
            p.write_text(json.dumps(state), encoding="utf-8")
            self.assertEqual(sc.load_state(p), state)

    def test_write_json_atomic_creates_parent_and_no_tmp_left(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "sub" / "dir" / "out.json"
            sc.write_json_atomic(p, {"a": 1})
            self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"a": 1})
            # no leftover .tmp sibling
            self.assertEqual(list(p.parent.glob("*.tmp")), [])


class LedgerTests(unittest.TestCase):
    def test_append_ledger_roundtrip_and_mkdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = Path(tmp) / "state" / "ledger.jsonl"
            sc.append_ledger(led, {"url": "u1", "date": "2026-06-28T00:00:00+00:00"})
            sc.append_ledger(led, {"url": "u2", "date": "2026-06-28T00:00:00+00:00"})
            self.assertEqual(sc.posted_urls(led), {"u1", "u2"})

    def test_posted_urls_skips_malformed_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = Path(tmp) / "l.jsonl"
            led.write_text(
                '{"url": "u1"}\nnot json\n{"no_url": 1}\n{"url": "u2"}\n',
                encoding="utf-8",
            )
            self.assertEqual(sc.posted_urls(led), {"u1", "u2"})

    def test_density_counts_windows(self):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        recent = (now - timedelta(days=5)).isoformat()
        midish = (now - timedelta(days=60)).isoformat()
        old = (now - timedelta(days=200)).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            led = Path(tmp) / "l.jsonl"
            led.write_text(
                "\n".join(
                    json.dumps({"url": f"u{i}", "date": d})
                    for i, d in enumerate((recent, midish, old))
                ),
                encoding="utf-8",
            )
            counts = sc.density_counts(led)
            self.assertEqual(counts[30], 1)  # only the 5-day-old one
            self.assertEqual(counts[90], 2)  # 5-day + 60-day


class GhTests(unittest.TestCase):
    def test_gh_parses_json(self):
        with mock.patch.object(
            sc.subprocess, "run", return_value=_proc(stdout='{"a":1}')
        ):
            data, err = sc.gh(["api", "x"])
            self.assertIsNone(err)
            self.assertEqual(data, {"a": 1})

    def test_gh_returns_text_when_not_json(self):
        with mock.patch.object(
            sc.subprocess, "run", return_value=_proc(stdout="plain")
        ):
            data, err = sc.gh(["x"])
            self.assertIsNone(err)
            self.assertEqual(data, "plain")

    def test_gh_auth_failure_exits(self):
        with mock.patch.object(
            sc.subprocess,
            "run",
            return_value=_proc(returncode=1, stderr="HTTP 401 Bad credentials"),
        ):
            with self.assertRaises(SystemExit):
                sc.gh(["x"])

    def test_gh_nonauth_error_returns_err(self):
        with mock.patch.object(
            sc.subprocess,
            "run",
            return_value=_proc(returncode=1, stderr="some other failure"),
        ):
            data, err = sc.gh(["x"])
            self.assertIsNone(data)
            self.assertIn("some other failure", err)

    def test_gh_graphql_auth_failure_exits(self):
        with mock.patch.object(
            sc.subprocess,
            "run",
            return_value=_proc(returncode=1, stderr="gh auth login required"),
        ):
            with self.assertRaises(SystemExit):
                sc.gh_graphql("query{}")

    def test_gh_graphql_parses(self):
        with mock.patch.object(
            sc.subprocess, "run", return_value=_proc(stdout='{"data":{}}')
        ):
            data, err = sc.gh_graphql("query{}")
            self.assertIsNone(err)
            self.assertEqual(data, {"data": {}})


class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class HttpGetTests(unittest.TestCase):
    def test_200_returns_body(self):
        with mock.patch.object(
            sc.urllib.request, "urlopen", return_value=_FakeResp(200, "hello")
        ):
            status, body, err = sc.http_get("http://x")
            self.assertEqual((status, body, err), (200, "hello", None))

    def test_404_returns_immediately_no_retry(self):
        err404 = urllib.error.HTTPError("http://x", 404, "nf", {}, None)
        m = mock.Mock(side_effect=err404)
        with mock.patch.object(sc.urllib.request, "urlopen", m):
            status, body, err = sc.http_get("http://x", retries=2)
            self.assertEqual(status, 404)
            self.assertEqual(m.call_count, 1)  # no retry on 404

    def test_429_retries_then_succeeds(self):
        err429 = urllib.error.HTTPError("http://x", 429, "rl", {}, None)
        seq = [err429, _FakeResp(200, "ok")]

        def _side(*a, **k):
            v = seq.pop(0)
            if isinstance(v, Exception):
                raise v
            return v

        with mock.patch.object(sc.urllib.request, "urlopen", side_effect=_side):
            with mock.patch.object(sc.time, "sleep") as slept:
                status, body, err = sc.http_get("http://x", retries=2)
        self.assertEqual((status, body, err), (200, "ok", None))
        self.assertTrue(slept.called)  # backed off before the retry

    def test_urlerror_returns_none_status(self):
        with mock.patch.object(
            sc.urllib.request, "urlopen", side_effect=urllib.error.URLError("down")
        ):
            status, body, err = sc.http_get("http://x")
            self.assertIsNone(status)
            self.assertTrue(err)


class RelevanceTierTests(unittest.TestCase):
    def test_high_unanswered_pattern_popular(self):
        cand = {"is_answered": False, "comments": 0, "pattern": "memory", "stars": 5000}
        self.assertEqual(sc.relevance_tier(cand), "high")

    def test_url_match_is_high(self):
        self.assertEqual(
            sc.relevance_tier({"match_type": "url", "comments": 0}), "high"
        )

    def test_name_unconfirmed_is_low(self):
        self.assertEqual(
            sc.relevance_tier({"match_type": "name-unconfirmed", "comments": 5}), "low"
        )

    def test_watchlist_generic_is_low_or_med(self):
        cand = {"pattern": "watchlist", "comments": 5, "stars": 10}
        self.assertEqual(sc.relevance_tier(cand), "low")

    def test_tier_rank_orders(self):
        self.assertGreater(sc.TIER_RANK["high"], sc.TIER_RANK["med"])
        self.assertGreater(sc.TIER_RANK["med"], sc.TIER_RANK["low"])


if __name__ == "__main__":
    unittest.main()
