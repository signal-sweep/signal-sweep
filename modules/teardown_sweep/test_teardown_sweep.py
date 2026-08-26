#!/usr/bin/env python3
"""Offline unit tests for teardown-sweep (stdlib unittest only).

Every gh / subprocess call and every network read is mocked. These tests make
NO live calls. Run: python -m unittest discover -s modules/teardown_sweep -p 'test_*.py'
"""

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

# Import the module by path so the test runs from any cwd.
_MOD_PATH = Path(__file__).resolve().parent / "teardown_sweep.py"
_spec = importlib.util.spec_from_file_location("teardown_sweep", _MOD_PATH)
ts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ts)


CONFIG = {
    "own_repos": ["me/my-project"],
    "search_queries": ["agent architecture"],
    "topics": [],
    "hn_queries": ["agent architecture"],
    "richness_keywords": [
        "memory",
        "context",
        "hooks",
        "subagent",
        "provenance",
        "verification",
        "orchestration",
        "eval",
        "retrieval",
        "guardrail",
    ],
    "min_stars": 50,
    "active_within_days": 365,
    "hn_min_points": 10,
    "per_query": 20,
    "emit_cap": 60,
    "seen_retention_days": 180,
    "default_window_days": 30,
    "state_dir": "state",
    "candidates_file": "candidates.json",
}


def _full_config():
    cfg = dict(CONFIG)
    for key, val in ts.DEFAULTS.items():
        cfg.setdefault(key, val)
    return cfg


def _venue(name, kind, topics, url="https://example.com/submit", **overrides):
    """An already-normalized publish_venues entry, for tests that exercise
    suggest_venues/_venue_match directly rather than through valid_venues -
    small and explicit rather than depending on the shipped DEFAULTS
    registry, so these tests keep passing if the real defaults change."""
    venue = {
        "name": name,
        "kind": kind,
        "url": url,
        "topics": topics,
        "etiquette": "be polite",
        "manual_only": True,
    }
    venue.update(overrides)
    return venue


class ConfigTests(unittest.TestCase):
    def _write(self, tmp, cfg):
        p = Path(tmp) / "config.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")
        return str(p)

    def test_missing_own_repos_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                ts.load_config(self._write(tmp, {}))

    def test_own_repos_must_be_a_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                ts.load_config(self._write(tmp, {"own_repos": "not-a-list"}))

    def test_defaults_are_filled_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ts.load_config(self._write(tmp, {"own_repos": []}))
        for key, val in ts.DEFAULTS.items():
            self.assertEqual(cfg[key], val)

    def test_explicit_values_override_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ts.load_config(self._write(tmp, {"own_repos": [], "min_stars": 999}))
        self.assertEqual(cfg["min_stars"], 999)

    def test_missing_config_file_exits(self):
        with self.assertRaises(SystemExit):
            ts.load_config("does/not/exist.json")

    def test_invalid_json_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.json"
            p.write_text("{not json", encoding="utf-8")
            with self.assertRaises(SystemExit):
                ts.load_config(str(p))

    def test_search_queries_must_be_a_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                ts.load_config(
                    self._write(tmp, {"own_repos": [], "search_queries": "nope"})
                )

    def test_richness_keywords_must_be_a_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                ts.load_config(
                    self._write(tmp, {"own_repos": [], "richness_keywords": "nope"})
                )


class DryRunConfigFallbackTests(unittest.TestCase):
    def test_falls_back_to_example_when_config_json_absent(self):
        cfg = ts.load_config_for_dry_run("does/not/exist/config.json")
        self.assertIn("own_repos", cfg)
        self.assertIsInstance(cfg["search_queries"], list)
        self.assertIsInstance(cfg["hn_queries"], list)


class IsOwnRepoTests(unittest.TestCase):
    def test_case_insensitive_match(self):
        self.assertTrue(ts.is_own_repo("Acme/Project", ["acme/project"]))

    def test_no_match(self):
        self.assertFalse(ts.is_own_repo("other/repo", ["acme/project"]))

    def test_empty_list_never_matches(self):
        self.assertFalse(ts.is_own_repo("acme/project", []))


class GithubQueryConstructionTests(unittest.TestCase):
    def _capture(self):
        seen = []

        def _search_repos(query, limit, extra_args, errors):
            seen.append((query, tuple(extra_args)))
            return []

        return seen, _search_repos

    def test_phrase_query_includes_pushed_floor_and_sort(self):
        seen, fake = self._capture()
        cfg = _full_config()
        cfg["search_queries"] = ["agent architecture"]
        cfg["topics"] = []
        with mock.patch.object(ts, "search_repos", side_effect=fake):
            ts.github_lane(cfg, "2026-01-01", datetime.now(timezone.utc), [])
        self.assertEqual(
            seen, [("agent architecture pushed:>2026-01-01 sort:stars", ())]
        )

    def test_topic_query_uses_topic_flag_with_no_free_text_term(self):
        seen, fake = self._capture()
        cfg = _full_config()
        cfg["search_queries"] = []
        cfg["topics"] = ["ai-agents"]
        with mock.patch.object(ts, "search_repos", side_effect=fake):
            ts.github_lane(cfg, "2026-01-01", datetime.now(timezone.utc), [])
        self.assertEqual(
            seen, [("pushed:>2026-01-01 sort:stars", ("--topic", "ai-agents"))]
        )

    def test_per_query_limit_is_passed_through(self):
        seen_limits = []

        def _search_repos(query, limit, extra_args, errors):
            seen_limits.append(limit)
            return []

        cfg = _full_config()
        cfg["search_queries"] = ["a"]
        cfg["topics"] = ["b"]
        cfg["per_query"] = 7
        with mock.patch.object(ts, "search_repos", side_effect=_search_repos):
            ts.github_lane(cfg, "2026-01-01", datetime.now(timezone.utc), [])
        self.assertEqual(seen_limits, [7, 7])


