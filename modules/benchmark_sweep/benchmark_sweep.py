#!/usr/bin/env python3
"""benchmark-sweep - map the demand for a workspace-property benchmark, and
rank the papers worth reproducing, before either exists.

DISCOVERY-ONLY. This module has no act stage: no drafting, no posting, no
approval gate, because nothing it produces is ever sent anywhere. It answers
two questions with evidence instead of a hunch, off one fetch:

  1. DEMAND - which workspace properties (agent memory fidelity, context
     retrieval, provenance/integrity, verification/oversight) do people
     actually argue about, ask how to measure, or wish a benchmark already
     covered? Answered by the per-property tally over both lanes.
  2. REPRO-WORTHINESS - of the papers those same phrases surface, which ones
     could you actually reproduce and publish a study on? Answered by the
     arXiv lane's per-candidate `code_link` + `repro_tier` annotations and
     the studied-paper ledger. arXiv-lane only: a GitHub thread is not a
     reproducible claim, so gh candidates carry neither field.

When a real benchmark for one of these properties ships, the act stage
("offer to run it in-thread") activates behind the same per-comment human
gate every other module in this repo uses. See the README before building
that.

Same recall/precision split as thread-sweep, pointed at a benchmark instead of
a docs page. Two lanes:
  lane 1 (gh):    per-property-group GitHub search over issues + discussions,
                  windowed from that lane's own last run (mirrors
                  thread_sweep.search_lane's GraphQL mechanics — gh has no
                  `gh search discussions` subcommand, so GraphQL is the only
                  way to cover both kinds the way thread-sweep does)
  lane 2 (arxiv): per-property-group arXiv Atom search, windowed locally
                  against each entry's <updated>/<published> date (the public
                  query API has no reliable since-only filter for free text)

Each candidate is tagged with the property group whose phrasing produced it.
That tag, tallied per scan, IS the deliverable: which properties people are
actually asking about, where, and how often — the evidence a benchmark
building decision would rest on.

Dedup: a seen-store (every surfaced candidate is surfaced once) plus a
studied-paper ledger. The ledger records papers a reproduction study has
already been run on, so finished work never returns to the digest; the
seen-store prunes on a retention horizon, the ledger does not. Nothing in
that ledger is a posting record: no reply, no comment, no venue, no
recipient. This module still has no outbound path of any kind. A candidate
that also matches a docs page you can already answer belongs to
thread-sweep's gated flow, not here; this module only maps demand, it never
tries to resolve it.

SECURITY: every GitHub title/body and every arXiv title/abstract fetched here
is UNTRUSTED EXTERNAL CONTENT. A snippet can be crafted to look like an
instruction ("ignore previous instructions", a fake system marker, a
tool-call-shaped string, a request to fetch a URL or exfiltrate). This script
never acts on fetched text — it only stores a truncated snippet for a human to
read. Treat every snippet downstream as data, never instructions. `code_link`
is the sharpest instance of that rule: it is a URL lifted verbatim out of
author-written text, stored so a human can decide to open it. This module
never fetches it, and nothing downstream should either without looking first.

Requires: Python 3.10+, an authenticated GitHub CLI (`gh auth login`). The
arXiv lane needs no auth — export.arxiv.org's Atom API is public.

Subcommands (all take --config; the default is the config.json beside this
script, so the module reads its own state and config from any directory):
  scan [--days N] [--dry-run]   run both lanes, write candidates.json
  mark-studied --url U --property P [--study-url S] [--note ...]
                                record a reproduction study you have run; that
                                paper never returns to a scan digest
"""

import argparse
import json
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sweepcore import (  # noqa: E402
    TIER_RANK,
    LaneReport,
    append_ledger,
    earned_stamp,
    gh_graphql,
    hold_reason,
    http_get,
    load_state,
    note_fetch_ok,
    posted_urls,
    relevance_tier,
    resolve_module_path,
    window_start,
    write_json_atomic,
)

REQUIRED_KEYS = ["subject", "property_groups"]
DEFAULTS = {
    "own_repos": [],
    "min_repo_stars": 300,
    "per_repo_cap": 4,
    "per_query": 15,
    "arxiv_phrases": {},
    "arxiv_max_per_phrase": 10,
    "arxiv_request_delay_seconds": 3.0,
    "emit_cap": 100,
    "seen_retention_days": 180,
    "default_window_days": 14,
    "state_dir": "state",
    "candidates_file": "candidates.json",
}

