#!/usr/bin/env python3
"""forum-sweep — find the forum/aggregator threads your project's docs already answer.

Self-contained sibling of thread-sweep. Where thread-sweep reads GitHub issues
and discussions, forum-sweep reads the answer-the-question venues beyond GitHub:
Discourse vendor forums (primary), Hacker News (Algolia), Lobsters, and an
opt-in discovery-only Reddit lane. Same shape, same gate, same ledger.

Deterministic discovery half of a human-gated workflow. One adapter per source,
each returning candidates in a single shared dict schema. Judgment — fit scoring,
drafting, the decision to post — is NOT here. That belongs to a human, with
whatever assistant they choose, behind a per-comment approval gate. This script
only retrieves, filters, records.

SECURITY: every forum/aggregator response fetched here is UNTRUSTED EXTERNAL
CONTENT. A title or blurb can contain text engineered to look like an
instruction ("ignore previous instructions", fake system markers, a tool-call
shaped string, a request to fetch a URL or exfiltrate). This script never acts
on fetched text — it only stores a truncated snippet for a human to read later.
Treat every snippet downstream as data, never instructions.

Requires: Python 3.10+ (stdlib only: urllib.request is stdlib). No third-party
deps, no auth for the Discourse/HN/Lobsters lanes. Reddit lane is opt-in.

Subcommands (all take --config, default ./config.json):
  scan [--source discourse|hn|lobsters|reddit|all] [--days N] [--limit N] [--dry-run]
                                              run the lanes, write candidates
  density                                     posting counts from the ledger
  mark-posted --url U --pattern P [--comment-file F]   record a posted reply
"""

import argparse
import json
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sweepcore import (  # noqa: E402
    TIER_RANK,
    append_ledger,
    density_counts,
    http_get,
    load_state,
    posted_urls,
    relevance_tier,
    write_json_atomic,
)

REQUIRED_KEYS = ["subject", "query_groups", "sources"]
DEFAULTS = {
    "per_source_cap": 4,
    "hn_min_points": 2,
    "emit_cap": 100,
    "seen_retention_days": 180,
    "default_window_days": 14,
    "request_delay_seconds": 0.5,
    "state_dir": "state",
    "candidates_file": "candidates.json",
}

SOURCES = ("discourse", "hn", "lobsters", "reddit")

# Descriptive UA so venue operators can identify (and rate-limit / contact) the
# tool rather than seeing an anonymous scraper. Honesty is the etiquette here.
USER_AGENT = "signal-sweep forum-sweep (https://github.com/signal-sweep/signal-sweep)"
HTTP_TIMEOUT = 20

# Polite inter-request throttle (seconds), set from config at scan start.
# Discourse anonymous /search.json and Reddit rate-limit bursts hard (HTTP 429);
# a small delay keeps the primary lanes usable. 0 disables.
REQUEST_DELAY = 0.0


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
    if not isinstance(cfg["query_groups"], dict) or not cfg["query_groups"]:
        sys.exit("config 'query_groups' must be a non-empty object of pattern groups")
    for slug, phrases in cfg["query_groups"].items():
        if not isinstance(phrases, list) or not phrases:
            sys.exit(
                f"config 'query_groups.{slug}' must be a non-empty list of phrases"
            )
    if not isinstance(cfg["sources"], dict):
        sys.exit("config 'sources' must be an object")
    for key, val in DEFAULTS.items():
        cfg.setdefault(key, val)
    cfg.setdefault("thresholds", {})
    # thresholds{} is the published config location; flatten onto top level so
    # both config.example.json shapes (flat or nested) resolve.
    for key in ("per_source_cap", "hn_min_points"):
        if key in cfg["thresholds"]:
            cfg[key] = cfg["thresholds"][key]
    return cfg


def state_paths(cfg):
    state_dir = Path(cfg["state_dir"])
    return (
        state_dir,
        state_dir / "forum_sweep_state.json",
        state_dir / "forum_sweep_log.jsonl",
    )


