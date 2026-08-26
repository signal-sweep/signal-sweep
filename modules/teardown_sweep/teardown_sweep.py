#!/usr/bin/env python3
"""teardown-sweep - find published agent architectures worth a public teardown.

A teardown here means a respectful written analysis of someone's PUBLISHED,
credited work: what's good about the design, what it trades off, what you
would do differently. This module does the deterministic discovery half
only - it never writes the teardown itself. Two lanes:
  lane 1 (gh search):  repos matching architecture-shaped search phrases or
                        topics, scored on README/docs richness
  lane 2 (HN Algolia):  story titles matching the same kind of phrasing,
                        scored on points + title keyword hits

INWARD-FACING, like placement-health: nothing here is outbound. The script
never posts a comment, opens an issue, or contacts anyone - it reads public
search results and public READMEs and writes a ranked reading list. The rest
of this toolkit gates outbound actions behind human approval; this module has
no outbound action to gate. A human reads candidates.json and decides,
entirely outside this tool, whether and how to write a teardown.

SECURITY: every description/README/title fetched here is UNTRUSTED EXTERNAL
CONTENT. It is scanned for configured keywords and stored (truncated) for a
human to read later - never executed, never treated as an instruction.

Read README.md's Etiquette section before writing anything from this list: a
teardown is analysis of work its author chose to publish and be credited for,
not an excuse to dig through anything they did not make public.

Requires: Python 3.10+, an authenticated GitHub CLI (`gh auth login`) for lane
1. Lane 2 needs no auth (public Algolia read).

Subcommands (all take --config; the default is the config.json beside this
script, so the module reads its own state and config from any directory):
  scan [--days N] [--limit N] [--dry-run]   run both lanes, write candidates
  mark-covered --url U [--note ...]         record a completed teardown
  log                                       show recorded teardowns
"""

import argparse
import base64
import binascii
import json
import sys
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

UA = "teardown-sweep (https://github.com/signal-sweep/signal-sweep)"
HN_TIMEOUT = 15


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
    for key in ("search_queries", "topics", "hn_queries", "richness_keywords"):
        if not isinstance(cfg[key], list):
            sys.exit(f"config {key!r} must be a list")
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
    try:
        # GitHub line-wraps the base64 body; b64decode's default validate=False
        # discards the embedded newlines before decoding, so no stripping needed.
        return base64.b64decode(content).decode("utf-8", errors="replace")
    except binascii.Error:
        return ""


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
    # governs the marker. Lane 1's floor is active_within_days back from NOW
    # every run, not from last_run - there is no "stretch since last time" for
    # a gh search error to lose, so lane-1 failures are reported (advisory)
    # but never hold the marker. This is the mirror image of list_sweep, where
    # the query lane governs the marker and the watchlist lane is untimed;
    # here it is the query-shaped HN lane that is windowed and the repo lane
    # that is not.
    report = LaneReport()
    advisory = []
    raw = github_lane(cfg, floor_date, now, advisory) + hn_lane(
        cfg, since_epoch, report
    )

    seen = state.get("seen", {})
    covered = posted_urls(ledger_file)
    errors = list(report) + advisory

    dropped = {"dup": 0, "own": 0, "seen": 0, "covered": 0, "stars": 0, "points": 0}
    kept = []
    batch = set()
    for cand in raw:
        # Seen/dedup key: repo full_name (lowercased) for lane 1, the story
        # URL for lane 2 - both are stable identities a candidate surfaces
        # under exactly once.
        key = cand["repo"].lower() if cand["lane"] == "github" else cand["url"]
        if key in batch:
            dropped["dup"] += 1
            continue
        if cand["lane"] == "github" and is_own_repo(cand["repo"], cfg["own_repos"]):
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
        key = cand["repo"].lower() if cand["lane"] == "github" else cand["url"]
        # Retrieved and shown to the human, so a re-scan of a held window will
        # not re-surface it; only the never-retrieved rest comes back.
        seen[key] = today
    cutoff = (now - timedelta(days=cfg["seen_retention_days"])).date().isoformat()
    state["seen"] = {k: d for k, d in seen.items() if d >= cutoff}
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

    print(
        f"TEARDOWN_SWEEP_OK raw={len(raw)} kept={len(kept)} dropped={dropped} "
        f"errors={len(errors)}"
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

    scan = sub.add_parser("scan", help="run both lanes, write candidates JSON")
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
