#!/usr/bin/env python3
"""Offline unit tests for benchmark-sweep (stdlib unittest only).

No network, no live gh: gh_graphql and http_get are patched to return fixture
payloads (GraphQL search results for the gh lane, Atom XML bodies for the
arXiv lane). The shared dedup/seen-store/window plumbing lives in sweepcore
and is covered by modules/test_sweepcore.py; these tests cover benchmark-
sweep's own pure helpers (candidate mapping for both lanes, config validation,
filtering, capping, per-lane window markers) plus the load-bearing
NoOutboundTests guard that discovery-only v1 has zero posting path, mirroring
thread-sweep's identical gate guard even though this module has no act stage
yet to gate.

Run: python -m unittest discover -s modules/benchmark_sweep -p 'test_*.py'
"""

import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

# Import the module by path so the test runs from any cwd. benchmark_sweep
# itself adds modules/ to sys.path to find sweepcore; replicate that here first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark_sweep as bs  # noqa: E402

# --- shared fixtures -----------------------------------------------------------


def _issue_node(url="https://github.com/acme/widgets/issues/7", **over):
    """One well-formed, keepable GitHub search hit (foreign repo, over the
    star floor)."""
    node = {
        "url": url,
        "title": "How do you test agent memory across sessions?",
        "createdAt": "2026-07-10T00:00:00Z",
        "bodyText": "first line\nsecond line\r third",
        "repository": {"nameWithOwner": "acme/widgets", "stargazerCount": 900},
        "comments": {"totalCount": 0},
        "isAnswered": False,
        "author": {"login": "someone"},
    }
    node.update(over)
    return node


FEED_TEMPLATE = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    '<feed xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" '
    'xmlns:arxiv="http://arxiv.org/schemas/atom" '
    'xmlns="http://www.w3.org/2005/Atom">'
    "<id>https://arxiv.org/api/abc</id>"
    "<title>arXiv Query</title>"
    "<updated>2026-08-26T03:42:13Z</updated>"
    "<opensearch:totalResults>{count}</opensearch:totalResults>"
    "{entries}"
    "</feed>"
)


def _entry_inner(
    id_url="http://arxiv.org/abs/2606.29914v1",
    title="A Paper Title",
    summary=None,
    published="2026-06-29T07:51:22Z",
    updated="2026-06-29T07:51:22Z",
    include_id=True,
    include_title=True,
):
    summary = "x" * 300 if summary is None else summary
    parts = []
    if include_id:
        parts.append(f"<id>{id_url}</id>")
    if include_title:
        parts.append(f"<title>{title}</title>")
    if updated is not None:
        parts.append(f"<updated>{updated}</updated>")
    if published is not None:
        parts.append(f"<published>{published}</published>")
    parts.append(f"<summary>{summary}</summary>")
    parts.append("<author><name>Someone</name></author>")
    return "".join(parts)


def _entry_xml(**kw):
    """<entry> for embedding inside a <feed> that already declares the atom
    default namespace, matching the real export.arxiv.org response shape."""
    return "<entry>" + _entry_inner(**kw) + "</entry>"


def _entry_el(**kw):
    """A standalone, parsed <entry> Element for unit-testing entry_to_candidate
    in isolation, without a wrapping <feed>."""
    ns = bs.ATOM_NS.strip("{}")
    xml = f'<entry xmlns="{ns}">' + _entry_inner(**kw) + "</entry>"
    return ET.fromstring(xml)


def _feed(entry_xmls, count=None):
    count = len(entry_xmls) if count is None else count
    return FEED_TEMPLATE.format(count=count, entries="".join(entry_xmls))


# --- lane 1: gh_node_to_candidate ------------------------------------------------


class GhNodeToCandidateTests(unittest.TestCase):
    def test_maps_fields_into_candidate_schema(self):
        cand = bs.gh_node_to_candidate(
            _issue_node(), "memory-fidelity", "how do you test agent memory"
        )
        self.assertEqual(cand["url"], "https://github.com/acme/widgets/issues/7")
        self.assertEqual(cand["repo"], "acme/widgets")
        self.assertEqual(cand["stars"], 900)
        self.assertEqual(cand["comments"], 0)
        self.assertIs(cand["is_answered"], False)
        self.assertEqual(cand["author"], "someone")
        self.assertEqual(cand["property_group"], "memory-fidelity")
        self.assertEqual(cand["pattern"], "memory-fidelity")
        self.assertEqual(cand["matched_phrase"], "how do you test agent memory")
        self.assertEqual(cand["lane"], "gh")

    def test_snippet_is_newline_flattened_and_truncated(self):
        long_body = "x" * 1000
        cand = bs.gh_node_to_candidate(_issue_node(bodyText=long_body), "p", "q")
        self.assertEqual(len(cand["snippet"]), 500)
        cand2 = bs.gh_node_to_candidate(_issue_node(), "p", "q")
        self.assertNotIn("\n", cand2["snippet"])
        self.assertNotIn("\r", cand2["snippet"])

    def test_missing_url_returns_none(self):
        self.assertIsNone(bs.gh_node_to_candidate({"title": "no url"}, "p", "q"))
        self.assertIsNone(bs.gh_node_to_candidate(None, "p", "q"))

    def test_missing_repo_and_author_default_safely(self):
        cand = bs.gh_node_to_candidate(
            {"url": "https://x/1", "repository": None, "author": None}, "p", "q"
        )
        self.assertEqual(cand["repo"], "")
        self.assertEqual(cand["author"], "")
        self.assertEqual(cand["stars"], 0)


# --- config -----------------------------------------------------------------


