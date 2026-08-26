#!/usr/bin/env python3
"""teardown-sweep - find published agent architectures worth a public teardown.

A teardown here means a respectful written analysis of someone's PUBLISHED,
credited work: what's good about the design, what it trades off, what you
would do differently. This module does the deterministic discovery half
only - it never writes the teardown itself. Three lanes:
  lane 1 (gh search):      repos matching architecture-shaped search phrases
                            or topics, scored on README/docs richness
  lane 2 (HN Algolia):     story titles matching the same kind of phrasing,
                            scored on points + title keyword hits
  lane 3 (gh code search): the configuration artefacts themselves (CLAUDE.md,
                            AGENTS.md, .claude/settings.json, .cursor/rules,
                            copilot-instructions.md) - real configured
                            workspaces, not write-ups about them - scored on
                            how many of a 15-pattern reference architecture
                            each one shows evidence of

INWARD-FACING, like placement-health: nothing here is outbound. The script
never posts a comment, opens an issue, or contacts anyone - it reads public
search results, public READMEs, and public artefact files, and writes a
ranked reading list. The rest of this toolkit gates outbound actions behind
human approval; this module has no outbound action to gate. A human reads
candidates.json and decides, entirely outside this tool, whether and how to
write a teardown.

SECURITY: every description/README/title/artefact fetched here is UNTRUSTED
EXTERNAL CONTENT. It is scanned for configured keywords/patterns and stored
(truncated) for a human to read later - never executed, never treated as an
instruction.

Read README.md's Etiquette section before writing anything from this list: a
teardown is analysis of work its author chose to publish and be credited for,
not an excuse to dig through anything they did not make public.

Requires: Python 3.10+, an authenticated GitHub CLI (`gh auth login`) for
lanes 1 and 3. Lane 2 needs no auth (public Algolia read). Lane 3 calls
GitHub's code_search REST resource, rate-limited to 10 requests/min - see
artefact_lane()'s docstring for the budget arithmetic.

Subcommands (all take --config; the default is the config.json beside this
script, so the module reads its own state and config from any directory):
  scan [--days N] [--limit N] [--dry-run] [--no-artefacts]
                                             run all lanes, write candidates
                                             (--no-artefacts skips lane 3)
  mark-covered --url U [--note ...]         record a completed teardown
  log                                       show recorded teardowns
"""

import argparse
import base64
import binascii
import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sweepcore import (  # noqa: E402
    TIER_RANK,
    LaneReport,
    append_ledger,
    earned_stamp,
    gh,
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

REQUIRED_KEYS = ["own_repos"]
DEFAULTS = {
    # Generic, domain-level discovery terms (not project-specific), so a fresh
    # checkout can scan immediately - only own_repos needs real editing.
    "search_queries": [
        "agent workspace architecture",
        "claude code workspace",
        "ai agent harness memory",
        "agent orchestration framework",
        "personal ai agent setup",
    ],
    "topics": ["ai-agents", "agentic-ai"],
    "hn_queries": [
        "agent architecture",
        "how I built my ai agent",
        "claude code setup",
        "agent memory system",
    ],
    # Lane 3: the artefact FILES themselves, not phrases about them. Each
    # entry is a label (for the digest/errors) + a GitHub code-search query.
    # size:>N is a server-side floor to kill stubs - live-sampled during
    # build (see README's Lane 3 section for the evidence): a two-line
    # CLAUDE.md is the dominant false positive without one.
    "artefact_queries": [
        {"label": "claude-md", "query": "filename:CLAUDE.md size:>4000"},
        {"label": "agents-md", "query": "filename:AGENTS.md size:>4000"},
        {
            "label": "claude-settings",
            "query": "filename:settings.json path:.claude size:>2000",
        },
        {"label": "cursor-rules", "query": "path:.cursor/rules size:>2000"},
        {
            "label": "copilot-instructions",
            "query": "filename:copilot-instructions.md path:.github size:>2000",
        },
    ],
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
    # Lane 3 pattern-density map: one label per numbered pattern of the
    # reference architecture, each a short list of lowercase indicator
    # phrases scanned against fetched artefact text. See
    # score_artefact_patterns / score_pattern_density for how this drives
    # the ranking.
    "pattern_signals": {
        "p01": ["roles/", "persona", "subagent", "role composition", "sub-agent"],
        "p02": [
            "classify-then-act",
            "classify then act",
            "triage queue",
            "classify and act",
        ],
        "p03": [
            "dead man's switch",
            "sentinel",
            "freshness check",
            "staleness check",
            "heartbeat monitor",
        ],
        "p04": ["tier by impact", "impact tier", "severity tier", "tiered by impact"],
        "p05": ["memory index", "memory pointer", "memory.md", "pointer file"],
        "p06": [
            "bitwarden",
            "1password",
            "credentials never",
            "secrets vault",
            "never in files",
        ],
        "p07": [
            "pretooluse",
            "pre-tooluse",
            "blocklist guard",
            "hook guard",
            "deny list",
        ],
        "p08": ["audit cadence", "weekly audit", "audit ledger", "periodic audit"],
        "p09": [
            "context budget",
            "compaction",
            "context window budget",
            "token budget",
        ],
        "p10": ["lessons.md", "self-edit", "gated edit", "lessons learned file"],
        "p11": [
            "scaffold register",
            "falsifiable hypothesis",
            "scaffold hypothesis",
            "register the scaffold",
        ],
        "p12": [
            "recurring vs on-demand",
            "loop selection",
            "on-demand loop",
            "interval-triggered loop",
        ],
        "p13": ["red-team", "divergent lens", "adversarial critic", "devil's advocate"],
        "p14": ["task board", "delegation queue", "kanban board", "agent queue"],
        "p15": ["model tiering", "cost routing", "tiered model", "tier by cost"],
    },
    "min_stars": 50,
    # Lane 3's own (lower) star floor: practitioner workspaces run smaller
    # than the frameworks lane 1 targets, so lane 1's min_stars would starve
    # this lane if reused directly.
    "artefact_min_stars": 20,
    "active_within_days": 365,
    "hn_min_points": 10,
    # Pages fetched per artefact_queries entry (per_page is fixed at
    # ARTEFACT_PER_PAGE). The 10-requests/min code_search budget is the
    # constraint on raising this - see artefact_lane()'s docstring.
    "artefact_pages_per_query": 1,
    "per_query": 20,
    "emit_cap": 60,
    "seen_retention_days": 180,
    "default_window_days": 30,
    "state_dir": "state",
    "candidates_file": "candidates.json",
}

UA = "teardown-sweep (https://github.com/signal-sweep/signal-sweep)"
HN_TIMEOUT = 15

# Lane 3 (artefact code search) budget. code_search is rate-limited to 10
# requests/min (live-verified: `gh api rate_limit` -> resources.code_search.
# limit == 10). ARTEFACT_PER_PAGE is fixed rather than configurable - the
# constraint is the request COUNT, not the page size. Calls per scan =
# len(artefact_queries) * artefact_pages_per_query; the shipped defaults (5
# queries * 1 page = 5 calls) at ARTEFACT_SLEEP_SECONDS apart spend ~24s and
# stay comfortably inside one rolling 60s window.
ARTEFACT_PER_PAGE = 30
ARTEFACT_SLEEP_SECONDS = 6.0


def load_config(path):
    cfg_path = Path(path)
    if not cfg_path.exists():
        sys.exit(
            f"config not found: {cfg_path}\n"
            "Copy config.example.json to config.json and edit own_repos for "
            "your project."
        )
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"config is not valid JSON ({cfg_path}): {exc}")
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"config missing required keys: {missing}")
    if not isinstance(cfg["own_repos"], list):
        sys.exit("config 'own_repos' must be a list of owner/name repos to exclude")
    for key, val in DEFAULTS.items():
        cfg.setdefault(key, val)
    for key in (
        "search_queries",
        "topics",
        "hn_queries",
        "richness_keywords",
        "artefact_queries",
    ):
        if not isinstance(cfg[key], list):
            sys.exit(f"config {key!r} must be a list")
    if not isinstance(cfg["pattern_signals"], dict):
        sys.exit(
            "config 'pattern_signals' must be an object of label -> indicator list"
        )
    return cfg