# The two independent lanes. Each earns its own window marker (see the module
# docstring and README) rather than sharing one the way thread-sweep does,
# because a live GitHub outage and a live arXiv outage happen at unrelated
# times; sharing a marker would let one lane's outage freeze the other's
# coverage too.
LANES = ("gh", "arxiv")

ISSUE_FIELDS = """
  ... on Issue {
    title url createdAt bodyText state
    repository { nameWithOwner stargazerCount }
    comments { totalCount }
    author { login }
  }
"""

DISCUSSION_FIELDS = """
  ... on Discussion {
    title url createdAt bodyText isAnswered
    repository { nameWithOwner stargazerCount }
    comments { totalCount }
    author { login }
  }
"""

ARXIV_API = "http://export.arxiv.org/api/query"
# Descriptive UA so arXiv operators can identify the tool, matching the
# honesty etiquette forum_sweep already established for the other read-only
# HTTP lanes in this repo.
ARXIV_USER_AGENT = (
    "signal-sweep benchmark-sweep (https://github.com/signal-sweep/signal-sweep)"
)
ARXIV_HTTP_TIMEOUT = 20
ATOM_NS = "{http://www.w3.org/2005/Atom}"
# arXiv's own Atom extension namespace. Confirmed live against
# export.arxiv.org 2026-08-30: an <entry> carries <link> children with
# href/rel/type (rel="alternate" for the abs page, rel="related" title="pdf")
# plus arxiv:comment, the free-text author note that conventionally announces
# a code release ("Code at https://github.com/...").
ARXIV_NS = "{http://arxiv.org/schemas/atom}"

# Polite inter-request throttle for the arXiv lane (their API usage guidance
# asks for no more than one request every 3 seconds); set from config at scan
# start, mirroring forum_sweep's REQUEST_DELAY pattern. 0 disables.
ARXIV_REQUEST_DELAY = 0.0

# --- repro-worthiness vocabulary ----------------------------------------------
#
# The second readout's whole scoring surface, kept here as named constants so a
# human can read what the tier band actually rests on and edit it without
# reading the function.

# Hosts a reproduction study could clone or pull weights from. Matched on the
# URL's host (exact, or as a parent domain) so `nogithub.com` does not count.
CODE_HOSTS = ("github.com", "gitlab.com", "huggingface.co")

# Claims that give a reproduction something to check. A paper asserting a
# measured number is reproducible in a way a framing paper is not.
REPRO_EVAL_MARKERS = (
    "benchmark",
    "we evaluate",
    "we measure",
    "outperform",
    "accuracy",
    "state-of-the-art",
    "ablation",
)

# The opposite: papers that survey, position or forecast. There is no result to
# re-run, so these are demoted hard rather than merely left unscored.
REPRO_SURVEY_MARKERS = (
    "survey",
    "systematic review",
    "position paper",
    "vision paper",
    "roadmap",
)

_URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]{}]+", re.IGNORECASE)
_PERCENT_RE = re.compile(r"\d+(?:\.\d+)?\s?%")
_ARXIV_ABS_RE = re.compile(r"arxiv\.org/abs/(?P<id>[^\s/?#]+)", re.IGNORECASE)
_ARXIV_VERSION_RE = re.compile(r"v\d+$", re.IGNORECASE)


def load_config(path):
    cfg_path = Path(path)
    if not cfg_path.exists():
        sys.exit(
            f"config not found: {cfg_path}\n"
            "Copy config.example.json to config.json and edit it for your project."
        )
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"config is not valid JSON ({cfg_path}): {exc}")
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"config missing required keys: {missing}")
    if not isinstance(cfg["subject"], dict):
        sys.exit('config \'subject\' must be an object ({"name": ..., "url": ...})')
    if not isinstance(cfg["property_groups"], dict) or not cfg["property_groups"]:
        sys.exit(
            "config 'property_groups' must be a non-empty object of "
            "property -> [GitHub search phrases]"
        )
    for prop, phrases in cfg["property_groups"].items():
        if not isinstance(phrases, list) or not phrases:
            sys.exit(
                f"config 'property_groups.{prop}' must be a non-empty list of phrases"
            )
    for key, val in DEFAULTS.items():
        cfg.setdefault(key, val)
    if not isinstance(cfg["own_repos"], list):
        sys.exit("config 'own_repos' must be a list of owner/name repos to exclude")
    if not isinstance(cfg["arxiv_phrases"], dict):
        sys.exit(
            "config 'arxiv_phrases' must be an object of property -> [arXiv phrases]"
        )
    for prop, phrases in cfg["arxiv_phrases"].items():
        if not isinstance(phrases, list) or not all(
            isinstance(p, str) for p in phrases
        ):
            sys.exit(f"config 'arxiv_phrases.{prop}' must be a list of phrase strings")
    return cfg