class LoadConfigTests(unittest.TestCase):
    def _write(self, tmp, cfg):
        p = Path(tmp) / "config.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")
        return str(p)

    def _valid(self):
        return {
            "subject": {"name": "proj", "url": "https://x"},
            "property_groups": {"memory-fidelity": ["agent memory benchmark"]},
        }

    def test_valid_config_gets_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = bs.load_config(self._write(tmp, self._valid()))
            self.assertEqual(cfg["own_repos"], [])
            self.assertEqual(cfg["min_repo_stars"], bs.DEFAULTS["min_repo_stars"])
            self.assertEqual(cfg["arxiv_phrases"], {})
            self.assertEqual(cfg["state_dir"], "state")

    def test_missing_subject_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = self._valid()
            del bad["subject"]
            with self.assertRaises(SystemExit):
                bs.load_config(self._write(tmp, bad))

    def test_missing_property_groups_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = self._valid()
            del bad["property_groups"]
            with self.assertRaises(SystemExit):
                bs.load_config(self._write(tmp, bad))

    def test_subject_wrong_type_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = self._valid()
            bad["subject"] = "not an object"
            with self.assertRaises(SystemExit):
                bs.load_config(self._write(tmp, bad))

    def test_property_groups_empty_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = self._valid()
            bad["property_groups"] = {}
            with self.assertRaises(SystemExit):
                bs.load_config(self._write(tmp, bad))

    def test_property_group_with_empty_phrases_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = self._valid()
            bad["property_groups"] = {"memory-fidelity": []}
            with self.assertRaises(SystemExit):
                bs.load_config(self._write(tmp, bad))

    def test_property_group_not_a_list_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = self._valid()
            bad["property_groups"] = {"memory-fidelity": "not a list"}
            with self.assertRaises(SystemExit):
                bs.load_config(self._write(tmp, bad))

    def test_own_repos_wrong_type_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = self._valid()
            bad["own_repos"] = "not a list"
            with self.assertRaises(SystemExit):
                bs.load_config(self._write(tmp, bad))

    def test_arxiv_phrases_wrong_type_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = self._valid()
            bad["arxiv_phrases"] = "not an object"
            with self.assertRaises(SystemExit):
                bs.load_config(self._write(tmp, bad))

    def test_arxiv_phrases_group_not_a_list_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = self._valid()
            bad["arxiv_phrases"] = {"memory-fidelity": "not a list"}
            with self.assertRaises(SystemExit):
                bs.load_config(self._write(tmp, bad))

    def test_arxiv_phrases_group_with_non_string_items_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = self._valid()
            bad["arxiv_phrases"] = {"memory-fidelity": [1, 2]}
            with self.assertRaises(SystemExit):
                bs.load_config(self._write(tmp, bad))

    def test_arxiv_phrases_empty_dict_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._valid()
            cfg["arxiv_phrases"] = {}
            loaded = bs.load_config(self._write(tmp, cfg))
            self.assertEqual(loaded["arxiv_phrases"], {})

    def test_missing_config_file_exits(self):
        with self.assertRaises(SystemExit):
            bs.load_config("does/not/exist.json")

    def test_invalid_json_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.json"
            p.write_text("{not json", encoding="utf-8")
            with self.assertRaises(SystemExit):
                bs.load_config(str(p))


class StatePathsTests(unittest.TestCase):
    def test_state_paths_derive_from_state_dir(self):
        sdir, state_file = bs.state_paths({"state_dir": "st"})
        self.assertEqual(Path(state_file).name, "benchmark_sweep_state.json")
        self.assertEqual(Path(sdir).name, "st")

    def test_state_paths_are_module_anchored_not_cwd(self):
        own = Path(bs.__file__).resolve().parent
        sdir, state_file = bs.state_paths(dict(bs.DEFAULTS))
        self.assertEqual(Path(sdir), own / "state")
        self.assertEqual(Path(state_file), own / "state" / "benchmark_sweep_state.json")


# --- lane 1: gh_search_lane ---------------------------------------------------


class GhSearchLaneTests(unittest.TestCase):
    def test_inclusive_date_boundary_and_own_repo_exclusion(self):
        captured = []

        def fake_graphql(query, **variables):
            captured.append(variables)
            return {"data": {"search": {"nodes": []}}}, None

        cfg = {
            "property_groups": {"memory-fidelity": ["agent memory benchmark"]},
            "own_repos": ["me/proj"],
            "per_query": 5,
        }
        with mock.patch.object(bs, "gh_graphql", side_effect=fake_graphql):
            bs.gh_search_lane(cfg, "2026-07-01", [])
        self.assertTrue(captured)
        for variables in captured:
            self.assertIn("created:>=2026-07-01", variables["q"])
            self.assertNotIn("created:>2026-07-01", variables["q"])
            self.assertIn("-repo:me/proj", variables["q"])

    def test_issue_kind_gets_open_filter_discussion_does_not(self):
        queries_by_kind = {}

        def fake_graphql(query, **variables):
            kind = "ISSUE" if "type:ISSUE" in query else "DISCUSSION"
            queries_by_kind[kind] = variables["q"]
            return {"data": {"search": {"nodes": []}}}, None

        cfg = {
            "property_groups": {"memory-fidelity": ["agent memory benchmark"]},
            "own_repos": [],
            "per_query": 5,
        }
        with mock.patch.object(bs, "gh_graphql", side_effect=fake_graphql):
            bs.gh_search_lane(cfg, "2026-07-01", [])
        self.assertIn("is:open is:issue", queries_by_kind["ISSUE"])
        self.assertNotIn("is:open is:issue", queries_by_kind["DISCUSSION"])

    def test_no_own_repos_produces_no_dash_repo_token(self):
        captured = []

        def fake_graphql(query, **variables):
            captured.append(variables["q"])
            return {"data": {"search": {"nodes": []}}}, None

        cfg = {"property_groups": {"p": ["x"]}, "own_repos": [], "per_query": 5}
        with mock.patch.object(bs, "gh_graphql", side_effect=fake_graphql):
            bs.gh_search_lane(cfg, "2026-07-01", [])
        for q in captured:
            self.assertNotIn("-repo:", q)

    def test_results_are_tagged_with_property_group_and_matched_phrase(self):
        def fake_graphql(query, **variables):
            return {"data": {"search": {"nodes": [_issue_node()]}}}, None

        cfg = {
            "property_groups": {"memory-fidelity": ["agent memory benchmark"]},
            "own_repos": [],
            "per_query": 5,
        }
        with mock.patch.object(bs, "gh_graphql", side_effect=fake_graphql):
            results = bs.gh_search_lane(cfg, "2026-07-01", [])
        # ISSUE + DISCUSSION both return the same fixture node -> 2 raw results.
        self.assertEqual(len(results), 2)
        for cand in results:
            self.assertEqual(cand["property_group"], "memory-fidelity")
            self.assertEqual(cand["matched_phrase"], "agent memory benchmark")
            self.assertEqual(cand["pattern"], "memory-fidelity")

    def test_error_is_recorded_with_property_and_kind_context(self):
        def fake_graphql(query, **variables):
            return None, "HTTP 500 boom"

        cfg = {
            "property_groups": {"memory-fidelity": ["x"]},
            "own_repos": [],
            "per_query": 5,
        }
        errors = []
        with mock.patch.object(bs, "gh_graphql", side_effect=fake_graphql):
            bs.gh_search_lane(cfg, "2026-07-01", errors)
        self.assertTrue(any("memory-fidelity/ISSUE" in e for e in errors))
        self.assertTrue(any("memory-fidelity/DISCUSSION" in e for e in errors))

    def test_note_fetch_ok_called_via_lanereport(self):
        def fake_graphql(query, **variables):
            return {"data": {"search": {"nodes": []}}}, None

        cfg = {"property_groups": {"p": ["x"]}, "own_repos": [], "per_query": 5}
        report = bs.LaneReport()
        with mock.patch.object(bs, "gh_graphql", side_effect=fake_graphql):
            bs.gh_search_lane(cfg, "2026-07-01", report)
        self.assertEqual(report.fetches_ok, 2)  # ISSUE + DISCUSSION