def load_config_for_dry_run(path):
    """Dry-run resolves to config.example.json when the live config.json is
    absent, so the queries can be previewed (no calls, no writes) before a
    project copies the example. Mirrors list-sweep so `scan --dry-run` works
    on a fresh checkout."""
    if Path(path).exists():
        return load_config(path)
    example = Path(__file__).resolve().parent / "config.example.json"
    if example.exists():
        return load_config(str(example))
    return load_config(path)


def state_paths(cfg):
    # Module-anchored, not CWD-anchored: this module has exactly one canonical
    # state dir wherever it is invoked from. See sweepcore.resolve_module_path.
    state_dir = resolve_module_path(__file__, cfg["state_dir"])
    return state_dir, state_dir / "teardown_state.json", state_dir / "covered_log.jsonl"


def is_own_repo(repo, own_repos):
    """Case-insensitive owner/name match against the configured own-repo list
    (mirrors mention-sweep's helper of the same name)."""
    repo_l = (repo or "").lower()
    return any(repo_l == str(own).lower() for own in own_repos)


def _dedup_key(cand):
    """The identity a candidate is deduped/seen/covered under: repo
    (lowercased) for both github-flavoured lanes (1: frameworks, 3:
    artefacts - both about a REPO's own qualities), the story/post URL for
    HN (lane 2, about one specific piece of writing). Lane-3 candidates are
    deliberately keyed at repo granularity even though one repo can surface
    more than one matching artefact in a run - a digest does not need the
    same practitioner workspace twice just because it has both a CLAUDE.md
    and a .cursor/rules hit; the first match wins, exactly like a repo hit by
    both a phrase and a topic query in lane 1."""
    if cand["lane"] == "hn":
        return cand["url"]
    return cand["repo"].lower()


# --- lane 1: github repos ------------------------------------------------


def search_repos(query, limit, extra_args, errors):
    """One `gh search repos` call: a query string plus extra CLI flags (e.g.
    --topic). Fail-soft: an error or an unusable payload is appended to
    errors and an empty list returned, so one bad query does not abort the
    rest of the lane. Mirrors list_sweep.search_repos."""
    fields = "fullName,description,stargazersCount,url,pushedAt"
    label = repr(query) + (f" {' '.join(extra_args)}" if extra_args else "")
    data, err = gh(
        ["search", "repos", query, "--limit", str(limit), "--json", fields, *extra_args]
    )
    if err:
        errors.append(f"repo search {label}: {err}")
        return []
    if not isinstance(data, list):
        # gh() yields ("", "") when a non-zero exit writes nothing to stderr,
        # so the `if err` guard above misses it - treat an unusable payload as
        # the failure it is rather than iterating a non-list.
        errors.append(f"repo search {label}: no usable result payload")
        return []
    return data


