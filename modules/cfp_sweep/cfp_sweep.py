#!/usr/bin/env python3
"""cfp-sweep - find conference/meetup CFPs matching your topics before they close.

Submission and distribution are the other end of the discoverability funnel
from list-sweep: instead of getting a project listed, cfp-sweep gets a person
speaking about it. Two lanes:
  lane 1 (conference-data): the public tech-conferences/conference-data
    dataset, scanned per configured topic for the current + next year,
    filtered to CFPs that are still open and events that have not happened
    yet. A conference indexed under more than one configured topic collapses
    into one candidate with an accumulated topic match count.
  lane 2 (watchlist): a pinned list of venues worth tracking directly,
    best-effort classified open/closed/unknown by scanning each one's own CFP
    page - for a venue that recurs every year but is not (yet) indexed by the
    dataset, or one you want closer scrutiny on.

This script does the deterministic half only: discovery, submission-window
detection, dedup, and a per-venue cooldown ledger so a venue that just heard
from you is not pitched again next month. Drafting the pitch/abstract and
deciding whether to submit are NOT here - that is a human, with whatever
assistant they choose, behind a per-submission approval gate. This script
never submits anything, anywhere. Burning a venue by over-pitching it is a
permanent cost, and the cooldown ledger plus the human gate exist because that
cost is real, not out of caution for its own sake.

Lane 2's fetched page text is UNTRUSTED EXTERNAL CONTENT: it is scanned only
for a small set of open/closed phrases and dated-deadline patterns, never
executed or followed. A closing date is only ever recorded when the page
states an explicit year - a date without one (common in the wild: "CFP closes
11 October") is left unparsed rather than guessed at, and the venue is
reported `unknown` instead of a fabricated deadline.

Requires: Python 3.10+. No `gh` CLI needed - both lanes are plain public HTTP
reads (raw.githubusercontent.com for lane 1, each venue's own page for lane 2).

Subcommands (all take --config; the default is the config.json beside this
script, so the module reads its own state and config from any directory):
  scan [--limit N] [--dry-run]                    run both lanes, write candidates
  mark-submitted --venue V --url U [--note ...]    record a submission
  log                                              show recorded submissions
  density                                          submission counts from the ledger
"""

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
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
    relevance_tier,
    resolve_module_path,
    window_start,
    write_json_atomic,
)

REQUIRED_KEYS = ["subject", "topics"]
DEFAULTS = {
    "watchlist": [],
    "countries": [],
    "include_online": True,
    "default_cooldown_days": 180,
    "seen_retention_days": 180,
    "default_window_days": 30,
    "emit_cap": 60,
    "state_dir": "state",
    "candidates_file": "candidates.json",
}

# conferences/<year>/<topic>.json - verified live 2026-08-26 against the real
# repo. Not every topic exists for every year: next year's files are seeded
# gradually (2027 had 6 of 2026's 30 topic files at verification time), so a
# 404 here is an expected, common outcome, not a failure - see fetch_topic_year.
CONFERENCE_DATA_BASE = "https://raw.githubusercontent.com/tech-conferences/conference-data/main/conferences"
USER_AGENT = "signal-sweep cfp-sweep (https://github.com/signal-sweep/signal-sweep)"
HTTP_TIMEOUT = 15

# A candidate whose deadline cannot be determined ranks last within its tier
# and topic-match bracket, rather than pretending to a false urgency.
UNKNOWN_DEADLINE_SENTINEL = 10_000


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
    if not isinstance(cfg["topics"], list) or not cfg["topics"]:
        sys.exit(
            "config 'topics' must be a non-empty list of conference-data topic names"
        )
    for key, val in DEFAULTS.items():
        cfg.setdefault(key, val)
    return cfg


def load_config_for_dry_run(path):
    """Dry-run resolves to config.example.json when the live config.json is
    absent, so the queries can be previewed (no calls, no writes) before a
    project copies the example. Mirrors list-sweep/thread-sweep so `scan
    --dry-run` works on a fresh checkout."""
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
    return state_dir, state_dir / "cfp_state.json", state_dir / "submitted_log.jsonl"


# --- shared helpers ------------------------------------------------------