# --- lane 2: arXiv URL + window + parsing --------------------------------------


class ArxivUrlTests(unittest.TestCase):
    def test_confirmed_live_encoding_shape(self):
        # Confirmed live against export.arxiv.org during build (200, correct
        # single-result response): urlencode's default quote_plus produces this
        # exact shape and the API accepts it.
        url = bs._arxiv_url("agent memory evaluation", 3)
        self.assertTrue(url.startswith(bs.ARXIV_API + "?"))
        self.assertIn("search_query=all%3A%22agent+memory+evaluation%22", url)
        self.assertIn("sortBy=submittedDate", url)
        self.assertIn("sortOrder=descending", url)
        self.assertIn("max_results=3", url)

    def test_max_results_reflects_the_argument(self):
        url = bs._arxiv_url("x", 25)
        self.assertIn("max_results=25", url)

    def test_phrase_round_trips_through_the_query_string(self):
        url = bs._arxiv_url("agent memory evaluation", 5)
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(qs["search_query"][0], 'all:"agent memory evaluation"')


class ArxivWithinWindowTests(unittest.TestCase):
    SINCE = datetime(2026, 7, 1, tzinfo=timezone.utc)

    def test_after_since_is_within(self):
        self.assertTrue(bs._arxiv_within_window("2026-07-15T00:00:00Z", self.SINCE))

    def test_before_since_is_not_within(self):
        self.assertFalse(bs._arxiv_within_window("2026-06-01T00:00:00Z", self.SINCE))

    def test_exactly_at_since_is_within(self):
        self.assertTrue(bs._arxiv_within_window("2026-07-01T00:00:00Z", self.SINCE))

    def test_missing_date_fails_open(self):
        self.assertTrue(bs._arxiv_within_window("", self.SINCE))
        self.assertTrue(bs._arxiv_within_window(None, self.SINCE))

    def test_unparseable_date_fails_open(self):
        self.assertTrue(bs._arxiv_within_window("not-a-date", self.SINCE))


class EntryToCandidateTests(unittest.TestCase):
    def test_maps_fields(self):
        el = _entry_el(
            title="  MemDelta:\n  a paper  ", summary="Abstract " + "y" * 300
        )
        cand = bs.entry_to_candidate(el, "memory-fidelity", "agent memory evaluation")
        self.assertEqual(cand["url"], "http://arxiv.org/abs/2606.29914v1")
        self.assertEqual(cand["title"], "MemDelta: a paper")  # whitespace collapsed
        self.assertEqual(cand["created"], "2026-06-29T07:51:22Z")
        self.assertEqual(len(cand["snippet"]), 200)
        self.assertEqual(cand["property_group"], "memory-fidelity")
        self.assertEqual(cand["matched_phrase"], "agent memory evaluation")
        self.assertEqual(cand["pattern"], "memory-fidelity")
        self.assertEqual(cand["lane"], "arxiv")

    def test_missing_id_returns_none(self):
        el = _entry_el(include_id=False)
        self.assertIsNone(bs.entry_to_candidate(el, "p", "q"))

    def test_missing_title_defaults_to_empty(self):
        el = _entry_el(include_title=False)
        cand = bs.entry_to_candidate(el, "p", "q")
        self.assertEqual(cand["title"], "")

    def test_created_falls_back_to_updated_when_published_missing(self):
        el = _entry_el(published=None, updated="2026-05-01T00:00:00Z")
        cand = bs.entry_to_candidate(el, "p", "q")
        self.assertEqual(cand["created"], "2026-05-01T00:00:00Z")