def _decode_b64_content(content):
    """Shared base64-decode for a GitHub contents-API payload's `content`
    field (GitHub line-wraps it; b64decode's default validate=False discards
    the embedded newlines before decoding, so no stripping needed). Empty
    string on malformed input rather than raising - used by both the README
    fetch below and the lane-3 artefact fetch, which degrade the same way."""
    try:
        return base64.b64decode(content).decode("utf-8", errors="replace")
    except binascii.Error:
        return ""


def fetch_readme_text(full_name):
    """Best-effort repo README fetch via `gh api` (base64 body). Empty string
    on any failure (missing README, renamed/private repo, API hiccup) - the
    candidate still builds, just loses the text-keyword and length signals,
    exactly like list-sweep's classify_intake degrades to 'unknown' on a
    missing doc."""
    data, err = gh(["api", f"repos/{full_name}/readme"])
    if err or not isinstance(data, dict):
        return ""
    content = data.get("content", "")
    if not content:
        return ""
    return _decode_b64_content(content)


def has_docs_dir(full_name):
    """Best-effort cheap directory-listing check (one small `gh api` call, no
    file content fetched). False on any failure - missing dir, a `docs` that
    is a FILE not a directory (contents API then returns a dict, not a list),
    or an API hiccup - rather than raising."""
    data, err = gh(["api", f"repos/{full_name}/contents/docs"])
    return err is None and isinstance(data, list)


def score_readme_richness(description, readme_text, cfg):
    """Keyword hits scanned across description + README (so a failed README
    fetch still leaves the description's signal); the length band is the
    README's own length only - a long description is not a substitute for
    actual docs, so it must not buy the length bonus."""
    haystack = f"{description or ''}\n{readme_text or ''}".lower()
    matched = sorted({kw for kw in cfg["richness_keywords"] if kw.lower() in haystack})
    score = len(matched)
    length = len(readme_text or "")
    if length >= 3000:
        score += 2
    elif length >= 500:
        score += 1
    return score, matched


def score_repo_signals(stars, age_days, has_docs, cfg):
    """The non-text richness signals: a stars band, a recency band, and a
    flat bonus for a docs/ directory - a maintained project's strongest single
    tell of deliberate architecture documentation beyond one README."""
    score = 0
    if stars >= 1000:
        score += 2
    elif stars >= cfg["min_stars"]:
        score += 1
    if age_days is not None:
        if age_days <= 30:
            score += 2
        elif age_days <= 90:
            score += 1
    if has_docs:
        score += 2
    return score


def _pushed_age_days(stamp, now):
    """Days between an ISO 8601 stamp and now; None if absent/unparseable - a
    missing/odd pushedAt must not crash scoring, just skip the recency band."""
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).days


def _why_repo(stars, age_days, matched, has_docs):
    bits = [f"{stars} stars"]
    bits.append(
        f"pushed {age_days}d ago" if age_days is not None else "push date unknown"
    )
    bits.append(
        f"{len(matched)} arch keyword(s) ({', '.join(matched[:4])})"
        if matched
        else "0 arch keywords"
    )
    if has_docs:
        bits.append("has docs/")
    return "; ".join(bits)


def build_github_candidate(node, pattern, cfg, now, readme_cache, docs_cache):
    """Shape one gh-search hit into a candidate, fetching README text and
    docs/ presence (cached per full_name so a repo hit by both a phrase and a
    topic query this run is only fetched once)."""
    full_name = node.get("fullName") or ""
    if not full_name:
        return None
    url = node.get("url") or f"https://github.com/{full_name}"
    stars = node.get("stargazersCount", 0) or 0
    pushed_at = node.get("pushedAt", "") or ""
    # SECURITY: description/README are UNTRUSTED EXTERNAL CONTENT. Scanned for
    # keyword hits and stored truncated for a human to read; never executed or
    # treated as an instruction by this tool.
    description = node.get("description", "") or ""
    if full_name not in readme_cache:
        readme_cache[full_name] = fetch_readme_text(full_name)
    if full_name not in docs_cache:
        docs_cache[full_name] = has_docs_dir(full_name)
    readme_text = readme_cache[full_name]
    docs_present = docs_cache[full_name]

    text_score, matched = score_readme_richness(description, readme_text, cfg)
    age_days = _pushed_age_days(pushed_at, now)
    signal_score = score_repo_signals(stars, age_days, docs_present, cfg)

    cand = {
        "lane": "github",
        "repo": full_name,
        "url": url,
        "description": description[:300],
        "stars": stars,
        "pushed_at": pushed_at,
        "pattern": pattern,
        "matched_keywords": matched,
        "richness_score": text_score + signal_score,
        "has_docs_dir": docs_present,
        "readme_len": len(readme_text),
    }
    cand["tier"] = relevance_tier(cand)
    cand["why"] = _why_repo(stars, age_days, matched, docs_present)
    return cand