class SearchReposFailSoftTests(unittest.TestCase):
    def test_gh_error_is_recorded_and_returns_empty(self):
        errors = []
        with mock.patch.object(ts, "gh", return_value=(None, "HTTP 500 boom")):
            out = ts.search_repos("q", 10, [], errors)
        self.assertEqual(out, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("boom", errors[0])

    def test_non_list_payload_is_recorded_and_returns_empty(self):
        # gh() yields ("", "") when a non-zero exit writes nothing to stderr;
        # the falsy err must not mask an unusable (non-list) payload.
        errors = []
        with mock.patch.object(ts, "gh", return_value=("", "")):
            out = ts.search_repos("q", 10, [], errors)
        self.assertEqual(out, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("no usable result payload", errors[0])

    def test_topic_flag_included_in_error_label(self):
        errors = []
        with mock.patch.object(ts, "gh", return_value=(None, "boom")):
            ts.search_repos("q", 10, ["--topic", "ai-agents"], errors)
        self.assertIn("--topic", errors[0])


class HnUrlConstructionTests(unittest.TestCase):
    def test_url_shape(self):
        url = ts.hn_search_url("agent architecture", 10, 1700000000, 20)
        self.assertIn("tags=story", url)
        self.assertIn("hitsPerPage=20", url)
        self.assertIn("agent%20architecture", url)
        self.assertIn("points%3E%3D10", url)
        self.assertIn("created_at_i%3E1700000000", url)
        self.assertTrue(url.startswith("https://hn.algolia.com/api/v1/search_by_date"))


class ReadmeFetchTests(unittest.TestCase):
    def test_decodes_base64_content(self):
        import base64

        raw = base64.b64encode(b"# hi there").decode()
        with mock.patch.object(ts, "gh", return_value=({"content": raw}, None)):
            self.assertEqual(ts.fetch_readme_text("a/b"), "# hi there")

    def test_gh_error_returns_empty_string(self):
        with mock.patch.object(ts, "gh", return_value=(None, "404 Not Found")):
            self.assertEqual(ts.fetch_readme_text("a/b"), "")

    def test_missing_content_key_returns_empty_string(self):
        with mock.patch.object(ts, "gh", return_value=({}, None)):
            self.assertEqual(ts.fetch_readme_text("a/b"), "")

    def test_malformed_base64_returns_empty_string_not_raise(self):
        with mock.patch.object(
            ts, "gh", return_value=({"content": "!!!not-base64!!!"}, None)
        ):
            self.assertEqual(ts.fetch_readme_text("a/b"), "")


class DocsDirTests(unittest.TestCase):
    def test_present_when_gh_returns_a_list(self):
        with mock.patch.object(ts, "gh", return_value=([{"name": "x"}], None)):
            self.assertTrue(ts.has_docs_dir("a/b"))

    def test_absent_on_404(self):
        with mock.patch.object(ts, "gh", return_value=(None, "404 Not Found")):
            self.assertFalse(ts.has_docs_dir("a/b"))

    def test_absent_when_docs_is_a_file_not_a_directory(self):
        # The contents API returns a dict (not a list) for a single file.
        with mock.patch.object(
            ts, "gh", return_value=({"name": "docs", "type": "file"}, None)
        ):
            self.assertFalse(ts.has_docs_dir("a/b"))


class RichnessScoringTests(unittest.TestCase):
    def test_keyword_hits_are_case_insensitive_and_deduped(self):
        cfg = _full_config()
        score, matched = ts.score_readme_richness("", "Memory MEMORY memory hooks", cfg)
        self.assertEqual(matched, ["hooks", "memory"])
        self.assertEqual(score, 2)

    def test_length_band_under_500_adds_nothing(self):
        cfg = _full_config()
        score, _m = ts.score_readme_richness("", "x" * 100, cfg)
        self.assertEqual(score, 0)

    def test_length_band_boundary_499_is_zero(self):
        cfg = _full_config()
        score, _m = ts.score_readme_richness("", "x" * 499, cfg)
        self.assertEqual(score, 0)

    def test_length_band_boundary_500_is_one(self):
        cfg = _full_config()
        score, _m = ts.score_readme_richness("", "x" * 500, cfg)
        self.assertEqual(score, 1)

    def test_length_band_boundary_2999_is_one(self):
        cfg = _full_config()
        score, _m = ts.score_readme_richness("", "x" * 2999, cfg)
        self.assertEqual(score, 1)

    def test_length_band_boundary_3000_is_two(self):
        cfg = _full_config()
        score, _m = ts.score_readme_richness("", "x" * 3000, cfg)
        self.assertEqual(score, 2)

    def test_description_contributes_keywords_but_not_length_band(self):
        cfg = _full_config()
        score, matched = ts.score_readme_richness("hooks", "", cfg)
        self.assertIn("hooks", matched)
        self.assertEqual(score, 1)

    def test_stars_band_below_floor(self):
        cfg = _full_config()
        self.assertEqual(ts.score_repo_signals(1, None, False, cfg), 0)

    def test_stars_band_at_floor(self):
        cfg = _full_config()
        self.assertEqual(ts.score_repo_signals(cfg["min_stars"], None, False, cfg), 1)

    def test_stars_band_1000_plus(self):
        cfg = _full_config()
        self.assertEqual(ts.score_repo_signals(1000, None, False, cfg), 2)

    def test_recency_band_within_30_days(self):
        cfg = _full_config()
        self.assertEqual(ts.score_repo_signals(0, 30, False, cfg), 2)

    def test_recency_band_31_to_90_days(self):
        cfg = _full_config()
        self.assertEqual(ts.score_repo_signals(0, 90, False, cfg), 1)

    def test_recency_band_over_90_days_is_zero(self):
        cfg = _full_config()
        self.assertEqual(ts.score_repo_signals(0, 91, False, cfg), 0)

    def test_missing_pushed_at_skips_recency_band_without_crashing(self):
        cfg = _full_config()
        self.assertEqual(ts.score_repo_signals(0, None, False, cfg), 0)

    def test_docs_dir_bonus(self):
        cfg = _full_config()
        self.assertEqual(ts.score_repo_signals(0, None, True, cfg), 2)

    def test_all_signals_combine(self):
        cfg = _full_config()
        self.assertEqual(ts.score_repo_signals(1000, 10, True, cfg), 6)

    def test_hn_points_band_below_floor(self):
        cfg = _full_config()
        score, _m = ts.score_hn_richness("nothing relevant here", 0, cfg)
        self.assertEqual(score, 0)

    def test_hn_points_band_at_floor(self):
        cfg = _full_config()
        score, _m = ts.score_hn_richness("x", cfg["hn_min_points"], cfg)
        self.assertEqual(score, 1)

    def test_hn_points_band_100_plus(self):
        cfg = _full_config()
        score, _m = ts.score_hn_richness("x", 100, cfg)
        self.assertEqual(score, 2)

    def test_hn_keyword_hits_in_title(self):
        cfg = _full_config()
        score, matched = ts.score_hn_richness("agent memory and eval harness", 0, cfg)
        self.assertIn("memory", matched)
        self.assertIn("eval", matched)
        self.assertEqual(score, len(matched))


class AgeDaysTests(unittest.TestCase):
    def test_none_when_missing(self):
        self.assertIsNone(ts._pushed_age_days("", datetime.now(timezone.utc)))

    def test_none_when_unparseable(self):
        self.assertIsNone(ts._pushed_age_days("not-a-date", datetime.now(timezone.utc)))

    def test_computes_day_difference(self):
        now = datetime.now(timezone.utc)
        stamp = (now - timedelta(days=10)).isoformat()
        self.assertEqual(ts._pushed_age_days(stamp, now), 10)

    def test_handles_z_suffix(self):
        now = datetime.now(timezone.utc)
        stamp = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(ts._pushed_age_days(stamp, now), 5)


class GithubCandidateBuildingTests(unittest.TestCase):
    def _build(
        self, node, pattern="agent architecture", readme="", has_docs=False, cfg=None
    ):
        cfg = cfg or _full_config()
        now = datetime.now(timezone.utc)
        with (
            mock.patch.object(ts, "fetch_readme_text", return_value=readme),
            mock.patch.object(ts, "has_docs_dir", return_value=has_docs),
        ):
            return ts.build_github_candidate(node, pattern, cfg, now, {}, {})

    def test_missing_full_name_is_skipped(self):
        self.assertIsNone(self._build({"stargazersCount": 500}))

    def test_url_falls_back_to_github_url(self):
        cand = self._build({"fullName": "a/b", "stargazersCount": 5})
        self.assertEqual(cand["url"], "https://github.com/a/b")

    def test_explicit_url_is_kept(self):
        cand = self._build({"fullName": "a/b", "url": "https://example.com/mirror"})
        self.assertEqual(cand["url"], "https://example.com/mirror")

    def test_readme_keyword_hit_feeds_matched_keywords(self):
        cand = self._build(
            {"fullName": "a/b", "stargazersCount": 5},
            readme="This project uses hooks and a subagent architecture.",
        )
        self.assertIn("hooks", cand["matched_keywords"])
        self.assertIn("subagent", cand["matched_keywords"])

    def test_docs_dir_presence_recorded(self):
        cand = self._build({"fullName": "a/b", "stargazersCount": 5}, has_docs=True)
        self.assertTrue(cand["has_docs_dir"])

    def test_readme_fetch_failure_still_builds_candidate(self):
        # fetch_readme_text returning "" (best-effort failure) must not raise
        # or drop the candidate - it just scores lower.
        cand = self._build({"fullName": "a/b", "stargazersCount": 5}, readme="")
        self.assertIsNotNone(cand)
        self.assertEqual(cand["readme_len"], 0)

    def test_tier_is_computed(self):
        cand = self._build({"fullName": "a/b", "stargazersCount": 5000})
        self.assertIn(cand["tier"], ("high", "med", "low"))

    def test_description_truncated_to_300_chars(self):
        cand = self._build(
            {"fullName": "a/b", "description": "x" * 500, "stargazersCount": 5}
        )
        self.assertEqual(len(cand["description"]), 300)

    def test_why_mentions_stars_and_keyword_count(self):
        cand = self._build(
            {"fullName": "a/b", "stargazersCount": 42}, readme="memory hooks"
        )
        self.assertIn("42 stars", cand["why"])


class HnCandidateBuildingTests(unittest.TestCase):
    def test_non_dict_hit_is_skipped(self):
        self.assertIsNone(ts.build_hn_candidate("not-a-dict", "q", _full_config()))

    def test_missing_object_id_is_skipped(self):
        self.assertIsNone(ts.build_hn_candidate({"title": "x"}, "q", _full_config()))

    def test_missing_points_defaults_to_zero(self):
        cand = ts.build_hn_candidate(
            {"objectID": "1", "title": "agent memory system"}, "q", _full_config()
        )
        self.assertEqual(cand["score_or_stars"], 0)

    def test_non_numeric_points_defaults_to_zero(self):
        cand = ts.build_hn_candidate(
            {"objectID": "1", "title": "x", "points": "not-a-number"},
            "q",
            _full_config(),
        )
        self.assertEqual(cand["score_or_stars"], 0)

    def test_url_falls_back_to_item_permalink_for_self_posts(self):
        cand = ts.build_hn_candidate(
            {"objectID": "42", "title": "Ask HN: agent memory", "points": 50},
            "q",
            _full_config(),
        )
        self.assertEqual(cand["url"], "https://news.ycombinator.com/item?id=42")

    def test_explicit_url_is_kept(self):
        cand = ts.build_hn_candidate(
            {
                "objectID": "1",
                "title": "x",
                "points": 5,
                "url": "https://blog.example/post",
            },
            "q",
            _full_config(),
        )
        self.assertEqual(cand["url"], "https://blog.example/post")

    def test_title_falls_back_to_story_title(self):
        cand = ts.build_hn_candidate(
            {"objectID": "1", "story_title": "fallback title", "points": 5},
            "q",
            _full_config(),
        )
        self.assertEqual(cand["title"], "fallback title")

    def test_title_keyword_hits_populate_matched_keywords(self):
        cand = ts.build_hn_candidate(
            {
                "objectID": "1",
                "title": "my agent memory and orchestration setup",
                "points": 20,
            },
            "q",
            _full_config(),
        )
        self.assertIn("memory", cand["matched_keywords"])
        self.assertIn("orchestration", cand["matched_keywords"])

    def test_below_floor_points_still_builds_a_candidate(self):
        # The points floor is applied by cmd_scan's dedup loop, not here (kept
        # visible in `dropped`, symmetric with the star floor) - so a
        # low-point hit still becomes a real candidate at this layer.
        cand = ts.build_hn_candidate(
            {"objectID": "1", "title": "x", "points": 1}, "q", _full_config()
        )
        self.assertEqual(cand["score_or_stars"], 1)

    def test_comments_count_carried_through(self):
        cand = ts.build_hn_candidate(
            {"objectID": "1", "title": "x", "points": 20, "num_comments": 7},
            "q",
            _full_config(),
        )
        self.assertEqual(cand["hn_comments"], 7)


class HnLaneTests(unittest.TestCase):
    def test_malformed_hit_in_a_batch_does_not_abort_the_rest(self):
        cfg = _full_config()
        cfg["hn_queries"] = ["q"]
        body = json.dumps(
            {
                "hits": [
                    {"objectID": "1", "title": "good one", "points": 20},
                    "not-a-dict",
                    {"title": "no id"},
                ]
            }
        )
        report = ts.LaneReport()
        with mock.patch.object(ts, "http_get", return_value=(200, body, None)):
            results = ts.hn_lane(cfg, 0, report)
        self.assertEqual(len(results), 1)
        self.assertEqual(report.fetches_ok, 1)

    def test_http_error_is_recorded_and_does_not_note_fetch_ok(self):
        cfg = _full_config()
        cfg["hn_queries"] = ["q"]
        report = ts.LaneReport()
        with mock.patch.object(ts, "http_get", return_value=(None, "", "network boom")):
            results = ts.hn_lane(cfg, 0, report)
        self.assertEqual(results, [])
        self.assertEqual(report.fetches_ok, 0)
        self.assertTrue(any("boom" in e for e in report))

    def test_non_200_status_is_recorded(self):
        cfg = _full_config()
        cfg["hn_queries"] = ["q"]
        report = ts.LaneReport()
        with mock.patch.object(ts, "http_get", return_value=(500, "", None)):
            ts.hn_lane(cfg, 0, report)
        self.assertTrue(any("HTTP 500" in e for e in report))

    def test_bad_json_is_recorded_not_raised(self):
        cfg = _full_config()
        cfg["hn_queries"] = ["q"]
        report = ts.LaneReport()
        with mock.patch.object(ts, "http_get", return_value=(200, "{not json", None)):
            results = ts.hn_lane(cfg, 0, report)
        self.assertEqual(results, [])
        self.assertTrue(any("bad json" in e for e in report))


class ReadmeCacheTests(unittest.TestCase):
    def test_readme_and_docs_fetched_once_per_repo_across_two_patterns(self):
        node = {"fullName": "dup/repo", "stargazersCount": 500, "url": "x"}
        cfg = _full_config()
        cfg["search_queries"] = ["phrase-one"]
        cfg["topics"] = ["topic-one"]

        def _search_repos(query, limit, extra_args, errors):
            return [node]

        with (
            mock.patch.object(ts, "search_repos", side_effect=_search_repos),
            mock.patch.object(ts, "fetch_readme_text", return_value="") as m_readme,
            mock.patch.object(ts, "has_docs_dir", return_value=False) as m_docs,
        ):
            raw = ts.github_lane(cfg, "2026-01-01", datetime.now(timezone.utc), [])
        self.assertEqual(len(raw), 2)  # one candidate per query hit, cache is internal
        m_readme.assert_called_once_with("dup/repo")
        m_docs.assert_called_once_with("dup/repo")


class ScanIntegrationTests(unittest.TestCase):
    """End-to-end scan with gh/http mocked, verifying candidates.json shape,
    dedup against the seen-store + covered ledger, and star/points floors."""

    def _run_scan(
        self,
        tmp,
        *,
        github_nodes=None,
        hn_hits=None,
        covered=None,
        seen=None,
        own_repos=None,
    ):
        cfg = _full_config()
        cfg["state_dir"] = str(Path(tmp) / "state")
        cfg["candidates_file"] = str(Path(tmp) / "candidates.json")
        cfg["search_queries"] = ["agent architecture"]
        cfg["topics"] = []
        cfg["hn_queries"] = ["agent architecture"]
        if own_repos is not None:
            cfg["own_repos"] = own_repos

        state_dir = Path(cfg["state_dir"])
        if seen is not None:
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "teardown_state.json").write_text(
                json.dumps({"last_run": None, "seen": seen}), encoding="utf-8"
            )
        if covered:
            state_dir.mkdir(parents=True, exist_ok=True)
            with (state_dir / "covered_log.jsonl").open("w", encoding="utf-8") as fh:
                for url in covered:
                    fh.write(
                        json.dumps(
                            {
                                "date": "2026-01-01T00:00:00+00:00",
                                "url": url,
                                "note": "",
                            }
                        )
                        + "\n"
                    )

        args = mock.Mock(config="config.json", days=30, limit=None, dry_run=False)

        def _search_repos(query, limit, extra_args, errors):
            return github_nodes or []

        def _http_get(url, timeout=None, headers=None):
            return 200, json.dumps({"hits": hn_hits or []}), None

        with (
            mock.patch.object(ts, "load_config", return_value=cfg),
            mock.patch.object(ts, "search_repos", side_effect=_search_repos),
            mock.patch.object(
                ts, "fetch_readme_text", return_value="hooks and memory and provenance"
            ),
            mock.patch.object(ts, "has_docs_dir", return_value=False),
            mock.patch.object(ts, "http_get", side_effect=_http_get),
        ):
            rc = ts.cmd_scan(args)
        payload = json.loads(Path(cfg["candidates_file"]).read_text(encoding="utf-8"))
        return rc, payload

    def test_github_candidate_shape(self):
        nodes = [
            {
                "fullName": "good/repo",
                "description": "an agent",
                "stargazersCount": 500,
                "url": "https://github.com/good/repo",
                "pushedAt": datetime.now(timezone.utc).isoformat(),
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            rc, payload = self._run_scan(tmp, github_nodes=nodes)
        self.assertEqual(rc, 0)
        github_cands = [c for c in payload["candidates"] if c["lane"] == "github"]
        self.assertEqual(len(github_cands), 1)
        cand = github_cands[0]
        for field in (
            "lane",
            "repo",
            "url",
            "stars",
            "matched_keywords",
            "tier",
            "why",
            "richness_score",
        ):
            self.assertIn(field, cand)

    def test_hn_candidate_shape(self):
        hits = [
            {
                "objectID": "1",
                "title": "agent memory system deep dive",
                "points": 50,
                "num_comments": 3,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            rc, payload = self._run_scan(tmp, hn_hits=hits)
        self.assertEqual(rc, 0)
        hn_cands = [c for c in payload["candidates"] if c["lane"] == "hn"]
        self.assertEqual(len(hn_cands), 1)
        cand = hn_cands[0]
        for field in (
            "lane",
            "title",
            "url",
            "score_or_stars",
            "matched_keywords",
            "tier",
            "why",
            "richness_score",
        ):
            self.assertIn(field, cand)

    def test_own_repo_excluded(self):
        nodes = [{"fullName": "me/my-project", "stargazersCount": 5000, "url": "x"}]
        with tempfile.TemporaryDirectory() as tmp:
            _rc, payload = self._run_scan(
                tmp, github_nodes=nodes, own_repos=["me/my-project"]
            )
        self.assertEqual(payload["candidates"], [])
        self.assertEqual(payload["dropped"]["own"], 1)

    def test_seen_store_excludes_repeat_repo(self):
        nodes = [{"fullName": "seen/repo", "stargazersCount": 500, "url": "x"}]
        with tempfile.TemporaryDirectory() as tmp:
            _rc, payload = self._run_scan(
                tmp, github_nodes=nodes, seen={"seen/repo": "2099-01-01"}
            )
        self.assertEqual(payload["candidates"], [])
        self.assertEqual(payload["dropped"]["seen"], 1)

    def test_covered_ledger_excludes_repo_by_url(self):
        nodes = [
            {
                "fullName": "done/repo",
                "stargazersCount": 500,
                "url": "https://github.com/done/repo",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            _rc, payload = self._run_scan(
                tmp, github_nodes=nodes, covered=["https://github.com/done/repo"]
            )
        self.assertEqual(payload["candidates"], [])
        self.assertEqual(payload["dropped"]["covered"], 1)

    def test_covered_ledger_excludes_hn_story_by_url(self):
        hits = [
            {
                "objectID": "9",
                "title": "x",
                "points": 50,
                "url": "https://example.com/post",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            _rc, payload = self._run_scan(
                tmp, hn_hits=hits, covered=["https://example.com/post"]
            )
        self.assertEqual(payload["candidates"], [])
        self.assertEqual(payload["dropped"]["covered"], 1)

    def test_star_floor_drops_below_min_stars(self):
        nodes = [{"fullName": "tiny/repo", "stargazersCount": 1, "url": "x"}]
        with tempfile.TemporaryDirectory() as tmp:
            _rc, payload = self._run_scan(tmp, github_nodes=nodes)
        self.assertEqual(payload["candidates"], [])
        self.assertEqual(payload["dropped"]["stars"], 1)

    def test_points_floor_drops_below_hn_min_points(self):
        hits = [{"objectID": "1", "title": "x", "points": 1}]
        with tempfile.TemporaryDirectory() as tmp:
            _rc, payload = self._run_scan(tmp, hn_hits=hits)
        self.assertEqual(payload["candidates"], [])
        self.assertEqual(payload["dropped"]["points"], 1)

    def test_duplicate_repo_across_queries_is_deduped(self):
        node = {"fullName": "dup/repo", "stargazersCount": 500, "url": "x"}

        def _search_repos(query, limit, extra_args, errors):
            return [node]

        cfg = _full_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            cfg["candidates_file"] = str(Path(tmp) / "candidates.json")
            cfg["search_queries"] = ["a", "b"]
            cfg["topics"] = []
            cfg["hn_queries"] = []
            args = mock.Mock(config="config.json", days=30, limit=None, dry_run=False)
            with (
                mock.patch.object(ts, "load_config", return_value=cfg),
                mock.patch.object(ts, "search_repos", side_effect=_search_repos),
                mock.patch.object(ts, "fetch_readme_text", return_value=""),
                mock.patch.object(ts, "has_docs_dir", return_value=False),
            ):
                ts.cmd_scan(args)
            payload = json.loads(
                Path(cfg["candidates_file"]).read_text(encoding="utf-8")
            )
        self.assertEqual(len(payload["candidates"]), 1)
        self.assertEqual(payload["dropped"]["dup"], 1)

    def test_emit_cap_limits_kept_candidates(self):
        nodes = [
            {"fullName": f"o/repo{i}", "stargazersCount": 500 + i, "url": f"x{i}"}
            for i in range(5)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _full_config()
            cfg["state_dir"] = str(Path(tmp) / "state")
            cfg["candidates_file"] = str(Path(tmp) / "candidates.json")
            cfg["search_queries"] = ["a"]
            cfg["topics"] = []
            cfg["hn_queries"] = []
            cfg["emit_cap"] = 2
            args = mock.Mock(config="config.json", days=30, limit=None, dry_run=False)
            with (
                mock.patch.object(ts, "load_config", return_value=cfg),
                mock.patch.object(
                    ts, "search_repos", side_effect=lambda *a, **k: nodes
                ),
                mock.patch.object(ts, "fetch_readme_text", return_value=""),
                mock.patch.object(ts, "has_docs_dir", return_value=False),
            ):
                ts.cmd_scan(args)
            payload = json.loads(
                Path(cfg["candidates_file"]).read_text(encoding="utf-8")
            )
        self.assertEqual(len(payload["candidates"]), 2)


class WindowEarnHoldTests(unittest.TestCase):
    """last_run is a claim about lane-2 (HN) coverage only - lane 1's floor is
    a fixed trailing window from now, not since-last-run, so it never gates
    the marker. See teardown_sweep.cmd_scan's comment for the full reasoning.
    """

    OLD = "2026-07-01T00:00:00+00:00"

    def _setup(self, tmp, state_obj=None, **overrides):
        cfg = _full_config()
        cfg["state_dir"] = str(Path(tmp) / "state")
        cfg["candidates_file"] = str(Path(tmp) / "candidates.json")
        cfg["search_queries"] = []
        cfg["topics"] = []
        cfg["hn_queries"] = ["agent architecture"]
        cfg.update(overrides)
        state_file = Path(cfg["state_dir"]) / "teardown_state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        if state_obj is not None:
            state_file.write_text(json.dumps(state_obj), encoding="utf-8")
        return cfg, state_file

    def _scan(self, cfg, http_get_side_effect, days=None):
        args = mock.Mock(config="config.json", days=days, limit=None, dry_run=False)
        err = io.StringIO()
        with (
            mock.patch.object(ts, "load_config", return_value=cfg),
            mock.patch.object(ts, "http_get", side_effect=http_get_side_effect),
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
    def _digest(cfg):
        return json.loads(Path(cfg["candidates_file"]).read_text(encoding="utf-8"))

    # --- fake http_get outcomes ---
    @staticmethod
    def _ok(*args, **kwargs):
        return 200, json.dumps({"hits": []}), None

    @staticmethod
    def _boom(*args, **kwargs):
        return None, "", "network boom"

    def test_hn_failure_holds_the_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(tmp, {"last_run": self.OLD, "seen": {}})
            self._scan(cfg, self._boom)
            self.assertEqual(self._marker(state_file), self.OLD)

    def test_hn_success_with_empty_hits_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(tmp, {"last_run": self.OLD, "seen": {}})
            self._scan(cfg, self._ok)
            self.assertNotEqual(self._marker(state_file), self.OLD)

    def test_run_with_no_hn_queries_holds_the_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(
                tmp, {"last_run": self.OLD, "seen": {}}, hn_queries=[]
            )
            self._scan(cfg, self._ok)
            self.assertEqual(self._marker(state_file), self.OLD)

    def test_github_lane_failure_does_not_hold_the_marker(self):
        # The key asymmetry: lane 1 is not windowed against last_run, so its
        # failures must never gate the earn/hold marker - only lane 2 does.
        def _failing_search_repos(query, limit, extra_args, errors):
            errors.append(f"boom on {query}")
            return []

        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(
                tmp, {"last_run": self.OLD, "seen": {}}, search_queries=["agent x"]
            )
            with mock.patch.object(
                ts, "search_repos", side_effect=_failing_search_repos
            ):
                self._scan(cfg, self._ok)
            self.assertNotEqual(self._marker(state_file), self.OLD)
            self.assertTrue(any("boom on" in e for e in self._digest(cfg)["errors"]))

    def test_first_clean_run_with_no_prior_marker_lays_one_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(tmp, {"seen": {}})
            self._scan(cfg, self._ok)
            age = datetime.now(timezone.utc) - datetime.fromisoformat(
                self._marker(state_file)
            )
            self.assertAlmostEqual(age.total_seconds(), 0, delta=120)

    def test_failed_run_with_no_prior_marker_invents_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(tmp, {"seen": {}})
            self._scan(cfg, self._boom)
            self.assertFalse(self._marker(state_file))

    def test_narrow_days_override_does_not_swallow_the_uncovered_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
            cfg, state_file = self._setup(tmp, {"last_run": old, "seen": {}})
            self._scan(cfg, self._ok, days=2)
            self.assertEqual(self._marker(state_file), old)

    def test_wide_days_override_covers_the_marker_and_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
            cfg, state_file = self._setup(tmp, {"last_run": old, "seen": {}})
            self._scan(cfg, self._ok, days=30)
            self.assertNotEqual(self._marker(state_file), old)

    def test_unreadable_marker_warns_and_re_windows_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _state_file = self._setup(
                tmp, {"last_run": "last tuesday", "seen": {}}
            )
            err = self._scan(cfg, self._ok)
            self.assertIn("unreadable last_run", err)

    def test_unreadable_marker_is_not_overwritten_by_an_unearned_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, state_file = self._setup(tmp, {"last_run": "last tuesday", "seen": {}})
            self._scan(cfg, self._ok)
            self.assertEqual(self._marker(state_file), "last tuesday")

    def test_held_window_is_reported_in_the_digest_and_on_stderr(self):
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

    def test_held_window_is_re_covered_by_the_next_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _state_file = self._setup(tmp, {"last_run": self.OLD, "seen": {}})
            self._scan(cfg, self._boom)
            self._scan(cfg, self._ok)
            self.assertEqual(self._digest(cfg)["hn_window_since"][:10], self.OLD[:10])


class DryRunTests(unittest.TestCase):
    def test_dry_run_makes_no_calls_and_writes_nothing(self):
        cfg = _full_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            cfg["candidates_file"] = str(Path(tmp) / "candidates.json")
            args = mock.Mock(config="config.json", days=7, limit=None, dry_run=True)
            buf = io.StringIO()
            with (
                mock.patch.object(ts, "load_config", return_value=cfg),
                mock.patch.object(
                    ts, "search_repos", side_effect=AssertionError("no gh in dry-run")
                ),
                mock.patch.object(
                    ts,
                    "fetch_readme_text",
                    side_effect=AssertionError("no gh in dry-run"),
                ),
                mock.patch.object(
                    ts, "has_docs_dir", side_effect=AssertionError("no gh in dry-run")
                ),
                mock.patch.object(
                    ts, "http_get", side_effect=AssertionError("no network in dry-run")
                ),
                mock.patch("sys.stdout", buf),
            ):
                rc = ts.cmd_scan(args)
        self.assertEqual(rc, 0)
        self.assertIn("DRY-RUN", buf.getvalue())
        self.assertFalse(Path(cfg["candidates_file"]).exists())
        self.assertFalse(Path(cfg["state_dir"]).exists())

    def test_dry_run_preview_lists_configured_queries(self):
        cfg = _full_config()
        cfg["search_queries"] = ["a phrase"]
        cfg["topics"] = ["a-topic"]
        cfg["hn_queries"] = ["an hn query"]
        args = mock.Mock(config="config.json", days=None, limit=None, dry_run=True)
        buf = io.StringIO()
        with (
            mock.patch.object(ts, "load_config", return_value=cfg),
            mock.patch("sys.stdout", buf),
        ):
            ts.cmd_scan(args)
        out = buf.getvalue()
        self.assertIn("a phrase", out)
        self.assertIn("a-topic", out)
        self.assertIn("an hn query", out)


class MarkCoveredLogTests(unittest.TestCase):
    def test_mark_covered_appends_ledger_entry(self):
        cfg = _full_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            with mock.patch.object(ts, "load_config", return_value=cfg):
                args = mock.Mock(
                    config="config.json",
                    url="https://github.com/a/b",
                    note="great writeup",
                )
                ts.cmd_mark_covered(args)
            ledger = Path(cfg["state_dir"]) / "covered_log.jsonl"
            entry = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(entry["url"], "https://github.com/a/b")
        self.assertEqual(entry["note"], "great writeup")
        self.assertIn("date", entry)

    def test_mark_covered_without_note_defaults_to_empty_string(self):
        cfg = _full_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            with mock.patch.object(ts, "load_config", return_value=cfg):
                args = mock.Mock(config="config.json", url="https://x", note=None)
                ts.cmd_mark_covered(args)
            ledger = Path(cfg["state_dir"]) / "covered_log.jsonl"
            entry = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(entry["note"], "")

    def test_covered_url_is_then_visible_to_posted_urls(self):
        cfg = _full_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            with mock.patch.object(ts, "load_config", return_value=cfg):
                ts.cmd_mark_covered(
                    mock.Mock(config="config.json", url="https://x/y", note=None)
                )
            ledger = Path(cfg["state_dir"]) / "covered_log.jsonl"
            self.assertIn("https://x/y", ts.posted_urls(ledger))

    def test_log_prints_recorded_entries(self):
        cfg = _full_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "covered_log.jsonl").write_text(
                json.dumps(
                    {
                        "date": "2026-01-01T00:00:00+00:00",
                        "url": "https://x",
                        "note": "n",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            buf = io.StringIO()
            with (
                mock.patch.object(ts, "load_config", return_value=cfg),
                contextlib.redirect_stdout(buf),
            ):
                ts.cmd_log(mock.Mock(config="config.json"))
        self.assertIn("https://x", buf.getvalue())

    def test_log_with_no_ledger_says_so(self):
        cfg = _full_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            buf = io.StringIO()
            with (
                mock.patch.object(ts, "load_config", return_value=cfg),
                contextlib.redirect_stdout(buf),
            ):
                ts.cmd_log(mock.Mock(config="config.json"))
        self.assertIn("no teardowns recorded", buf.getvalue())

    def test_log_skips_malformed_lines(self):
        cfg = _full_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "covered_log.jsonl").write_text(
                "{not json\n"
                + json.dumps(
                    {
                        "date": "2026-01-01T00:00:00+00:00",
                        "url": "https://ok",
                        "note": "",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            buf = io.StringIO()
            with (
                mock.patch.object(ts, "load_config", return_value=cfg),
                contextlib.redirect_stdout(buf),
            ):
                rc = ts.cmd_log(mock.Mock(config="config.json"))
        self.assertEqual(rc, 0)
        self.assertIn("https://ok", buf.getvalue())


# --- lane 3: artefact code search -------------------------------------------


class ArtefactConfigTests(unittest.TestCase):
    def _write(self, tmp, cfg):
        p = Path(tmp) / "config.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")
        return str(p)

    def test_artefact_queries_must_be_a_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                ts.load_config(
                    self._write(tmp, {"own_repos": [], "artefact_queries": "nope"})
                )

    def test_pattern_signals_must_be_a_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                ts.load_config(
                    self._write(tmp, {"own_repos": [], "pattern_signals": ["nope"]})
                )

    def test_artefact_defaults_are_filled_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ts.load_config(self._write(tmp, {"own_repos": []}))
        self.assertEqual(cfg["artefact_min_stars"], 20)
        self.assertEqual(cfg["artefact_pages_per_query"], 1)
        self.assertEqual(len(cfg["artefact_queries"]), 5)
        self.assertEqual(len(cfg["pattern_signals"]), 15)
        self.assertIn("p01", cfg["pattern_signals"])
        self.assertIn("p15", cfg["pattern_signals"])


class DedupKeyTests(unittest.TestCase):
    def test_github_lane_keys_by_lowercased_repo(self):
        cand = {"lane": "github", "repo": "Owner/Repo", "url": "https://x"}
        self.assertEqual(ts._dedup_key(cand), "owner/repo")

    def test_artefact_lane_keys_by_lowercased_repo(self):
        cand = {"lane": "artefact", "repo": "Owner/Repo", "url": "https://x"}
        self.assertEqual(ts._dedup_key(cand), "owner/repo")

    def test_hn_lane_keys_by_url(self):
        cand = {"lane": "hn", "repo": "", "url": "https://example.com/post"}
        self.assertEqual(ts._dedup_key(cand), "https://example.com/post")


class ContentPrefixHashTests(unittest.TestCase):
    def test_identical_text_hashes_identically(self):
        self.assertEqual(
            ts.content_prefix_hash("hello world"), ts.content_prefix_hash("hello world")
        )

    def test_whitespace_differences_within_prefix_hash_identically(self):
        a = ts.content_prefix_hash("hello    world\n\n\tfoo")
        b = ts.content_prefix_hash("hello world foo")
        self.assertEqual(a, b)

    def test_content_differing_past_the_prefix_still_hashes_identically(self):
        base = "x" * 2048
        a = ts.content_prefix_hash(base + "one tail")
        b = ts.content_prefix_hash(base + "a totally different tail")
        self.assertEqual(a, b)

    def test_different_content_hashes_differently(self):
        self.assertNotEqual(
            ts.content_prefix_hash("alpha workspace"),
            ts.content_prefix_hash("beta workspace"),
        )

    def test_none_text_does_not_raise_and_matches_empty(self):
        self.assertEqual(ts.content_prefix_hash(None), ts.content_prefix_hash(""))


class PatternDensityScoringTests(unittest.TestCase):
    def test_zero_present_scores_zero(self):
        self.assertEqual(ts.score_pattern_density(0), 0)

    def test_peak_band_scores_max(self):
        for n in range(ts.PATTERN_PEAK_LOW, ts.PATTERN_PEAK_HIGH + 1):
            self.assertEqual(ts.score_pattern_density(n), ts.PATTERN_PEAK_SCORE)

    def test_fifteen_present_scores_zero(self):
        self.assertEqual(ts.score_pattern_density(15), 0)

    def test_all_fifteen_scores_below_a_six_pattern_artefact(self):
        self.assertLess(ts.score_pattern_density(15), ts.score_pattern_density(6))

    def test_score_ramps_up_toward_the_peak(self):
        self.assertLess(ts.score_pattern_density(1), ts.score_pattern_density(2))
        self.assertLess(ts.score_pattern_density(2), ts.score_pattern_density(3))
        self.assertLess(ts.score_pattern_density(3), ts.score_pattern_density(4))

    def test_score_ramps_down_after_the_peak(self):
        self.assertGreater(ts.score_pattern_density(9), ts.score_pattern_density(10))
        self.assertGreater(ts.score_pattern_density(10), ts.score_pattern_density(11))

    def test_score_never_negative(self):
        for n in range(30):
            self.assertGreaterEqual(ts.score_pattern_density(n), 0)


class ArtefactPatternMatchingTests(unittest.TestCase):
    SIGNALS = {
        "p01": ["alpha indicator"],
        "p02": ["beta indicator"],
        "p03": ["gamma indicator"],
    }

    def test_empty_artefact_scores_zero(self):
        present, absent, score = ts.score_artefact_patterns("", self.SIGNALS)
        self.assertEqual(present, [])
        self.assertEqual(absent, ["p01", "p02", "p03"])
        self.assertEqual(score, 0)

    def test_none_text_scores_zero(self):
        present, _absent, score = ts.score_artefact_patterns(None, self.SIGNALS)
        self.assertEqual(present, [])
        self.assertEqual(score, 0)

    def test_case_insensitive_indicator_match(self):
        present, _absent, _score = ts.score_artefact_patterns(
            "This mentions Alpha Indicator right here.", self.SIGNALS
        )
        self.assertIn("p01", present)

    def test_present_and_absent_partition_all_labels(self):
        present, absent, _score = ts.score_artefact_patterns(
            "alpha indicator", self.SIGNALS
        )
        self.assertEqual(sorted(present + absent), ["p01", "p02", "p03"])

    def test_one_indicator_hit_is_enough_to_mark_a_label_present(self):
        signals = {"p01": ["one", "two", "three"]}
        present, _absent, _score = ts.score_artefact_patterns(
            "contains two only", signals
        )
        self.assertEqual(present, ["p01"])


class ArtefactTierTests(unittest.TestCase):
    def test_peak_score_is_high(self):
        self.assertEqual(ts.artefact_tier(10), "high")

    def test_zero_is_low(self):
        self.assertEqual(ts.artefact_tier(0), "low")

    def test_mid_score_is_med(self):
        self.assertEqual(ts.artefact_tier(4), "med")

    def test_boundary_eight_is_high(self):
        self.assertEqual(ts.artefact_tier(8), "high")

    def test_boundary_seven_is_med(self):
        self.assertEqual(ts.artefact_tier(7), "med")

    def test_boundary_three_is_med(self):
        self.assertEqual(ts.artefact_tier(3), "med")

    def test_boundary_two_is_low(self):
        self.assertEqual(ts.artefact_tier(2), "low")


class SearchCodePageTests(unittest.TestCase):
    def test_parses_items_list(self):
        payload = {"total_count": 1, "items": [{"path": "CLAUDE.md"}]}
        with mock.patch.object(ts, "gh", return_value=(payload, None)):
            items, err = ts.search_code_page("q", 1, 30)
        self.assertIsNone(err)
        self.assertEqual(items, [{"path": "CLAUDE.md"}])

    def test_null_stargazers_in_embedded_repository_does_not_break_parsing(self):
        # The code-search item's embedded `repository` sub-object never
        # carries stargazers_count at all (live-verified) - a payload that
        # includes a null one anyway (defensive fixture) must not raise.
        payload = {
            "items": [
                {
                    "path": "CLAUDE.md",
                    "repository": {"full_name": "a/b", "stargazers_count": None},
                }
            ]
        }
        with mock.patch.object(ts, "gh", return_value=(payload, None)):
            items, err = ts.search_code_page("q", 1, 30)
        self.assertIsNone(err)
        self.assertIsNone(items[0]["repository"]["stargazers_count"])

    def test_gh_error_is_returned_not_raised(self):
        with mock.patch.object(ts, "gh", return_value=(None, "boom")):
            items, err = ts.search_code_page("q", 1, 30)
        self.assertEqual(items, [])
        self.assertEqual(err, "boom")

    def test_missing_items_key_is_a_soft_error(self):
        with mock.patch.object(ts, "gh", return_value=({"total_count": 0}, None)):
            items, err = ts.search_code_page("q", 1, 30)
        self.assertEqual(items, [])
        self.assertIn("no usable result payload", err)

    def test_non_dict_payload_is_a_soft_error(self):
        with mock.patch.object(ts, "gh", return_value=("", None)):
            items, err = ts.search_code_page("q", 1, 30)
        self.assertEqual(items, [])
        self.assertIn("no usable result payload", err)

    def test_query_page_and_per_page_are_in_the_request(self):
        seen = {}

        def _fake_gh(args):
            seen["args"] = args
            return {"items": []}, None

        with mock.patch.object(ts, "gh", side_effect=_fake_gh):
            ts.search_code_page("filename:CLAUDE.md size:>4000", 1, 30)
        endpoint = seen["args"][1]
        self.assertTrue(endpoint.startswith("search/code?q="))
        self.assertIn("page=1", endpoint)
        self.assertIn("per_page=30", endpoint)


class FetchRepoMetaTests(unittest.TestCase):
    def test_returns_metadata_dict(self):
        data = {"full_name": "a/b", "stargazers_count": 5, "fork": False}
        with mock.patch.object(ts, "gh", return_value=(data, None)):
            self.assertEqual(ts.fetch_repo_meta("a/b"), data)

    def test_gh_error_returns_none(self):
        with mock.patch.object(ts, "gh", return_value=(None, "404 Not Found")):
            self.assertIsNone(ts.fetch_repo_meta("a/b"))

    def test_non_dict_payload_returns_none(self):
        with mock.patch.object(ts, "gh", return_value=("oops", None)):
            self.assertIsNone(ts.fetch_repo_meta("a/b"))


class FetchArtefactContentTests(unittest.TestCase):
    def test_decodes_base64_content(self):
        import base64

        raw = base64.b64encode(b"# workspace notes").decode()
        with mock.patch.object(ts, "gh", return_value=({"content": raw}, None)):
            self.assertEqual(
                ts.fetch_artefact_content("a/b", "CLAUDE.md"), "# workspace notes"
            )

    def test_gh_error_returns_empty_string(self):
        with mock.patch.object(ts, "gh", return_value=(None, "404 Not Found")):
            self.assertEqual(ts.fetch_artefact_content("a/b", "CLAUDE.md"), "")

    def test_malformed_base64_returns_empty_string_not_raise(self):
        with mock.patch.object(
            ts, "gh", return_value=({"content": "!!!not-base64!!!"}, None)
        ):
            self.assertEqual(ts.fetch_artefact_content("a/b", "CLAUDE.md"), "")

    def test_path_is_included_in_the_contents_call(self):
        seen = {}

        def _fake_gh(args):
            seen["args"] = args
            return {"content": ""}, None

        with mock.patch.object(ts, "gh", side_effect=_fake_gh):
            ts.fetch_artefact_content("a/b", ".cursor/rules/general.md")
        self.assertIn("contents/.cursor/rules/general.md", seen["args"][1])


class BuildArtefactCandidateTests(unittest.TestCase):
    def _hit(
        self,
        repo="a/b",
        path="CLAUDE.md",
        html_url="https://github.com/a/b/blob/x/CLAUDE.md",
    ):
        return {"path": path, "html_url": html_url, "repository": {"full_name": repo}}

    def _meta(self, **overrides):
        base = {
            "stargazers_count": 100,
            "pushed_at": datetime.now(timezone.utc).isoformat(),
            "fork": False,
            "is_template": False,
            "archived": False,
        }
        base.update(overrides)
        return base

    def _build(self, hit, label="claude-md", meta="__default__", content="", cfg=None):
        cfg = cfg or _full_config()
        now = datetime.now(timezone.utc)
        if meta == "__default__":
            meta = self._meta()
        with (
            mock.patch.object(ts, "fetch_repo_meta", return_value=meta),
            mock.patch.object(ts, "fetch_artefact_content", return_value=content),
        ):
            return ts.build_artefact_candidate(hit, label, cfg, now, {}, {})

    def test_missing_repository_full_name_is_skipped(self):
        self.assertIsNone(self._build({"path": "CLAUDE.md", "repository": {}}))

    def test_missing_path_is_skipped(self):
        self.assertIsNone(self._build({"path": "", "repository": {"full_name": "a/b"}}))

    def test_metadata_fetch_failure_skips_candidate(self):
        self.assertIsNone(self._build(self._hit(), meta=None))

    def test_null_stargazers_count_becomes_zero_not_a_crash(self):
        meta = self._meta(stargazers_count=None, pushed_at="")
        cand = self._build(self._hit(), meta=meta)
        self.assertEqual(cand["stars"], 0)

    def test_candidate_shape(self):
        cand = self._build(
            self._hit(), content="memory index and bitwarden vault notes"
        )
        for field in (
            "lane",
            "repo",
            "url",
            "stars",
            "pushed_at",
            "fork",
            "is_template",
            "archived",
            "artefact_label",
            "artefact_path",
            "artefact_url",
            "patterns_present",
            "patterns_absent",
            "pattern_score",
            "richness_score",
            "content_hash",
            "tier",
            "why",
        ):
            self.assertIn(field, cand)
        self.assertEqual(cand["lane"], "artefact")

    def test_pattern_hits_recorded(self):
        cfg = _full_config()
        cfg["pattern_signals"] = {"p05": ["memory index"], "p06": ["bitwarden"]}
        cand = self._build(
            self._hit(), content="uses a memory index and bitwarden vault", cfg=cfg
        )
        self.assertIn("p05", cand["patterns_present"])
        self.assertIn("p06", cand["patterns_present"])

    def test_fork_flag_carried_through(self):
        cand = self._build(self._hit(), meta=self._meta(fork=True))
        self.assertTrue(cand["fork"])

    def test_url_is_the_repo_url_not_the_artefact_url(self):
        cand = self._build(
            self._hit(repo="a/b", html_url="https://github.com/a/b/blob/x/CLAUDE.md")
        )
        self.assertEqual(cand["url"], "https://github.com/a/b")
        self.assertEqual(
            cand["artefact_url"], "https://github.com/a/b/blob/x/CLAUDE.md"
        )

    def test_metadata_and_content_cached_per_repo_and_path(self):
        hit = self._hit()
        cfg = _full_config()
        now = datetime.now(timezone.utc)
        meta_cache, content_cache = {}, {}
        with (
            mock.patch.object(
                ts, "fetch_repo_meta", return_value=self._meta()
            ) as m_meta,
            mock.patch.object(
                ts, "fetch_artefact_content", return_value=""
            ) as m_content,
        ):
            ts.build_artefact_candidate(
                hit, "claude-md", cfg, now, meta_cache, content_cache
            )
            ts.build_artefact_candidate(
                hit, "claude-md", cfg, now, meta_cache, content_cache
            )
        m_meta.assert_called_once_with("a/b")
        m_content.assert_called_once_with("a/b", "CLAUDE.md")


class ArtefactLaneTests(unittest.TestCase):
    def _cfg(self, queries=None, pages=1):
        cfg = _full_config()
        cfg["artefact_queries"] = (
            queries
            if queries is not None
            else [{"label": "claude-md", "query": "filename:CLAUDE.md size:>4000"}]
        )
        cfg["artefact_pages_per_query"] = pages
        return cfg

    def test_no_sleep_before_the_first_call(self):
        cfg = self._cfg()
        with (
            mock.patch.object(ts, "search_code_page", return_value=([], None)),
            mock.patch.object(ts.time, "sleep") as m_sleep,
        ):
            ts.artefact_lane(cfg, datetime.now(timezone.utc), [])
        m_sleep.assert_not_called()

    def test_sleeps_between_successive_calls(self):
        cfg = self._cfg(
            queries=[{"label": "a", "query": "qa"}, {"label": "b", "query": "qb"}]
        )
        with (
            mock.patch.object(ts, "search_code_page", return_value=([], None)),
            mock.patch.object(ts.time, "sleep") as m_sleep,
        ):
            ts.artefact_lane(cfg, datetime.now(timezone.utc), [])
        m_sleep.assert_called_once_with(ts.ARTEFACT_SLEEP_SECONDS)

    def test_builds_candidates_from_hits(self):
        cfg = self._cfg()
        hit = {"path": "CLAUDE.md", "html_url": "x", "repository": {"full_name": "a/b"}}
        meta = {
            "stargazers_count": 100,
            "pushed_at": "",
            "fork": False,
            "is_template": False,
            "archived": False,
        }
        with (
            mock.patch.object(ts, "search_code_page", return_value=([hit], None)),
            mock.patch.object(ts, "fetch_repo_meta", return_value=meta),
            mock.patch.object(ts, "fetch_artefact_content", return_value=""),
            mock.patch.object(ts.time, "sleep"),
        ):
            raw = ts.artefact_lane(cfg, datetime.now(timezone.utc), [])
        self.assertEqual(len(raw), 1)
        self.assertEqual(raw[0]["repo"], "a/b")

    def test_non_rate_limit_error_is_recorded_and_continues(self):
        cfg = self._cfg(
            queries=[{"label": "a", "query": "qa"}, {"label": "b", "query": "qb"}]
        )
        calls = []

        def _fake(query, page, per_page):
            calls.append(query)
            return [], "network boom"

        advisory = []
        with (
            mock.patch.object(ts, "search_code_page", side_effect=_fake),
            mock.patch.object(ts.time, "sleep"),
        ):
            raw = ts.artefact_lane(cfg, datetime.now(timezone.utc), advisory)
        self.assertEqual(raw, [])
        self.assertEqual(len(calls), 2)
        self.assertTrue(any("boom" in e for e in advisory))

    def test_rate_limit_403_stops_the_lane_early(self):
        cfg = self._cfg(
            queries=[{"label": "a", "query": "qa"}, {"label": "b", "query": "qb"}]
        )
        calls = []

        def _fake(query, page, per_page):
            calls.append(query)
            return [], "gh: API rate limit exceeded (HTTP 403)"

        advisory = []
        with (
            mock.patch.object(ts, "search_code_page", side_effect=_fake),
            mock.patch.object(ts.time, "sleep"),
        ):
            raw = ts.artefact_lane(cfg, datetime.now(timezone.utc), advisory)
        self.assertEqual(raw, [])
        self.assertEqual(len(calls), 1)
        self.assertTrue(any("rate limit" in e for e in advisory))

    def test_label_falls_back_to_query_when_missing(self):
        cfg = self._cfg(queries=[{"query": "filename:CLAUDE.md"}])
        hit = {"path": "CLAUDE.md", "html_url": "x", "repository": {"full_name": "a/b"}}
        meta = {
            "stargazers_count": 1,
            "pushed_at": "",
            "fork": False,
            "is_template": False,
            "archived": False,
        }
        with (
            mock.patch.object(ts, "search_code_page", return_value=([hit], None)),
            mock.patch.object(ts, "fetch_repo_meta", return_value=meta),
            mock.patch.object(ts, "fetch_artefact_content", return_value=""),
            mock.patch.object(ts.time, "sleep"),
        ):
            raw = ts.artefact_lane(cfg, datetime.now(timezone.utc), [])
        self.assertEqual(raw[0]["artefact_label"], "filename:CLAUDE.md")

    def test_malformed_query_entry_is_skipped(self):
        cfg = self._cfg(queries=["not-a-dict", {"label": "x"}])
        with (
            mock.patch.object(ts, "search_code_page") as m_search,
            mock.patch.object(ts.time, "sleep"),
        ):
            raw = ts.artefact_lane(cfg, datetime.now(timezone.utc), [])
        self.assertEqual(raw, [])
        m_search.assert_not_called()


class ArtefactDropReasonTests(unittest.TestCase):
    def _cand(self, **overrides):
        base = {
            "fork": False,
            "is_template": False,
            "archived": False,
            "stars": 100,
            "pushed_at": datetime.now(timezone.utc).isoformat(),
            "content_hash": "deadbeef",
        }
        base.update(overrides)
        return base

    def _cfg(self, **overrides):
        cfg = _full_config()
        cfg.update(overrides)
        return cfg

    def test_fork_is_dropped(self):
        cand = self._cand(fork=True)
        reason = ts._artefact_drop_reason(
            cand, self._cfg(), datetime.now(timezone.utc), set(), {}
        )
        self.assertEqual(reason, "fork")

    def test_template_is_dropped(self):
        cand = self._cand(is_template=True)
        reason = ts._artefact_drop_reason(
            cand, self._cfg(), datetime.now(timezone.utc), set(), {}
        )
        self.assertEqual(reason, "template")

    def test_archived_is_dropped(self):
        cand = self._cand(archived=True)
        reason = ts._artefact_drop_reason(
            cand, self._cfg(), datetime.now(timezone.utc), set(), {}
        )
        self.assertEqual(reason, "archived")

    def test_below_artefact_star_floor_is_dropped(self):
        cfg = self._cfg(artefact_min_stars=50)
        cand = self._cand(stars=10)
        reason = ts._artefact_drop_reason(
            cand, cfg, datetime.now(timezone.utc), set(), {}
        )
        self.assertEqual(reason, "stars")

    def test_stale_pushed_at_is_dropped(self):
        cfg = self._cfg(active_within_days=30)
        old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        cand = self._cand(pushed_at=old)
        reason = ts._artefact_drop_reason(
            cand, cfg, datetime.now(timezone.utc), set(), {}
        )
        self.assertEqual(reason, "stale")

    def test_missing_pushed_at_does_not_trigger_stale(self):
        cand = self._cand(pushed_at="")
        reason = ts._artefact_drop_reason(
            cand, self._cfg(), datetime.now(timezone.utc), set(), {}
        )
        self.assertIsNone(reason)

    def test_dup_content_against_this_run_batch(self):
        cand = self._cand(content_hash="abc123")
        reason = ts._artefact_drop_reason(
            cand, self._cfg(), datetime.now(timezone.utc), {"abc123"}, {}
        )
        self.assertEqual(reason, "dup_content")

    def test_dup_content_against_prior_state(self):
        cand = self._cand(content_hash="abc123")
        reason = ts._artefact_drop_reason(
            cand,
            self._cfg(),
            datetime.now(timezone.utc),
            set(),
            {"abc123": "2020-01-01"},
        )
        self.assertEqual(reason, "dup_content")

    def test_clean_candidate_is_not_dropped(self):
        cand = self._cand()
        reason = ts._artefact_drop_reason(
            cand, self._cfg(), datetime.now(timezone.utc), set(), {}
        )
        self.assertIsNone(reason)

    def test_fork_checked_before_stars(self):
        cfg = self._cfg(artefact_min_stars=1000)
        cand = self._cand(fork=True, stars=1)
        reason = ts._artefact_drop_reason(
            cand, cfg, datetime.now(timezone.utc), set(), {}
        )
        self.assertEqual(reason, "fork")


class ArtefactScanIntegrationTests(unittest.TestCase):
    """End-to-end scan with lane 3 exercised (no_artefacts=False): gh is
    mocked at the search_code_page / fetch_repo_meta / fetch_artefact_content
    boundary, same altitude ScanIntegrationTests mocks lanes 1-2 at, so the
    dedup/drop-reason/digest wiring in cmd_scan is exercised for real."""

    def _hit(self, repo, path="CLAUDE.md"):
        return {
            "path": path,
            "html_url": f"https://github.com/{repo}/blob/x/{path}",
            "repository": {"full_name": repo},
        }

    def _meta(self, **overrides):
        base = {
            "stargazers_count": 100,
            "pushed_at": datetime.now(timezone.utc).isoformat(),
            "fork": False,
            "is_template": False,
            "archived": False,
        }
        base.update(overrides)
        return base

    def _run_scan(
        self,
        tmp,
        *,
        hits=None,
        meta_by_repo=None,
        content_by_repo=None,
        seen=None,
        content_hashes=None,
        own_repos=None,
        artefact_min_stars=None,
        active_within_days=None,
        no_artefacts=False,
    ):
        cfg = _full_config()
        cfg["state_dir"] = str(Path(tmp) / "state")
        cfg["candidates_file"] = str(Path(tmp) / "candidates.json")
        cfg["search_queries"] = []
        cfg["topics"] = []
        cfg["hn_queries"] = []
        cfg["artefact_queries"] = [
            {"label": "claude-md", "query": "filename:CLAUDE.md size:>4000"}
        ]
        if own_repos is not None:
            cfg["own_repos"] = own_repos
        if artefact_min_stars is not None:
            cfg["artefact_min_stars"] = artefact_min_stars
        if active_within_days is not None:
            cfg["active_within_days"] = active_within_days

        state_dir = Path(cfg["state_dir"])
        state_dir.mkdir(parents=True, exist_ok=True)
        state_obj = {
            "last_run": None,
            "seen": seen or {},
            "content_hashes": content_hashes or {},
        }
        (state_dir / "teardown_state.json").write_text(
            json.dumps(state_obj), encoding="utf-8"
        )

        meta_by_repo = meta_by_repo or {}
        content_by_repo = content_by_repo or {}

        def _search_code_page(query, page, per_page):
            return (hits or []) if page == 1 else [], None

        def _fetch_repo_meta(full_name):
            return meta_by_repo.get(full_name)

        def _fetch_artefact_content(full_name, path):
            return content_by_repo.get(full_name, "")

        args = mock.Mock(
            config="config.json",
            days=30,
            limit=None,
            dry_run=False,
            no_artefacts=no_artefacts,
        )
        with (
            mock.patch.object(ts, "load_config", return_value=cfg),
            mock.patch.object(ts, "search_code_page", side_effect=_search_code_page),
            mock.patch.object(ts, "fetch_repo_meta", side_effect=_fetch_repo_meta),
            mock.patch.object(
                ts, "fetch_artefact_content", side_effect=_fetch_artefact_content
            ),
            mock.patch.object(ts.time, "sleep"),
        ):
            rc = ts.cmd_scan(args)
        payload = json.loads(Path(cfg["candidates_file"]).read_text(encoding="utf-8"))
        return rc, payload, cfg

    def test_artefact_candidate_shape_in_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, payload, _cfg = self._run_scan(
                tmp,
                hits=[self._hit("good/repo")],
                meta_by_repo={"good/repo": self._meta()},
                content_by_repo={"good/repo": "memory index"},
            )
        self.assertEqual(rc, 0)
        art = [c for c in payload["candidates"] if c["lane"] == "artefact"]
        self.assertEqual(len(art), 1)
        self.assertEqual(art[0]["repo"], "good/repo")

    def test_fork_is_excluded_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            _rc, payload, _cfg = self._run_scan(
                tmp,
                hits=[self._hit("a/fork-repo")],
                meta_by_repo={"a/fork-repo": self._meta(fork=True)},
                content_by_repo={"a/fork-repo": "x"},
            )
        self.assertEqual(payload["dropped"]["fork"], 1)
        self.assertEqual(
            [c for c in payload["candidates"] if c["lane"] == "artefact"], []
        )

    def test_template_is_excluded_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            _rc, payload, _cfg = self._run_scan(
                tmp,
                hits=[self._hit("a/tmpl")],
                meta_by_repo={"a/tmpl": self._meta(is_template=True)},
                content_by_repo={"a/tmpl": "x"},
            )
        self.assertEqual(payload["dropped"]["template"], 1)

    def test_archived_is_excluded_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            _rc, payload, _cfg = self._run_scan(
                tmp,
                hits=[self._hit("a/old")],
                meta_by_repo={"a/old": self._meta(archived=True)},
                content_by_repo={"a/old": "x"},
            )
        self.assertEqual(payload["dropped"]["archived"], 1)

    def test_below_artefact_star_floor_is_excluded_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            _rc, payload, _cfg = self._run_scan(
                tmp,
                hits=[self._hit("a/tiny")],
                meta_by_repo={"a/tiny": self._meta(stargazers_count=1)},
                content_by_repo={"a/tiny": "x"},
                artefact_min_stars=20,
            )
        self.assertEqual(payload["dropped"]["stars"], 1)

    def test_stale_repo_is_excluded_and_counted(self):
        old = (datetime.now(timezone.utc) - timedelta(days=500)).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            _rc, payload, _cfg = self._run_scan(
                tmp,
                hits=[self._hit("a/stale")],
                meta_by_repo={"a/stale": self._meta(pushed_at=old)},
                content_by_repo={"a/stale": "x"},
                active_within_days=365,
            )
        self.assertEqual(payload["dropped"]["stale"], 1)

    def test_own_repo_excluded_via_artefact_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            _rc, payload, _cfg = self._run_scan(
                tmp,
                hits=[self._hit("me/my-project")],
                meta_by_repo={"me/my-project": self._meta()},
                content_by_repo={"me/my-project": "x"},
                own_repos=["me/my-project"],
            )
        self.assertEqual(payload["dropped"]["own"], 1)

    def test_metadata_fetch_failure_drops_silently_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, payload, _cfg = self._run_scan(
                tmp,
                hits=[self._hit("a/gone")],
                meta_by_repo={},
                content_by_repo={},
            )
        self.assertEqual(rc, 0)
        self.assertEqual(payload["candidates"], [])

    def test_content_hash_dedup_across_different_repos(self):
        with tempfile.TemporaryDirectory() as tmp:
            _rc, payload, _cfg = self._run_scan(
                tmp,
                hits=[self._hit("a/one"), self._hit("a/two")],
                meta_by_repo={"a/one": self._meta(), "a/two": self._meta()},
                content_by_repo={
                    "a/one": "identical template body",
                    "a/two": "identical template body",
                },
            )
        art = [c for c in payload["candidates"] if c["lane"] == "artefact"]
        self.assertEqual(len(art), 1)
        self.assertEqual(payload["dropped"]["dup_content"], 1)

    def test_content_hash_dedup_against_prior_state(self):
        prior_hash = ts.content_prefix_hash("identical template body")
        with tempfile.TemporaryDirectory() as tmp:
            _rc, payload, _cfg = self._run_scan(
                tmp,
                hits=[self._hit("a/one")],
                meta_by_repo={"a/one": self._meta()},
                content_by_repo={"a/one": "identical template body"},
                content_hashes={prior_hash: "2020-01-01"},
            )
        self.assertEqual(payload["dropped"]["dup_content"], 1)

    def test_seen_store_excludes_repeat_artefact_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            _rc, payload, _cfg = self._run_scan(
                tmp,
                hits=[self._hit("seen/repo")],
                meta_by_repo={"seen/repo": self._meta()},
                content_by_repo={"seen/repo": "x"},
                seen={"seen/repo": "2099-01-01"},
            )
        self.assertEqual(payload["dropped"]["seen"], 1)

    def test_covered_ledger_excludes_artefact_repo_by_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            with (state_dir / "covered_log.jsonl").open("w", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "date": "2026-01-01T00:00:00+00:00",
                            "url": "https://github.com/done/repo",
                            "note": "",
                        }
                    )
                    + "\n"
                )
            _rc, payload, _cfg = self._run_scan(
                tmp,
                hits=[self._hit("done/repo")],
                meta_by_repo={"done/repo": self._meta()},
                content_by_repo={"done/repo": "x"},
            )
        self.assertEqual(payload["dropped"]["covered"], 1)

    def test_same_repo_two_artefacts_dedups_to_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            _rc, payload, _cfg = self._run_scan(
                tmp,
                hits=[self._hit("a/b", "CLAUDE.md"), self._hit("a/b", "AGENTS.md")],
                meta_by_repo={"a/b": self._meta()},
                content_by_repo={"a/b": "memory index"},
            )
        art = [c for c in payload["candidates"] if c["lane"] == "artefact"]
        self.assertEqual(len(art), 1)
        self.assertEqual(payload["dropped"]["dup"], 1)

    def test_no_artefacts_flag_skips_lane_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ts, "artefact_lane") as m_lane:
                _rc, payload, _cfg = self._run_scan(
                    tmp,
                    hits=[self._hit("a/b")],
                    meta_by_repo={"a/b": self._meta()},
                    content_by_repo={"a/b": "x"},
                    no_artefacts=True,
                )
            m_lane.assert_not_called()
        self.assertEqual(payload["candidates"], [])
        self.assertEqual(sum(payload["dropped"].values()), 0)

    def test_summary_line_reports_artefacts_and_repos(self):
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self._run_scan(
                    tmp,
                    hits=[self._hit("a/b")],
                    meta_by_repo={"a/b": self._meta()},
                    content_by_repo={"a/b": "x"},
                )
            out = buf.getvalue()
        self.assertIn("artefacts=1", out)
        self.assertIn("repos=1", out)

    def test_rate_limited_lane_does_not_fail_the_whole_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _full_config()
            cfg["state_dir"] = str(Path(tmp) / "state")
            cfg["candidates_file"] = str(Path(tmp) / "candidates.json")
            cfg["search_queries"] = []
            cfg["topics"] = []
            cfg["hn_queries"] = []
            args = mock.Mock(
                config="config.json",
                days=30,
                limit=None,
                dry_run=False,
                no_artefacts=False,
            )
            with (
                mock.patch.object(ts, "load_config", return_value=cfg),
                mock.patch.object(
                    ts,
                    "search_code_page",
                    return_value=([], "gh: API rate limit exceeded (HTTP 403)"),
                ),
                mock.patch.object(ts.time, "sleep"),
            ):
                rc = ts.cmd_scan(args)
            payload = json.loads(
                Path(cfg["candidates_file"]).read_text(encoding="utf-8")
            )
        self.assertEqual(rc, 0)
        self.assertTrue(any("rate limit" in e for e in payload["errors"]))


class ArtefactDryRunTests(unittest.TestCase):
    def test_dry_run_lists_artefact_queries_by_default(self):
        cfg = _full_config()
        args = mock.Mock(
            config="config.json",
            days=None,
            limit=None,
            dry_run=True,
            no_artefacts=False,
        )
        buf = io.StringIO()
        with (
            mock.patch.object(ts, "load_config", return_value=cfg),
            mock.patch("sys.stdout", buf),
        ):
            ts.cmd_scan(args)
        out = buf.getvalue()
        self.assertIn("lane 3", out)
        self.assertIn("claude-md", out)
        self.assertIn("filename:CLAUDE.md", out)

    def test_no_artefacts_suppresses_lane_3_preview(self):
        cfg = _full_config()
        args = mock.Mock(
            config="config.json",
            days=None,
            limit=None,
            dry_run=True,
            no_artefacts=True,
        )
        buf = io.StringIO()
        with (
            mock.patch.object(ts, "load_config", return_value=cfg),
            mock.patch("sys.stdout", buf),
        ):
            ts.cmd_scan(args)
        out = buf.getvalue()
        self.assertIn("skipped (--no-artefacts)", out)
        self.assertNotIn("claude-md", out)

    def test_dry_run_makes_no_calls_with_artefacts_enabled(self):
        cfg = _full_config()
        args = mock.Mock(
            config="config.json",
            days=None,
            limit=None,
            dry_run=True,
            no_artefacts=False,
        )
        with (
            mock.patch.object(ts, "load_config", return_value=cfg),
            mock.patch.object(
                ts, "search_code_page", side_effect=AssertionError("no gh in dry-run")
            ),
            mock.patch.object(
                ts,
                "artefact_lane",
                side_effect=AssertionError("no lane call in dry-run"),
            ),
            mock.patch("sys.stdout", io.StringIO()),
        ):
            rc = ts.cmd_scan(args)
        self.assertEqual(rc, 0)


class PublishVenueConfigTests(unittest.TestCase):
    """publish_venues at the load_config altitude: a type check like every
    other list-shaped config key, and the shipped defaults are real,
    validation-clean registry entries."""

    def _write(self, tmp, cfg):
        p = Path(tmp) / "config.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")
        return str(p)

    def test_publish_venues_must_be_a_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                ts.load_config(
                    self._write(tmp, {"own_repos": [], "publish_venues": "nope"})
                )

    def test_publish_venues_default_is_filled_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ts.load_config(self._write(tmp, {"own_repos": []}))
        self.assertEqual(len(cfg["publish_venues"]), 6)
        names = {v["name"] for v in cfg["publish_venues"]}
        self.assertEqual(
            names,
            {
                "Hacker News",
                "r/LocalLLaMA",
                "r/ClaudeAI",
                "r/AI_Agents",
                "lobste.rs",
                "dev.to",
            },
        )

    def test_default_registry_entries_all_pass_validation_cleanly(self):
        advisory = []
        venues = ts.valid_venues(ts.DEFAULTS["publish_venues"], advisory)
        self.assertEqual(len(venues), len(ts.DEFAULTS["publish_venues"]))
        self.assertEqual(advisory, [])


class PublishVenueValidationTests(unittest.TestCase):
    """_validate_venue / valid_venues: one bad publish_venues entry is
    skipped with an advisory note, never a SystemExit - a config typo here
    is a coverage-advisory issue, not a scan-killing one, mirroring
    newsletter-sweep's _validate_outlet. manual_only and the
    subject's-own-space host ban are enforced here, not trusted."""

    def _entry(self, **overrides):
        base = {
            "name": "Test Venue",
            "kind": "hn",
            "url": "https://example.com/submit",
            "topics": ["agent"],
            "etiquette": "be polite",
            "manual_only": True,
        }
        base.update(overrides)
        return base

    def test_non_dict_entry_is_skipped_with_advisory(self):
        advisory = []
        self.assertIsNone(ts._validate_venue("not-a-dict", advisory))
        self.assertEqual(len(advisory), 1)
        self.assertIn("not an object", advisory[0])

    def test_missing_required_string_keys_are_skipped(self):
        for key in ("name", "kind", "url", "etiquette"):
            with self.subTest(key=key):
                advisory = []
                entry = self._entry()
                del entry[key]
                self.assertIsNone(ts._validate_venue(entry, advisory))
                self.assertEqual(len(advisory), 1)

    def test_blank_string_value_is_treated_as_missing(self):
        advisory = []
        self.assertIsNone(ts._validate_venue(self._entry(name="   "), advisory))
        self.assertEqual(len(advisory), 1)

    def test_topics_missing_is_skipped(self):
        advisory = []
        entry = self._entry()
        del entry["topics"]
        self.assertIsNone(ts._validate_venue(entry, advisory))
        self.assertIn("topics", advisory[0])

    def test_topics_not_a_list_is_skipped(self):
        advisory = []
        self.assertIsNone(ts._validate_venue(self._entry(topics="agent"), advisory))

    def test_topics_with_non_string_element_is_skipped(self):
        advisory = []
        self.assertIsNone(
            ts._validate_venue(self._entry(topics=["agent", 5]), advisory)
        )

    def test_empty_topics_list_is_valid(self):
        advisory = []
        venue = ts._validate_venue(self._entry(topics=[]), advisory)
        self.assertIsNotNone(venue)
        self.assertEqual(venue["topics"], [])
        self.assertEqual(advisory, [])

    def test_manual_only_false_is_rejected(self):
        advisory = []
        self.assertIsNone(ts._validate_venue(self._entry(manual_only=False), advisory))
        self.assertIn("manual_only", advisory[0])

    def test_manual_only_missing_is_rejected(self):
        advisory = []
        entry = self._entry()
        del entry["manual_only"]
        self.assertIsNone(ts._validate_venue(entry, advisory))
        self.assertIn("manual_only", advisory[0])

    def test_manual_only_truthy_non_bool_is_rejected(self):
        # 1 == True in Python but is not True; the boundary this field
        # guards is worth an `is True` check, not a truthy one.
        advisory = []
        self.assertIsNone(ts._validate_venue(self._entry(manual_only=1), advisory))

    def test_github_host_is_rejected(self):
        advisory = []
        entry = self._entry(url="https://github.com/some/repo/issues")
        self.assertIsNone(ts._validate_venue(entry, advisory))
        self.assertIn("subject's own space", advisory[0])

    def test_www_github_host_is_rejected(self):
        advisory = []
        entry = self._entry(url="https://www.github.com/some/repo")
        self.assertIsNone(ts._validate_venue(entry, advisory))

    def test_non_github_host_with_github_in_the_path_is_not_rejected(self):
        # Only the HOST is checked, not the url text generally - a venue
        # legitimately pathed/named around "github" elsewhere must not be
        # caught by an over-broad substring check.
        advisory = []
        entry = self._entry(url="https://example.com/github-teardowns/submit")
        venue = ts._validate_venue(entry, advisory)
        self.assertIsNotNone(venue)
        self.assertEqual(advisory, [])

    def test_valid_entry_is_normalized(self):
        advisory = []
        venue = ts._validate_venue(self._entry(), advisory)
        self.assertEqual(
            venue,
            {
                "name": "Test Venue",
                "kind": "hn",
                "url": "https://example.com/submit",
                "topics": ["agent"],
                "etiquette": "be polite",
                "manual_only": True,
            },
        )
        self.assertEqual(advisory, [])

    def test_valid_venues_skips_bad_entries_without_killing_the_rest(self):
        raw = [
            self._entry(name="Good One"),
            {"name": "Broken", "kind": "hn"},  # missing url/topics/etiquette
            self._entry(name="Also Good", url="https://example.org/submit"),
            self._entry(name="Flips The Flag", manual_only=False),
        ]
        advisory = []
        venues = ts.valid_venues(raw, advisory)
        self.assertEqual([v["name"] for v in venues], ["Good One", "Also Good"])
        self.assertEqual(len(advisory), 2)

    def test_valid_venues_empty_raw_returns_empty_list(self):
        self.assertEqual(ts.valid_venues([], []), [])

    def test_valid_venues_none_raw_returns_empty_list(self):
        self.assertEqual(ts.valid_venues(None, []), [])


class CandidateTopicHaystackTests(unittest.TestCase):
    """_candidate_topic_haystack: the text surface venue topics are matched
    against, built only from fields the candidate already carries."""

    def test_includes_matched_keywords(self):
        cand = {"matched_keywords": ["memory", "orchestration"]}
        haystack = ts._candidate_topic_haystack(cand)
        self.assertIn("memory", haystack)
        self.assertIn("orchestration", haystack)

    def test_strips_topic_prefix_from_pattern(self):
        cand = {"pattern": "topic:agentic-ai"}
        self.assertIn("agentic-ai", ts._candidate_topic_haystack(cand))

    def test_plain_phrase_pattern_included_verbatim(self):
        cand = {"pattern": "claude code setup"}
        self.assertIn("claude code setup", ts._candidate_topic_haystack(cand))

    def test_includes_artefact_label(self):
        cand = {"artefact_label": "claude-md"}
        self.assertIn("claude-md", ts._candidate_topic_haystack(cand))

    def test_includes_patterns_present(self):
        cand = {"patterns_present": ["p01", "p05"]}
        haystack = ts._candidate_topic_haystack(cand)
        self.assertIn("p01", haystack)
        self.assertIn("p05", haystack)

    def test_missing_fields_produce_safe_empty_string_not_crash(self):
        self.assertEqual(ts._candidate_topic_haystack({}), "   ")

    def test_haystack_is_lowercased(self):
        cand = {"artefact_label": "Claude-MD"}
        self.assertIn("claude-md", ts._candidate_topic_haystack(cand))


class VenueMatchTests(unittest.TestCase):
    """_venue_match: the provenance short-circuit for lane 'hn' against an
    'hn'-kind venue, and plain topical-overlap counting for everything
    else - including artefact-lane candidates, which have no provenance
    rule of their own (see the module's publish-venue-suggestions banner
    comment)."""

    def test_hn_lane_gets_provenance_for_hn_kind_venue(self):
        cand = {"lane": "hn", "matched_keywords": [], "pattern": ""}
        venue = _venue("HN", "hn", ["agent"])
        self.assertEqual(
            ts._venue_match(cand, venue), (ts.PROVENANCE_SCORE, ts.PROVENANCE_WHY)
        )

    def test_provenance_fires_even_with_zero_topical_overlap(self):
        cand = {"lane": "hn", "matched_keywords": [], "pattern": "unrelated phrase"}
        venue = _venue("HN", "hn", ["something-else-entirely"])
        self.assertIsNotNone(ts._venue_match(cand, venue))
        self.assertEqual(ts._venue_match(cand, venue)[0], ts.PROVENANCE_SCORE)

    def test_non_hn_lane_gets_no_provenance_for_hn_venue(self):
        cand = {"lane": "github", "matched_keywords": [], "pattern": "agent"}
        venue = _venue("HN", "hn", ["agent"])
        score, why = ts._venue_match(cand, venue)
        self.assertNotEqual(score, ts.PROVENANCE_SCORE)
        self.assertNotEqual(why, ts.PROVENANCE_WHY)

    def test_hn_lane_against_non_hn_venue_is_a_plain_topical_match(self):
        cand = {"lane": "hn", "matched_keywords": ["memory"], "pattern": ""}
        venue = _venue("Lobsters", "lobsters", ["memory", "practices"])
        self.assertEqual(ts._venue_match(cand, venue), (1, "topic overlap: memory"))

    def test_topical_hit_count_and_why_lists_every_hit(self):
        cand = {"pattern": "topic:agentic-ai", "matched_keywords": ["agent"]}
        venue = _venue("AI Agents", "reddit", ["agent", "agentic-ai", "multi-agent"])
        score, why = ts._venue_match(cand, venue)
        self.assertEqual(score, 2)
        self.assertEqual(why, "topic overlap: agent, agentic-ai")

    def test_no_hits_returns_none(self):
        cand = {"lane": "github", "matched_keywords": [], "pattern": "nothing here"}
        venue = _venue("Lobsters", "lobsters", ["ai", "practices"])
        self.assertIsNone(ts._venue_match(cand, venue))


class SuggestVenuesTests(unittest.TestCase):
    """suggest_venues: the full ranking - provenance + topical scoring,
    at-most-one-per-kind, capped at MAX_SUGGESTED_VENUES, deterministic on
    ties. Uses small explicit venue fixtures (see _venue above) rather than
    the shipped DEFAULTS, so these stay independent of registry edits."""

    def test_empty_registry_returns_empty_list(self):
        cand = {"lane": "hn", "matched_keywords": [], "pattern": "agent"}
        self.assertEqual(ts.suggest_venues(cand, []), [])

    def test_no_matching_venue_returns_empty_list(self):
        cand = {"lane": "github", "matched_keywords": [], "pattern": "nothing"}
        venues = [_venue("Lobsters", "lobsters", ["ai", "practices"])]
        self.assertEqual(ts.suggest_venues(cand, venues), [])

    def test_cap_at_three_suggestions_across_four_matching_kinds(self):
        cand = {"lane": "github", "matched_keywords": [], "pattern": "agent"}
        venues = [
            _venue("V-HN", "hn", ["agent"]),
            _venue("V-Reddit", "reddit", ["agent"]),
            _venue("V-Lobsters", "lobsters", ["agent"]),
            _venue("V-Devto", "devto", ["agent"]),
        ]
        out = ts.suggest_venues(cand, venues)
        self.assertEqual(len(out), ts.MAX_SUGGESTED_VENUES)
        self.assertEqual([s["name"] for s in out], ["V-HN", "V-Reddit", "V-Lobsters"])

    def test_kind_diversity_keeps_only_the_best_per_kind(self):
        cand = {
            "lane": "github",
            "matched_keywords": [],
            "pattern": "claude code local-llm agent self-hosted",
        }
        venues = [
            _venue("r/LocalLLaMA", "reddit", ["local-llm", "self-hosted"]),
            _venue("r/ClaudeAI", "reddit", ["claude"]),
            _venue("r/AI_Agents", "reddit", ["agent"]),
            _venue("HN", "hn", ["agent"]),
        ]
        out = ts.suggest_venues(cand, venues)
        reddit_names = {v["name"] for v in venues if v["kind"] == "reddit"}
        picked_reddit = [s["name"] for s in out if s["name"] in reddit_names]
        self.assertEqual(picked_reddit, ["r/LocalLLaMA"])

    def test_deterministic_tie_break_keeps_registry_order(self):
        cand = {"lane": "github", "matched_keywords": [], "pattern": "x"}
        venues = [
            _venue("First", "a", ["x"]),
            _venue("Second", "b", ["x"]),
            _venue("Third", "c", ["x"]),
        ]
        out = ts.suggest_venues(cand, venues)
        self.assertEqual([s["name"] for s in out], ["First", "Second", "Third"])

    def test_artefact_lane_matches_via_artefact_label(self):
        cand = {
            "lane": "artefact",
            "artefact_label": "claude-md",
            "patterns_present": [],
        }
        venues = [_venue("r/ClaudeAI", "reddit", ["claude", "claude-code"])]
        out = ts.suggest_venues(cand, venues)
        self.assertEqual(out, [{"name": "r/ClaudeAI", "why": "topic overlap: claude"}])

    def test_provenance_venue_ranks_above_a_strong_topical_match(self):
        cand = {"lane": "hn", "matched_keywords": [], "pattern": "agent dev-tools"}
        venues = [
            # Registered AFTER the topical venue, but provenance must still
            # sort first - PROVENANCE_SCORE comfortably outranks any
            # realistic topic-hit count.
            _venue("Topical", "reddit", ["agent", "dev-tools"]),
            _venue("HN", "hn", ["agent"]),
        ]
        out = ts.suggest_venues(cand, venues)
        self.assertEqual(out[0]["name"], "HN")
        self.assertEqual(out[0]["why"], ts.PROVENANCE_WHY)

    def test_output_entries_have_only_name_and_why(self):
        cand = {"lane": "hn", "matched_keywords": [], "pattern": "agent"}
        venues = [_venue("HN", "hn", ["agent"])]
        out = ts.suggest_venues(cand, venues)
        self.assertEqual(len(out), 1)
        self.assertEqual(set(out[0].keys()), {"name", "why"})

    def test_never_list_venue_never_reaches_suggestions(self):
        # The full pipeline: a registry entry aimed at the subject's own
        # space is dropped by valid_venues, so it never even reaches
        # suggest_venues - even against a candidate it would otherwise
        # match perfectly.
        raw = list(ts.DEFAULTS["publish_venues"]) + [
            {
                "name": "Subject Own Issue Tracker",
                "kind": "github-issues",
                "url": "https://github.com/some/subject/issues",
                "topics": ["agent", "claude", "memory"],
                "etiquette": "n/a",
                "manual_only": True,
            }
        ]
        advisory = []
        venues = ts.valid_venues(raw, advisory)
        self.assertTrue(any("Subject Own" in msg for msg in advisory))
        cand = {
            "lane": "hn",
            "matched_keywords": ["memory"],
            "pattern": "claude code setup agent",
        }
        out = ts.suggest_venues(cand, venues)
        self.assertTrue(all(s["name"] != "Subject Own Issue Tracker" for s in out))


class MarkCoveredPostedToTests(unittest.TestCase):
    """--posted-to: where a finished teardown actually went, once it's
    known. Optional and repeatable; the ledger and `log` both round-trip
    it. See MarkCoveredLogTests above for the pre-existing url/note/log
    coverage this extends without duplicating."""

    def test_single_posted_to_is_recorded(self):
        cfg = _full_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            with mock.patch.object(ts, "load_config", return_value=cfg):
                args = mock.Mock(
                    config="config.json",
                    url="https://github.com/a/b",
                    note="n",
                    posted_to=["Hacker News"],
                )
                ts.cmd_mark_covered(args)
            ledger = Path(cfg["state_dir"]) / "covered_log.jsonl"
            entry = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(entry["posted_to"], ["Hacker News"])

    def test_repeated_posted_to_flag_collects_a_list(self):
        # action="append" is what argparse gives cmd_mark_covered when
        # --posted-to is passed more than once; simulate its output shape
        # directly rather than re-testing argparse itself.
        cfg = _full_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            with mock.patch.object(ts, "load_config", return_value=cfg):
                args = mock.Mock(
                    config="config.json",
                    url="https://x",
                    note=None,
                    posted_to=["Hacker News", "r/ClaudeAI"],
                )
                ts.cmd_mark_covered(args)
            ledger = Path(cfg["state_dir"]) / "covered_log.jsonl"
            entry = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(entry["posted_to"], ["Hacker News", "r/ClaudeAI"])

    def test_posted_to_none_normalizes_to_empty_list(self):
        # Real argparse shape when the flag was never passed.
        cfg = _full_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            with mock.patch.object(ts, "load_config", return_value=cfg):
                args = mock.Mock(
                    config="config.json", url="https://x", note=None, posted_to=None
                )
                ts.cmd_mark_covered(args)
            ledger = Path(cfg["state_dir"]) / "covered_log.jsonl"
            entry = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(entry["posted_to"], [])

    def test_bare_mock_without_posted_to_set_does_not_crash(self):
        # Regression guard: a bare mock.Mock() auto-vivifies an unset
        # attribute as a truthy child Mock, not None - exactly the
        # no_artefacts footgun cmd_scan's own comment documents. Without the
        # isinstance guard in cmd_mark_covered this would try to
        # json-serialize a Mock object and blow up append_ledger.
        cfg = _full_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            with mock.patch.object(ts, "load_config", return_value=cfg):
                args = mock.Mock(config="config.json", url="https://x", note=None)
                rc = ts.cmd_mark_covered(args)
            ledger = Path(cfg["state_dir"]) / "covered_log.jsonl"
            entry = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(rc, 0)
        self.assertEqual(entry["posted_to"], [])

    def test_log_prints_posted_to_suffix(self):
        cfg = _full_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "covered_log.jsonl").write_text(
                json.dumps(
                    {
                        "date": "2026-01-01T00:00:00+00:00",
                        "url": "https://x",
                        "note": "n",
                        "posted_to": ["Hacker News", "r/ClaudeAI"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            buf = io.StringIO()
            with (
                mock.patch.object(ts, "load_config", return_value=cfg),
                contextlib.redirect_stdout(buf),
            ):
                ts.cmd_log(mock.Mock(config="config.json"))
        self.assertIn("posted: Hacker News, r/ClaudeAI", buf.getvalue())

    def test_log_omits_suffix_when_posted_to_empty(self):
        cfg = _full_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "covered_log.jsonl").write_text(
                json.dumps(
                    {
                        "date": "2026-01-01T00:00:00+00:00",
                        "url": "https://x",
                        "note": "n",
                        "posted_to": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            buf = io.StringIO()
            with (
                mock.patch.object(ts, "load_config", return_value=cfg),
                contextlib.redirect_stdout(buf),
            ):
                ts.cmd_log(mock.Mock(config="config.json"))
        self.assertNotIn("posted:", buf.getvalue())

    def test_log_handles_legacy_entries_without_posted_to_key(self):
        # A ledger line written before this feature existed has no
        # posted_to key at all - log must not KeyError on it.
        cfg = _full_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "covered_log.jsonl").write_text(
                json.dumps(
                    {
                        "date": "2026-01-01T00:00:00+00:00",
                        "url": "https://x",
                        "note": "",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            buf = io.StringIO()
            with (
                mock.patch.object(ts, "load_config", return_value=cfg),
                contextlib.redirect_stdout(buf),
            ):
                rc = ts.cmd_log(mock.Mock(config="config.json"))
        self.assertEqual(rc, 0)
        self.assertIn("https://x", buf.getvalue())
        self.assertNotIn("posted:", buf.getvalue())


class ScanSuggestedVenuesIntegrationTests(unittest.TestCase):
    """suggested_venues wired into a real cmd_scan run: every kept
    candidate carries it, and a malformed publish_venues entry never
    crashes or holds the scan. Mirrors ScanIntegrationTests' _run_scan
    shape above."""

    def _run_scan(self, tmp, *, github_nodes=None, hn_hits=None, publish_venues=None):
        cfg = _full_config()
        cfg["state_dir"] = str(Path(tmp) / "state")
        cfg["candidates_file"] = str(Path(tmp) / "candidates.json")
        cfg["search_queries"] = ["agent architecture"]
        cfg["topics"] = []
        cfg["hn_queries"] = ["agent architecture"]
        if publish_venues is not None:
            cfg["publish_venues"] = publish_venues

        args = mock.Mock(config="config.json", days=30, limit=None, dry_run=False)

        def _search_repos(query, limit, extra_args, errors):
            return github_nodes or []

        def _http_get(url, timeout=None, headers=None):
            return 200, json.dumps({"hits": hn_hits or []}), None

        with (
            mock.patch.object(ts, "load_config", return_value=cfg),
            mock.patch.object(ts, "search_repos", side_effect=_search_repos),
            mock.patch.object(ts, "fetch_readme_text", return_value=""),
            mock.patch.object(ts, "has_docs_dir", return_value=False),
            mock.patch.object(ts, "http_get", side_effect=_http_get),
        ):
            rc = ts.cmd_scan(args)
        payload = json.loads(Path(cfg["candidates_file"]).read_text(encoding="utf-8"))
        return rc, payload

    def test_kept_github_candidate_carries_suggested_venues_field(self):
        nodes = [
            {
                "fullName": "good/repo",
                "description": "an agent workspace",
                "stargazersCount": 500,
                "url": "https://github.com/good/repo",
                "pushedAt": datetime.now(timezone.utc).isoformat(),
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            rc, payload = self._run_scan(tmp, github_nodes=nodes)
        self.assertEqual(rc, 0)
        github_cands = [c for c in payload["candidates"] if c["lane"] == "github"]
        self.assertEqual(len(github_cands), 1)
        self.assertIn("suggested_venues", github_cands[0])
        self.assertIsInstance(github_cands[0]["suggested_venues"], list)

    def test_kept_hn_candidate_gets_hn_provenance_suggestion(self):
        hits = [
            {"objectID": "1", "title": "agent architecture deep dive", "points": 50}
        ]
        with tempfile.TemporaryDirectory() as tmp:
            rc, payload = self._run_scan(tmp, hn_hits=hits)
        self.assertEqual(rc, 0)
        hn_cands = [c for c in payload["candidates"] if c["lane"] == "hn"]
        self.assertEqual(len(hn_cands), 1)
        suggestions = hn_cands[0]["suggested_venues"]
        hn_entry = next(s for s in suggestions if s["name"] == "Hacker News")
        self.assertEqual(hn_entry["why"], ts.PROVENANCE_WHY)

    def test_malformed_publish_venues_entry_does_not_crash_or_hold_scan(self):
        nodes = [
            {
                "fullName": "good/repo2",
                "description": "an agent",
                "stargazersCount": 500,
                "url": "https://github.com/good/repo2",
                "pushedAt": datetime.now(timezone.utc).isoformat(),
            }
        ]
        broken_venues = list(ts.DEFAULTS["publish_venues"]) + [
            {"name": "Broken", "kind": "hn"}
        ]
        with tempfile.TemporaryDirectory() as tmp:
            rc, payload = self._run_scan(
                tmp, github_nodes=nodes, publish_venues=broken_venues
            )
        self.assertEqual(rc, 0)
        self.assertFalse(payload["window_held"])
        github_cands = [c for c in payload["candidates"] if c["lane"] == "github"]
        self.assertEqual(len(github_cands), 1)
        self.assertIsInstance(github_cands[0]["suggested_venues"], list)
        self.assertTrue(any("Broken" in e for e in payload["errors"]))


class ArtefactScanSuggestedVenuesIntegrationTests(unittest.TestCase):
    """Lane 3's own signal (artefact_label) drives a real suggestion the
    same way lanes 1-2's matched_keywords/pattern do. Mirrors
    ArtefactScanIntegrationTests' fixture shape above."""

    def _hit(self, repo, path="CLAUDE.md"):
        return {
            "path": path,
            "html_url": f"https://github.com/{repo}/blob/x/{path}",
            "repository": {"full_name": repo},
        }

    def _meta(self, **overrides):
        base = {
            "stargazers_count": 100,
            "pushed_at": datetime.now(timezone.utc).isoformat(),
            "fork": False,
            "is_template": False,
            "archived": False,
        }
        base.update(overrides)
        return base

    def test_artefact_candidate_gets_claude_venue_suggestion_via_label(self):
        cfg = _full_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg["state_dir"] = str(Path(tmp) / "state")
            cfg["candidates_file"] = str(Path(tmp) / "candidates.json")
            cfg["search_queries"] = []
            cfg["topics"] = []
            cfg["hn_queries"] = []
            cfg["artefact_queries"] = [
                {"label": "claude-md", "query": "filename:CLAUDE.md size:>4000"}
            ]
            state_dir = Path(cfg["state_dir"])
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "teardown_state.json").write_text(
                json.dumps({"last_run": None, "seen": {}, "content_hashes": {}}),
                encoding="utf-8",
            )

            def _search_code_page(query, page, per_page):
                return ([self._hit("good/repo")] if page == 1 else []), None

            args = mock.Mock(
                config="config.json",
                days=30,
                limit=None,
                dry_run=False,
                no_artefacts=False,
            )
            with (
                mock.patch.object(ts, "load_config", return_value=cfg),
                mock.patch.object(
                    ts, "search_code_page", side_effect=_search_code_page
                ),
                mock.patch.object(ts, "fetch_repo_meta", return_value=self._meta()),
                mock.patch.object(
                    ts, "fetch_artefact_content", return_value="memory index"
                ),
                mock.patch.object(ts.time, "sleep"),
            ):
                rc = ts.cmd_scan(args)
            payload = json.loads(
                Path(cfg["candidates_file"]).read_text(encoding="utf-8")
            )
        self.assertEqual(rc, 0)
        art = [c for c in payload["candidates"] if c["lane"] == "artefact"]
        self.assertEqual(len(art), 1)
        names = [v["name"] for v in art[0]["suggested_venues"]]
        self.assertIn("r/ClaudeAI", names)


class NoOutboundTests(unittest.TestCase):
    """teardown-sweep is read-only discovery with no outbound path, like
    placement-health. This guard freezes that property: an edit that adds a
    posting/mutation/scheduler affordance must fail here."""

    def test_no_outbound_or_scheduler_token_in_source(self):
        src = _MOD_PATH.read_text(encoding="utf-8").lower()
        banned = [
            "mutation",
            "issues/{",
            "/comments",
            "adddiscussioncomment",
            "pr create",
            "pull-request",
            "-x post",
            "--method post",
            "auto_post",
            "auto-post",
            "batch_approve",
            "batch-approve",
            "schedule",
            "cron",
        ]
        for token in banned:
            self.assertNotIn(
                token, src, f"outbound/scheduler token {token!r} must not appear"
            )

    def test_only_scan_mark_covered_log_subcommands(self):
        src = _MOD_PATH.read_text(encoding="utf-8")
        # \s* between the paren and the name tolerates ruff-format wrapping a
        # long add_parser(...) call onto its own line.
        for name in ("scan", "mark-covered", "log"):
            self.assertRegex(src, rf'add_parser\(\s*"{name}"')
        for banned in ('add_parser("submit', 'add_parser("post', 'add_parser("comment'):
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()