class ParseArxivFeedTests(unittest.TestCase):
    SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def test_valid_multi_entry_feed_parses_all(self):
        body = _feed(
            [
                _entry_xml(id_url="http://arxiv.org/abs/1"),
                _entry_xml(id_url="http://arxiv.org/abs/2"),
            ]
        )
        cands = bs.parse_arxiv_feed(
            body, "memory-fidelity", "agent memory eval", self.SINCE
        )
        self.assertEqual(
            [c["url"] for c in cands],
            ["http://arxiv.org/abs/1", "http://arxiv.org/abs/2"],
        )
        for c in cands:
            self.assertEqual(c["property_group"], "memory-fidelity")

    def test_entry_missing_id_is_skipped_not_crashed(self):
        body = _feed(
            [
                _entry_xml(include_id=False),
                _entry_xml(id_url="http://arxiv.org/abs/2"),
            ]
        )
        cands = bs.parse_arxiv_feed(body, "p", "q", self.SINCE)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["url"], "http://arxiv.org/abs/2")

    def test_entry_outside_window_is_dropped(self):
        body = _feed(
            [
                _entry_xml(
                    published="2025-01-01T00:00:00Z", updated="2025-01-01T00:00:00Z"
                )
            ]
        )
        cands = bs.parse_arxiv_feed(body, "p", "q", self.SINCE)
        self.assertEqual(cands, [])

    def test_entry_at_or_after_window_is_kept(self):
        body = _feed(
            [
                _entry_xml(
                    published="2026-06-01T00:00:00Z", updated="2026-06-01T00:00:00Z"
                )
            ]
        )
        cands = bs.parse_arxiv_feed(body, "p", "q", self.SINCE)
        self.assertEqual(len(cands), 1)

    def test_malformed_xml_raises_parse_error(self):
        with self.assertRaises(ET.ParseError):
            bs.parse_arxiv_feed("not xml at all <<<", "p", "q", self.SINCE)

    def test_empty_feed_parses_to_no_candidates(self):
        self.assertEqual(bs.parse_arxiv_feed(_feed([]), "p", "q", self.SINCE), [])

    def test_real_captured_payload_shape_parses(self):
        # A trimmed capture of the actual export.arxiv.org response shape
        # (confirmed live during build), including the extra arxiv:/opensearch:
        # namespaced siblings a real response carries alongside the atom ones.
        body = (
            "<?xml version='1.0' encoding='UTF-8'?>\n"
            '<feed xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" '
            'xmlns:arxiv="http://arxiv.org/schemas/atom" '
            'xmlns="http://www.w3.org/2005/Atom">\n'
            "  <entry>\n"
            "    <id>http://arxiv.org/abs/2606.29914v1</id>\n"
            "    <title>MemDelta: Controlled Baselines and Hidden Confounds in"
            " Agent Memory Evaluation</title>\n"
            "    <updated>2026-06-29T07:51:22Z</updated>\n"
            '    <link href="https://arxiv.org/abs/2606.29914v1" rel="alternate"'
            ' type="text/html"/>\n'
            "    <summary>Agent memory systems are increasingly evaluated"
            " against RAG baselines.</summary>\n"
            '    <category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>\n'
            "    <published>2026-06-29T07:51:22Z</published>\n"
            "    <arxiv:comment>13 pages, 2 figures</arxiv:comment>\n"
            '    <arxiv:primary_category term="cs.CL"/>\n'
            "    <author>\n      <name>Kuan Wang</name>\n    </author>\n"
            "  </entry>\n"
            "</feed>"
        )
        cands = bs.parse_arxiv_feed(
            body, "memory-fidelity", "agent memory eval", self.SINCE
        )
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["url"], "http://arxiv.org/abs/2606.29914v1")
        self.assertIn("MemDelta", cands[0]["title"])


class ArxivLaneTests(unittest.TestCase):
    SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def _cfg(self, **over):
        cfg = {
            "arxiv_phrases": {"memory-fidelity": ["agent memory evaluation"]},
            "arxiv_max_per_phrase": 10,
        }
        cfg.update(over)
        return cfg

    def setUp(self):
        # Every test in this class controls its own delay explicitly; reset the
        # module global afterwards so no test leaks a nonzero delay into another.
        self.addCleanup(setattr, bs, "ARXIV_REQUEST_DELAY", 0.0)

    def test_success_returns_candidates_and_counts_fetch_ok(self):
        body = _feed([_entry_xml()])

        def fake_http_get(url, **kw):
            return 200, body, None

        report = bs.LaneReport()
        with mock.patch.object(bs, "http_get", side_effect=fake_http_get):
            results = bs.arxiv_lane(self._cfg(), self.SINCE, report)
        self.assertEqual(len(results), 1)
        self.assertEqual(report.fetches_ok, 1)
        self.assertTrue(report.clean)

    def test_http_error_is_soft_and_recorded(self):
        def fake_http_get(url, **kw):
            return None, "", "connection refused"

        report = bs.LaneReport()
        with mock.patch.object(bs, "http_get", side_effect=fake_http_get):
            results = bs.arxiv_lane(self._cfg(), self.SINCE, report)
        self.assertEqual(results, [])
        self.assertFalse(report.clean)
        self.assertTrue(any("connection refused" in e for e in report))

    def test_non_200_status_is_soft_and_recorded(self):
        def fake_http_get(url, **kw):
            return 503, "", None

        report = bs.LaneReport()
        with mock.patch.object(bs, "http_get", side_effect=fake_http_get):
            results = bs.arxiv_lane(self._cfg(), self.SINCE, report)
        self.assertEqual(results, [])
        self.assertTrue(any("HTTP 503" in e for e in report))

    def test_malformed_xml_is_soft_not_a_crash(self):
        def fake_http_get(url, **kw):
            return 200, "not xml <<<", None

        report = bs.LaneReport()
        with mock.patch.object(bs, "http_get", side_effect=fake_http_get):
            results = bs.arxiv_lane(self._cfg(), self.SINCE, report)
        self.assertEqual(results, [])
        self.assertFalse(report.clean)
        self.assertTrue(any("bad xml" in e for e in report))

    def test_no_phrases_configured_makes_no_request_and_holds(self):
        report = bs.LaneReport()
        with mock.patch.object(bs, "http_get") as mocked:
            results = bs.arxiv_lane(self._cfg(arxiv_phrases={}), self.SINCE, report)
        mocked.assert_not_called()
        self.assertEqual(results, [])
        self.assertFalse(report.clean)

    def test_candidates_tagged_with_property_group_and_phrase(self):
        body = _feed([_entry_xml()])

        def fake_http_get(url, **kw):
            return 200, body, None

        with mock.patch.object(bs, "http_get", side_effect=fake_http_get):
            results = bs.arxiv_lane(self._cfg(), self.SINCE, bs.LaneReport())
        self.assertEqual(results[0]["property_group"], "memory-fidelity")
        self.assertEqual(results[0]["matched_phrase"], "agent memory evaluation")

    def test_request_delay_is_honoured_when_configured(self):
        body = _feed([_entry_xml()])

        def fake_http_get(url, **kw):
            return 200, body, None

        cfg = self._cfg(arxiv_phrases={"a": ["x", "y"]})
        bs.ARXIV_REQUEST_DELAY = 3.0
        with (
            mock.patch.object(bs, "http_get", side_effect=fake_http_get),
            mock.patch.object(bs.time, "sleep") as slept,
        ):
            bs.arxiv_lane(cfg, self.SINCE, bs.LaneReport())
        self.assertEqual(slept.call_count, 2)

    def test_zero_delay_does_not_sleep(self):
        body = _feed([_entry_xml()])

        def fake_http_get(url, **kw):
            return 200, body, None

        bs.ARXIV_REQUEST_DELAY = 0.0
        with (
            mock.patch.object(bs, "http_get", side_effect=fake_http_get),
            mock.patch.object(bs.time, "sleep") as slept,
        ):
            bs.arxiv_lane(self._cfg(), self.SINCE, bs.LaneReport())
        slept.assert_not_called()