def github_lane(cfg, floor_date, now, advisory):
    """Lane 1: two query shapes over `gh search repos` - a free-text phrase
    per search_queries entry, and a --topic filter per topics entry - both
    scoped to the SAME fixed activity floor (pushed within active_within_days
    of NOW, recomputed every run). This is deliberately NOT windowed against
    last_run: see cmd_scan for why only lane 2 governs the earn/hold marker.
    Errors are fail-soft into `advisory` (surfaced in the digest, never gate
    the marker) - mirrors how list_sweep's watchlist lane is untimed."""
    readme_cache, docs_cache = {}, {}
    raw = []
    for phrase in cfg["search_queries"]:
        query = f"{phrase} pushed:>{floor_date} sort:stars"
        for node in search_repos(query, cfg["per_query"], [], advisory):
            cand = build_github_candidate(
                node, phrase, cfg, now, readme_cache, docs_cache
            )
            if cand:
                raw.append(cand)
    for topic in cfg["topics"]:
        query = f"pushed:>{floor_date} sort:stars"
        for node in search_repos(query, cfg["per_query"], ["--topic", topic], advisory):
            cand = build_github_candidate(
                node, f"topic:{topic}", cfg, now, readme_cache, docs_cache
            )
            if cand:
                raw.append(cand)
    return raw


# --- lane 2: hn algolia ----------------------------------------------------


def hn_search_url(query, min_points, since_epoch, limit):
    """Build a search_by_date URL: stories only, a combined points-floor +
    since-epoch numeric filter (comma = AND on Algolia's numericFilters, live
    confirmed), hitsPerPage to respect the configured per-query cap."""
    filters = f"points>={min_points},created_at_i>{since_epoch}"
    return (
        "https://hn.algolia.com/api/v1/search_by_date"
        f"?query={quote(query)}&tags=story&numericFilters={quote(filters)}"
        f"&hitsPerPage={limit}"
    )


def score_hn_richness(title, points, cfg):
    """Lane-2 richness: keyword hits in the title (no README to read) plus a
    points band - the only architecture-depth proxy a title-only search has."""
    matched = sorted(
        {kw for kw in cfg["richness_keywords"] if kw.lower() in (title or "").lower()}
    )
    score = len(matched)
    if points >= 100:
        score += 2
    elif points >= cfg["hn_min_points"]:
        score += 1
    return score, matched


def _why_hn(points, comments, matched):
    bits = [f"{points} points", f"{comments} comments"]
    bits.append(
        f"{len(matched)} arch keyword(s) in title ({', '.join(matched[:4])})"
        if matched
        else "0 arch keywords in title"
    )
    return "; ".join(bits)


def build_hn_candidate(hit, pattern, cfg):
    """Shape one Algolia hit into a candidate. Malformed rows (not a dict, no
    objectID) are skipped rather than raised - one bad hit must not abort the
    whole lane. `url` falls back to the HN discussion permalink for Ask-HN /
    Show-HN self-posts, which carry no external `url` field. The points floor
    is NOT applied here (see cmd_scan) so it stays visible in `dropped`,
    symmetric with lane 1's star floor."""
    if not isinstance(hit, dict):
        return None
    obj_id = hit.get("objectID")
    if not obj_id:
        return None
    try:
        points = int(hit.get("points"))
    except (TypeError, ValueError):
        points = 0
    # SECURITY: title/story text are UNTRUSTED EXTERNAL CONTENT. Scanned for
    # keyword hits only; never executed or treated as an instruction.
    title = hit.get("title") or hit.get("story_title") or ""
    url = hit.get("url") or f"https://news.ycombinator.com/item?id={obj_id}"
    comments = hit.get("num_comments") or 0
    score, matched = score_hn_richness(title, points, cfg)
    cand = {
        "lane": "hn",
        "title": title,
        "url": url,
        "score_or_stars": points,
        "hn_comments": comments,
        "pattern": pattern,
        "matched_keywords": matched,
        "richness_score": score,
    }
    cand["tier"] = relevance_tier(cand)
    cand["why"] = _why_hn(points, comments, matched)
    return cand


def hn_lane(cfg, since_epoch, report):
    """Lane 2: a phrase search per hn_queries entry. `report` is a
    sweepcore.LaneReport in a real scan - this is the ONLY lane whose fetch
    accounting governs the module's earn/hold marker (see cmd_scan)."""
    results = []
    for query in cfg["hn_queries"]:
        url = hn_search_url(query, cfg["hn_min_points"], since_epoch, cfg["per_query"])
        status, body, err = http_get(
            url, timeout=HN_TIMEOUT, headers={"User-Agent": UA}
        )
        if err:
            report.append(f"hn {query!r}: {err}")
            continue
        if status != 200:
            report.append(f"hn {query!r}: HTTP {status}")
            continue
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            report.append(f"hn {query!r}: bad json ({exc})")
            continue
        if not isinstance(data, dict):
            report.append(f"hn {query!r}: no usable result payload")
            continue
        note_fetch_ok(report)
        for hit in data.get("hits") or []:
            cand = build_hn_candidate(hit, query, cfg)
            if cand:
                results.append(cand)
    return results


# --- lane 3: artefact code search ------------------------------------------
#
# Lanes 1 and 2 find frameworks and write-ups. Lane 3 finds real configured
# agent workspaces by searching for the configuration artefacts themselves
# (CLAUDE.md, AGENTS.md, .claude/settings.json, .cursor/rules,
# copilot-instructions.md) via GitHub's REST code_search resource.