def make_candidate(
    url, title, created, source, score_or_stars, comments, snippet, pattern, lane
):
    """Single shared schema across all adapters (analogue of thread_sweep
    node_to_candidate). `source` is the instance/site (analogue of repo);
    `score_or_stars` is the venue's notability number (points / upvotes)."""
    body = (snippet or "").replace("\r", " ").replace("\n", " ")
    return {
        "url": url,
        "title": title or "",
        "created": created or "",
        "source": source or "",
        "score_or_stars": score_or_stars or 0,
        "comments": comments or 0,
        # SECURITY: snippet is UNTRUSTED EXTERNAL CONTENT. Stored for a human to
        # read; never interpreted as an instruction by this tool or downstream.
        "snippet": body[:500],
        "pattern": pattern or "",
        "lane": lane or "",
    }


# --- HTTP helper -------------------------------------------------------------


def http_get_json(url, errors, label):
    """GET a URL and parse JSON. Fail-soft: any error appends to errors[] and
    returns None so the caller continues to the next instance/source. Delegates
    the fetch to sweepcore.http_get, which adds 429/503 Retry-After backoff."""
    if REQUEST_DELAY > 0:
        time.sleep(REQUEST_DELAY)
    status, body, err = http_get(
        url,
        timeout=HTTP_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    if err:
        # A network error or non-2xx (403/503 is typically a Cloudflare/login
        # wall on a Discourse instance) — degrade gracefully, don't abort.
        errors.append(f"{label}: {err}")
        return None
    if status != 200:
        errors.append(f"{label}: HTTP {status}")
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        # A login/Cloudflare interstitial returns HTML, not JSON — treat as a
        # soft failure for this instance and move on.
        errors.append(f"{label}: non-JSON response (login/Cloudflare wall?)")
        return None


# --- adapters: one per source, all returning the shared candidate schema -----


def discourse_adapter(cfg, since_dt, errors):
    """PRIMARY lane. For each configured Discourse instance, for each query
    phrase, GET <instance>/search.json?q=<term> and parse the topics array.
    Degrades gracefully on Cloudflare/login/non-200 (errors[], continue)."""
    results = []
    src = cfg["sources"].get("discourse") or {}
    instances = src.get("instances") or []
    for instance in instances:
        host = instance.strip().rstrip("/")
        if not host:
            continue
        if "://" not in host:
            host = "https://" + host
        for pattern, phrases in cfg["query_groups"].items():
            for phrase in phrases:
                q = urllib.parse.quote(phrase)
                url = f"{host}/search.json?q={q}"
                data = http_get_json(url, errors, f"discourse {instance} {pattern}")
                if not data:
                    continue
                topics = data.get("topics")
                if not isinstance(topics, list):
                    continue
                # The human-readable search blurb lives on the sibling posts[]
                # array, keyed by topic_id; topics[] only carry an excerpt on
                # instances configured to include one (many return it empty).
                blurbs = {}
                posts = data.get("posts")
                if isinstance(posts, list):
                    for p in posts:
                        if isinstance(p, dict) and p.get("topic_id") is not None:
                            blurbs.setdefault(p["topic_id"], p.get("blurb", ""))
                for t in topics:
                    if not isinstance(t, dict):
                        continue
                    slug = t.get("slug")
                    tid = t.get("id")
                    if slug and tid:
                        turl = f"{host}/t/{slug}/{tid}"
                    elif tid:
                        turl = f"{host}/t/topic/{tid}"
                    else:
                        continue
                    created = t.get("created_at", "")
                    if not _within_window(created, since_dt):
                        continue
                    results.append(
                        make_candidate(
                            url=turl,
                            title=t.get("title", ""),
                            created=created,
                            source=instance,
                            score_or_stars=t.get("like_count", 0) or t.get("views", 0),
                            comments=t.get("posts_count", 0),
                            snippet=blurbs.get(tid, "")
                            or t.get("blurb", "")
                            or t.get("excerpt", ""),
                            pattern=pattern,
                            lane="discourse",
                        )
                    )
    return results


def hn_adapter(cfg, since_dt, errors):
    """Hacker News via the free Algolia API. One query per phrase over stories
    and comments, numericFilters created_at_i > since-epoch."""
    results = []
    src = cfg["sources"].get("hn") or {}
    if not src.get("enabled", False):
        return results
    since_epoch = int(since_dt.timestamp())
    min_points = cfg.get("hn_min_points", 0)
    for pattern, phrases in cfg["query_groups"].items():
        for phrase in phrases:
            q = urllib.parse.quote(phrase)
            url = (
                "https://hn.algolia.com/api/v1/search_by_date"
                f"?query={q}&tags=(story,comment)"
                f"&numericFilters=created_at_i>{since_epoch}"
            )
            data = http_get_json(url, errors, f"hn {pattern}")
            if not data:
                continue
            for hit in data.get("hits") or []:
                if not isinstance(hit, dict):
                    continue
                obj_id = hit.get("objectID")
                if not obj_id:
                    continue
                raw_points = hit.get("points")
                if raw_points is None:
                    # HN comments carry no points field; they are a core part of
                    # the answerable-question surface (Ask HN replies, design
                    # threads), so the story-points floor must not drop them.
                    points = 0
                else:
                    try:
                        points = int(raw_points)
                    except (TypeError, ValueError):
                        points = 0
                    if points < min_points:
                        continue
                title = hit.get("title") or hit.get("story_title") or ""
                results.append(
                    make_candidate(
                        url=f"https://news.ycombinator.com/item?id={obj_id}",
                        title=title,
                        created=hit.get("created_at", ""),
                        source="news.ycombinator.com",
                        score_or_stars=points,
                        comments=hit.get("num_comments", 0) or 0,
                        snippet=hit.get("story_text") or hit.get("comment_text") or "",
                        pattern=pattern,
                        lane="hn",
                    )
                )
    return results


def lobsters_adapter(cfg, since_dt, errors):
    """Lobsters via GET https://lobste.rs/t/<tag>.json per configured tag.
    Tag-based, not phrase-based — the venue's hottest stories in your tags."""
    results = []
    src = cfg["sources"].get("lobsters") or {}
    tags = src.get("tags") or []
    for tag in tags:
        tag = str(tag).strip()
        if not tag:
            continue
        url = f"https://lobste.rs/t/{urllib.parse.quote(tag)}.json"
        data = http_get_json(url, errors, f"lobsters {tag}")
        if not data:
            continue
        # /t/<tag>.json returns a list of story objects.
        stories = data if isinstance(data, list) else data.get("stories", [])
        for s in stories or []:
            if not isinstance(s, dict):
                continue
            surl = s.get("short_id_url") or s.get("url") or s.get("comments_url")
            if not surl:
                continue
            created = s.get("created_at", "")
            if not _within_window(created, since_dt):
                continue
            results.append(
                make_candidate(
                    url=surl,
                    title=s.get("title", ""),
                    created=created,
                    source="lobste.rs",
                    score_or_stars=s.get("score", 0),
                    comments=s.get("comment_count", 0),
                    snippet=s.get("description_plain") or s.get("description", ""),
                    pattern=f"tag:{tag}",
                    lane="lobsters",
                )
            )
    return results


def reddit_adapter(cfg, since_dt, errors):
    """Reddit — DISCOVERY-ONLY, opt-in (gated behind sources.reddit.enabled,
    default FALSE).

    ToS / safety caveat (read before enabling):
      * Governed by the Reddit Data API terms. This is a best-effort public
        JSON read for DISCOVERY ONLY. Do NOT automate posting through it.
      * Reddit shadowbans are INVISIBLE: a removed comment still looks live to
        the account that posted it. A posted-ledger entry for Reddit can
        therefore be a lie — verify any Reddit post out-of-band (logged-out
        view of the comment) before trusting the ledger.
      * The full Data API is OAuth-gated and pre-approval-gated; this public
        .json read is unauthenticated and may be rate-limited or blocked at any
        time. Fail-soft if so.
    """
    results = []
    src = cfg["sources"].get("reddit") or {}
    if not src.get("enabled", False):
        return results
    # Reddit caps results server-side by the `t` bucket; a fixed t=month
    # silently truncated any window longer than a month while the digest
    # still advertised the full range. Pick the smallest covering bucket.
    t_param = _reddit_time_param(since_dt, datetime.now(timezone.utc))
    subs = src.get("subs") or []
    for sub in subs:
        sub = str(sub).strip()
        if not sub:
            continue
        for pattern, phrases in cfg["query_groups"].items():
            for phrase in phrases:
                q = urllib.parse.quote(phrase)
                url = (
                    f"https://www.reddit.com/r/{urllib.parse.quote(sub)}/search.json"
                    f"?q={q}&restrict_sr=1&sort=new&t={t_param}"
                )
                data = http_get_json(url, errors, f"reddit r/{sub} {pattern}")
                if not data:
                    continue
                children = ((data.get("data") or {}).get("children")) or []
                for child in children:
                    post = child.get("data") or {}
                    permalink = post.get("permalink")
                    if not permalink:
                        continue
                    created = post.get("created_utc")
                    created_iso = ""
                    if created:
                        try:
                            created_ts = float(created)
                        except (TypeError, ValueError):
                            created_ts = None
                        if created_ts is not None:
                            created_dt = datetime.fromtimestamp(
                                created_ts, tz=timezone.utc
                            )
                            if created_dt < since_dt:
                                continue
                            created_iso = created_dt.isoformat()
                    results.append(
                        make_candidate(
                            url=f"https://www.reddit.com{permalink}",
                            title=post.get("title", ""),
                            created=created_iso,
                            source=f"r/{sub}",
                            score_or_stars=post.get("score", 0),
                            comments=post.get("num_comments", 0),
                            snippet=post.get("selftext", ""),
                            pattern=pattern,
                            lane="reddit",
                        )
                    )
    return results


def _reddit_time_param(since_dt, now):
    """Smallest Reddit `t` bucket that covers the scan window."""
    days = (now - since_dt).days
    if days <= 7:
        return "week"
    if days <= 31:
        return "month"
    if days <= 365:
        return "year"
    return "all"


def _within_window(created, since_dt):
    """True if `created` (ISO 8601 string) is at or after since_dt. Missing or
    unparseable timestamps are kept (fail-open on the time filter — the
    seen-store and human gate are the real backstop)."""
    if not created:
        return True
    raw = created.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= since_dt


ADAPTERS = {
    "discourse": discourse_adapter,
    "hn": hn_adapter,
    "lobsters": lobsters_adapter,
    "reddit": reddit_adapter,
}


# --- commands ----------------------------------------------------------------


def cmd_scan(args):
    cfg = load_config(args.config)
    global REQUEST_DELAY
    REQUEST_DELAY = cfg.get("request_delay_seconds", 0.0)
    _dir, state_file, ledger_file = state_paths(cfg)
    state = load_state(state_file)
    now = datetime.now(timezone.utc)
    if args.days is not None:
        since = now - timedelta(days=args.days)
    elif state.get("last_run"):
        since = datetime.fromisoformat(state["last_run"])
    else:
        since = now - timedelta(days=cfg["default_window_days"])
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    since_date = since.strftime("%Y-%m-%d")

    selected = SOURCES if args.source == "all" else (args.source,)

    errors = []
    raw = []
    for name in selected:
        adapter = ADAPTERS[name]
        try:
            raw.extend(adapter(cfg, since, errors))
        except Exception as exc:  # noqa: BLE001
            # One bad source (malformed-but-valid JSON, unexpected shape) must
            # never abort the whole scan and take the other lanes down with it.
            errors.append(f"{name}: adapter crashed: {exc}")

    seen = state.get("seen", {})
    posted = posted_urls(ledger_file)
    kept, dropped = [], {"seen": 0, "posted": 0, "dup": 0}
    batch_urls = set()
    for cand in raw:
        url = cand["url"]
        if url in batch_urls:
            dropped["dup"] += 1
            continue
        if url in posted:
            dropped["posted"] += 1
            continue
        if url in seen:
            dropped["seen"] += 1
            continue
        batch_urls.add(url)
        kept.append(cand)

    for cand in kept:
        cand["tier"] = relevance_tier(cand)
    kept.sort(
        key=lambda c: (TIER_RANK[c["tier"]], c["score_or_stars"], c["created"]),
        reverse=True,
    )
    # Per-source cap (analogue of thread_sweep's per-repo cap): one busy
    # instance/site can't flood the digest.
    per_source, capped = {}, []
    for cand in kept:
        src = cand["source"]
        if per_source.get(src, 0) >= cfg["per_source_cap"]:
            dropped["source_cap"] = dropped.get("source_cap", 0) + 1
            continue
        per_source[src] = per_source.get(src, 0) + 1
        capped.append(cand)
    limit = args.limit or cfg["emit_cap"]
    kept = capped[:limit]

    if not args.dry_run:
        if errors and not raw:
            # Every request failed and nothing came back — advancing last_run
            # now would silently skip this window forever. Keep the old stamp
            # so the next scan re-covers it (the seen-store dedups any overlap).
            print(
                "WARN all sources errored with nothing retrieved — "
                "keeping last_run so this window is re-scanned next time",
                file=sys.stderr,
            )
        else:
            today = now.date().isoformat()
            for cand in kept:
                seen[cand["url"]] = today
            cutoff = (
                (now - timedelta(days=cfg["seen_retention_days"])).date().isoformat()
            )
            state["seen"] = {u: d for u, d in seen.items() if d >= cutoff}
            state["last_run"] = now.isoformat()
            write_json_atomic(state_file, state)

    by_tier = {}
    for cand in kept:
        by_tier[cand["tier"]] = by_tier.get(cand["tier"], 0) + 1
    payload = {
        "scanned_at": now.isoformat(),
        "window_since": since_date,
        "sources": list(selected),
        "posting_density": density_counts(ledger_file),
        "by_tier": by_tier,
        "dropped": dropped,
        "errors": errors,
        "candidates": kept,
    }
    out = Path(cfg["candidates_file"])
    out.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    dens = payload["posting_density"]
    mode = " DRY-RUN (state untouched)" if args.dry_run else ""
    print(
        f"FORUM_SWEEP_OK{mode} window>{since_date} raw={len(raw)} kept={len(kept)} "
        f"dropped={dropped} errors={len(errors)}"
    )
    print(
        f"fit tiers: {by_tier.get('high', 0)} high / {by_tier.get('med', 0)} med / "
        f"{by_tier.get('low', 0)} low"
    )
    print(f"posting density: {dens[30]} in 30d / {dens[90]} in 90d")
    print(f"candidates -> {out}")
    for err in errors[:5]:
        print(f"WARN {err}", file=sys.stderr)
    return 0


def cmd_density(args):
    cfg = load_config(args.config)
    _dir, _state, ledger_file = state_paths(cfg)
    dens = density_counts(ledger_file)
    print(f"posted replies: {dens[30]} in last 30d, {dens[90]} in last 90d")
    return 0


def cmd_mark_posted(args):
    cfg = load_config(args.config)
    _dir, _state, ledger_file = state_paths(cfg)
    comment = ""
    if args.comment_file:
        comment = Path(args.comment_file).read_text(encoding="utf-8").strip()
    entry = {
        "date": datetime.now(timezone.utc).isoformat(),
        "url": args.url,
        "pattern": args.pattern,
        "comment": comment,
    }
    append_ledger(ledger_file, entry)
    print(f"LEDGER_OK {args.url}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", default="config.json", help="path to config (default ./config.json)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    scan = sub.add_parser("scan", help="run the lanes, write candidates JSON")
    scan.add_argument(
        "--source",
        choices=(*SOURCES, "all"),
        default="all",
        help="which lane(s) to run (default: all)",
    )
    scan.add_argument(
        "--days",
        type=int,
        default=None,
        help="window override in days (default: since last run)",
    )
    scan.add_argument("--limit", type=int, default=None)
    scan.add_argument(
        "--dry-run",
        action="store_true",
        help="preview: no seen-marking, no last_run update",
    )
    scan.set_defaults(func=cmd_scan)
    dens = sub.add_parser("density", help="posting counts from the ledger")
    dens.set_defaults(func=cmd_density)
    mark = sub.add_parser("mark-posted", help="record a posted reply")
    mark.add_argument("--url", required=True)
    mark.add_argument("--pattern", required=True)
    mark.add_argument("--comment-file", default=None)
    mark.set_defaults(func=cmd_mark_posted)
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