# --- filtering + capping ------------------------------------------------------


class FilterCandidatesTests(unittest.TestCase):
    def _cfg(self, **over):
        cfg = {"own_repos": ["me/proj"], "min_repo_stars": 300}
        cfg.update(over)
        return cfg

    def _gh_cand(self, **over):
        cand = {
            "url": "https://github.com/acme/widgets/issues/1",
            "repo": "acme/widgets",
            "stars": 800,
            "lane": "gh",
            "property_group": "memory-fidelity",
        }
        cand.update(over)
        return cand

    def _arxiv_cand(self, **over):
        cand = {
            "url": "http://arxiv.org/abs/1",
            "lane": "arxiv",
            "property_group": "memory-fidelity",
        }
        cand.update(over)
        return cand

    def test_each_gh_drop_reason_is_counted(self):
        raw = [
            self._gh_cand(),  # kept
            self._gh_cand(),  # same url again -> dup
            self._gh_cand(url="https://x/seen"),  # in seen store
            self._gh_cand(url="https://x/own", repo="me/proj"),  # own repo
            self._gh_cand(url="https://x/low", stars=10),  # under star floor
        ]
        kept, dropped = bs.filter_candidates(
            raw, {"https://x/seen": "2026-01-01"}, self._cfg()
        )
        self.assertEqual([c["url"] for c in kept], [raw[0]["url"]])
        self.assertEqual(dropped, {"seen": 1, "stars": 1, "own": 1, "dup": 1})

    def test_own_repos_match_is_case_insensitive(self):
        raw = [self._gh_cand(repo="ME/PROJ")]
        kept, dropped = bs.filter_candidates(raw, {}, self._cfg())
        self.assertEqual(kept, [])
        self.assertEqual(dropped["own"], 1)

    def test_arxiv_candidates_bypass_own_repos_and_star_floor(self):
        raw = [self._arxiv_cand()]
        kept, dropped = bs.filter_candidates(raw, {}, self._cfg())
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped["own"], 0)
        self.assertEqual(dropped["stars"], 0)

    def test_arxiv_candidates_still_hit_dup_and_seen(self):
        raw = [self._arxiv_cand(), self._arxiv_cand()]
        kept, dropped = bs.filter_candidates(raw, {}, self._cfg())
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped["dup"], 1)

    def test_pure_no_input_mutation(self):
        seen = {"https://x/seen": "2026-01-01"}
        bs.filter_candidates([self._gh_cand()], seen, self._cfg())
        self.assertEqual(seen, {"https://x/seen": "2026-01-01"})


class CapPerRepoTests(unittest.TestCase):
    def test_overflow_within_one_group_is_capped_and_counted(self):
        kept = [
            {
                "repo": "acme/widgets",
                "property_group": "memory-fidelity",
                "url": f"https://x/{i}",
            }
            for i in range(3)
        ] + [
            {
                "repo": "other/repo",
                "property_group": "memory-fidelity",
                "url": "https://x/o",
            }
        ]
        dropped = {}
        capped = bs.cap_per_repo(kept, 2, dropped)
        self.assertEqual(len(capped), 3)  # 2 from acme + 1 from other
        self.assertEqual(dropped["repo_cap"], 1)

    def test_cap_is_scoped_per_property_group_not_bare_repo(self):
        kept = [
            {
                "repo": "acme/widgets",
                "property_group": "memory-fidelity",
                "url": "https://x/1",
            },
            {
                "repo": "acme/widgets",
                "property_group": "memory-fidelity",
                "url": "https://x/2",
            },
            # A different property, same repo: gets its own budget rather than
            # being crowded out by memory-fidelity's cap.
            {
                "repo": "acme/widgets",
                "property_group": "provenance-integrity",
                "url": "https://x/3",
            },
        ]
        dropped = {}
        capped = bs.cap_per_repo(kept, 1, dropped)
        self.assertEqual({c["url"] for c in capped}, {"https://x/1", "https://x/3"})
        self.assertEqual(dropped["repo_cap"], 1)

    def test_arxiv_candidates_without_repo_bypass_the_cap(self):
        kept = [
            {"property_group": "memory-fidelity", "url": f"https://arxiv/{i}"}
            for i in range(10)
        ]
        dropped = {}
        capped = bs.cap_per_repo(kept, 1, dropped)
        self.assertEqual(len(capped), 10)
        self.assertEqual(dropped.get("repo_cap", 0), 0)


# --- per-lane window markers ---------------------------------------------------