def _parse_date(value):
    """Parse a YYYY-MM-DD string into a date; None on anything else. The
    conference-data schema uses this format throughout (verified live); a
    value that does not match is treated as absent rather than guessed at."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _normalize_url(url):
    return (url or "").strip().rstrip("/").lower()


def _dedup_key(cfp_url, url, year):
    """The seen-store / in-batch dedup key: a normalized cfp_url when one
    exists (the CFP is the thing being tracked), else the event url + year as
    a fallback for a record missing one."""
    if cfp_url:
        return _normalize_url(cfp_url)
    return f"{_normalize_url(url)}::{year}"


def _region_ok(entry, countries, include_online):
    """True if entry clears the config region filter. An online entry is
    always included when include_online is set, regardless of country; an
    empty countries list means no region restriction at all."""
    if entry.get("online") and include_online:
        return True
    if not countries:
        return True
    return entry.get("country") in countries


def _days_until(cfp_end_iso, today):
    """Ranking input: days from today to a candidate's cfp_end, closer first."""
    parsed = _parse_date(cfp_end_iso) if cfp_end_iso else None
    if parsed is None:
        return UNKNOWN_DEADLINE_SENTINEL
    return (parsed - today).days


# --- lane 1: conference-data ----------------------------------------------


def fetch_topic_year(topic, year, errors):
    """GET one conferences/<year>/<topic>.json file. Returns a list of raw
    entries; [] if the file does not exist for that year/topic yet (a 404 is
    an ANSWERED request, not a failure - the dataset seeds next year's files
    gradually, so most topic x next-year combinations are legitimately not
    there); None on a real fetch/parse error (network failure, a non-404 HTTP
    error, or a payload that is not a JSON list)."""
    url = f"{CONFERENCE_DATA_BASE}/{year}/{topic}.json"
    status, body, err = http_get(
        url, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}
    )
    if status == 404:
        note_fetch_ok(errors)
        return []
    if err or status != 200:
        errors.append(f"{topic} {year}: {err or f'HTTP {status}'}")
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        errors.append(f"{topic} {year}: bad json ({exc})")
        return None
    if not isinstance(data, list):
        errors.append(
            f"{topic} {year}: expected a JSON list, got {type(data).__name__}"
        )
        return None
    note_fetch_ok(errors)
    return data


def _merge_conference_entry(entry, topic, year, cfg, today, merged, dropped):
    """Validate + filter one raw conference-data record, then fold it into
    `merged` keyed by dedup key. A record already present (the same
    conference indexed under a second configured topic - it happens, verified
    live: the same entry can appear byte-identical in two topic files) gains
    the new topic instead of creating a second candidate."""
    if not isinstance(entry, dict):
        dropped["malformed"] += 1
        return
    name = entry.get("name")
    if not name or not isinstance(name, str):
        dropped["malformed"] += 1
        return
    start_date = _parse_date(entry.get("startDate"))
    if start_date is None:
        dropped["malformed"] += 1
        return
    raw_cfp_end = entry.get("cfpEndDate")
    if raw_cfp_end is None:
        dropped["no_cfp"] += 1  # no CFP tracked for this entry at all
        return
    cfp_end = _parse_date(raw_cfp_end)
    if cfp_end is None:
        dropped["malformed"] += 1
        return
    if cfp_end < today:
        dropped["cfp_closed"] += 1
        return
    if start_date < today:
        dropped["past_event"] += 1
        return
    if not _region_ok(entry, cfg["countries"], cfg["include_online"]):
        dropped["region"] += 1
        return

    cfp_url = entry.get("cfpUrl") or ""
    url = entry.get("url") or ""
    key = _dedup_key(cfp_url, url, year)
    if key in merged:
        merged[key]["topics_matched"].add(topic)
        return
    merged[key] = {
        "venue": name,
        "lane": "conference-data",
        "url": url,
        "cfp_url": cfp_url,
        "start_date": entry.get("startDate"),
        "end_date": entry.get("endDate"),
        "city": entry.get("city") or "",
        "country": entry.get("country") or "",
        "online": bool(entry.get("online")),
        "cfp_end": raw_cfp_end,
        # A kept lane-1 entry always has cfpEndDate >= today by construction.
        "detected_state": "open",
        "detection_note": "",
        "cadence_note": "",
        "format_note": "",
        "cooldown_days": None,  # no per-entry override source for this lane
        "topics_matched": {topic},
        "year": year,
    }


