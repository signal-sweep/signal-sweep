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
from datetime import datetime, timedelta, timezone
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

    def test_load_state_wrong_shape_resets(self):
        # Valid JSON that is not an object still breaks every caller (they all
        # do state.get(...)), so the documented "reset cleanly if corrupt"
        # contract has to cover shape, not just parseability.
        for payload in ("[]", '["a", "b"]', '"a string"', "42", "null", "true"):
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as tmp:
                    p = Path(tmp) / "s.json"
                    p.write_text(payload, encoding="utf-8")
                    err = io.StringIO()
                    with mock.patch("sys.stderr", err):
                        state = sc.load_state(p)
                    self.assertEqual(state, {"last_run": None, "seen": {}})
                    # the reset stays visible rather than silently swallowing
                    self.assertIn("WARN", err.getvalue())

    def test_load_state_malformed_json_resets_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "s.json"
            p.write_text("{not json", encoding="utf-8")
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                state = sc.load_state(p)
            self.assertEqual(state, {"last_run": None, "seen": {}})
            self.assertIn("WARN", err.getvalue())

    def test_load_state_valid_dict_returned_untouched(self):
        # Control for the two resets above: a well-shaped state must survive
        # verbatim, with no warning emitted.
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "s.json"
            state = {
                "last_run": "2026-06-28T00:00:00+00:00",
                "seen": {"https://example.invalid/1": "2026-06-28"},
                "extra": [1, 2, 3],
            }
            p.write_text(json.dumps(state), encoding="utf-8")
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                self.assertEqual(sc.load_state(p), state)
            self.assertEqual(err.getvalue(), "")

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