class SinceEarnedForLaneTests(unittest.TestCase):
    NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

    def test_since_prefers_the_days_override(self):
        since = bs._since_for_lane(
            "gh",
            {"gh": "2026-07-01T00:00:00+00:00"},
            {"default_window_days": 14},
            self.NOW,
            3,
        )
        self.assertEqual(since, self.NOW - timedelta(days=3))

    def test_since_uses_the_lanes_own_stored_marker(self):
        since = bs._since_for_lane(
            "gh",
            {"gh": "2026-07-01T00:00:00+00:00", "arxiv": "2026-01-01T00:00:00+00:00"},
            {"default_window_days": 14},
            self.NOW,
            None,
        )
        self.assertEqual(since, datetime(2026, 7, 1, tzinfo=timezone.utc))

    def test_since_falls_back_to_default_window_on_first_run(self):
        since = bs._since_for_lane(
            "arxiv", {}, {"default_window_days": 14}, self.NOW, None
        )
        self.assertEqual(since, self.NOW - timedelta(days=14))

    def test_earned_stamp_advances_when_window_reached_the_marker(self):
        prior = {"gh": "2026-07-25T00:00:00+00:00"}
        since_by_lane = {"gh": datetime(2026, 7, 25, tzinfo=timezone.utc)}
        self.assertEqual(
            bs._earned_stamp_for_lane("gh", prior, since_by_lane, self.NOW),
            self.NOW.isoformat(),
        )

    def test_earned_stamp_holds_when_window_narrower_than_the_marker(self):
        prior = {"gh": "2026-07-01T00:00:00+00:00"}
        since_by_lane = {"gh": datetime(2026, 7, 30, tzinfo=timezone.utc)}
        self.assertEqual(
            bs._earned_stamp_for_lane("gh", prior, since_by_lane, self.NOW), prior["gh"]
        )

    def test_earned_stamp_is_independent_per_lane(self):
        prior = {
            "gh": "2026-07-01T00:00:00+00:00",
            "arxiv": "2026-07-20T00:00:00+00:00",
        }
        since_by_lane = {
            "gh": datetime(2026, 7, 1, tzinfo=timezone.utc),
            "arxiv": datetime(
                2026, 7, 30, tzinfo=timezone.utc
            ),  # narrower than its own marker
        }
        self.assertEqual(
            bs._earned_stamp_for_lane("gh", prior, since_by_lane, self.NOW),
            self.NOW.isoformat(),
        )
        self.assertEqual(
            bs._earned_stamp_for_lane("arxiv", prior, since_by_lane, self.NOW),
            prior["arxiv"],
        )


# --- full cmd_scan: per-lane earn/hold independence ----------------------------