def conference_data_lane(cfg, today, errors, dropped):
    """Lane 1: scan configured topics across the current + next year
    conference-data files. `errors` is a sweepcore.LaneReport: every fetch
    that comes back (200 or 404) is counted, so a run that covered every
    topic/year combination can be told apart from one that did not."""
    merged = {}
    for topic in cfg["topics"]:
        for year in (today.year, today.year + 1):
            raw = fetch_topic_year(topic, year, errors)
            if raw is None:
                continue  # a real fetch error, already recorded in errors
            for entry in raw:
                _merge_conference_entry(entry, topic, year, cfg, today, merged, dropped)
    return list(merged.values())


# --- lane 2: watchlist -----------------------------------------------------

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _iso_from_match(m):
    year, month, day = m.groups()
    return date(int(year), int(month), int(day))


def _month_first_from_match(m):
    month_name, day, year = m.groups()
    return date(int(year), MONTHS[month_name.lower()], int(day))


def _day_first_from_match(m):
    day, month_name, year = m.groups()
    return date(int(year), MONTHS[month_name.lower()], int(day))


# Longest names first, so "september" is tried before "sep" would otherwise
# shadow it in alternation.
_MONTH_NAMES = "|".join(sorted(MONTHS, key=len, reverse=True))
_DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), _iso_from_match),
    (
        re.compile(r"\b(" + _MONTH_NAMES + r")\.?\s+(\d{1,2}),?\s+(\d{4})\b", re.I),
        _month_first_from_match,
    ),
    (
        re.compile(r"\b(\d{1,2})\s+(" + _MONTH_NAMES + r")\.?,?\s+(\d{4})\b", re.I),
        _day_first_from_match,
    ),
]

# Words a date must appear near to be trusted as the CFP close date, so an
# unrelated date on the page (the conference's own start date, a sponsor
# deadline) is not mistaken for the submission deadline.
DEADLINE_CONTEXT = (
    "deadline",
    "due by",
    "due date",
    "closes",
    "closing",
    "close by",
    "submissions close",
    "submit by",
    "submission deadline",
)

CLOSED_MARKERS = (
    "submissions are closed",
    "cfp is closed",
    "cfp has closed",
    "the cfp is now closed",
    "call for papers has closed",
    "call for proposals has closed",
    "submissions closed",
    "no longer accepting submissions",
    "cfp closed",
)

OPEN_MARKERS = (
    "submissions are open",
    "cfp is open",
    "the cfp is now open",
    "call for papers is open",
    "call for proposals is open",
    "now accepting submissions",
    "we are looking for speakers",
    "cfp open",
    # Deliberately NOT "submit your talk" / "submit a talk": matching runs
    # over the raw fetched body, not rendered/stripped text, and a real page
    # (KubeCon + CloudNativeCon Europe's CFP page, verified live) carries that
    # exact phrase as a permanent nav-anchor label - <a href="#submit-your
    # -talk">Submit Your Talk</a> - that stays on the page whether the CFP is
    # open or closed. A status-asserting sentence is a safe marker; an
    # evergreen call-to-action label is not.
)

GENERIC_CFP_PHRASES = ("call for papers", "call for proposals", "call for speakers")


def _extract_dated_deadline(text_low):
    """Best-effort deadline extraction: a date pattern found within a short
    window after a deadline-context word, WITH an explicit year. A day+month
    with no year (real example: a major conference's own CFP page reading
    "CFP Closes: Sunday, 11 October") is common and deliberately left
    unparsed - guessing the year is exactly the kind of guess this module
    refuses to make."""
    for word in DEADLINE_CONTEXT:
        idx = text_low.find(word)
        if idx == -1:
            continue
        window = text_low[idx : idx + 80]
        for pattern, extractor in _DATE_PATTERNS:
            m = pattern.search(window)
            if not m:
                continue
            try:
                return extractor(m)
            except (ValueError, KeyError):
                continue
    return None