def state_paths(cfg):
    # Module-anchored, not CWD-anchored: this module has exactly one canonical
    # state dir wherever it is invoked from. See sweepcore.resolve_module_path.
    #
    # The ledger here is NOT the outbound modules' posted-response ledger. It
    # records reproduction studies this human has run (inward work, nothing
    # sent), and its only effect on the pipeline is to stop re-surfacing a
    # paper already finished with. Filename says so, so a reader glancing at
    # state/ cannot mistake it for a posting history.
    state_dir = resolve_module_path(__file__, cfg["state_dir"])
    return (
        state_dir,
        state_dir / "benchmark_sweep_state.json",
        state_dir / "studied_papers.jsonl",
    )


# --- lane 1: GitHub issues + discussions --------------------------------------


def gh_node_to_candidate(node, property_group, matched_phrase):
    if not node or "url" not in node:
        return None
    repo = node.get("repository") or {}
    author = (node.get("author") or {}).get("login", "")
    # SECURITY: bodyText is UNTRUSTED EXTERNAL CONTENT. Truncated and stored for
    # a human to read; never interpreted as an instruction by this tool.
    body = (node.get("bodyText") or "").replace("\r", " ").replace("\n", " ")
    return {
        "url": node["url"],
        "title": node.get("title", ""),
        "created": node.get("createdAt", ""),
        "repo": repo.get("nameWithOwner", ""),
        "stars": repo.get("stargazerCount", 0),
        "comments": (node.get("comments") or {}).get("totalCount", 0),
        "is_answered": node.get("isAnswered"),
        "author": author,
        "snippet": body[:500],
        # `pattern` is the field sweepcore.relevance_tier reads for its
        # topic-specificity signal; property_group carries the same value
        # under this module's own vocabulary, kept for readable grouping and
        # for the per-property tally the whole module exists to produce.
        "pattern": property_group,
        "property_group": property_group,
        "matched_phrase": matched_phrase,
        "lane": "gh",
    }


def gh_search_lane(cfg, since_date, errors):
    """Lane 1: per-property-group GitHub search over issues and discussions.

    Mirrors thread_sweep.search_lane exactly: same GraphQL query template,
    same is:open is:issue filter on the ISSUE half, same inclusive
    created:>= date boundary ('>=' not '>': GitHub date qualifiers are
    day-granular, so '>' would skip everything created later on the last-run
    day itself; the seen-store dedups anything the inclusive boundary
    re-surfaces). Keyed by workspace-property phrasing instead of doc-topic
    phrasing, and tags each hit with the property group instead of an
    answers_with doc pointer — this module maps demand, it does not claim to
    answer it.

    `errors` is a sweepcore.LaneReport in a real scan: every search that comes
    back is counted, so the caller can tell a covered-but-empty window from
    one that was never searched. A plain list still works (nothing counted).
    """
    results = []
    gql = (
        "query($q:String!,$n:Int!){ search(query:$q, type:%s, first:$n)"
        "{ nodes { %s } } }"
    )
    for prop, phrases in cfg["property_groups"].items():
        for phrase in phrases:
            base = f"{phrase} created:>={since_date} sort:created-desc"
            for own in cfg["own_repos"]:
                base += f" -repo:{own}"
            for kind, fields in (
                ("ISSUE", ISSUE_FIELDS),
                ("DISCUSSION", DISCUSSION_FIELDS),
            ):
                qstr = f"{base} is:open is:issue" if kind == "ISSUE" else base
                data, err = gh_graphql(gql % (kind, fields), q=qstr, n=cfg["per_query"])
                if err:
                    errors.append(f"{prop}/{kind}: {err}")
                    continue
                note_fetch_ok(errors)
                for node in (data.get("data", {}).get("search", {}) or {}).get(
                    "nodes", []
                ):
                    cand = gh_node_to_candidate(node, prop, phrase)
                    if cand:
                        results.append(cand)
    return results


# --- lane 2: arXiv Atom API ----------------------------------------------------