class EarnedWindowIntegrationTests(unittest.TestCase):
    """The two-lane analogue of thread_sweep's EarnedWindowTests: a GitHub
    outage must not freeze the arXiv marker, and vice versa, because the two
    APIs fail on unrelated schedules."""

    OLD = "2026-07-01T00:00:00+00:00"

    def _cfg(self, tmp, **over):
        cfg = {
            "subject": {"name": "x", "url": "https://x"},
            "property_groups": {"memory-fidelity": ["agent memory benchmark"]},
            "arxiv_phrases": {"memory-fidelity": ["agent memory evaluation"]},
            "state_dir": str(Path(tmp) / "state"),
            "candidates_file": str(Path(tmp) / "candidates.json"),
            "arxiv_request_delay_seconds": 0.0,
        }
        cfg.update(over)
        return cfg

    def _setup(self, tmp, state_obj=None, **cfg_over):
        cfg = self._cfg(tmp, **cfg_over)
        cfg_path = Path(tmp) / "config.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        state_file = Path(cfg["state_dir"]) / "benchmark_sweep_state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        if state_obj is not None:
            state_file.write_text(json.dumps(state_obj), encoding="utf-8")
        return cfg_path, state_file

    def _scan(self, cfg_path, gh_side_effect, http_side_effect, days=None):
        args = argparse.Namespace(config=str(cfg_path), days=days, dry_run=False)
        err = io.StringIO()
        with (
            mock.patch.object(bs, "gh_graphql", side_effect=gh_side_effect),
            mock.patch.object(bs, "http_get", side_effect=http_side_effect),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(err),
        ):
            bs.cmd_scan(args)
        return err.getvalue()

    @staticmethod
    def _markers(state_file):
        if not state_file.exists():
            return {}
        return json.loads(state_file.read_text(encoding="utf-8")).get(
            "last_run_by_lane", {}
        )

    @staticmethod
    def _digest(cfg_path):
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        return json.loads(Path(cfg["candidates_file"]).read_text(encoding="utf-8"))

    @staticmethod
    def _ok_gh(query, **kw):
        return {"data": {"search": {"nodes": []}}}, None

    @staticmethod
    def _boom_gh(query, **kw):
        return None, "HTTP 500 boom"

    @staticmethod
    def _ok_http(url, **kw):
        return 200, _feed([]), None

    @staticmethod
    def _boom_http(url, **kw):
        return None, "", "connection refused"

    def test_gh_failure_holds_gh_but_arxiv_still_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(
                tmp,
                {"last_run_by_lane": {"gh": self.OLD, "arxiv": self.OLD}, "seen": {}},
            )
            self._scan(cfg_path, self._boom_gh, self._ok_http)
            markers = self._markers(state_file)
            self.assertEqual(markers["gh"], self.OLD)
            self.assertNotEqual(markers["arxiv"], self.OLD)

    def test_arxiv_failure_holds_arxiv_but_gh_still_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(
                tmp,
                {"last_run_by_lane": {"gh": self.OLD, "arxiv": self.OLD}, "seen": {}},
            )
            self._scan(cfg_path, self._ok_gh, self._boom_http)
            markers = self._markers(state_file)
            self.assertNotEqual(markers["gh"], self.OLD)
            self.assertEqual(markers["arxiv"], self.OLD)

    def test_both_clean_empty_scans_advance_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(
                tmp,
                {"last_run_by_lane": {"gh": self.OLD, "arxiv": self.OLD}, "seen": {}},
            )
            self._scan(cfg_path, self._ok_gh, self._ok_http)
            markers = self._markers(state_file)
            self.assertNotEqual(markers["gh"], self.OLD)
            self.assertNotEqual(markers["arxiv"], self.OLD)

    def test_both_failing_holds_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(
                tmp,
                {"last_run_by_lane": {"gh": self.OLD, "arxiv": self.OLD}, "seen": {}},
            )
            self._scan(cfg_path, self._boom_gh, self._boom_http)
            markers = self._markers(state_file)
            self.assertEqual(markers["gh"], self.OLD)
            self.assertEqual(markers["arxiv"], self.OLD)

    def test_first_run_with_no_prior_marker_lays_one_down_per_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(tmp, {"seen": {}})
            self._scan(cfg_path, self._ok_gh, self._ok_http)
            markers = self._markers(state_file)
            now = datetime.now(timezone.utc)
            for name in ("gh", "arxiv"):
                age = now - datetime.fromisoformat(markers[name])
                self.assertAlmostEqual(age.total_seconds(), 0, delta=120)

    def test_failed_run_with_no_prior_marker_invents_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(tmp, {"seen": {}})
            self._scan(cfg_path, self._boom_gh, self._boom_http)
            markers = self._markers(state_file)
            self.assertFalse(markers.get("gh"))
            self.assertFalse(markers.get("arxiv"))

    def test_narrow_days_override_does_not_swallow_the_uncovered_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            cfg_path, state_file = self._setup(
                tmp, {"last_run_by_lane": {"gh": old, "arxiv": old}, "seen": {}}
            )
            self._scan(cfg_path, self._ok_gh, self._ok_http, days=2)
            markers = self._markers(state_file)
            self.assertEqual(markers["gh"], old)
            self.assertEqual(markers["arxiv"], old)

    def test_wide_days_override_covers_the_marker_and_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
            cfg_path, state_file = self._setup(
                tmp, {"last_run_by_lane": {"gh": old, "arxiv": old}, "seen": {}}
            )
            self._scan(cfg_path, self._ok_gh, self._ok_http, days=30)
            markers = self._markers(state_file)
            self.assertNotEqual(markers["gh"], old)
            self.assertNotEqual(markers["arxiv"], old)

    def test_unreadable_marker_warns_and_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(
                tmp,
                {
                    "last_run_by_lane": {"gh": "last tuesday", "arxiv": self.OLD},
                    "seen": {},
                },
            )
            err = self._scan(cfg_path, self._ok_gh, self._ok_http)
            self.assertIn("unreadable last_run for gh", err)
            markers = self._markers(state_file)
            # A rotted marker is kept verbatim even on an otherwise-clean run:
            # the re-windowed run cannot prove it reached back to whatever the
            # rotted marker meant, so overwriting it would both swallow that
            # gap and erase the evidence that the marker needs a human to look
            # at it. Matches sweepcore.earned_stamp's documented contract
            # exactly (see thread_sweep's identical test for this rule).
            self.assertEqual(markers["gh"], "last tuesday")
            # arxiv was never touched by gh's rotted marker: it still advances.
            self.assertNotEqual(markers["arxiv"], self.OLD)

    def test_held_lane_is_reported_on_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(
                tmp,
                {"last_run_by_lane": {"gh": self.OLD, "arxiv": self.OLD}, "seen": {}},
            )
            err = self._scan(cfg_path, self._boom_gh, self._ok_http)
            self.assertIn("keeping their last_run", err)

    def test_held_window_is_reported_in_the_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(
                tmp,
                {"last_run_by_lane": {"gh": self.OLD, "arxiv": self.OLD}, "seen": {}},
            )
            self._scan(cfg_path, self._boom_gh, self._ok_http)
            self.assertEqual(self._digest(cfg_path)["window_held"], ["gh"])

    def test_clean_run_reports_no_held_lanes(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, state_file = self._setup(
                tmp,
                {"last_run_by_lane": {"gh": self.OLD, "arxiv": self.OLD}, "seen": {}},
            )
            self._scan(cfg_path, self._ok_gh, self._ok_http)
            self.assertEqual(self._digest(cfg_path)["window_held"], [])


# --- dry-run -------------------------------------------------------------------


class DryRunWritesNothingTests(unittest.TestCase):
    """A --dry-run scan must be side-effect-free on disk. candidates.json is
    the human's working digest, the file they may be mid-way through
    triaging, so a preview that silently overwrites it destroys the run it
    was meant to preview."""

    SENTINEL = '{"candidates": ["hand-triaged, do not clobber"]}'

    def _setup(self, tmp):
        cfg = {
            "subject": {"name": "x", "url": "https://x"},
            "property_groups": {"memory-fidelity": ["agent memory benchmark"]},
            "arxiv_phrases": {"memory-fidelity": ["agent memory evaluation"]},
            "state_dir": str(Path(tmp) / "state"),
            "candidates_file": str(Path(tmp) / "candidates.json"),
            "arxiv_request_delay_seconds": 0.0,
        }
        cfg_path = Path(tmp) / "config.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        cand_path = Path(cfg["candidates_file"])
        cand_path.write_text(self.SENTINEL, encoding="utf-8")
        state_file = Path(cfg["state_dir"]) / "benchmark_sweep_state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            json.dumps(
                {
                    "last_run_by_lane": {
                        "gh": "2026-07-01T00:00:00+00:00",
                        "arxiv": "2026-07-01T00:00:00+00:00",
                    },
                    "seen": {},
                }
            ),
            encoding="utf-8",
        )
        return cfg_path, cand_path, state_file

    @staticmethod
    def _one_gh_hit(query, **kw):
        return {"data": {"search": {"nodes": [_issue_node()]}}}, None

    @staticmethod
    def _one_arxiv_hit(url, **kw):
        # Dated relative to real now (not a fixed past date): the scan's
        # window is computed from datetime.now(), so a fixture entry with a
        # hardcoded date would silently age out of the window depending on
        # when the suite happens to run.
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        return 200, _feed([_entry_xml(published=recent, updated=recent)]), None

    def _scan(self, cfg_path, dry_run):
        args = argparse.Namespace(config=str(cfg_path), days=30, dry_run=dry_run)
        buf = io.StringIO()
        with (
            mock.patch.object(bs, "gh_graphql", side_effect=self._one_gh_hit),
            mock.patch.object(bs, "http_get", side_effect=self._one_arxiv_hit),
            contextlib.redirect_stdout(buf),
        ):
            bs.cmd_scan(args)
        return buf.getvalue()

    def test_dry_run_leaves_files_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, cand_path, state_file = self._setup(tmp)
            before, state_before = cand_path.read_bytes(), state_file.read_bytes()
            out = self._scan(cfg_path, dry_run=True)
            self.assertEqual(cand_path.read_bytes(), before)
            self.assertEqual(state_file.read_bytes(), state_before)
            # 1 gh hit (ISSUE + DISCUSSION dedup to the same url) + 1 arxiv hit:
            # the write path really was exercised, the file is intact because
            # the dry run declined to write, not because nothing was found.
            self.assertIn("kept=2", out)

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
            written = json.loads(cand_path.read_text(encoding="utf-8"))
            self.assertNotEqual(cand_path.read_text(encoding="utf-8"), self.SENTINEL)
            self.assertEqual(len(written["candidates"]), 2)
            self.assertIn(f"candidates -> {cand_path}", out)