def classify_cfp_page(cfp_url, entry, today, errors):
    """Best-effort fetch + classify one watchlist venue's CFP page.

    Returns (detected_state, cfp_end_date_or_None, detection_note). Priority:
    an explicit closed phrase, then an explicit open phrase, then a dated
    deadline found near deadline-context text (future -> open, past ->
    closed), else unknown. Fetched text is UNTRUSTED EXTERNAL CONTENT: it is
    only ever scanned for these phrases/dates, never executed or followed.

    Matching runs over the raw fetched body, tags and all - no HTML parser,
    to stay stdlib-simple. That is safe only because the marker lists are
    chosen to avoid phrases a page's structure (nav labels, section anchors)
    would carry regardless of status; see the comment on OPEN_MARKERS for a
    live example of exactly that trap. The same raw-body scan means markup
    between a deadline-context word and its date can push the date outside
    _extract_dated_deadline's window and the result falls back to `unknown`
    - a false negative, not a false positive, which is the failure mode this
    module prefers.
    """
    closed_markers = set(CLOSED_MARKERS) | set(entry.get("closed_markers") or [])
    open_markers = set(OPEN_MARKERS) | set(entry.get("open_markers") or [])
    status, body, err = http_get(
        cfp_url, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}
    )
    if err or status != 200:
        errors.append(f"{entry.get('name', cfp_url)}: {err or f'HTTP {status}'}")
        return "unknown", None, "fetch failed"
    low = body.lower()
    if any(phrase in low for phrase in closed_markers):
        return "closed", None, "explicit closed phrase found"
    if any(phrase in low for phrase in open_markers):
        return "open", None, "explicit open phrase found"
    deadline = _extract_dated_deadline(low)
    if deadline:
        if deadline >= today:
            return "open", deadline, "inferred from a future dated deadline"
        return "closed", deadline, "inferred from a past dated deadline"
    if any(phrase in low for phrase in GENERIC_CFP_PHRASES):
        return (
            "unknown",
            None,
            "cfp language present, no open/closed signal or dated deadline",
        )
    return (
        "unknown",
        None,
        "no cfp language detected (page may be JS-rendered, or the URL is stale)",
    )


def watchlist_lane(cfg, today, advisory):
    """Lane 2: best-effort classify each watchlist venue's own CFP page.

    A malformed watchlist entry is a config typo, not a coverage gap, so it
    goes to `advisory` rather than the lane-1 LaneReport - the same reasoning
    list-sweep and thread-sweep use for their own watchlists."""
    candidates = []
    for entry in cfg["watchlist"]:
        if not isinstance(entry, dict):
            advisory.append(f"watchlist entry is not an object, skipped: {entry!r}")
            continue
        name = entry.get("name")
        cfp_url = entry.get("cfp_url")
        topics = entry.get("topics")
        if not name or not cfp_url or not isinstance(topics, list) or not topics:
            advisory.append(
                f"watchlist entry missing name/cfp_url/topics, skipped: {entry!r}"
            )
            continue
        state, cfp_end, note = classify_cfp_page(cfp_url, entry, today, advisory)
        candidates.append(
            {
                "venue": name,
                "lane": "watchlist",
                "url": entry.get("url") or "",
                "cfp_url": cfp_url,
                "start_date": None,
                "end_date": None,
                "city": "",
                "country": "",
                "online": None,
                "cfp_end": cfp_end.isoformat() if cfp_end else None,
                "detected_state": state,
                "detection_note": note,
                "cadence_note": entry.get("cadence_note", "") or "",
                "format_note": entry.get("format_note", "") or "",
                "cooldown_days": entry.get("cooldown_days"),
                "topics_matched": set(topics),
                "year": today.year,
            }
        )
    return candidates


# --- dedup, cooldown, ranking ----------------------------------------------


def last_submission_by_venue(ledger_file):
    """Most recent submission date per normalized venue name, from the ledger."""
    latest = {}
    if not ledger_file.exists():
        return latest
    for line in ledger_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            when = datetime.fromisoformat(entry["date"])
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            continue
        venue = (entry.get("venue") or "").strip().lower()
        if not venue:
            continue
        day = when.date()
        if venue not in latest or day > latest[venue]:
            latest[venue] = day
    return latest