def _arxiv_url(phrase, max_results):
    """Build the arXiv Atom API query URL for one phrase.

    Confirmed live against export.arxiv.org during build: urlencode's default
    quote_plus percent-encodes the colon and quotes
    (search_query=all%3A%22...%22) and the API accepts that fine (200, correct
    results) — no need to hand-encode only the quotes the way a literal
    `all:%22<phrase>%22` example would suggest.
    """
    params = urllib.parse.urlencode(
        {
            "search_query": f'all:"{phrase}"',
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": max_results,
        }
    )
    return f"{ARXIV_API}?{params}"


def _arxiv_within_window(entry_date, since_dt):
    """True if `entry_date` (ISO 8601, e.g. 2026-06-29T07:51:22Z) is at or
    after since_dt. Missing/unparseable dates are kept (fail-open on the time
    filter, matching forum_sweep._within_window — the seen-store is the real
    backstop against re-surfacing something already shown)."""
    if not entry_date:
        return True
    raw = entry_date.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= since_dt


def _arxiv_text(entry, tag, ns=ATOM_NS):
    """Namespace-qualified child text, whitespace-collapsed; "" if the child
    is absent or empty (a malformed/sparse entry must degrade, not crash).
    Defaults to the Atom namespace; pass ARXIV_NS for arXiv's own extension
    children (comment, primary_category)."""
    el = entry.find(f"{ns}{tag}")
    if el is None or not el.text:
        return ""
    return " ".join(el.text.split())


def _code_url(candidate_url):
    """`candidate_url` if it points at a code host, else "".

    Trailing sentence punctuation is stripped first: a URL written into an
    abstract almost always ends the sentence it is in, and `...repo.` is not
    a clonable address. Host is matched exactly or as a parent domain, so
    `evilgithub.com` fails and `www.github.com` passes.
    """
    url = (candidate_url or "").strip().rstrip(".,;:!?'\"")
    if "://" not in url:
        return ""
    host = url.split("://", 1)[1].split("/", 1)[0].split("@")[-1].lower()
    host = host.split(":", 1)[0]
    for code_host in CODE_HOSTS:
        if host == code_host or host.endswith("." + code_host):
            return url
    return ""


def _first_code_url(text):
    """First code-host URL appearing in a blob of text, or ""."""
    for match in _URL_RE.finditer(text or ""):
        url = _code_url(match.group(0))
        if url:
            return url
    return ""


def detect_code_link(entry, summary):
    """The code URL an arXiv entry advertises, or "". The single strongest
    repro signal, because a claim you cannot run is a claim you cannot check.

    Three places, in descending order of how deliberately the author put it
    there. (1) <link> children: arXiv renders a declared code/DOI resource as
    its own link element, which is structured and unambiguous. (2) arxiv:
    comment, the free-text author note where "Code at https://github.com/..."
    is the long-standing convention. (3) the abstract itself, the loosest
    source and therefore last.

    SECURITY: the returned URL is UNTRUSTED EXTERNAL CONTENT chosen by the
    paper's author. It is stored for a human to open deliberately. Nothing in
    this module fetches it.
    """
    for link in entry.findall(f"{ATOM_NS}link"):
        url = _code_url(link.get("href") or "")
        if url:
            return url
    comment = _first_code_url(_arxiv_text(entry, "comment", ARXIV_NS))
    if comment:
        return comment
    return _first_code_url(summary)


def repro_tier(title, abstract, code_link):
    """A deterministic high/med/low band for "is this worth reproducing?",
    plus the named signals that produced it.

    Same shape and spirit as sweepcore.relevance_tier: a sum of a few small
    named signals a human can audit, a triage HINT and never a verdict.
    Nothing is ever dropped on this score; it only ranks. The signals are
    returned alongside the band and stored on the candidate, so the digest
    shows WHY a paper sits where it does without the reader re-deriving it.

      +2  code_link          a runnable artefact exists
      +1  eval-claim         the paper asserts a measured result to re-check
      -2  survey             nothing to re-run; a framing paper, not a result

    >=3 high, >=1 med, else low.
    """
    signals = []
    score = 0
    if code_link:
        score += 2
        signals.append("code-link")
    haystack = f"{title} {abstract}".lower()
    if any(marker in haystack for marker in REPRO_EVAL_MARKERS) or _PERCENT_RE.search(
        haystack
    ):
        score += 1
        signals.append("eval-claim")
    if any(marker in haystack for marker in REPRO_SURVEY_MARKERS):
        score -= 2
        signals.append("survey")
    if score >= 3:
        return "high", signals
    if score >= 1:
        return "med", signals
    return "low", signals