# --- the demand tally ---------------------------------------------------------


class PerPropertyTallyTests(unittest.TestCase):
    def _cfg(self, tmp):
        return {
            "subject": {"name": "x", "url": "https://x"},
            "property_groups": {
                "memory-fidelity": ["agent memory benchmark"],
                "context-retrieval": ["benchmark rag agent workspace"],
                "provenance-integrity": ["verify agent cited source"],
                "verification-oversight": ["how do you verify agent work"],
            },
            "arxiv_phrases": {},
            "state_dir": str(Path(tmp) / "state"),
            "candidates_file": str(Path(tmp) / "candidates.json"),
            "arxiv_request_delay_seconds": 0.0,
        }

    def test_kept_counts_are_grouped_by_property_including_zero_groups(self):
        def fake_graphql(query, **variables):
            if "agent memory benchmark" in variables["q"]:
                return {
                    "data": {
                        "search": {
                            "nodes": [
                                _issue_node(url="https://github.com/a/b/issues/1")
                            ]
                        }
                    }
                }, None
            return {"data": {"search": {"nodes": []}}}, None

        def fake_http(url, **kw):
            return 200, _feed([]), None

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            cfg_path.write_text(json.dumps(self._cfg(tmp)), encoding="utf-8")
            args = argparse.Namespace(config=str(cfg_path), days=30, dry_run=False)
            buf = io.StringIO()
            with (
                mock.patch.object(bs, "gh_graphql", side_effect=fake_graphql),
                mock.patch.object(bs, "http_get", side_effect=fake_http),
                contextlib.redirect_stdout(buf),
            ):
                bs.cmd_scan(args)
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            payload = json.loads(
                Path(cfg["candidates_file"]).read_text(encoding="utf-8")
            )
        self.assertEqual(
            payload["by_property_group"],
            {
                # ISSUE + DISCUSSION both hit but dedup to the same url.
                "memory-fidelity": 1,
                "context-retrieval": 0,
                "provenance-integrity": 0,
                "verification-oversight": 0,
            },
        )
        out = buf.getvalue()
        self.assertIn("demand by property:", out)
        for prop in (
            "memory-fidelity",
            "context-retrieval",
            "provenance-integrity",
            "verification-oversight",
        ):
            self.assertIn(prop, out)


# --- discovery-only guard ------------------------------------------------------


class NoOutboundTests(unittest.TestCase):
    """v1 is discovery-only by design (see the README). There must be no code
    path that posts, drafts for posting, batch-approves, or schedules: the
    same Iron Law every outbound module in this repo enforces, made
    mechanically checkable here even though this module has no act stage yet
    to gate against."""

    def test_no_outbound_or_scheduler_token_in_source(self):
        src = Path(bs.__file__).read_text(encoding="utf-8")
        banned = [
            "issues/{",
            "addDiscussionComment",
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
            "append_ledger",
            "mark-posted",
            "mark_posted",
        ]
        for token in banned:
            self.assertNotIn(
                token,
                src,
                f"outbound/auto-post/scheduler token {token!r} must not appear",
            )

    def test_only_scan_subcommand_is_registered(self):
        src = Path(bs.__file__).read_text(encoding="utf-8")
        self.assertIn('add_parser("scan"', src)
        for banned in (
            'add_parser("mark-posted',
            'add_parser("submit',
            'add_parser("post',
            'add_parser("comment',
            'add_parser("density',
        ):
            self.assertNotIn(banned, src)


# --- CLI wiring ----------------------------------------------------------------


class DefaultConfigPathTests(unittest.TestCase):
    def test_default_config_resolves_beside_the_module(self):
        own = Path(bs.__file__).resolve().parent
        seen = {}

        def _capture(args):
            seen["config"] = args.config
            return 0

        argv = ["benchmark_sweep.py", "scan"]
        with (
            mock.patch.object(bs, "cmd_scan", _capture),
            mock.patch.object(sys, "argv", argv),
        ):
            with self.assertRaises(SystemExit) as exit_ctx:
                bs.main()
        self.assertEqual(exit_ctx.exception.code, 0)
        self.assertEqual(Path(seen["config"]), own / "config.json")

    def test_an_explicit_config_override_is_still_honoured(self):
        seen = {}

        def _capture(args):
            seen["config"] = args.config
            return 0

        argv = ["benchmark_sweep.py", "--config", "mine.json", "scan"]
        with (
            mock.patch.object(bs, "cmd_scan", _capture),
            mock.patch.object(sys, "argv", argv),
        ):
            with self.assertRaises(SystemExit):
                bs.main()
        self.assertEqual(seen["config"], "mine.json")


if __name__ == "__main__":
    unittest.main()