def filter_candidates(raw, seen, last_submitted, cfg, today):
    """The keep/drop pass over raw candidates from both lanes: batch-dup,
    seen-store, and per-venue submission cooldown. Pure - mutates nothing."""
    kept, dropped = [], {"dup": 0, "seen": 0, "cooldown": 0}
    batch = set()
    for cand in raw:
        key = _dedup_key(cand["cfp_url"], cand["url"], cand["year"])
        if key in batch:
            dropped["dup"] += 1
            continue
        if key in seen:
            dropped["seen"] += 1
            continue
        cooldown_days = cand.get("cooldown_days") or cfg["default_cooldown_days"]
        last = last_submitted.get(cand["venue"].strip().lower())
        if last is not None and (today - last).days < cooldown_days:
            dropped["cooldown"] += 1
            continue
        batch.add(key)
        kept.append(cand)
    return kept, dropped


def _tier_for(cand):
    """Feed relevance_tier only the one signal that has a genuine meaning
    here: `pattern` (a real topic match vs a generic watchlist pull - the
    same distinction thread-sweep and forum-sweep use it for). `comments=1`
    is a neutralizing stand-in, not a real field on this candidate:
    relevance_tier treats an absent/zero comment count as a +1 recall signal
    (an unanswered thread is a good target), which has no CFP analogue and
    would otherwise silently erase the pattern-driven tier split by pushing
    every candidate up a band. is_answered/match_type/stars have no honest
    mapping in this domain and are left unset, so 'high' is not reachable
    here - topic_match_count and deadline proximity carry the real ranking."""
    return relevance_tier({"pattern": cand["pattern"], "comments": 1})


# --- scan --------------------------------------------------------------


def cmd_scan(args):
    now = datetime.now(timezone.utc)
    today = now.date()

    if args.dry_run:
        # No network, no state. Print exactly what we WOULD fetch.
        cfg = load_config_for_dry_run(args.config)
        print("CFP_SWEEP_DRY-RUN (no network, no state writes)")
        print(f"  subject: {cfg['subject']}")
        print("  lane 1 (conference-data) would fetch:")
        for topic in cfg["topics"]:
            for year in (today.year, today.year + 1):
                print(f"    {CONFERENCE_DATA_BASE}/{year}/{topic}.json")
        print("  lane 2 (watchlist) would classify:")
        for entry in cfg["watchlist"] or [{"name": "(none configured)", "cfp_url": ""}]:
            print(f"    {entry.get('name', '?')} -> {entry.get('cfp_url', '?')}")
        print(
            "  candidates would be written to: "
            f"{resolve_module_path(__file__, cfg['candidates_file'])}"
        )
        return 0

    cfg = load_config(args.config)
    state_dir, state_file, ledger_file = state_paths(cfg)
    state = load_state(state_file)
    # since/window_start feed only the earned-marker coverage proof below:
    # lane 1 always re-reads the full current + next year dataset rather than
    # an incremental query, so there is nothing for a day-count window to
    # bound - unlike the rest of the toolkit, `scan` takes no --days override.
    since = window_start(
        state.get("last_run"), cfg["default_window_days"], now, None, label="cfp-sweep"
    )

    report = LaneReport()  # lane 1 only: the coverage-marker lane
    advisory = []
    dropped = {
        "malformed": 0,
        "no_cfp": 0,
        "cfp_closed": 0,
        "past_event": 0,
        "region": 0,
    }
    lane1 = conference_data_lane(cfg, today, report, dropped)
    lane2 = watchlist_lane(cfg, today, advisory)
    raw = lane1 + lane2
    errors = list(report) + advisory

    seen = state.get("seen", {})
    last_submitted = last_submission_by_venue(ledger_file)
    kept, filter_dropped = filter_candidates(raw, seen, last_submitted, cfg, today)
    dropped.update(filter_dropped)

    for cand in kept:
        matched = set(cand["topics_matched"]) & set(cfg["topics"])
        cand["topic_match_count"] = len(matched)
        cand["topics_matched"] = sorted(cand["topics_matched"])
        cand["pattern"] = (
            ",".join(sorted(matched))
            if cand["lane"] == "conference-data"
            else "watchlist"
        )
        cand["days_until_deadline"] = _days_until(cand["cfp_end"], today)
        cand["tier"] = _tier_for(cand)

    kept.sort(
        key=lambda c: (
            TIER_RANK[c["tier"]],
            c["topic_match_count"],
            -c["days_until_deadline"],
        ),
        reverse=True,
    )
    limit = args.limit or cfg["emit_cap"]
    kept = kept[:limit]

    # The marker advances only on a lane 1 that proved it covered this scan:
    # every topic/year fetch came back (200 or 404 both count - see
    # fetch_topic_year) and none failed. A held run keeps the old marker so a
    # transient GitHub failure is retried in full next time rather than
    # silently accepted as "already covered".
    held = not report.clean
    today_iso = today.isoformat()
    for cand in kept:
        key = _dedup_key(cand["cfp_url"], cand["url"], cand["year"])
        seen[key] = today_iso
    cutoff = (now - timedelta(days=cfg["seen_retention_days"])).date().isoformat()
    state["seen"] = {k: d for k, d in seen.items() if d >= cutoff}
    if held:
        print(
            f"WARN {hold_reason(report)} — keeping last_run so lane 1 is "
            "re-fetched in full next time",
            file=sys.stderr,
        )
    else:
        state["last_run"] = earned_stamp(state.get("last_run"), since, now)
    write_json_atomic(state_file, state)

    tier_counts = {"high": 0, "med": 0, "low": 0}
    for cand in kept:
        tier_counts[cand["tier"]] += 1

    payload = {
        "scanned_at": now.isoformat(),
        "window_held": held,
        "dropped": dropped,
        "errors": errors,
        "candidates": kept,
    }
    out = resolve_module_path(__file__, cfg["candidates_file"])
    write_json_atomic(out, payload)

    print(
        f"CFP_SWEEP_OK topics={','.join(cfg['topics'])} raw={len(raw)} "
        f"kept={len(kept)} dropped={dropped} errors={len(errors)}"
    )
    print(
        f"  fit tiers: {tier_counts['high']} high / {tier_counts['med']} med / "
        f"{tier_counts['low']} low"
    )
    print(f"candidates -> {out}")
    for err in errors[:5]:
        print(f"WARN {err}", file=sys.stderr)
    return 0