def entry_to_candidate(entry, property_group, matched_phrase):
    """One <entry> -> the shared candidate schema. None on a malformed entry
    (no <id>, which is the abs-link url every real result carries — arXiv's
    id IS the URL, unlike the other lane where url and id are separate).

    The repro annotations are computed here rather than downstream because
    this is the last place the FULL abstract exists: `snippet` keeps only the
    first 200 characters, and a code link or an eval claim in the second half
    of an abstract would be invisible to anything reading the candidate.
    """
    url = _arxiv_text(entry, "id")
    if not url:
        return None
    published = _arxiv_text(entry, "published")
    updated = _arxiv_text(entry, "updated")
    summary = _arxiv_text(entry, "summary")
    title = _arxiv_text(entry, "title")
    code_link = detect_code_link(entry, summary)
    tier, signals = repro_tier(title, summary, code_link)
    return {
        "url": url,
        "title": title,
        # The human-meaningful "when did this first appear" date; `updated`
        # is kept alongside it (a revision bump means renewed activity) and is
        # what the window filter checks first, below.
        "created": published or updated,
        "updated": updated,
        # SECURITY: title/summary are UNTRUSTED EXTERNAL CONTENT (arXiv
        # authors write both). Truncated and stored for a human to read; never
        # acted on by this tool or anything downstream.
        "snippet": summary[:200],
        "pattern": property_group,
        "property_group": property_group,
        "matched_phrase": matched_phrase,
        "lane": "arxiv",
        # SECURITY: an author-written URL. Data for a human to open, never
        # fetched here. "" when the paper advertises no code.
        "code_link": code_link,
        "repro_tier": tier,
        "repro_signals": signals,
    }


def parse_arxiv_feed(body, property_group, matched_phrase, since_dt):
    """Atom body -> candidates within the window. Raises ET.ParseError on
    malformed XML; the caller (arxiv_lane) treats that exactly like an HTTP
    error — a fetch that returned garbage never actually covered this
    phrase's slice of the window, so it must not be counted as coverage."""
    root = ET.fromstring(body)
    candidates = []
    for entry in root.findall(f"{ATOM_NS}entry"):
        cand = entry_to_candidate(entry, property_group, matched_phrase)
        if cand is None:
            continue
        if not _arxiv_within_window(cand["updated"] or cand["created"], since_dt):
            continue
        candidates.append(cand)
    return candidates


def arxiv_lane(cfg, since_dt, errors):
    """Lane 2: arXiv Atom search per property-group phrase, newest submission
    first, filtered locally to entries at or after `since_dt` — the query API
    has no reliable since-only filter for a free-text search, so the window is
    enforced client-side against <updated>/<published> instead.

    `errors` is a sweepcore.LaneReport in a real scan, exactly like the gh
    lane's: every request that comes back (regardless of how many entries
    survive the window filter) counts as coverage of that phrase.
    """
    results = []
    max_results = cfg["arxiv_max_per_phrase"]
    for prop, phrases in cfg["arxiv_phrases"].items():
        for phrase in phrases:
            if ARXIV_REQUEST_DELAY > 0:
                time.sleep(ARXIV_REQUEST_DELAY)
            url = _arxiv_url(phrase, max_results)
            status, body, err = http_get(
                url,
                timeout=ARXIV_HTTP_TIMEOUT,
                headers={"User-Agent": ARXIV_USER_AGENT},
            )
            if err:
                errors.append(f"{prop}/ARXIV {phrase!r}: {err}")
                continue
            if status != 200:
                errors.append(f"{prop}/ARXIV {phrase!r}: HTTP {status}")
                continue
            try:
                results.extend(parse_arxiv_feed(body, prop, phrase, since_dt))
            except ET.ParseError as exc:
                errors.append(f"{prop}/ARXIV {phrase!r}: bad xml: {exc}")
                continue
            note_fetch_ok(errors)
    return results


# --- filtering + ranking -------------------------------------------------------


def _studied_key(url):
    """The stable identity a studied-paper ledger entry is matched on.

    The digest stores arXiv's own <id> (http://arxiv.org/abs/2606.29914v1),
    but a human marking a paper studied is just as likely to paste the https
    form off the abstract page, or the versionless id. All three name one
    paper, and a v2 revision must not resurface a reproduction already
    published, so the key is the versionless arXiv id wherever the URL is an
    arXiv abs link, and the scheme-stripped URL otherwise. Applied to both
    sides of the comparison, so it never has to be right about which form the
    human will use.
    """
    raw = (url or "").strip()
    match = _ARXIV_ABS_RE.search(raw)
    if match:
        return _ARXIV_VERSION_RE.sub("", match.group("id")).lower()
    return raw.split("://", 1)[-1].rstrip("/").lower()


