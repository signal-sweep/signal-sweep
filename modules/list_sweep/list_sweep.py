#!/usr/bin/env python3
"""list-sweep - find curated lists where your project could be listed.

Placement is the top of the discoverability funnel: the awesome-lists and
directories that send people to a project. This module does the deterministic
half of getting listed. Two lanes:
  lane 1 (query-first): `gh search repos` for awesome-*/list repos by topic
  lane 2 (watchlist):   a seed list of known target directories from config

For each candidate it fetches CONTRIBUTING/README and classifies the intake
path - PR, issue-form, web-form, or unknown - so you know how a submission is
made before you make it. Web-form and human-only lists are FLAGGED, not
handled: some directories ban automated or non-human submissions, and that
line is the human's to respect.

Dedup: a seen-store (every surfaced list is surfaced once) plus, via an
optional config path, the placement-health placements.json registry, so lists
you are already in or have already submitted to are excluded.

Judgment - the fit verdict, the submission copy, the decision to submit - is
NOT here. That belongs to a human, with whatever assistant they choose, behind
a per-submission approval gate. This script only retrieves, classifies,
filters, records. It never submits anything, anywhere.

It complements placement-health: list-sweep gets you LISTED; placement-health
confirms you STAY listed.

Requires: Python 3.10+, an authenticated GitHub CLI (`gh auth login`).

Subcommands (all take --config; the default is the config.json beside this
script, so the module reads its own state and config from any directory):
  scan [--days N] [--limit N] [--dry-run]   run both lanes, write candidates
  mark-submitted --url U --list L [--note ...]   record a submission
  log                                        show recorded submissions
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sweepcore import (  # noqa: E402
    LaneReport,
    append_ledger,
    earned_stamp,
    gh,
    hold_reason,
    http_get,
    load_state,
    note_fetch_ok,
    resolve_module_path,
    window_start,
    write_json_atomic,
)

REQUIRED_KEYS = ["own_repo", "topics", "search_keywords"]
DEFAULTS = {
    "min_stars": 100,
    "per_query": 20,
    "emit_cap": 60,
    "seen_retention_days": 180,
    "default_window_days": 30,
    "state_dir": "state",
    "candidates_file": "candidates.json",
    "watchlist": [],
    "placements_path": None,
    "fit_floor": 1,
}

UA = "list-sweep (https://github.com/signal-sweep/signal-sweep)"
INTAKE_TIMEOUT = 15

# Files inside a target repo that describe how to submit, most specific first.
INTAKE_DOCS = [
    "CONTRIBUTING.md",
    ".github/CONTRIBUTING.md",
    "docs/CONTRIBUTING.md",
    "README.md",
]

# Intake-mechanic classification. Each path maps to a set of lowercase
# signals; the first path whose signals appear in the fetched doc text wins.
# web-form and human-only paths are FLAGGED so a human handles them - some
# lists explicitly forbid automated or non-human submissions.
INTAKE_SIGNALS = {
    "web-form": [
        "google form",
        "forms.gle",
        "docs.google.com/forms",
        "airtable.com",
        "typeform.com",
        "submit via the form",
        "submission form",
        "fill out the form",
    ],
    "issue-form": [
        "open an issue",
        "create an issue",
        "issue template",
        "submit an issue",
        "new issue",
        "issues/new",
    ],
    "pr": [
        "pull request",
        "open a pr",
        "submit a pr",
        "fork ",
        "fork the repo",
        "send a pr",
        "via pr",
    ],
}

# Lists that ban automated / non-human submissions. If any of these phrases
# appear, the candidate is flagged human-only regardless of the intake path.
HUMAN_ONLY_SIGNALS = [
    "no automated",
    "no bots",
    "human submissions only",
    "manual review only",
    "do not use bots",
    "automated submissions will be",
    "by a human",
]


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
        sys.exit("config 'topics' must be a non-empty list of topic strings")
    if not isinstance(cfg["search_keywords"], list) or not cfg["search_keywords"]:
        sys.exit("config 'search_keywords' must be a non-empty list of search terms")
    for key, val in DEFAULTS.items():
        cfg.setdefault(key, val)
    return cfg


def load_config_for_dry_run(path):
    """Dry-run resolves to config.example.json when the live config.json is
    absent, so the queries can be previewed (no calls, no writes) before a
    project copies the example. A real scan still requires the live config.
    Mirrors mention-sweep so `scan --dry-run` works on a fresh checkout."""
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
    return state_dir, state_dir / "list_state.json", state_dir / "submitted_log.jsonl"


def search_repos(query, limit, errors):
    """Lane 1: `gh search repos` for awesome-list / directory repos.

    This is the only lane the pushed-since window governs, so `errors` is a
    sweepcore.LaneReport in a real scan: a search that comes back is counted
    even when it matched nothing, which is what separates a covered-but-empty
    window from one that was never searched. A plain list still works.
    """
    fields = "fullName,description,stargazersCount,url"
    data, err = gh(
        [
            "search",
            "repos",
            query,
            "--limit",
            str(limit),
            "--json",
            fields,
        ]
    )
    if err:
        errors.append(f"search {query!r}: {err}")
        return []
    if not isinstance(data, list):
        # gh() yields ("", "") when a non-zero exit writes nothing to stderr, so
        # the `if err` guard above misses it. Counting that as a covered window
        # would advance the marker over ground the search never returned. Treat
        # an unusable payload as the failure it is, BEFORE note_fetch_ok.
        errors.append(f"search {query!r}: no usable result payload")
        return []
    note_fetch_ok(errors)
    return data


def fetch_raw(repo, doc_path):
    """Fetch a file from a repo's default branch over raw.githubusercontent.

    urllib (not gh) keeps the intake fetch a plain public read and avoids
    burning gh API quota on doc-sniffing. Returns text or None.
    """
    for branch in ("HEAD", "main", "master"):
        url = f"https://raw.githubusercontent.com/{repo}/{branch}/{doc_path}"
        status, body, err = http_get(
            url, timeout=INTAKE_TIMEOUT, headers={"User-Agent": UA}
        )
        if err is None and status == 200:
            return body
        if status is None:
            # network-level failure (URLError / timeout / OSError) — give up
            return None
        # an HTTP error on this branch (e.g. 404) — try the next one
    return None


def classify_intake(repo, errors):
    """Detect how a list takes submissions.

    Returns (path, doc, human_only). path is one of pr/issue-form/web-form/
    unknown; human_only is True if the docs ban automated or non-human
    submissions (the caller flags these for a human regardless of path).
    """
    seen_doc = None
    human_only = False
    for doc in INTAKE_DOCS:
        text = fetch_raw(repo, doc)
        if not text:
            continue
        low = text.lower()
        if seen_doc is None:
            seen_doc = doc
        # Accumulate across docs: a generic CONTRIBUTING.md must not mask a
        # README that says "human submissions only" further down the list.
        human_only = human_only or any(sig in low for sig in HUMAN_ONLY_SIGNALS)
        for path, signals in INTAKE_SIGNALS.items():
            if any(sig in low for sig in signals):
                return path, doc, human_only
        # Doc exists but no intake signal matched: keep looking, remembering
        # we at least saw a doc so 'unknown' is honest, not a fetch miss.
    return "unknown", seen_doc, human_only


def fit_score(repo_full, description, cfg):
    """Topic-overlap score: how many configured topics/keywords this list hits.

    Cheap recall heuristic over the repo name + description. Real fit is a
    judgment call the human makes from candidates.json; this only ranks.
    """
    haystack = f"{repo_full} {description or ''}".lower()
    terms = {t.lower() for t in cfg["topics"]} | {
        k.lower() for k in cfg["search_keywords"]
    }
    return sum(1 for term in terms if term and term in haystack)


def load_placements(path, errors):
    """Optional dedup source: repo names already in a placement-health registry.

    Reads the placements.json the placement-health module maintains and pulls
    every owner/name it can find from each entry's url/expect, so a list you
    are already in (or have a pending submission to) never resurfaces here.
    """
    listed = set()
    if not path:
        return listed
    # A relative placements_path (the shipped example is
    # "../placement_health/placements.json") points at a sibling module, so it
    # resolves from this module's directory, never from the process CWD.
    p = resolve_module_path(__file__, path)
    if not p.exists():
        errors.append(f"placements_path not found, dedup skipped: {p}")
        return listed
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(f"placements_path not valid JSON, dedup skipped: {exc}")
        return listed
    for entry in data.get("placements", []) or []:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url", "")
        repo = repo_from_url(url)
        if repo:
            listed.add(repo.lower())
    return listed


def repo_from_url(url):
    """Pull an owner/name from a github.com or raw.githubusercontent.com URL."""
    if not url:
        return None
    marker = None
    if "raw.githubusercontent.com/" in url:
        marker = "raw.githubusercontent.com/"
    elif "github.com/" in url:
        marker = "github.com/"
    if not marker:
        return None
    tail = url.split(marker, 1)[1].strip("/")
    parts = tail.split("/")
    if len(parts) >= 2 and parts[0] and parts[1]:
        return f"{parts[0]}/{parts[1]}"
    return None


def build_candidate(node, lane, cfg, errors, classify=True):
    """Shape a search/watchlist hit into a candidate with intake + draft stub."""
    repo_full = node.get("fullName") or node.get("repo") or ""
    if not repo_full:
        return None
    # SECURITY: the repo description is UNTRUSTED EXTERNAL CONTENT. Scored and
    # stored for a human to read; never interpreted as an instruction.
    description = node.get("description", "")
    stars = node.get("stargazersCount", node.get("stars", 0))
    url = node.get("url") or f"https://github.com/{repo_full}"
    if classify:
        intake_path, intake_doc, human_only = classify_intake(repo_full, errors)
    else:
        intake_path, intake_doc, human_only = "unknown", None, False
    score = fit_score(repo_full, description, cfg)
    flagged = human_only or intake_path == "web-form"
    return {
        "repo": repo_full,
        "url": url,
        "description": (description or "")[:300],
        "stars": stars,
        "lane": lane,
        "fit_score": score,
        "intake_path": intake_path,
        "intake_doc": intake_doc,
        "human_only": human_only,
        "flagged": flagged,
        "flag_reason": (
            "human-only / automated submissions banned"
            if human_only
            else "web-form intake - submit by hand"
            if intake_path == "web-form"
            else ""
        ),
        # Drafted submission-entry stub. NEVER submitted - material for a human
        # to review, edit, and (if they choose) submit through the gate.
        "submission_draft": {
            "project": cfg["own_repo"],
            "intake_path": intake_path,
            "needs_human": flagged,
            "entry_line": f"{cfg['own_repo']} - {cfg.get('own_tagline', '')}".strip(
                " -"
            ),
            "where": intake_doc or "see repo CONTRIBUTING/README",
        },
    }


def cmd_scan(args):
    if args.dry_run:
        cfg = load_config_for_dry_run(args.config)
    else:
        cfg = load_config(args.config)
    state_dir, state_file, ledger_file = state_paths(cfg)
    now = datetime.now(timezone.utc)

    if args.dry_run:
        # No network, no gh, no state. Print exactly what we WOULD query.
        print("LIST_SWEEP_DRY-RUN (no network, no gh, no state writes)")
        print(f"  own_repo: {cfg['own_repo']}")
        print("  lane 1 (gh search repos) would run these queries:")
        for q in dry_run_queries(cfg, args):
            print(f"    gh search repos {q!r} --limit {cfg['per_query']}")
        print("  lane 2 (watchlist) would classify intake for:")
        for full in cfg["watchlist"] or ["(none configured)"]:
            print(f"    {full}")
        # Print the RESOLVED paths: a preview that shows a bare relative name
        # is exactly what let a CWD-anchored write look correct while landing
        # somewhere else.
        dedup = cfg.get("placements_path")
        dedup_shown = (
            resolve_module_path(__file__, dedup) if dedup else "(none configured)"
        )
        print(f"  dedup vs placements: {dedup_shown}")
        print(
            "  candidates would be written to: "
            f"{resolve_module_path(__file__, cfg['candidates_file'])}"
        )
        return 0

    state = load_state(state_file)
    # A marker that no longer parses re-windows to the default with a warning
    # rather than raising out of the scan; sweepcore.window_start owns the rule.
    since = window_start(
        state.get("last_run"),
        cfg["default_window_days"],
        now,
        args.days,
        label="list-sweep",
    )
    pushed_floor = since.strftime("%Y-%m-%d")

    # Only lane 1 is time-scoped (pushed:>floor), so only its report can earn or
    # hold the marker. Lane 2 is a fixed seed list and the placements registry is
    # a dedup source: neither has a window to lose, and a bad watchlist entry or
    # a missing placements file would otherwise freeze the marker permanently.
    report = LaneReport()
    advisory = []
    raw = []
    # Lane 1: query-first repo search.
    for query in build_queries(cfg, pushed_floor):
        for node in search_repos(query, cfg["per_query"], report):
            cand = build_candidate(node, "query", cfg, advisory)
            if cand:
                raw.append(cand)
    # Lane 2: seed watchlist (classified the same way).
    for full in cfg["watchlist"]:
        full = full.strip()
        if "/" not in full:
            advisory.append(
                f"watchlist entry not in owner/name form, skipped: {full!r}"
            )
            continue
        cand = build_candidate({"repo": full}, "watchlist", cfg, advisory)
        if cand:
            raw.append(cand)

    seen = state.get("seen", {})
    submitted = submitted_repos(ledger_file)
    already_listed = load_placements(cfg.get("placements_path"), advisory)
    errors = list(report) + advisory

    kept = []
    dropped = {"seen": 0, "submitted": 0, "placed": 0, "own": 0, "stars": 0, "fit": 0}
    dropped["dup"] = 0
    batch = set()
    for cand in raw:
        key = cand["repo"].lower()
        if key in batch:
            dropped["dup"] += 1
            continue
        if key == cfg["own_repo"].lower():
            dropped["own"] += 1
            continue
        if key in seen:
            dropped["seen"] += 1
            continue
        if key in submitted:
            dropped["submitted"] += 1
            continue
        if key in already_listed:
            dropped["placed"] += 1
            continue
        if cand["lane"] == "query" and cand["stars"] < cfg["min_stars"]:
            dropped["stars"] += 1
            continue
        # Both floors are query-lane recall heuristics: a watchlist entry is
        # hand-curated (and carries no description for fit_score to read), so
        # it bypasses the fit floor exactly like it bypasses the star floor.
        if cand["lane"] == "query" and cand["fit_score"] < cfg["fit_floor"]:
            dropped["fit"] += 1
            continue
        batch.add(key)
        kept.append(cand)

    kept.sort(key=lambda c: (c["fit_score"], c["stars"]), reverse=True)
    limit = args.limit or cfg["emit_cap"]
    kept = kept[:limit]

    # The marker advances only on a lane 1 that proved it covered this window:
    # at least one search came back and none failed. Zero lists from searches
    # that came back is a real, empty window and advances; zero because the
    # searches errored, or because no search was made at all, does NOT — that
    # stretch was never looked at, and moving the marker over it loses it
    # silently and permanently. A run with no stored marker that fails writes no
    # marker either, rather than inventing one.
    held = not report.clean
    today = now.date().isoformat()
    for cand in kept:
        # Retrieved and shown to the human, so a re-scan of a held window will
        # not re-surface it; only the never-retrieved rest comes back.
        seen[cand["repo"].lower()] = today
    cutoff = (now - timedelta(days=cfg["seen_retention_days"])).date().isoformat()
    state["seen"] = {r: d for r, d in seen.items() if d >= cutoff}
    if held:
        print(
            f"WARN {hold_reason(report)} — keeping last_run so this window "
            "is re-scanned next time",
            file=sys.stderr,
        )
    else:
        state["last_run"] = earned_stamp(state.get("last_run"), since, now)
    write_json_atomic(state_file, state)

    flagged = sum(1 for c in kept if c["flagged"])
    payload = {
        "scanned_at": now.isoformat(),
        "pushed_since": pushed_floor,
        # True when this run did not earn the marker: the lane-1 slice of this
        # digest is incomplete and the same window is re-scanned next run.
        "window_held": held,
        "dropped": dropped,
        "flagged_count": flagged,
        "errors": errors,
        "candidates": kept,
    }
    out = resolve_module_path(__file__, cfg["candidates_file"])
    out.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    print(
        f"LIST_SWEEP_OK pushed>{pushed_floor} raw={len(raw)} kept={len(kept)} "
        f"flagged={flagged} dropped={dropped} errors={len(errors)}"
    )
    print(f"candidates -> {out}")
    print("  flagged = web-form / human-only: submit those by hand, never automate.")
    for err in errors[:5]:
        print(f"WARN {err}", file=sys.stderr)
    return 0


def build_queries(cfg, pushed_floor):
    """Lane-1 search strings: keyword x topic, scoped to list-shaped repos."""
    queries = []
    for keyword in cfg["search_keywords"]:
        q = f"{keyword} in:name,description,readme pushed:>{pushed_floor} sort:stars"
        queries.append(q)
    return queries


def dry_run_queries(cfg, args):
    """The queries scan would build, for --dry-run preview (no time scoping)."""
    floor = "<since-last-run>"
    if args.days is not None:
        floor = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime(
            "%Y-%m-%d"
        )
    return build_queries(cfg, floor)


def submitted_repos(ledger_file):
    """owner/name repos already submitted to, from the ledger (excluded forever)."""
    repos = set()
    if ledger_file.exists():
        for line in ledger_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            repo = repo_from_url(entry.get("url", "")) or entry.get("list", "")
            if repo:
                repos.add(repo.lower())
    return repos


def cmd_mark_submitted(args):
    cfg = load_config(args.config)
    _state_dir, _state, ledger_file = state_paths(cfg)
    entry = {
        "date": datetime.now(timezone.utc).isoformat(),
        "url": args.url,
        "list": args.list,
        "note": args.note or "",
    }
    append_ledger(ledger_file, entry)
    print(f"LEDGER_OK {args.list} <- submission recorded")
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
        print(f"  {e.get('date', '')[:10]}  {e.get('list', ''):<40} {e.get('url', '')}")
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

    scan = sub.add_parser("scan", help="run both lanes, write candidate lists JSON")
    scan.add_argument(
        "--days",
        type=int,
        default=None,
        help="window override (default: since last run)",
    )
    scan.add_argument("--limit", type=int, default=None)
    scan.add_argument(
        "--dry-run",
        action="store_true",
        help="preview queries only: no network, no gh, no state writes",
    )
    scan.set_defaults(func=cmd_scan)

    mark = sub.add_parser("mark-submitted", help="record a submission in the ledger")
    mark.add_argument("--url", required=True, help="the list's repo/page URL")
    mark.add_argument("--list", required=True, help="human label for the list")
    mark.add_argument("--note", default=None)
    mark.set_defaults(func=cmd_mark_submitted)

    log = sub.add_parser("log", help="show recorded submissions")
    log.set_defaults(func=cmd_log)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