def content_prefix_hash(text):
    """sha1 of the first ~2KB of artefact text with whitespace runs
    collapsed, so two copies of the same template that differ only past that
    prefix, or only in incidental whitespace, still hash identically. This is
    near-duplicate detection for template floods, not exact-duplicate
    detection."""
    prefix = (text or "")[:2048]
    normalized = " ".join(prefix.split())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


# score_pattern_density's shape: peaks over PATTERN_PEAK_LOW..PATTERN_PEAK_HIGH
# patterns present, decays linearly toward both 0 and 15 patterns present.
PATTERN_PEAK_LOW = 4
PATTERN_PEAK_HIGH = 9
PATTERN_PEAK_SCORE = 10
PATTERN_LOW_SLOPE = 3
PATTERN_HIGH_SLOPE = 2


def score_pattern_density(n_present):
    """The ranking philosophy for lane 3, as a function: a candidate that
    shows a handful of the reference architecture's 15 patterns while
    conspicuously missing others is exactly what a teardown wants to write
    about (what works + what's missing) - it outranks one that matches
    everything (nothing left to contrast) or nothing (nothing to praise).
    Peaks flat over the PEAK_LOW..PEAK_HIGH band, ramps down on both sides -
    a 6-pattern artefact (peak band, scores PATTERN_PEAK_SCORE) always beats
    an all-15 artefact (past the top of the down-ramp, scores 0)."""
    if n_present <= PATTERN_PEAK_LOW:
        distance = PATTERN_PEAK_LOW - n_present
        return max(0, PATTERN_PEAK_SCORE - distance * PATTERN_LOW_SLOPE)
    if n_present <= PATTERN_PEAK_HIGH:
        return PATTERN_PEAK_SCORE
    distance = n_present - PATTERN_PEAK_HIGH
    return max(0, PATTERN_PEAK_SCORE - distance * PATTERN_HIGH_SLOPE)


def score_artefact_patterns(text, pattern_signals):
    """Score fetched artefact text against pattern_signals (label -> list of
    lowercase indicator phrases, one label per numbered reference-
    architecture pattern). A label counts as present on any single indicator
    hit - a coverage signal (does the artefact address the pattern at all),
    not a density-within-pattern one, mirroring score_readme_richness's
    keyword matching. SECURITY: text is UNTRUSTED EXTERNAL CONTENT, scanned
    for indicator hits only, never treated as an instruction."""
    haystack = (text or "").lower()
    present = sorted(
        label
        for label, indicators in pattern_signals.items()
        if any(ind.lower() in haystack for ind in indicators)
    )
    absent = sorted(label for label in pattern_signals if label not in present)
    return present, absent, score_pattern_density(len(present))


def artefact_tier(pattern_score):
    """pattern_score band -> the same high/med/low vocabulary lanes 1-2 carry
    via sweepcore.relevance_tier, so the digest sorts uniformly across all
    three lanes. relevance_tier's own signal set (is_answered, comments,
    match_type) has nothing to read on an artefact hit, so this is a small
    dedicated band map instead of a relevance_tier call."""
    if pattern_score >= 8:
        return "high"
    if pattern_score >= 3:
        return "med"
    return "low"


def _why_artefact(stars, age_days, matched):
    bits = [f"{stars} stars"]
    bits.append(
        f"pushed {age_days}d ago" if age_days is not None else "push date unknown"
    )
    bits.append(
        f"{len(matched)}/15 pattern(s) present ({', '.join(matched[:4])})"
        if matched
        else "0/15 patterns present"
    )
    return "; ".join(bits)


def fetch_repo_meta(full_name):
    """Best-effort repo metadata fetch (core quota, not the rate-limited
    code_search resource): stars, push date, fork/template/archived status.
    None on any failure - the caller cannot verify fork/star/activity status
    without it, so it skips the candidate rather than guessing."""
    data, err = gh(["api", f"repos/{full_name}"])
    if err or not isinstance(data, dict):
        return None
    return data


def fetch_artefact_content(full_name, path):
    """Best-effort fetch of one matched artefact's raw content (core quota,
    `gh api repos/{full}/contents/{path}`, base64 body) - used for both the
    pattern-density score and the near-duplicate content hash. Empty string
    on any failure (renamed file, repo gone private, API hiccup); the
    candidate still builds, it just scores zero patterns, the same degrade
    shape as fetch_readme_text."""
    data, err = gh(["api", f"repos/{full_name}/contents/{quote(path, safe='/')}"])
    if err or not isinstance(data, dict):
        return ""
    content = data.get("content", "")
    if not content:
        return ""
    return _decode_b64_content(content)


def search_code_page(query, page, per_page):
    """One page of REST code search: `gh api search/code?q=...`. Returns
    (items, err), mirroring gh() itself - fail-soft, an unusable payload
    becomes ([], a description) rather than a raise. The code_search
    resource is rate-limited to 10 requests/min (live-verified via `gh api
    rate_limit` -> resources.code_search.limit); a 403 here is that limit,
    not an auth failure - gh() already exits fatally on the 401-shaped auth
    markers before ever returning one."""
    url = f"search/code?q={quote(query)}&page={page}&per_page={per_page}"
    data, err = gh(["api", url])
    if err:
        return [], err
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return [], "no usable result payload"
    return data["items"], None