def filter_candidates(raw, seen, studied, cfg):
    """The keep/drop pass over raw candidates: batch-dup, the studied-paper
    ledger, the seen-store, and (gh lane only) own-repos exclusion + the star
    floor. arXiv candidates carry no repo/stars, so they only ever hit the
    dup/studied/seen checks. There is no "your own repo" or "notable enough"
    concept for a paper. Pure — mutates nothing.

    `studied` is the set of URLs from the studied-paper ledger. It is checked
    BEFORE the seen-store on purpose: a studied paper is almost always also in
    seen, and whichever check runs first owns the counter. "You already
    reproduced this" is the permanent, meaningful reason (the seen-store
    prunes at seen_retention_days; the ledger never does), so it wins the
    attribution and the summary line reports a true count.

    No posted-ledger check, because nothing here is ever posted.
    """
    kept, dropped = [], {"seen": 0, "stars": 0, "own": 0, "dup": 0, "studied": 0}
    batch_urls = set()
    own_repos = {str(r).lower() for r in cfg["own_repos"]}
    studied_keys = {_studied_key(u) for u in studied}
    for cand in raw:
        url = cand["url"]
        if url in batch_urls:
            dropped["dup"] += 1
            continue
        if _studied_key(url) in studied_keys:
            dropped["studied"] += 1
            continue
        if url in seen:
            dropped["seen"] += 1
            continue
        if cand["lane"] == "gh":
            if (cand.get("repo") or "").lower() in own_repos:
                dropped["own"] += 1
                continue
            if cand["stars"] < cfg["min_repo_stars"]:
                dropped["stars"] += 1
                continue
        batch_urls.add(url)
        kept.append(cand)
    return kept, dropped


def cap_per_repo(kept, cap, dropped):
    """Keep at most `cap` candidates per (property_group, repo) pair (input
    already sorted best-first within each group); arXiv candidates (no repo)
    pass through uncapped. Overflow is counted in dropped['repo_cap'].

    Scoped to (property_group, repo) rather than bare repo: this module's
    output is sorted property-group-first, and the per-property tally IS the
    deliverable, so a single noisy repo that happens to have threads across
    several property groups must not let one group's cap silently starve
    another group's share of the same repo — that would corrupt exactly the
    demand evidence this module exists to produce. thread_sweep's plain
    per-repo cap doesn't face this because it has no group-tallied output.
    """
    per_key, capped = {}, []
    for cand in kept:
        repo = cand.get("repo")
        if not repo:
            capped.append(cand)
            continue
        key = (cand.get("property_group"), repo)
        if per_key.get(key, 0) >= cap:
            dropped["repo_cap"] = dropped.get("repo_cap", 0) + 1
            continue
        per_key[key] = per_key.get(key, 0) + 1
        capped.append(cand)
    return capped


# --- per-lane window markers ---------------------------------------------------


def _since_for_lane(name, prior_by_lane, cfg, now, days_override):
    """Window start for one lane: an explicit --days override, else that
    lane's own last_run, else the first-run default window. The marker rule
    itself lives in sweepcore.window_start; this only names the lane."""
    return window_start(
        prior_by_lane.get(name),
        cfg["default_window_days"],
        now,
        days_override,
        label=name,
    )


def _earned_stamp_for_lane(name, prior_by_lane, since_by_lane, now):
    """The last_run this lane's cleanly-fetched run earns — `now` only when
    the run reached back to the lane's own previous marker. Rule and
    rationale: sweepcore.earned_stamp, which every scanning module shares."""
    return earned_stamp(prior_by_lane.get(name), since_by_lane[name], now)


# --- commands ------------------------------------------------------------------


