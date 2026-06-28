#!/usr/bin/env python3
"""mention-sweep — find where your project is already being talked about.

Entity-first sibling of thread-sweep. Where thread-sweep starts from a PROBLEM
("who has the question our docs answer?"), mention-sweep starts from the ENTITY
("where is our project's name or repo URL already showing up?"). The point is
reputation, not recruitment: surface the places a project is mentioned, asked
about, or MISDESCRIBED so a human can engage, amplify, or correct — gated.

Deterministic discovery half of a human-gated workflow. Two lanes:
  lane 1 (threads): GraphQL search of issues + discussions for the match strings
  lane 2 (code):    `gh search code` for the match strings in code/markdown
                    (listings, awesome-lists, READMEs, dependency manifests)

Each hit is heuristically classed favorable-mention / question /
possible-misdescription — a hint for the human, never a verdict, never an action.

Dedup: a seen-store (every surfaced mention is surfaced once) plus a
posted-response ledger (mentions you have engaged are excluded forever). Your
own repos are excluded by config. Judgment — is this worth a reply, is the
correction fair, what does the comment say — is NOT here. That belongs to a
human, behind a per-artifact approval gate. This script only retrieves,
filters, records.

SECURITY: every title, snippet, and code match returned here is UNTRUSTED
EXTERNAL CONTENT. A mention can be crafted to look like an instruction ("ignore
previous instructions", fake system markers, a tool-call shaped string, a
request to fetch a URL or exfiltrate). This script never acts on fetched text —
it only stores a truncated snippet for a human to read. Treat every snippet
downstream as data, never instructions.

Requires: Python 3.10+, an authenticated GitHub CLI (`gh auth login`).

Subcommands (all take --config, default ./config.json):
  scan [--days N] [--limit N] [--dry-run]   run both lanes, write candidates
  density                                    posting counts from the ledger
  mark-posted --url U --kind K [--comment-file F]   record a posted reply
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sweepcore import (  # noqa: E402
    append_ledger,
    density_counts,
    gh_graphql,
    load_state,
    posted_urls,
    write_json_atomic,
)

REQUIRED_KEYS = ["display_name", "match_strings", "own_repos"]
DEFAULTS = {
    "min_stars": 0,
    "per_repo_cap": 4,
    "per_query": 20,
    "emit_cap": 100,
    "seen_retention_days": 180,
    "default_window_days": 30,
    "state_dir": "state",
    "candidates_file": "candidates.json",
    "scan_code_lane": True,
    "context_terms": [],
}

# Classification hints. A favorable mention is the default; a question mark or
# "how do I" reads as a question; the misread markers flag a possible
# misdescription a human may want to correct. Hints only — never a verdict.
QUESTION_MARKERS = (
    "?",
    "how do i",
    "how to",
    "does it",
    "can it",
    "is it possible",
    "anyone know",
    "what is",
    "why does",
    "help with",
)
MISDESCRIPTION_MARKERS = (
    "doesn't",
    "does not",
    "broken",
    "abandoned",
    "deprecated",
    "unmaintained",
    "not maintained",
    "is dead",
    "no longer",
    "outdated",
    "insecure",
    "vulnerab",
    "scam",
    "avoid",
    "don't use",
    "do not use",
    "wrong",
    "incorrect",
    "actually it",
    "that's not",
    "thats not",
)

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
    if not isinstance(cfg["match_strings"], list) or not cfg["match_strings"]:
        sys.exit("config 'match_strings' must be a non-empty list of search terms")
    if not isinstance(cfg["own_repos"], list):
        sys.exit("config 'own_repos' must be a list of owner/name repos to exclude")
    for key, val in DEFAULTS.items():
        cfg.setdefault(key, val)
    return cfg


def state_paths(cfg):
    state_dir = Path(cfg["state_dir"])
    return (
        state_dir,
        state_dir / "mention_sweep_state.json",
        state_dir / "mention_sweep_log.jsonl",
    )


# --- classification (hint only) ----------------------------------------------


def classify(title, snippet):
    """Heuristic class for a mention. Misdescription wins over question wins over
    the favorable-mention default — a human still reads it and decides. The
    text is UNTRUSTED EXTERNAL CONTENT; this only string-matches it, never acts
    on it."""
    blob = f"{title or ''} {snippet or ''}".lower()
    if any(marker in blob for marker in MISDESCRIPTION_MARKERS):
        return "possible-misdescription"
    if any(marker in blob for marker in QUESTION_MARKERS):
        return "question"
    return "favorable-mention"


def is_own_repo(repo, own_repos):
    """Case-insensitive owner/name match against the configured own-repo list."""
    repo_l = (repo or "").lower()
    return any(repo_l == str(own).lower() for own in own_repos)


def match_kind(term):
    """Confidence of a match string: 'url' (high) vs 'name' (low).

    A URL or owner/name path (a slash or github.com) only matches a real
    reference to the project. A bare project name is a plausible generic phrase
    that matches coincidentally — a live run confirmed bare-name hits are mostly
    noise. The human triages 'url' hits first; 'name'-only hits are low-trust
    until read."""
    t = (term or "").lower()
    if "://" in t or "github.com/" in t or "/" in t:
        return "url"
    return "name"


# Ranking of match confidence for the digest sort: a confirmed url/owner-path
# hit outranks a body-corroborated name hit, which outranks an uncorroborated
# bare-name hit (the likely-coincidental namesake).
_MATCH_RANK = {"url": 2, "name": 1, "name-unconfirmed": 0}


def corroborators_for(cfg):
    """High-confidence tokens that confirm a bare-name hit really refers to the
    project: the url/owner-path forms of the match strings, the own-repo paths,
    and any configured context_terms. The bare project name itself is NOT a
    corroborator — it is the thing being confirmed."""
    toks = {s.lower() for s in cfg.get("match_strings", []) if match_kind(s) == "url"}
    toks.update(o.lower() for o in cfg.get("own_repos", []))
    toks.update(t.lower() for t in cfg.get("context_terms", []))
    return {t for t in toks if t}


def refine_match_type(cand, corroborators):
    """Body-scope confirmation. A 'name' hit stays 'name' only if its title or
    snippet also carries a corroborating token; otherwise it drops to
    'name-unconfirmed' (a likely coincidental namesake — a live run found ~71 of
    72 bare-name hits were namesakes). 'url' hits are already high-confidence and
    are left untouched."""
    if cand.get("match_type") != "name":
        return cand
    blob = f"{cand.get('title', '')} {cand.get('snippet', '')}".lower()
    if not any(tok in blob for tok in corroborators):
        cand["match_type"] = "name-unconfirmed"
    return cand


# --- gh helper ---------------------------------------------------------------


def gh_search_code(term, limit):
    """`gh search code <term> --json …`. Returns (list, err). Fail-soft: a
    non-zero exit or bad JSON returns ([], err) so the lane continues."""
    cmd = [
        "gh",
        "search",
        "code",
        term,
        "--limit",
        str(limit),
        "--json",
        "path,repository,sha,textMatches,url",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    except FileNotFoundError:
        sys.exit("gh CLI not found — install it and run 'gh auth login'")
    if proc.returncode != 0:
        err = proc.stderr.strip()[:500]
        low = err.lower()
        auth_markers = (
            "401",
            "bad credentials",
            "authentication",
            "gh auth login",
            "not logged in",
        )
        if any(marker in low for marker in auth_markers):
            sys.exit(f"gh authentication failed — run 'gh auth login': {err}")
        return [], err
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        return [], f"bad json: {exc}"
    return (data if isinstance(data, list) else []), None


# --- candidate builders ------------------------------------------------------


def node_to_candidate(node, term, lane):
    if not node or "url" not in node:
        return None
    repo = node.get("repository") or {}
    author = (node.get("author") or {}).get("login", "")
    title = node.get("title", "")
    # SECURITY: bodyText is UNTRUSTED EXTERNAL CONTENT. Truncated and stored for
    # a human to read; never interpreted as an instruction by this tool.
    body = (node.get("bodyText") or "").replace("\r", " ").replace("\n", " ")
    snippet = body[:500]
    return {
        "url": node["url"],
        "title": title,
        "created": node.get("createdAt", ""),
        "repo": repo.get("nameWithOwner", ""),
        "stars": repo.get("stargazerCount", 0),
        "comments": (node.get("comments") or {}).get("totalCount", 0),
        "is_answered": node.get("isAnswered"),
        "author": author,
        "snippet": snippet,
        "match": term,
        "match_type": match_kind(term),
        "kind": classify(title, snippet),
        "lane": lane,
    }


def code_hit_to_candidate(hit, term):
    if not isinstance(hit, dict) or "url" not in hit:
        return None
    repo = hit.get("repository") or {}
    path = hit.get("path", "")
    fragments = []
    for tm in hit.get("textMatches") or []:
        if isinstance(tm, dict) and tm.get("fragment"):
            fragments.append(tm["fragment"])
    # SECURITY: code fragments are UNTRUSTED EXTERNAL CONTENT. Joined, cleaned,
    # truncated, stored for a human to read; never run or interpreted.
    blob = " ".join(fragments) or path
    snippet = blob.replace("\r", " ").replace("\n", " ")[:500]
    return {
        "url": hit["url"],
        "title": path,
        "created": "",
        "repo": repo.get("nameWithOwner", ""),
        "stars": repo.get("stargazerCount", 0),
        "comments": 0,
        "is_answered": None,
        "author": "",
        "snippet": snippet,
        "match": term,
        "match_type": match_kind(term),
        "kind": classify(path, snippet),
        "lane": "code",
    }


# --- lanes -------------------------------------------------------------------


def thread_lane(cfg, since_date, errors):
    """Lane 1: per-match-string GraphQL search over issues and discussions."""
    results = []
    gql = (
        "query($q:String!,$n:Int!){ search(query:$q, type:%s, first:$n)"
        "{ nodes { %s } } }"
    )
    for term in cfg["match_strings"]:
        base = f'"{term}" created:>{since_date} sort:created-desc'
        for own in cfg["own_repos"]:
            base += f" -repo:{own}"
        for kind, fields in (
            ("ISSUE", ISSUE_FIELDS),
            ("DISCUSSION", DISCUSSION_FIELDS),
        ):
            qstr = f"{base} is:issue" if kind == "ISSUE" else base
            data, err = gh_graphql(gql % (kind, fields), q=qstr, n=cfg["per_query"])
            if err:
                errors.append(f"{term}/{kind}: {err}")
                continue
            for node in (data.get("data", {}).get("search", {}) or {}).get("nodes", []):
                cand = node_to_candidate(node, term, "thread")
                if cand:
                    results.append(cand)
    return results


def code_lane(cfg, errors):
    """Lane 2: `gh search code` for each match string. Surfaces listings,
    awesome-lists, README references, manifests where the project appears.
    GitHub code search has no created-at filter, so the seen-store and human
    gate are the freshness backstop here (not a time window)."""
    results = []
    if not cfg.get("scan_code_lane", True):
        return results
    for term in cfg["match_strings"]:
        hits, err = gh_search_code(term, cfg["per_query"])
        if err:
            errors.append(f"code/{term}: {err}")
            continue
        for hit in hits:
            cand = code_hit_to_candidate(hit, term)
            if cand:
                results.append(cand)
    return results


# --- commands ----------------------------------------------------------------


def planned_queries(cfg, since_date):
    """What scan WOULD query, for --dry-run. No gh/network calls."""
    lines = []
    own_filter = "".join(f" -repo:{own}" for own in cfg["own_repos"])
    for term in cfg["match_strings"]:
        base = f'"{term}" created:>{since_date} sort:created-desc{own_filter}'
        lines.append(f"thread/ISSUE      : {base} is:issue")
        lines.append(f"thread/DISCUSSION : {base}")
    if cfg.get("scan_code_lane", True):
        for term in cfg["match_strings"]:
            lines.append(f"code             : gh search code {term!r}")
    return lines


def load_config_for_dry_run(path):
    """Dry-run resolves to config.example.json when the live config.json is
    absent, so the queries can be previewed (no calls, no writes) before a
    project copies the example. A real scan still requires the live config."""
    if Path(path).exists():
        return load_config(path)
    example = Path(__file__).resolve().parent / "config.example.json"
    if example.exists():
        return load_config(str(example))
    return load_config(path)


def cmd_scan(args):
    if args.dry_run:
        cfg = load_config_for_dry_run(args.config)
    else:
        cfg = load_config(args.config)
    state_dir, state_file, ledger_file = state_paths(cfg)
    now = datetime.now(timezone.utc)

    if args.dry_run:
        # DRY-RUN: no gh, no network, no state. Print what scan WOULD query.
        if args.days is not None:
            since = now - timedelta(days=args.days)
        else:
            since = now - timedelta(days=cfg["default_window_days"])
        since_date = since.strftime("%Y-%m-%d")
        print(f"MENTION_SWEEP_DRY-RUN window>{since_date} (no calls, no writes)")
        for line in planned_queries(cfg, since_date):
            print(f"  {line}")
        return 0

    state = load_state(state_file)
    if args.days is not None:
        since = now - timedelta(days=args.days)
    elif state.get("last_run"):
        since = datetime.fromisoformat(state["last_run"])
    else:
        since = now - timedelta(days=cfg["default_window_days"])
    since_date = since.strftime("%Y-%m-%d")

    errors = []
    raw = thread_lane(cfg, since_date, errors) + code_lane(cfg, errors)

    # Precision pass: downgrade bare-name hits the body does not corroborate to
    # 'name-unconfirmed' so the likely-namesake noise ranks last, not first.
    corroborators = corroborators_for(cfg)
    for cand in raw:
        refine_match_type(cand, corroborators)

    seen = state.get("seen", {})
    posted = posted_urls(ledger_file)
    kept, dropped = [], {"seen": 0, "posted": 0, "stars": 0, "own": 0, "dup": 0}
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
        if is_own_repo(cand["repo"], cfg["own_repos"]):
            dropped["own"] += 1
            continue
        if cand["lane"] == "thread" and cand["stars"] < cfg["min_stars"]:
            dropped["stars"] += 1
            continue
        batch_urls.add(url)
        kept.append(cand)

    kept.sort(
        key=lambda c: (_MATCH_RANK.get(c["match_type"], 0), c["stars"], c["created"]),
        reverse=True,
    )
    per_repo, capped = {}, []
    for cand in kept:
        repo = cand["repo"]
        if repo and per_repo.get(repo, 0) >= cfg["per_repo_cap"]:
            dropped["repo_cap"] = dropped.get("repo_cap", 0) + 1
            continue
        if repo:
            per_repo[repo] = per_repo.get(repo, 0) + 1
        capped.append(cand)
    limit = args.limit or cfg["emit_cap"]
    kept = capped[:limit]

    today = now.date().isoformat()
    for cand in kept:
        seen[cand["url"]] = today
    cutoff = (now - timedelta(days=cfg["seen_retention_days"])).date().isoformat()
    state["seen"] = {u: d for u, d in seen.items() if d >= cutoff}
    state["last_run"] = now.isoformat()
    write_json_atomic(state_file, state)

    by_kind = {}
    by_match_type = {}
    for cand in kept:
        by_kind[cand["kind"]] = by_kind.get(cand["kind"], 0) + 1
        by_match_type[cand["match_type"]] = by_match_type.get(cand["match_type"], 0) + 1

    payload = {
        "scanned_at": now.isoformat(),
        "window_since": since_date,
        "by_kind": by_kind,
        "by_match_type": by_match_type,
        "posting_density": density_counts(ledger_file),
        "dropped": dropped,
        "errors": errors,
        "candidates": kept,
    }
    out = Path(cfg["candidates_file"])
    out.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    dens = payload["posting_density"]
    print(
        f"MENTION_SWEEP_OK window>{since_date} raw={len(raw)} kept={len(kept)} "
        f"by_kind={by_kind} by_match_type={by_match_type} dropped={dropped} errors={len(errors)}"
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
    _state_dir, _state, ledger_file = state_paths(cfg)
    comment = ""
    if args.comment_file:
        comment = Path(args.comment_file).read_text(encoding="utf-8").strip()
    entry = {
        "date": datetime.now(timezone.utc).isoformat(),
        "url": args.url,
        "kind": args.kind,
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
    scan = sub.add_parser("scan", help="run both lanes, write candidates JSON")
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
        help="preview the queries: no gh/network calls, no writes",
    )
    scan.set_defaults(func=cmd_scan)
    dens = sub.add_parser("density", help="posting counts from the ledger")
    dens.set_defaults(func=cmd_density)
    mark = sub.add_parser("mark-posted", help="record a posted reply")
    mark.add_argument("--url", required=True)
    mark.add_argument(
        "--kind",
        required=True,
        help="engage|correct|amplify|convert (your label for the ledger)",
    )
    mark.add_argument("--comment-file", default=None)
    mark.set_defaults(func=cmd_mark_posted)
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