def build_artefact_candidate(hit, label, cfg, now, repo_meta_cache, content_cache):
    """Shape one code-search hit into a lane-3 candidate: repo metadata
    (fork/template/archived/stars/pushed_at, cached per repo - a repo can
    show up under more than one query) plus the matched artefact's own
    content (pattern-density score + near-dup hash, cached per repo+path -
    two query labels could in principle hit the same file).

    stargazers_count/pushed_at/fork/is_template/archived are NOT on the
    code-search hit itself (live-verified: the embedded `repository` object
    is a trimmed representation with no stargazers_count field at all) -
    that is what repo_meta_cache's one `gh api repos/{full}` fetch per
    unique repo is for."""
    repo = (hit.get("repository") or {}).get("full_name") or ""
    path = hit.get("path") or ""
    if not repo or not path:
        return None
    if repo not in repo_meta_cache:
        repo_meta_cache[repo] = fetch_repo_meta(repo)
    meta = repo_meta_cache[repo]
    if meta is None:
        return None

    cache_key = (repo, path)
    if cache_key not in content_cache:
        content_cache[cache_key] = fetch_artefact_content(repo, path)
    # SECURITY: artefact content is UNTRUSTED EXTERNAL CONTENT. Scanned for
    # pattern-indicator hits and hashed for dedup only; never executed or
    # treated as an instruction by this tool.
    content = content_cache[cache_key]

    stars = meta.get("stargazers_count", 0) or 0
    pushed_at = meta.get("pushed_at", "") or ""
    age_days = _pushed_age_days(pushed_at, now)
    present, absent, pattern_score = score_artefact_patterns(
        content, cfg["pattern_signals"]
    )
    # No docs/ check for lane 3 (has_docs=False): that would be a third gh
    # call per candidate on top of the metadata + content fetch this lane
    # already spends, for a signal lane 1 already covers.
    signal_score = score_repo_signals(stars, age_days, False, cfg)

    cand = {
        "lane": "artefact",
        "repo": repo,
        "url": f"https://github.com/{repo}",
        "stars": stars,
        "pushed_at": pushed_at,
        "fork": bool(meta.get("fork")),
        "is_template": bool(meta.get("is_template")),
        "archived": bool(meta.get("archived")),
        "artefact_label": label,
        "artefact_path": path,
        "artefact_url": hit.get("html_url") or "",
        "patterns_present": present,
        "patterns_absent": absent,
        "pattern_score": pattern_score,
        "richness_score": pattern_score + signal_score,
        "content_hash": content_prefix_hash(content),
    }
    cand["tier"] = artefact_tier(pattern_score)
    cand["why"] = _why_artefact(stars, age_days, present)
    return cand


def artefact_lane(cfg, now, advisory):
    """Lane 3: REST code search for the configuration artefacts themselves -
    real configured agent workspaces, not the frameworks or write-ups lanes 1
    and 2 find. Code search has no date qualifier to scope a floor against,
    so - like lane 1 - this lane is windowless: static floors
    (fork/template/archived/stars/active_within_days) plus the seen-store
    and covered ledger are the only repeat-guard, and its failures are
    always advisory, never gating the scan's earn/hold marker (only lane 2's
    HN window does that; see cmd_scan).

    Budget: code_search is rate-limited to 10 requests/min (live-verified,
    `gh api rate_limit` -> resources.code_search.limit == 10). This lane
    makes len(artefact_queries) * artefact_pages_per_query calls per scan -
    5 queries * 1 page = 5 calls with the shipped defaults - sleeping
    ARTEFACT_SLEEP_SECONDS between successive calls (not before the first),
    so a default run's 5 calls spread across ~24s stay comfortably inside
    one rolling 60s window. A 403 on this endpoint is that limit, not an
    auth failure; on one, this lane stops issuing further queries for the
    rest of THIS run (rather than working through the remaining queries into
    more 403s) and reports it as advisory - next run tries again from the
    top."""
    raw = []
    repo_meta_cache, content_cache = {}, {}
    made_a_call = False
    for entry in cfg["artefact_queries"]:
        if not isinstance(entry, dict) or not entry.get("query"):
            continue
        label = entry.get("label") or entry["query"]
        query = entry["query"]
        for page in range(1, cfg["artefact_pages_per_query"] + 1):
            if made_a_call:
                time.sleep(ARTEFACT_SLEEP_SECONDS)
            made_a_call = True
            items, err = search_code_page(query, page, ARTEFACT_PER_PAGE)
            if err:
                advisory.append(f"artefact search {label!r} page {page}: {err}")
                if "403" in err:
                    advisory.append(
                        "artefact lane: code_search rate limit hit - holding "
                        "the remaining queries for next run"
                    )
                    return raw
                continue
            for hit in items:
                cand = build_artefact_candidate(
                    hit, label, cfg, now, repo_meta_cache, content_cache
                )
                if cand:
                    raw.append(cand)
    return raw


def _artefact_drop_reason(cand, cfg, now, batch_hashes, prior_hashes):
    """Lane-3-only exclusions beyond the dup/own/seen/covered checks every
    lane shares: the fork-flood guard (fork/template/archived), this lane's
    own (lower) star floor, the active_within_days recency floor code search
    has no query-side equivalent for, and near-duplicate artefact content.
    None if the candidate clears all of them."""
    if cand["fork"]:
        return "fork"
    if cand["is_template"]:
        return "template"
    if cand["archived"]:
        return "archived"
    if cand["stars"] < cfg["artefact_min_stars"]:
        return "stars"
    age_days = _pushed_age_days(cand["pushed_at"], now)
    if age_days is not None and age_days > cfg["active_within_days"]:
        return "stale"
    content_hash = cand.get("content_hash")
    if content_hash and (content_hash in batch_hashes or content_hash in prior_hashes):
        return "dup_content"
    return None