def cmd_scan(args):
    cfg = load_config(args.config)
    global ARXIV_REQUEST_DELAY
    ARXIV_REQUEST_DELAY = cfg.get("arxiv_request_delay_seconds", 0.0)
    state_dir, state_file, ledger_file = state_paths(cfg)
    state = load_state(state_file)
    now = datetime.now(timezone.utc)

    # A corrupt/legacy-shaped nested value must not crash the scan; a fresh
    # module has no legacy shape to migrate from, so (unlike forum_sweep) this
    # is just a defensive read, not a migration.
    raw_prior = state.get("last_run_by_lane")
    prior_by_lane = raw_prior if isinstance(raw_prior, dict) else {}

    # Each lane reads its OWN window, so a GitHub outage can never advance (or
    # hold back) the arXiv marker, and vice versa.
    since_by_lane = {
        name: _since_for_lane(name, prior_by_lane, cfg, now, args.days)
        for name in LANES
    }
    since_date_gh = since_by_lane["gh"].strftime("%Y-%m-%d")

    reports = {"gh": LaneReport(), "arxiv": LaneReport()}
    raw = gh_search_lane(cfg, since_date_gh, reports["gh"]) + arxiv_lane(
        cfg, since_by_lane["arxiv"], reports["arxiv"]
    )
    errors = list(reports["gh"]) + list(reports["arxiv"])
    clean = {name: reports[name].clean for name in LANES}

    seen = state.get("seen", {})
    # Papers a reproduction study has already been run on. Excluded forever,
    # the way every module in this repo excludes what it has finished with.
    studied = posted_urls(ledger_file)
    kept, dropped = filter_candidates(raw, seen, studied, cfg)

    for cand in kept:
        cand["tier"] = relevance_tier(cand)
    # Stable multi-key sort (least-significant key first): property group
    # ascending, fit tier descending, repro tier descending within a fit tier,
    # recency descending within that. The "grouped or sorted by property
    # group then tier" contract still holds, with the repro readout breaking
    # the tie the demand readout
    # leaves. A gh candidate carries no repro tier and ranks at the bottom of
    # that key, which is the honest answer rather than an artefact: you cannot
    # run a reproduction study on an issue thread.
    kept.sort(key=lambda c: c["created"], reverse=True)
    kept.sort(key=lambda c: TIER_RANK.get(c.get("repro_tier"), 0), reverse=True)
    kept.sort(key=lambda c: TIER_RANK[c["tier"]], reverse=True)
    kept.sort(key=lambda c: c["property_group"])
    capped = cap_per_repo(kept, cfg["per_repo_cap"], dropped)
    kept = capped[: cfg["emit_cap"]]

    # A lane earns a new marker only by proving it covered this window: at
    # least one request came back and none failed. Zero candidates from
    # requests that came back is a real, empty window and advances; zero
    # because requests errored, or because the lane made no request at all
    # (nothing configured to query), does NOT — that stretch was never looked
    # at, and moving the marker over it loses it silently and permanently.
    held = [name for name in LANES if not clean[name]]
    if not args.dry_run:
        today = now.date().isoformat()
        for cand in kept:
            # Retrieved and shown to the human, so a re-scan of a held window
            # will not re-surface it; only the never-retrieved rest returns.
            seen[cand["url"]] = today
        cutoff = (now - timedelta(days=cfg["seen_retention_days"])).date().isoformat()
        state["seen"] = {u: d for u, d in seen.items() if d >= cutoff}
        by_lane = dict(prior_by_lane)
        for name in LANES:
            if clean[name]:
                by_lane[name] = _earned_stamp_for_lane(
                    name, prior_by_lane, since_by_lane, now
                )
        state["last_run_by_lane"] = by_lane
        if held:
            grouped = {}
            for name in held:
                grouped.setdefault(hold_reason(reports[name]), []).append(name)
            detail = "; ".join(f"{r}: {', '.join(n)}" for r, n in grouped.items())
            print(
                f"WARN no clean fetch ({detail}) — keeping their last_run so those "
                "windows are re-scanned next time",
                file=sys.stderr,
            )
        write_json_atomic(state_file, state)

    by_property = {prop: 0 for prop in cfg["property_groups"]}
    by_tier = {}
    # Readout 2. arXiv-lane only, so these counts are over papers, not threads.
    repro_by_tier = {}
    code_linked = 0
    for cand in kept:
        group = cand["property_group"]
        by_property[group] = by_property.get(group, 0) + 1
        by_tier[cand["tier"]] = by_tier.get(cand["tier"], 0) + 1
        repro = cand.get("repro_tier")
        if repro:
            repro_by_tier[repro] = repro_by_tier.get(repro, 0) + 1
        if cand.get("code_link"):
            code_linked += 1

    since_headline = min(since_by_lane.values()).strftime("%Y-%m-%d")
    payload = {
        "scanned_at": now.isoformat(),
        "window_since": since_headline,
        "window_since_by_lane": {
            name: dt.strftime("%Y-%m-%d") for name, dt in since_by_lane.items()
        },
        # Lanes that did not complete a clean fetch: their slice of this
        # digest is incomplete and their window is being re-scanned next run.
        "window_held": held,
        # The demand evidence: how many surfaced candidates argue about each
        # workspace property, this run. Every configured property appears,
        # zero included — a quiet property is itself a finding.
        "by_property_group": by_property,
        "by_tier": by_tier,
        # The repro readout, kept in its own section so the existing fields
        # downstream consumers already read keep their exact shape. Counts
        # cover the arXiv lane only; `studied_excluded` is how many papers the
        # ledger kept out of this digest entirely.
        "repro": {
            "code_linked": code_linked,
            "by_tier": repro_by_tier,
            "studied_excluded": dropped.get("studied", 0),
        },
        "dropped": dropped,
        "errors": errors,
        "candidates": kept,
    }
    out = resolve_module_path(__file__, cfg["candidates_file"])
    # A dry run leaves the disk exactly as it found it. candidates.json is the
    # human's working digest — the file they are mid-way through triaging — so
    # overwriting it while announcing "state untouched" destroyed the very
    # thing the preview was meant to protect. The summary below IS the preview.
    if not args.dry_run:
        out.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    mode = " DRY-RUN (nothing written)" if args.dry_run else ""
    print(
        f"BENCHMARK_SWEEP_OK{mode} window>{since_headline} raw={len(raw)} "
        f"kept={len(kept)} dropped={dropped} errors={len(errors)}"
    )
    tally = " / ".join(f"{prop}={count}" for prop, count in by_property.items())
    print(f"demand by property: {tally}")
    print(
        f"fit tiers: {by_tier.get('high', 0)} high / {by_tier.get('med', 0)} med / "
        f"{by_tier.get('low', 0)} low"
    )
    print(
        f"repro: {code_linked} code-linked / {repro_by_tier.get('high', 0)} high / "
        f"{dropped.get('studied', 0)} studied-excluded"
    )
    if args.dry_run:
        print(f"candidates would be written to: {out} (dry run wrote nothing)")
    else:
        print(f"candidates -> {out}")
    for err in errors[:5]:
        print(f"WARN {err}", file=sys.stderr)
    return 0


