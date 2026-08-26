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