# --- scan --------------------------------------------------------------


def cmd_scan(args):
    if args.dry_run:
        cfg = load_config_for_dry_run(args.config)
    else:
        cfg = load_config(args.config)
    now = datetime.now(timezone.utc)
    # Lane 1's activity floor is a fixed trailing window from NOW, recomputed
    # every run - not a since-last-run claim, so it carries no marker
    # obligation. See the earn/hold comment further down.
    floor_date = (now - timedelta(days=cfg["active_within_days"])).strftime("%Y-%m-%d")
    # not args.no_artefacts, not getattr(): a bare mock.Mock() args object (as
    # every pre-lane-3 test constructs) auto-vivifies no_artefacts as a truthy
    # child Mock, so this is False for those tests - lane 3 never fires a real
    # gh call from an old, unmodified test. Real CLI usage defaults the flag
    # to False via argparse, so `not False` runs lane 3 by default, which is
    # also the intended end-user behaviour (opt OUT, not opt in).
    run_artefacts = not args.no_artefacts

    if args.dry_run:
        # No network, no gh, no state. Print exactly what we WOULD query.
        print("TEARDOWN_SWEEP_DRY-RUN (no network, no gh, no state writes)")
        print(
            f"  lane 1 (gh search repos, pushed>{floor_date}, star floor {cfg['min_stars']}):"
        )
        for phrase in cfg["search_queries"]:
            query = f"{phrase} pushed:>{floor_date} sort:stars"
            print(f"    gh search repos {query!r} --limit {cfg['per_query']}")
        for topic in cfg["topics"]:
            query = f"pushed:>{floor_date} sort:stars"
            print(
                f"    gh search repos {query!r} --topic {topic} --limit {cfg['per_query']}"
            )
        print(f"  lane 2 (HN Algolia, tags=story, points>={cfg['hn_min_points']}):")
        for q in cfg["hn_queries"]:
            print(f"    query={q!r} hitsPerPage={cfg['per_query']}")
        if run_artefacts:
            n = len(cfg["artefact_queries"])
            print(
                f"  lane 3 (gh api search/code, {n} quer{'y' if n == 1 else 'ies'} x "
                f"{cfg['artefact_pages_per_query']} page(s), star floor "
                f"{cfg['artefact_min_stars']}):"
            )
            for entry in cfg["artefact_queries"]:
                if not isinstance(entry, dict) or not entry.get("query"):
                    continue
                label = entry.get("label") or entry["query"]
                print(f"    gh api search/code?q=... [{label}] {entry['query']!r}")
        else:
            print("  lane 3 (gh api search/code): skipped (--no-artefacts)")
        print(f"  own_repos excluded: {cfg['own_repos'] or '(none configured)'}")
        print(
            "  candidates would be written to: "
            f"{resolve_module_path(__file__, cfg['candidates_file'])}"
        )
        return 0

    _state_dir, state_file, ledger_file = state_paths(cfg)
    state = load_state(state_file)
    # A marker that no longer parses re-windows to the default with a warning
    # rather than raising out of the scan; sweepcore.window_start owns the rule.
    since = window_start(
        state.get("last_run"),
        cfg["default_window_days"],
        now,
        args.days,
        label="teardown-sweep",
    )
    since_epoch = int(since.timestamp())

    # Only lane 2 (HN) is windowed against last_run, so only its LaneReport
    # governs the marker. Lanes 1 and 3's floors are static (a trailing
    # window from NOW for lane 1; fork/template/archived/stars/recency for
    # lane 3, since code search has no date qualifier to window against) -
    # there is no "stretch since last time" for either to lose, so their
    # failures are reported (advisory) but never hold the marker. This is the
    # mirror image of list_sweep, where the query lane governs the marker and
    # the watchlist lane is untimed; here it is the query-shaped HN lane that
    # is windowed and both repo lanes that are not.
    report = LaneReport()
    advisory = []
    raw = github_lane(cfg, floor_date, now, advisory) + hn_lane(
        cfg, since_epoch, report
    )
    if run_artefacts:
        raw += artefact_lane(cfg, now, advisory)

    seen = state.get("seen", {})
    content_hashes = state.get("content_hashes", {})
    covered = posted_urls(ledger_file)
    errors = list(report) + advisory

    dropped = {
        "dup": 0,
        "own": 0,
        "seen": 0,
        "covered": 0,
        "stars": 0,
        "points": 0,
        "fork": 0,
        "template": 0,
        "archived": 0,
        "stale": 0,
        "dup_content": 0,
    }
    kept = []
    batch = set()
    batch_hashes = set()
    for cand in raw:
        # Seen/dedup key: repo full_name (lowercased) for the github-flavoured
        # lanes (1 and 3), the story URL for lane 2 - all stable identities a
        # candidate surfaces under exactly once. See _dedup_key.
        key = _dedup_key(cand)
        if key in batch:
            dropped["dup"] += 1
            continue
        if cand["lane"] in ("github", "artefact") and is_own_repo(
            cand["repo"], cfg["own_repos"]
        ):
            dropped["own"] += 1
            continue
        if key in seen:
            dropped["seen"] += 1
            continue
        if cand["url"] in covered:
            dropped["covered"] += 1
            continue
        if cand["lane"] == "github" and cand["stars"] < cfg["min_stars"]:
            dropped["stars"] += 1
            continue
        if cand["lane"] == "hn" and cand["score_or_stars"] < cfg["hn_min_points"]:
            dropped["points"] += 1
            continue
        if cand["lane"] == "artefact":
            reason = _artefact_drop_reason(cand, cfg, now, batch_hashes, content_hashes)
            if reason:
                dropped[reason] += 1
                continue
            batch_hashes.add(cand["content_hash"])
        batch.add(key)
        kept.append(cand)

    kept.sort(key=lambda c: (TIER_RANK[c["tier"]], c["richness_score"]), reverse=True)
    limit = args.limit or cfg["emit_cap"]
    kept = kept[:limit]

    # The marker advances only on a run that proved lane 2 covered this
    # window: at least one HN query came back and none failed. Zero HN hits
    # from queries that came back is a real, covered, empty window and
    # advances; zero because a query errored, or because hn_queries is empty,
    # does NOT - that stretch was never looked at, and moving the marker over
    # it loses it silently and permanently. A run with no stored marker that
    # fails writes no marker either, rather than inventing one.
    held = not report.clean
    today = now.date().isoformat()
    for cand in kept:
        key = _dedup_key(cand)
        # Retrieved and shown to the human, so a re-scan of a held window will
        # not re-surface it; only the never-retrieved rest comes back.
        seen[key] = today
        if cand["lane"] == "artefact":
            content_hashes[cand["content_hash"]] = today
    cutoff = (now - timedelta(days=cfg["seen_retention_days"])).date().isoformat()
    state["seen"] = {k: d for k, d in seen.items() if d >= cutoff}
    state["content_hashes"] = {h: d for h, d in content_hashes.items() if d >= cutoff}
    if held:
        print(
            f"WARN {hold_reason(report)} — keeping last_run so this window "
            "is re-scanned next time",
            file=sys.stderr,
        )
    else:
        state["last_run"] = earned_stamp(state.get("last_run"), since, now)
    write_json_atomic(state_file, state)

    by_tier = {}
    for cand in kept:
        by_tier[cand["tier"]] = by_tier.get(cand["tier"], 0) + 1
    payload = {
        "scanned_at": now.isoformat(),
        "github_pushed_floor": floor_date,
        "hn_window_since": since.isoformat(),
        # True when this run did not earn the marker: lane 2's slice of the
        # digest is incomplete and the same HN window is re-scanned next run.
        "window_held": held,
        "by_tier": by_tier,
        "dropped": dropped,
        "errors": errors,
        "candidates": kept,
    }
    out = resolve_module_path(__file__, cfg["candidates_file"])
    write_json_atomic(out, payload)

    artefact_raw = [c for c in raw if c["lane"] == "artefact"]
    artefact_repos = {c["repo"] for c in artefact_raw}
    print(
        f"TEARDOWN_SWEEP_OK raw={len(raw)} kept={len(kept)} dropped={dropped} "
        f"errors={len(errors)} artefacts={len(artefact_raw)} repos={len(artefact_repos)}"
    )
    print(
        f"tiers: {by_tier.get('high', 0)} high / {by_tier.get('med', 0)} med / "
        f"{by_tier.get('low', 0)} low"
    )
    print(f"candidates -> {out}")
    for err in errors[:5]:
        print(f"WARN {err}", file=sys.stderr)
    return 0


