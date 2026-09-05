#!/usr/bin/env python3
"""forum-sweep — find the forum/aggregator threads your project's docs already answer.

Self-contained sibling of thread-sweep. Where thread-sweep reads GitHub issues
and discussions, forum-sweep reads the answer-the-question venues beyond GitHub:
Discourse vendor forums (primary), Hacker News (Algolia), Lobsters, an opt-in
discovery-only Reddit lane, a thin opt-in Stack Exchange lane, an opt-in dev.to
(Forem) lane, an opt-in Medium (RSS-by-tag) lane, and an opt-in Lemmy lane.
Same shape, same gate, same ledger.

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

Requires: Python 3.10+ (stdlib only: urllib.request and xml.etree.ElementTree
are both stdlib). No third-party deps, no auth for the Discourse/HN/Lobsters
lanes. Reddit, Stack Exchange, dev.to, Medium, and Lemmy lanes are opt-in.

Subcommands (all take --config; the default is the config.json beside this
script, so the module reads its own state and config from any directory):
  scan [--source discourse|hn|lobsters|reddit|stackexchange|devto|medium|lemmy|all]
       [--days N] [--limit N] [--dry-run]   run the lanes, write candidates
  density                                     posting counts from the ledger
  mark-posted --url U --pattern P [--comment-file F]   record a posted reply
"""