def cmd_mark_studied(args):
    """Record one reproduction study in the ledger, retiring that paper from
    every future digest.

    Inward-facing throughout: the entry names a paper, the property it sits
    under, and where the study was written up. There is no recipient, no
    body text, and no venue, because nothing here is sent anywhere. The gate
    on this is the human typing the command.
    """
    cfg = load_config(args.config)
    _state_dir, _state_file, ledger_file = state_paths(cfg)
    entry = {
        "date": datetime.now(timezone.utc).isoformat(),
        "url": args.url,
        "property": args.property,
        "study_url": args.study_url or "",
        "note": args.note or "",
    }
    append_ledger(ledger_file, entry)
    print(f"LEDGER_OK {args.url} <- studied ({args.property})")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        # Default resolves beside the module, not beside the CWD; an explicitly
        # passed --config is used verbatim (the user typed it, they meant it).
        default=str(resolve_module_path(__file__, "config.json")),
        help="path to config (default: config.json in the module directory)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    scan = sub.add_parser("scan", help="run both lanes, write candidates.json")
    scan.add_argument(
        "--days",
        type=int,
        default=None,
        help="window override (default: since each lane's last run)",
    )
    scan.add_argument(
        "--dry-run",
        action="store_true",
        help="preview: writes nothing at all (no candidates file, "
        "no seen-marking, no last_run update)",
    )
    scan.set_defaults(func=cmd_scan)
    mark = sub.add_parser(
        "mark-studied",
        help="record a reproduction study you ran on a paper (inward ledger; "
        "that paper stops appearing in scan digests)",
    )
    mark.add_argument(
        "--url",
        required=True,
        help="the arXiv abs URL of the paper studied (any version, http or https)",
    )
    mark.add_argument(
        "--property",
        required=True,
        help="the property_groups slug the study sits under",
    )
    mark.add_argument(
        "--study-url",
        dest="study_url",
        default=None,
        help="where your published reproduction study lives",
    )
    mark.add_argument("--note", default=None)
    mark.set_defaults(func=cmd_mark_studied)
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