def cmd_mark_covered(args):
    cfg = load_config(args.config)
    _state_dir, _state, ledger_file = state_paths(cfg)
    entry = {
        "date": datetime.now(timezone.utc).isoformat(),
        "url": args.url,
        "note": args.note or "",
    }
    append_ledger(ledger_file, entry)
    print(f"LEDGER_OK {args.url} <- marked covered")
    return 0


def cmd_log(args):
    cfg = load_config(args.config)
    _dir, _state, ledger_file = state_paths(cfg)
    if not ledger_file.exists():
        print("no teardowns recorded yet")
        return 0
    for line in ledger_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        print(f"  {e.get('date', '')[:10]}  {e.get('url', ''):<60} {e.get('note', '')}")
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

    scan = sub.add_parser("scan", help="run all lanes, write candidates JSON")
    scan.add_argument(
        "--days",
        type=int,
        default=None,
        help="lane-2 (HN) window override (default: since last run)",
    )
    scan.add_argument("--limit", type=int, default=None)
    scan.add_argument(
        "--dry-run",
        action="store_true",
        help="preview queries only: no network, no gh, no state writes",
    )
    scan.add_argument(
        "--no-artefacts",
        action="store_true",
        help="skip lane 3 (rate-limited code search) for a quick lanes-1/2 run",
    )
    scan.set_defaults(func=cmd_scan)

    mark = sub.add_parser(
        "mark-covered", help="record a completed teardown subject in the ledger"
    )
    mark.add_argument("--url", required=True, help="the repo or story URL torn down")
    mark.add_argument("--note", default=None)
    mark.set_defaults(func=cmd_mark_covered)

    log = sub.add_parser("log", help="show recorded teardowns")
    log.set_defaults(func=cmd_log)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