import argparse
import html
import json
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sweepcore import (  # noqa: E402
    TIER_RANK,
    LaneReport,
    append_ledger,
    density_counts,
    earned_stamp,
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

SOURCES = (
    "discourse",
    "hn",
    "lobsters",
    "reddit",
    "stackexchange",
    "devto",
    "medium",
    "lemmy",
)

# Descriptive UA so venue operators can identify (and rate-limit / contact) the
# tool rather than seeing an anonymous scraper. Honesty is the etiquette here.
USER_AGENT = "signal-sweep forum-sweep (https://github.com/signal-sweep/signal-sweep)"
HTTP_TIMEOUT = 20

# Polite inter-request throttle (seconds), set from config at scan start.
# Discourse anonymous /search.json rate-limits bursts hard (HTTP 429); a small
# delay keeps the primary lanes usable. 0 disables. Two lanes pace themselves
# above this floor because they 429 sooner than the rest, and only those lanes
# pay the cost: reddit (REDDIT_MIN_REQUEST_DELAY) and discourse
# (DISCOURSE_MIN_REQUEST_DELAY).
REQUEST_DELAY = 0.0

# Pacing floor for the discourse lane, held PER HOST. Anonymous /search.json
# is rate-limited per instance, and an unpaced sweep 429s across every
# configured instance at once (observed 2026-09-05 on forum.cursor.com,
# community.openai.com and discuss.huggingface.co), which holds the window for
# the module's primary lane run after run -- the held stamp becomes the normal
# outcome rather than the exception.
#
# Per host, not module-wide, because the limit is per instance. The lane
# iterates instance x phrase, so the time spent reading one instance is time
# the next one has already waited; charging every request the full floor would
# slow the sweep by the number of instances for no extra politeness.
DISCOURSE_MIN_REQUEST_DELAY = 1.0

# host -> time.monotonic() of the last request this process sent it. Only the
# per-host lanes touch it; a module-wide throttle needs no memory.
_LAST_REQUEST_AT = {}


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
    # Module-anchored, not CWD-anchored: this module has exactly one canonical
    # state dir wherever it is invoked from. See sweepcore.resolve_module_path.
    state_dir = resolve_module_path(__file__, cfg["state_dir"])
    return (
        state_dir,
        state_dir / "forum_sweep_state.json",
        state_dir / "forum_sweep_log.jsonl",
    )


def migrate_state(state):
    """Normalise state onto per-source last_run markers, in place.

    A single shared `last_run` meant a one-source scan (`--source hn`) advanced
    the window for all four lanes, so the three that never ran silently lost
    everything published in the gap. Markers are per source instead.

    The migration seeds every source with the old shared value. Dropping back to
    the default window here would re-window the scan in one direction or the
    other, which is the damage the shared marker already did — the whole point
    of migrating is that no lane loses (or re-covers) an unintended stretch.
    """
    by_source = state.get("last_run_by_source")
    if not isinstance(by_source, dict):
        by_source = {}
    legacy = state.pop("last_run", None)
    if legacy:
        for name in SOURCES:
            by_source.setdefault(name, legacy)
    state["last_run_by_source"] = by_source
    return state


def _since_for_source(name, state, cfg, now, days_override):
    """Window start for one source: an explicit --days override, else that
    source's own last_run, else the first-run default window. The marker rule
    itself lives in sweepcore.window_start; this only names the source."""
    return window_start(
        state.get("last_run_by_source", {}).get(name),
        cfg["default_window_days"],
        now,
        days_override,
        label=name,
    )


def _earned_stamp(name, state, since_by_source, now):
    """The last_run this source's cleanly-fetched lane earns — `now` only when
    the run reached back to the source's previous marker. Rule and rationale:
    sweepcore.earned_stamp, which every scanning module shares."""
    return earned_stamp(
        state.get("last_run_by_source", {}).get(name), since_by_source[name], now
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


def _pace_host(host, floor):
    """Sleep only as long as this host is still owed.

    A module-wide throttle charges every request the full delay. That is right
    for a global politeness setting and wrong for a per-instance rate limit,
    because time spent on OTHER hosts in between is real time this host's limit
    already counted. So the wait is the floor minus whatever has elapsed since
    this host was last read, and first contact waits not at all: there is no
    earlier request to space it from.
    """
    if floor <= 0:
        return
    previous = _LAST_REQUEST_AT.get(host)
    if previous is not None:
        wait = floor - (time.monotonic() - previous)
        if wait > 0:
            time.sleep(wait)
    _LAST_REQUEST_AT[host] = time.monotonic()


def http_get_json(url, errors, label, host=None, delay=None):
    """GET a URL and parse JSON. Fail-soft: any error appends to errors[] and
    returns None so the caller continues to the next instance/source. Delegates
    the fetch to sweepcore.http_get, which adds 429/503 Retry-After backoff.

    `delay` raises the throttle for this one call, so a lane with a tighter
    rate limit than the rest of the module can pace itself without the operator
    slowing every other lane to match (the same contract http_get_xml offers
    the reddit lane). `host` makes that wait per host rather than module-wide,
    for a limit enforced per instance. Only the discourse lane passes either
    today; every other caller keeps the module-wide REQUEST_DELAY exactly as
    before.
    """
    floor = REQUEST_DELAY if delay is None else max(REQUEST_DELAY, delay)
    if host is not None:
        _pace_host(host, floor)
    elif floor > 0:
        time.sleep(floor)
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
        parsed = json.loads(body)
    except json.JSONDecodeError:
        # A login/Cloudflare interstitial returns HTML, not JSON — treat as a
        # soft failure for this instance and move on.
        errors.append(f"{label}: non-JSON response (login/Cloudflare wall?)")
        return None
    note_fetch_ok(errors)
    return parsed


def http_get_xml(url, errors, label, delay=None):
    """GET a URL and parse XML. Fail-soft, the same shape as http_get_json:
    any error appends to errors[] and returns None so the caller continues to
    the next tag/instance. Delegates the fetch to sweepcore.http_get, which
    adds 429/503 Retry-After backoff.

    `delay` overrides the module-wide REQUEST_DELAY throttle for this one call,
    so a lane with a tighter rate limit than the rest of the module can pace
    itself without the operator having to slow every other lane to match. Only
    the reddit lane passes it today (see REDDIT_MIN_REQUEST_DELAY).
    """
    wait = REQUEST_DELAY if delay is None else delay
    if wait > 0:
        time.sleep(wait)
    status, body, err = http_get(
        url,
        timeout=HTTP_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, text/xml"},
    )
    if err:
        errors.append(f"{label}: {err}")
        return None
    if status != 200:
        errors.append(f"{label}: HTTP {status}")
        return None
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        # A Cloudflare/error interstitial returns HTML, not RSS -- the same
        # soft-fail shape as http_get_json's non-JSON branch above.
        errors.append(f"{label}: unparseable XML ({exc})")
        return None
    note_fetch_ok(errors)
    return root


# --- per-lane query-group selection ------------------------------------------


def _lane_query_groups(cfg, lane):
    """The query_groups a phrase-driven lane iterates.

    `sources.<lane>.groups` optionally narrows one lane to a subset of the
    shared query_groups. Every phrase lane costs one request per sub/site/
    instance x phrase, so a lane with a hard request budget (reddit's anonymous
    429 threshold) needs a way to stay inside it that does not shrink
    query_groups for the lanes with no such limit. Absent, empty, or not a list
    means all groups -- the behaviour every lane had before this key existed.

    An unknown slug is a config typo rather than a silent no-op: it warns once
    on stderr and is skipped, so a mistyped group cannot quietly turn a lane
    into a narrower scan than the operator thinks they configured.
    """
    groups = cfg["query_groups"]
    wanted = ((cfg.get("sources") or {}).get(lane) or {}).get("groups")
    if not isinstance(wanted, list) or not wanted:
        return groups
    selected = {}
    for slug in wanted:
        slug = str(slug).strip()
        if slug in groups:
            selected[slug] = groups[slug]
        else:
            print(
                f"WARN {lane}: sources.{lane}.groups names '{slug}', which is not "
                "a query_groups slug. Skipping it.",
                file=sys.stderr,
            )
    return selected


# --- adapters: one per source, all returning the shared candidate schema -----


def discourse_adapter(cfg, since_dt, errors):
    """PRIMARY lane. For each configured Discourse instance, for each query
    phrase, GET <instance>/search.json?q=<term> and parse the topics array.
    Degrades gracefully on Cloudflare/login/non-200 (errors[], continue).

    Paced per host at DISCOURSE_MIN_REQUEST_DELAY: anonymous search is
    rate-limited per instance, and an unpaced burst 429s the lane into a held
    window."""
    results = []
    src = cfg["sources"].get("discourse") or {}
    instances = src.get("instances") or []
    groups = _lane_query_groups(cfg, "discourse")
    for instance in instances:
        host = instance.strip().rstrip("/")
        if not host:
            continue
        if "://" not in host:
            host = "https://" + host
        for pattern, phrases in groups.items():
            for phrase in phrases:
                q = urllib.parse.quote(phrase)
                url = f"{host}/search.json?q={q}"
                data = http_get_json(
                    url,
                    errors,
                    f"discourse {instance} {pattern}",
                    host=host,
                    delay=DISCOURSE_MIN_REQUEST_DELAY,
                )
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
    for pattern, phrases in _lane_query_groups(cfg, "hn").items():
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


# Reddit's search feed is Atom, with every element in the Atom default
# namespace (confirmed live 2026-08-29: the <feed> root declares Atom plus
# a media: prefix this adapter does not read).
_REDDIT_ATOM_NS = "{http://www.w3.org/2005/Atom}"

# Pacing floor for the reddit lane only. The anonymous feed read starts
# returning HTTP 429 after roughly 20 quick requests (observed 2026-08-29),
# far sooner than any other lane in this module, and one lane's limit is no
# reason to slow the others -- so this floors reddit's own pace while
# request_delay_seconds keeps governing everything else. Paired with the
# sources.reddit.groups whitelist, which caps how many requests the lane
# makes at all (subs x phrases in the selected groups).
REDDIT_MIN_REQUEST_DELAY = 2.0

_WS_RE = re.compile(r"\s+")


def _collapse_ws(text):
    """Squeeze runs of whitespace to single spaces. Reddit's entry content is
    pretty-printed HTML, so stripping its tags leaves ragged indentation;
    make_candidate flattens newlines but keeps the runs."""
    return _WS_RE.sub(" ", text or "").strip()


def _atom_iso(stamp):
    """Normalise an Atom timestamp to ISO-8601 through one parse.

    Reddit already emits ISO (`2026-08-29T12:01:04+00:00`, confirmed live), so
    this is not a conversion the way _rfc822_to_iso is for Medium -- it is
    shape insurance. cmd_scan ranks on `created` as a STRING, and a bare `Z`
    suffix sorts apart from the identical `+00:00` instant. Fails open to ""
    on anything unparseable, which _within_window then keeps, matching the
    fail-open contract every other lane's timestamps have.
    """
    if not stamp:
        return ""
    try:
        dt = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _reddit_entry_to_candidate(entry, sub, pattern, since_dt):
    """One Atom <entry> to the shared candidate schema, or None to skip it.

    Defensive on every field: this is untrusted external content, and a single
    entry missing a link or carrying an unreadable timestamp must cost that
    entry only, never the rest of the sub's results.
    """
    link = entry.find(f"{_REDDIT_ATOM_NS}link")
    # The permalink is an href attribute, not element text, and it arrives
    # clean -- no tracking query string to strip the way Medium's links need.
    href = link.get("href") if link is not None else None
    if not href:
        return None
    created = _atom_iso(
        entry.findtext(f"{_REDDIT_ATOM_NS}published")
        or entry.findtext(f"{_REDDIT_ATOM_NS}updated")
        or ""
    )
    if not _within_window(created, since_dt):
        return None
    return make_candidate(
        url=href,
        title=(entry.findtext(f"{_REDDIT_ATOM_NS}title") or "").strip(),
        created=created,
        source=f"r/{sub}",
        # The feed carries no score and no comment count -- see the adapter
        # docstring for what that costs the ranking.
        score_or_stars=0,
        comments=0,
        # <content type="html"> holds entity-encoded markup; _clean_se_excerpt
        # unescapes then strips tags, the same pass the SE/Medium/Lemmy lanes
        # already share.
        snippet=_collapse_ws(
            _clean_se_excerpt(entry.findtext(f"{_REDDIT_ATOM_NS}content") or "")
        ),
        pattern=pattern,
        lane="reddit",
    )


def reddit_adapter(cfg, since_dt, errors):
    """Reddit — DISCOVERY-ONLY, opt-in (gated behind sources.reddit.enabled,
    default FALSE).

    Transport is the public per-subreddit Atom feed:
    GET /r/<sub>/search.rss?q=<phrase>&restrict_sr=1&sort=new&t=<bucket>.
    The .json read this lane used through v0.4.0 is 403-walled as of 2026-08
    (a hard HTTP 403 for non-browser user agents; old.reddit.com answers the
    same query with a 302 to a login wall), while the .rss form returns 200
    for this module's own descriptive UA. Same query, same `t` bucket, parsed
    with the stdlib ElementTree like the Medium lane.

    What the transport costs: an Atom entry carries NO score and NO comment
    count, so every candidate here is emitted with score_or_stars=0 and
    comments=0. relevance_tier reads comments==0 as an answer-gap signal, so
    every reddit candidate scores that signal identically. Being uniform, it
    cannot reorder anything WITHIN the lane; it only lifts reddit against the
    lanes that do carry real counts. Read a reddit tier as "unranked by
    engagement", and read the thread itself before judging the answer-gap.

    ToS / safety caveat (read before enabling):
      * Governed by the Reddit Data API terms. This is a best-effort public
        feed read for DISCOVERY ONLY. Do NOT automate posting through it.
      * Reddit shadowbans are INVISIBLE: a removed comment still looks live to
        the account that posted it. A posted-ledger entry for Reddit can
        therefore be a lie — verify any Reddit post out-of-band (logged-out
        view of the comment) before trusting the ledger.
      * The full Data API is OAuth-gated and pre-approval-gated; this public
        feed read is unauthenticated and rate-limited (see
        REDDIT_MIN_REQUEST_DELAY) and may be blocked at any time. Fail-soft
        if so: a 429 or an unparseable body is a lane error like any other,
        which holds this lane's window for the next run.
    """
    results = []
    src = cfg["sources"].get("reddit") or {}
    if not src.get("enabled", False):
        return results
    # Reddit caps results server-side by the `t` bucket; a fixed t=month
    # silently truncated any window longer than a month while the digest
    # still advertised the full range. Pick the smallest covering bucket.
    t_param = _reddit_time_param(since_dt, datetime.now(timezone.utc))
    delay = max(REQUEST_DELAY, REDDIT_MIN_REQUEST_DELAY)
    groups = _lane_query_groups(cfg, "reddit")
    subs = src.get("subs") or []
    for sub in subs:
        sub = str(sub).strip()
        if not sub:
            continue
        for pattern, phrases in groups.items():
            for phrase in phrases:
                q = urllib.parse.quote(phrase)
                url = (
                    f"https://www.reddit.com/r/{urllib.parse.quote(sub)}/search.rss"
                    f"?q={q}&restrict_sr=1&sort=new&t={t_param}"
                )
                root = http_get_xml(
                    url, errors, f"reddit r/{sub} {pattern}", delay=delay
                )
                if root is None:
                    continue
                for entry in root.findall(f"{_REDDIT_ATOM_NS}entry"):
                    cand = _reddit_entry_to_candidate(entry, sub, pattern, since_dt)
                    if cand is not None:
                        results.append(cand)
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


# Sites that predate the unified <slug>.stackexchange.com domain keep their own
# top-level domain; every site created since uses the .stackexchange.com form.
# Small and effectively frozen — SE stopped minting new vanity domains after
# the Area 51 era. Covers both shipped config.example.json defaults
# ("stackoverflow", "ai" -> ai.stackexchange.com) plus the other long-standing
# flagship sites, so an operator who adds one more common site still gets a
# working link.
SE_VANITY_DOMAINS = {
    "stackoverflow": "stackoverflow.com",
    "serverfault": "serverfault.com",
    "superuser": "superuser.com",
    "askubuntu": "askubuntu.com",
    "mathoverflow": "mathoverflow.net",
    "stackapps": "stackapps.com",
}


def _se_site_url(site):
    """Base https URL for an SE API `site` slug. filter=default (used below)
    carries no `link` field to read this from, so it is built from the slug
    via the /q/<id> and /a/<id> short-permalink convention SE's own UI
    generates, which works site-wide on every Stack Exchange property."""
    return f"https://{SE_VANITY_DOMAINS.get(site, f'{site}.stackexchange.com')}"


def _clean_se_excerpt(text):
    """SE wraps matched search terms in <span class="highlight"> and
    HTML-entity-encodes the rest (confirmed live: e.g. &#39;, &hellip;);
    strip both so the snippet reads as plain text like every other lane's."""
    return re.sub(r"<[^>]+>", "", html.unescape(text or ""))


def stackexchange_adapter(cfg, since_dt, errors):
    """Stack Exchange — THIN adapter, opt-in (gated behind
    sources.stackexchange.enabled, default FALSE). Per ROADMAP.md: a dedicated
    stack-sweep module was dropped as not worth building on its own (Stack
    Overflow's public-question volume is down roughly 95% off its peak, and
    the venues that absorbed the spillover are already covered by thread-sweep
    and forum-sweep), but a thin adapter here still catches the residual
    long tail.

    For each enabled site, each existing query_groups phrase runs against
    GET /2.3/search/excerpts?q=<phrase>&site=<site>&sort=creation&order=desc
    &fromdate=<since-epoch>&filter=default. `fromdate` is honoured
    server-side (confirmed live), the same shape as hn_adapter's
    numericFilters, so this windows without a local _within_window recheck.
    `sort=creation` rather than the illustrative `sort=activity`, so results
    are ordered by the same creation-time semantics every other lane windows
    on. item_type is "question" or "answer"; both carry a `title` (the parent
    question's, for an answer excerpt) and an `is_answered` flag for the
    parent question, which relevance_tier (sweepcore) already reads as an
    answer-gap signal — no adapter-side ranking needed.

    ToS / quota caveat (read before enabling — mirrored in config.example.json):
      * The anonymous IP quota is small (roughly 300 requests/day, confirmed
        live, shared across every site queried). A response can carry a
        `backoff` field demanding N seconds before the next request. Honoured
        below by sleeping; surfaced as a stderr NOTE rather than a LaneReport
        entry, because a throttled-but-successful fetch is not the failure
        PR #21's earned-marker fix guards against — the request still came
        back with real data, so marking the lane unclean would hold its stamp
        over a window this run actually covered.
      * Stack Overflow's own policy prohibits AI-generated answer content.
        This lane is discovery recall only, same as every other lane in this
        module: any reply a human chooses to write there must be genuinely
        human-authored and compliant with that policy.
    """
    results = []
    src = cfg["sources"].get("stackexchange") or {}
    if not src.get("enabled", False):
        return results
    sites = src.get("sites") or []
    min_score = src.get("min_score", 0)
    since_epoch = int(since_dt.timestamp())
    groups = _lane_query_groups(cfg, "stackexchange")
    for site in sites:
        site = str(site).strip()
        if not site:
            continue
        for pattern, phrases in groups.items():
            for phrase in phrases:
                q = urllib.parse.quote(phrase)
                url = (
                    "https://api.stackexchange.com/2.3/search/excerpts"
                    f"?order=desc&sort=creation&fromdate={since_epoch}"
                    f"&q={q}&site={site}&filter=default"
                )
                data = http_get_json(url, errors, f"stackexchange {site} {pattern}")
                if not data:
                    continue
                backoff = data.get("backoff")
                if backoff:
                    try:
                        wait_s = float(backoff)
                    except (TypeError, ValueError):
                        wait_s = 0.0
                    if wait_s > 0:
                        print(
                            f"NOTE stackexchange {site}: backoff {wait_s}s "
                            "requested by the API",
                            file=sys.stderr,
                        )
                        time.sleep(wait_s)
                items = data.get("items")
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    raw_score = item.get("score")
                    try:
                        score = int(raw_score) if raw_score is not None else 0
                    except (TypeError, ValueError):
                        score = 0
                    if score < min_score:
                        continue
                    answer_id = item.get("answer_id")
                    question_id = item.get("question_id")
                    if item.get("item_type") == "answer" and answer_id:
                        turl = f"{_se_site_url(site)}/a/{answer_id}"
                    elif question_id:
                        turl = f"{_se_site_url(site)}/q/{question_id}"
                    else:
                        continue
                    raw_created = item.get("creation_date")
                    try:
                        created = datetime.fromtimestamp(
                            int(raw_created), tz=timezone.utc
                        ).isoformat()
                    except (TypeError, ValueError, OSError, OverflowError):
                        created = ""
                    cand = make_candidate(
                        url=turl,
                        title=html.unescape(item.get("title") or ""),
                        created=created,
                        source=site,
                        score_or_stars=score,
                        comments=item.get("answer_count", 0) or 0,
                        snippet=_clean_se_excerpt(item.get("excerpt", "")),
                        pattern=pattern,
                        lane="stackexchange",
                    )
                    cand["is_answered"] = bool(item.get("is_answered"))
                    results.append(cand)
    return results


# A relevance FLOOR against a tag-wide feed, not a ranking model — ranking
# already happens centrally in cmd_scan via relevance_tier. 2 shared tokens is
# deliberately simple: this module's query_groups phrases are multi-word by
# convention (config.example.json has none under 2 words), so the floor is a
# real filter against generic tag noise, not a coin flip on any one word.
# Reused as-is by the medium lane below (also a tag-wide fetch) through the
# same _token_overlap_pattern call, not a second constant.
DEVTO_MIN_TOKEN_OVERLAP = 2


def _token_overlap_pattern(text, query_groups):
    """Cheap, deterministic keyword relevance: the query_groups phrase that
    shares the most whole-word tokens with `text`. No stemming, no scoring
    cleverness. Returns the best-matching pattern slug, or None if nothing
    clears DEVTO_MIN_TOKEN_OVERLAP shared tokens."""
    text_tokens = set(re.findall(r"[a-z0-9]+", text.lower()))
    best_pattern, best_overlap = None, 0
    for pattern, phrases in query_groups.items():
        for phrase in phrases:
            phrase_tokens = set(re.findall(r"[a-z0-9]+", phrase.lower()))
            overlap = len(phrase_tokens & text_tokens)
            if overlap > best_overlap:
                best_pattern, best_overlap = pattern, overlap
    return best_pattern if best_overlap >= DEVTO_MIN_TOKEN_OVERLAP else None


# dev.to's own default page size, pinned explicitly rather than relying on the
# API's implicit default so a future Forem change can't silently shrink recall.
DEVTO_PER_PAGE = 30


def devto_adapter(cfg, since_dt, errors):
    """dev.to (Forem) — DISCOVERY-ONLY, opt-in (gated behind
    sources.devto.enabled, default FALSE).

    GET /api/articles?tag=<tag>&per_page=<n> per configured tag: the
    documented, stable public lane. /api/search/feed_content (undocumented)
    404s live as of this build, so the tag lane is used instead of a search
    lane. One page per tag, like every other adapter in this module — no
    pagination.

    Windowed locally by `published_at` (no server-side date filter on this
    endpoint), same shape as discourse/lobsters. A tag alone is broad (e.g.
    "ai" pulls unrelated posts), so results are also filtered through the
    existing query_groups phrases via a cheap token-overlap floor
    (_token_overlap_pattern) rather than kept on tag membership alone.

    Etiquette: same discovery-only gate as every other lane. dev.to comment
    etiquette parallels the rest of the set: the reply must stand alone, and
    drive-by link-drops burn the account (see the module README).
    """
    results = []
    src = cfg["sources"].get("devto") or {}
    if not src.get("enabled", False):
        return results
    tags = src.get("tags") or []
    min_reactions = src.get("min_reactions", 0)
    for tag in tags:
        tag = str(tag).strip()
        if not tag:
            continue
        url = (
            "https://dev.to/api/articles"
            f"?tag={urllib.parse.quote(tag)}&per_page={DEVTO_PER_PAGE}"
        )
        data = http_get_json(url, errors, f"devto {tag}")
        if not data or not isinstance(data, list):
            continue
        for article in data:
            if not isinstance(article, dict):
                continue
            published = article.get("published_at", "")
            if not _within_window(published, since_dt):
                continue
            reactions = article.get("positive_reactions_count", 0) or 0
            if reactions < min_reactions:
                continue
            aurl = article.get("url")
            if not aurl:
                continue
            title = article.get("title") or ""
            desc = article.get("description") or ""
            pattern = _token_overlap_pattern(f"{title} {desc}", cfg["query_groups"])
            if not pattern:
                continue
            results.append(
                make_candidate(
                    url=aurl,
                    title=title,
                    created=published,
                    source="dev.to",
                    score_or_stars=reactions,
                    comments=article.get("comments_count", 0),
                    snippet=desc,
                    pattern=pattern,
                    lane="devto",
                )
            )
    return results


# --- Medium RSS namespaces ----------------------------------------------------
# Confirmed live 2026-08-26 against https://medium.com/feed/tag/<tag>: the feed
# root declares dc/content/atom/cc. dc:creator and content:encoded are the two
# this adapter reads; content:encoded did not appear on any live item in this
# build (Medium's tag feed only ships a <description> teaser today), but the
# element is still looked up by its proper namespaced name so a fuller item
# would be read correctly rather than silently skipped if Medium ever ships one.
_MEDIUM_DC_NS = "{http://purl.org/dc/elements/1.1/}"
_MEDIUM_CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"


def _rfc822_to_iso(pubdate):
    """Medium's <pubDate> is RFC 822 (`Wed, 26 Aug 2026 06:16:01 GMT`), unlike
    every other lane's already-ISO timestamp. Convert once so `created` stores
    the same sortable ISO-8601 shape the rest of the candidate set uses, and
    _within_window (which expects ISO) windows it correctly. Fails open (empty
    string) on anything that won't parse -- the same fail-open contract
    _within_window already has for a missing or unparseable created field."""
    if not pubdate:
        return ""
    try:
        dt = parsedate_to_datetime(pubdate)
    except (TypeError, ValueError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _strip_query(url):
    """Medium article links carry a tracking query string
    (`?source=rss------<tag>-5`, confirmed live), so the same article surfaced
    from two tags -- or from the same tag on two different runs -- would
    otherwise produce two different URLs. The seen-store and ledger key on the
    bare url, so a stable url is what makes dedup actually work."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def medium_adapter(cfg, since_dt, errors):
    """Medium -- DISCOVERY-ONLY, opt-in (gated behind sources.medium.enabled,
    default FALSE).

    GET /feed/tag/<tag> per configured tag: an RSS 2.0 feed, no auth, no
    pagination (confirmed live: roughly 4-10 items per tag, no documented
    paging param). Parsed with the stdlib xml.etree.ElementTree instead of
    json -- see the namespace constants above for the dc:creator /
    content:encoded lookups.

    Windowed locally against <pubDate> (RFC 822, converted by _rfc822_to_iso
    since every other lane's `created` is already ISO). A tag alone is broad,
    so results are also floored through the existing query_groups phrases via
    the same cheap token-overlap check devto uses (_token_overlap_pattern,
    reused rather than re-implemented) -- the category tags folded into that
    check are real signal here, since Medium's own preview snippet is often
    just "Continue reading on Medium" with no real content to score against.

    Medium article links carry a `?source=rss...` tracking query string;
    _strip_query drops it before the link becomes the candidate/seen-store
    key, so the same article does not resurface every time the tracking
    suffix changes.

    Etiquette: DISCOVERY only in a stronger sense than the HTTP lanes above --
    a Medium response (comment) is a manual human act on medium.com with no
    public reply API this module could call even if the project's posting
    gate allowed it. The value here is knowing which posts are pulling the
    conversation in your patterns, to feed replies on other lanes and
    outreach decisions, not to reply on Medium itself.
    """
    results = []
    src = cfg["sources"].get("medium") or {}
    if not src.get("enabled", False):
        return results
    tags = src.get("tags") or []
    for tag in tags:
        tag = str(tag).strip()
        if not tag:
            continue
        url = f"https://medium.com/feed/tag/{urllib.parse.quote(tag)}"
        root = http_get_xml(url, errors, f"medium {tag}")
        if root is None:
            continue
        channel = root.find("channel")
        items = channel.findall("item") if channel is not None else []
        for item in items:
            link = item.findtext("link")
            if not link:
                continue
            title = item.findtext("title") or ""
            created = _rfc822_to_iso(item.findtext("pubDate") or "")
            if not _within_window(created, since_dt):
                continue
            raw_snippet = (
                item.findtext(f"{_MEDIUM_CONTENT_NS}encoded")
                or item.findtext("description")
                or ""
            )
            categories = [c.text or "" for c in item.findall("category")]
            pattern = _token_overlap_pattern(
                f"{title} {raw_snippet} {' '.join(categories)}", cfg["query_groups"]
            )
            if not pattern:
                continue
            snippet = _clean_se_excerpt(raw_snippet)
            creator = item.findtext(f"{_MEDIUM_DC_NS}creator") or ""
            if creator:
                snippet = f"{creator}: {snippet}" if snippet else creator
            results.append(
                make_candidate(
                    url=_strip_query(link),
                    title=title,
                    created=created,
                    source="medium.com",
                    score_or_stars=0,
                    comments=0,
                    snippet=snippet,
                    pattern=pattern,
                    lane="medium",
                )
            )
    return results


# Lemmy's own upper bound on `limit` (confirmed live 2026-08-26 against
# programming.dev: 100 -> HTTP 400, 50 -> HTTP 200); pinned comfortably under
# that so a request never 400s regardless of exactly where between 51-99 the
# real ceiling sits.
LEMMY_SEARCH_LIMIT = 20

_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_MARKUP_RE = re.compile(r"[*_`#>~]+")


def _strip_markdown(text):
    """Cheap plain-text-ification of a Lemmy post body (CommonMark, per the
    API docs): collapse [label](url) links down to the label, drop the common
    emphasis/heading/quote/strikethrough markup characters. Not a parser --
    good enough for a truncated snippet a human skims in the digest, not a
    renderer. Composed with _clean_se_excerpt in the adapter below for the
    rarer case of raw HTML, which Lemmy's markdown also permits inline."""
    if not text:
        return ""
    text = _MD_LINK_RE.sub(r"\1", text)
    return _MD_MARKUP_RE.sub("", text)


def lemmy_adapter(cfg, since_dt, errors):
    """Lemmy -- DISCOVERY-ONLY, opt-in (gated behind sources.lemmy.enabled,
    default FALSE).

    For each configured instance, each existing query_groups phrase runs
    against GET /api/v3/search?q=<phrase>&type_=Posts&sort=New&limit=<n> --
    the same per-instance x per-phrase shape stackexchange uses for its
    sites. `sort=New` is confirmed live to work and matters here: the search
    has no server-side date filter, so without it a broad phrase can fill the
    request's `limit` with old top-ranked posts and never reach anything
    recent enough to matter. Windowed locally against `created` (the post's
    `published`, already ISO-8601 with a literal Z -- _within_window's
    existing Z-handling parses it with no adapter-side conversion needed).

    The candidate URL is the post's LOCAL permalink (`https://<instance>
    /post/<id>`), not `post.url` (whatever external link the post submits,
    often unrelated to the instance queried) and not `ap_id` (confirmed live:
    for a federated post, ap_id points at the ORIGIN instance -- e.g.
    lemmy.bestiver.se -- not the instance actually queried here). ap_id is
    still kept on the candidate in a spare field since it is already on hand
    and cheap to carry.

    Small, federated network: an unreachable or slow instance is exactly the
    kind of soft failure http_get_json already degrades on (errors[], skip,
    move to the next instance) -- a held window for that instance next run,
    not a crash of the whole lane.
    """
    results = []
    src = cfg["sources"].get("lemmy") or {}
    if not src.get("enabled", False):
        return results
    instances = src.get("instances") or []
    min_score = src.get("min_score", 0)
    groups = _lane_query_groups(cfg, "lemmy")
    for instance in instances:
        instance = str(instance).strip().rstrip("/")
        if not instance:
            continue
        for pattern, phrases in groups.items():
            for phrase in phrases:
                q = urllib.parse.quote(phrase)
                url = (
                    f"https://{instance}/api/v3/search?q={q}&type_=Posts"
                    f"&sort=New&limit={LEMMY_SEARCH_LIMIT}"
                )
                data = http_get_json(url, errors, f"lemmy {instance} {pattern}")
                if not data:
                    continue
                posts = data.get("posts")
                if not isinstance(posts, list):
                    continue
                for entry in posts:
                    if not isinstance(entry, dict):
                        continue
                    post = entry.get("post")
                    if not isinstance(post, dict):
                        continue
                    post_id = post.get("id")
                    if post_id is None:
                        continue
                    created = post.get("published", "")
                    if not _within_window(created, since_dt):
                        continue
                    counts = entry.get("counts") or {}
                    try:
                        score = int(counts.get("score", 0) or 0)
                    except (TypeError, ValueError):
                        score = 0
                    if score < min_score:
                        continue
                    cand = make_candidate(
                        url=f"https://{instance}/post/{post_id}",
                        title=post.get("name", ""),
                        created=created,
                        source=instance,
                        score_or_stars=score,
                        comments=counts.get("comments", 0) or 0,
                        snippet=_clean_se_excerpt(
                            _strip_markdown(post.get("body") or "")
                        ),
                        pattern=pattern,
                        lane="lemmy",
                    )
                    cand["ap_id"] = post.get("ap_id", "")
                    results.append(cand)
    return results


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
    "stackexchange": stackexchange_adapter,
    "devto": devto_adapter,
    "medium": medium_adapter,
    "lemmy": lemmy_adapter,
}


# --- commands ----------------------------------------------------------------


def cmd_scan(args):
    cfg = load_config(args.config)
    global REQUEST_DELAY
    REQUEST_DELAY = cfg.get("request_delay_seconds", 0.0)
    _dir, state_file, ledger_file = state_paths(cfg)
    state = migrate_state(load_state(state_file))
    now = datetime.now(timezone.utc)

    selected = SOURCES if args.source == "all" else (args.source,)
    # Each lane carries its own window, so scanning one source can never
    # advance (and blank out) another's.
    since_by_source = {
        name: _since_for_source(name, state, cfg, now, args.days) for name in selected
    }
    since = min(since_by_source.values())
    since_date = since.strftime("%Y-%m-%d")

    errors = []
    raw = []
    clean = {}
    reasons = {}
    for name in selected:
        adapter = ADAPTERS[name]
        report = LaneReport()
        found = []
        try:
            found = adapter(cfg, since_by_source[name], report)
        except Exception as exc:  # noqa: BLE001
            # One bad source (malformed-but-valid JSON, unexpected shape) must
            # never abort the whole scan and take the other lanes down with it.
            report.append(f"{name}: adapter crashed: {exc}")
        raw.extend(found)
        errors.extend(report)
        clean[name] = report.clean
        if not clean[name]:
            reasons[name] = hold_reason(report)

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

    # A lane earns a new last_run only by completing a fetch cleanly. Zero
    # candidates from requests that came back is a real, empty window and
    # advances; zero because the requests failed, the adapter crashed, or the
    # lane never made a request at all (source disabled, no instances or tags
    # configured) does NOT — that stretch was never covered, and moving the
    # marker over it loses it silently and permanently. Being selected is a
    # request to scan, not evidence that the scan happened.
    held = [name for name in selected if not clean[name]]
    if held:
        grouped = {}
        for name in held:
            grouped.setdefault(reasons[name], []).append(name)
        detail = "; ".join(f"{r}: {', '.join(n)}" for r, n in grouped.items())
        print(
            f"WARN no clean fetch ({detail}) — keeping their last_run so those "
            "windows are re-scanned next time",
            file=sys.stderr,
        )

    if not args.dry_run:
        today = now.date().isoformat()
        for cand in kept:
            # Retrieved and shown to the human, so a re-scan of the held window
            # will not re-surface it; only the never-retrieved rest comes back.
            seen[cand["url"]] = today
        cutoff = (now - timedelta(days=cfg["seen_retention_days"])).date().isoformat()
        state["seen"] = {u: d for u, d in seen.items() if d >= cutoff}
        for name in selected:
            if clean[name]:
                state["last_run_by_source"][name] = _earned_stamp(
                    name, state, since_by_source, now
                )
        write_json_atomic(state_file, state)

    by_tier = {}
    for cand in kept:
        by_tier[cand["tier"]] = by_tier.get(cand["tier"], 0) + 1
    payload = {
        "scanned_at": now.isoformat(),
        "window_since": since_date,
        "window_since_by_source": {
            name: dt.strftime("%Y-%m-%d") for name, dt in since_by_source.items()
        },
        "sources": list(selected),
        # The lanes that did not complete a clean fetch: their slice of this
        # digest is incomplete and their window is being re-scanned next run.
        "sources_held": held,
        "posting_density": density_counts(ledger_file),
        "by_tier": by_tier,
        "dropped": dropped,
        "errors": errors,
        "candidates": kept,
    }
    out = resolve_module_path(__file__, cfg["candidates_file"])
    # A dry run leaves the disk exactly as it found it. candidates.json is the
    # human's working digest — the file they are mid-way through triaging — so
    # overwriting it while announcing "state untouched" destroyed the very thing
    # the preview was meant to protect. The summary below IS the preview.
    if not args.dry_run:
        out.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    dens = payload["posting_density"]
    mode = " DRY-RUN (nothing written)" if args.dry_run else ""
    print(
        f"FORUM_SWEEP_OK{mode} window>{since_date} raw={len(raw)} kept={len(kept)} "
        f"dropped={dropped} errors={len(errors)}"
    )
    print(
        f"fit tiers: {by_tier.get('high', 0)} high / {by_tier.get('med', 0)} med / "
        f"{by_tier.get('low', 0)} low"
    )
    print(f"posting density: {dens[30]} in 30d / {dens[90]} in 90d")
    if args.dry_run:
        print(f"candidates would be written to: {out} (dry run wrote nothing)")
    else:
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
        "--config",
        # Default resolves beside the module, not beside the CWD; an explicitly
        # passed --config is used verbatim (the user typed it, they meant it).
        default=str(resolve_module_path(__file__, "config.json")),
        help="path to config (default: config.json in the module directory)",
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
        help="preview: writes nothing at all (no candidates file, "
        "no seen-marking, no last_run update)",
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