class WindowMarkerTests(unittest.TestCase):
    """The earned-marker rule every scanning module shares.

    A last_run marker claims coverage, so it may only move when the run proved
    it covered the window. The opposite failure is equally real: a marker that
    never moves re-scans a widening window forever, so a fetch that came back
    empty must still advance.
    """

    NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

    def test_lane_report_is_clean_only_after_a_fetch_with_no_error(self):
        report = sc.LaneReport()
        self.assertFalse(report.clean)  # nothing fetched yet
        report.fetch_ok()
        self.assertTrue(report.clean)  # fetched, no errors: a covered window
        report.append("boom")
        self.assertFalse(report.clean)  # one failure taints the whole run

    def test_lane_report_with_errors_but_no_fetch_is_not_clean(self):
        report = sc.LaneReport()
        report.append("boom")
        self.assertFalse(report.clean)

    def test_note_fetch_ok_is_a_no_op_on_a_plain_list(self):
        # Lane helpers are called with a bare list in tests and ad-hoc use.
        errors = []
        sc.note_fetch_ok(errors)  # must not raise
        self.assertEqual(errors, [])

    def test_note_fetch_ok_counts_on_a_lane_report(self):
        report = sc.LaneReport()
        sc.note_fetch_ok(report)
        self.assertEqual(report.fetches_ok, 1)

    def test_hold_reason_distinguishes_failure_from_no_request(self):
        failed = sc.LaneReport()
        failed.append("HTTP 500")
        self.assertIn("failed", sc.hold_reason(failed))
        self.assertIn("no request", sc.hold_reason(sc.LaneReport()))

    def test_parse_stamp_handles_absent_naive_and_junk(self):
        self.assertIsNone(sc.parse_stamp(None))
        self.assertIsNone(sc.parse_stamp(""))
        self.assertIsNone(sc.parse_stamp("last tuesday"))
        self.assertIsNone(sc.parse_stamp(42))
        naive = sc.parse_stamp("2026-07-01T00:00:00")
        self.assertEqual(naive.tzinfo, timezone.utc)

    def test_window_start_prefers_the_days_override(self):
        since = sc.window_start("2026-07-01T00:00:00+00:00", 14, self.NOW, 3)
        self.assertEqual(since, self.NOW - timedelta(days=3))

    def test_window_start_uses_the_stored_marker(self):
        since = sc.window_start("2026-07-01T00:00:00+00:00", 14, self.NOW)
        self.assertEqual(since, datetime(2026, 7, 1, tzinfo=timezone.utc))

    def test_window_start_falls_back_to_the_default_on_a_first_run(self):
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            since = sc.window_start(None, 14, self.NOW)
        self.assertEqual(since, self.NOW - timedelta(days=14))
        # Absent is normal, not a fault: no warning for a genuine first run.
        self.assertEqual(err.getvalue(), "")

    def test_window_start_warns_before_re_windowing_a_rotted_marker(self):
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            since = sc.window_start("last tuesday", 14, self.NOW, label="hn")
        self.assertEqual(since, self.NOW - timedelta(days=14))
        self.assertIn("unreadable last_run for hn", err.getvalue())

    def test_earned_stamp_advances_when_the_window_reached_the_marker(self):
        prior = "2026-07-25T00:00:00+00:00"
        since = datetime(2026, 7, 25, tzinfo=timezone.utc)
        self.assertEqual(sc.earned_stamp(prior, since, self.NOW), self.NOW.isoformat())

    def test_earned_stamp_holds_when_the_window_started_after_the_marker(self):
        # A narrowed run leaves the stretch in front of the window unread;
        # stamping `now` would swallow it silently and permanently.
        prior = "2026-07-01T00:00:00+00:00"
        since = datetime(2026, 7, 30, tzinfo=timezone.utc)
        self.assertEqual(sc.earned_stamp(prior, since, self.NOW), prior)

    def test_earned_stamp_holds_a_rotted_marker_verbatim(self):
        since = self.NOW - timedelta(days=14)
        self.assertEqual(
            sc.earned_stamp("last tuesday", since, self.NOW), "last tuesday"
        )

    def test_earned_stamp_lays_down_a_first_marker(self):
        # Absent is not unreadable: with no earlier marker there is nothing to
        # fall short of, so a completed fetch must stamp or the run re-scans the
        # default window forever.
        since = self.NOW - timedelta(days=14)
        self.assertEqual(sc.earned_stamp(None, since, self.NOW), self.NOW.isoformat())


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

    def test_gh_rate_limit_with_401_in_its_ids_is_not_an_auth_failure(self):
        # The digits 401 turn up in request IDs, user IDs and timestamps. A
        # rate limit is the one gh error a caller can wait out, so it has to
        # come back as an ordinary error, not end the run.
        stderr = (
            "HTTP 403: API rate limit exceeded for user ID 1401. If you reach "
            "out to GitHub Support for help, please include the request ID "
            "E401:30B63C:DFB37 and timestamp 2026-10-02 11:05:09 UTC."
        )
        with mock.patch.object(
            sc.subprocess, "run", return_value=_proc(returncode=1, stderr=stderr)
        ):
            data, err = sc.gh(["x"])
            self.assertIsNone(data)
            self.assertIn("rate limit", err)

    def test_is_auth_failure_matches_the_status_not_the_bare_digits(self):
        self.assertTrue(sc.is_auth_failure("HTTP 401: Bad credentials"))
        self.assertTrue(sc.is_auth_failure("gh: Requires authentication (HTTP 401)"))
        self.assertTrue(sc.is_auth_failure("run gh auth login to continue"))
        self.assertFalse(sc.is_auth_failure("HTTP 502: upstream, request ID A401:B2"))
        self.assertFalse(sc.is_auth_failure(""))
        self.assertFalse(sc.is_auth_failure(None))

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

    def test_429_waits_out_the_advertised_ratelimit_window(self):
        # The whole-path version of the header rule below: a 429 that names
        # the seconds left in its window must be retried AFTER the window, not
        # 1s later inside it where the retry is guaranteed to fail again.
        err429 = urllib.error.HTTPError(
            "http://x", 429, "rl", {"x-ratelimit-reset": "59"}, None
        )
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
        slept.assert_called_once_with(60.0)