# --- ledger commands ---------------------------------------------------


def cmd_mark_submitted(args):
    cfg = load_config(args.config)
    _dir, _state, ledger_file = state_paths(cfg)
    entry = {
        "date": datetime.now(timezone.utc).isoformat(),
        "venue": args.venue,
        "url": args.url,
        "note": args.note or "",
    }
    append_ledger(ledger_file, entry)
    print(f"LEDGER_OK {args.venue} <- submission recorded")
    return 0


def cmd_log(args):
    cfg = load_config(args.config)
    _dir, _state, ledger_file = state_paths(cfg)
    if not ledger_file.exists():
        print("no submissions recorded yet")
        return 0
    for line in ledger_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        print(
            f"  {e.get('date', '')[:10]}  {e.get('venue', ''):<40} {e.get('url', '')}"
        )
    return 0


def cmd_density(args):
    cfg = load_config(args.config)
    _dir, _state, ledger_file = state_paths(cfg)
    dens = density_counts(ledger_file)
    print(f"submissions: {dens[30]} in last 30d, {dens[90]} in last 90d")
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
    scan.add_argument("--limit", type=int, default=None)
    scan.add_argument(
        "--dry-run",
        action="store_true",
        help="preview only: no network calls, writes nothing, advances nothing",
    )
    scan.set_defaults(func=cmd_scan)

    mark = sub.add_parser("mark-submitted", help="record a submission in the ledger")
    mark.add_argument("--venue", required=True, help="conference/meetup name")
    mark.add_argument("--url", required=True, help="the CFP or event URL submitted to")
    mark.add_argument("--note", default=None)
    mark.set_defaults(func=cmd_mark_submitted)

    log = sub.add_parser("log", help="show recorded submissions")
    log.set_defaults(func=cmd_log)

    dens = sub.add_parser("density", help="submission counts from the ledger")
    dens.set_defaults(func=cmd_density)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