class RetryAfterSecondsTests(unittest.TestCase):
    """How long a 429/503 retry waits. Retry-After wins when it is usable; an
    x-ratelimit-reset count of seconds is the fallback for venues that send no
    Retry-After (observed 2026-10-02: a one-request-per-minute anonymous quota
    answered only with `x-ratelimit-reset`, so 1s and 2s backoff retries both
    landed inside the same window and failed); with neither, plain bounded
    exponential backoff, exactly as before."""

    @staticmethod
    def _exc(headers):
        return urllib.error.HTTPError("http://x", 429, "rl", headers, None)

    def test_ratelimit_reset_is_honoured_with_the_margin(self):
        wait = sc._retry_after_seconds(self._exc({"x-ratelimit-reset": "59"}), 0, 2.0)
        self.assertEqual(wait, 59 + sc.RATELIMIT_RESET_MARGIN)
        self.assertEqual(wait, 60.0)

    def test_ratelimit_reset_is_capped(self):
        wait = sc._retry_after_seconds(self._exc({"x-ratelimit-reset": "600"}), 0, 2.0)
        self.assertEqual(wait, sc.RATELIMIT_RESET_CAP)
        self.assertEqual(wait, 90.0)

    def test_fractional_reset_is_read_as_seconds(self):
        wait = sc._retry_after_seconds(self._exc({"x-ratelimit-reset": "12.5"}), 0, 2.0)
        self.assertEqual(wait, 13.5)

    def test_retry_after_is_preferred_over_ratelimit_reset(self):
        headers = {"Retry-After": "7", "x-ratelimit-reset": "59"}
        self.assertEqual(sc._retry_after_seconds(self._exc(headers), 0, 2.0), 7.0)

    def test_retry_after_keeps_its_own_30s_cap(self):
        headers = {"Retry-After": "120", "x-ratelimit-reset": "59"}
        self.assertEqual(sc._retry_after_seconds(self._exc(headers), 0, 2.0), 30.0)

    def test_unusable_retry_after_falls_through_to_ratelimit_reset(self):
        # Retry-After may also be an HTTP date, which this helper does not
        # parse. The reset header is still a usable answer.
        headers = {
            "Retry-After": "Fri, 02 Oct 2026 10:00:00 GMT",
            "x-ratelimit-reset": "20",
        }
        self.assertEqual(sc._retry_after_seconds(self._exc(headers), 0, 2.0), 21.0)

    def test_neither_header_is_plain_exponential_backoff(self):
        for attempt, expected in ((0, 1.0), (1, 2.0), (2, 4.0), (10, 30.0)):
            self.assertEqual(
                sc._retry_after_seconds(self._exc({}), attempt, 2.0), expected
            )

    def test_missing_headers_object_is_plain_exponential_backoff(self):
        self.assertEqual(sc._retry_after_seconds(self._exc(None), 1, 2.0), 2.0)

    def test_unparseable_or_negative_reset_falls_back_to_backoff(self):
        for value in ("soon", "-5", "nan"):
            self.assertEqual(
                sc._retry_after_seconds(
                    self._exc({"x-ratelimit-reset": value}), 1, 2.0
                ),
                2.0,
                value,
            )

    def test_epoch_style_reset_is_not_read_as_a_delta(self):
        # Some APIs put a Unix timestamp in the same header. Treating that as
        # seconds-remaining would cost the full cap on every such response.
        headers = {"x-ratelimit-reset": "1790935200"}
        self.assertEqual(sc._retry_after_seconds(self._exc(headers), 0, 2.0), 1.0)


class DensityRobustnessTests(unittest.TestCase):
    def test_naive_date_is_treated_as_utc_not_a_crash(self):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        naive_recent = (now - timedelta(days=5)).replace(tzinfo=None).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            led = Path(tmp) / "l.jsonl"
            led.write_text(
                json.dumps({"url": "u1", "date": naive_recent}) + "\n",
                encoding="utf-8",
            )
            counts = sc.density_counts(led)  # must not raise TypeError
            self.assertEqual(counts[30], 1)

    def test_garbage_date_line_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = Path(tmp) / "l.jsonl"
            led.write_text(
                json.dumps({"url": "u1", "date": "not-a-date"}) + "\n",
                encoding="utf-8",
            )
            counts = sc.density_counts(led)
            self.assertEqual(counts, {30: 0, 90: 0})


class HttpSchemeTests(unittest.TestCase):
    def test_non_web_schemes_are_refused_without_a_request(self):
        with mock.patch.object(sc.urllib.request, "urlopen") as opened:
            for url in ("file:///etc/passwd", "ftp://host/x", "no-scheme-at-all"):
                status, body, err = sc.http_get(url)
                self.assertIsNone(status)
                self.assertIn("scheme", err)
            opened.assert_not_called()


class NoAutoPostTests(unittest.TestCase):
    """sweepcore owns the only network/subprocess primitives in the repo, so it
    is where an outbound path would most plausibly be introduced. Extend the
    per-module gate guards to the shared transport layer: read-only, no
    mutations, no POSTs, no schedulers."""

    def test_no_outbound_or_scheduler_token_in_source(self):
        src = Path(sc.__file__).read_text(encoding="utf-8")
        banned = [
            "addDiscussionComment",
            "mutation",
            "--auto",
            "auto_post",
            "auto-post",
            "batch_approve",
            "batch-approve",
            "schedule",
            "cron",
            "-X POST",
            "--method POST",
        ]
        for token in banned:
            self.assertNotIn(
                token,
                src,
                f"outbound/auto-post/scheduler token {token!r} must not appear",
            )

    def test_http_layer_sends_no_request_body(self):
        # urllib POSTs by passing data=; the read-only transport must never.
        src = Path(sc.__file__).read_text(encoding="utf-8")
        self.assertNotIn("data=", src)


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
