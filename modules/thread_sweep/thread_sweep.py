#!/usr/bin/env python3
"""thread-sweep — find the GitHub threads your project's docs already answer.

Deterministic discovery half of a human-gated workflow. Two lanes:
  lane 1 (query-first): GraphQL search per topic phrasing, time-windowed
  lane 2 (watchlist):   newest threads in pinned high-overlap repos

Dedup: a seen-store (every surfaced candidate is surfaced once) plus a
posted-response ledger (threads you have answered are excluded forever).
Judgment — fit scoring, drafting, the decision to post — is NOT here. That
belongs to a human, with whatever assistant they choose, behind a
per-comment approval gate. This script only retrieves, filters, records.

Requires: Python 3.10+, an authenticated GitHub CLI (`gh auth login`).

Subcommands (all take --config, default ./config.json):
  scan [--days N] [--limit N] [--dry-run]   run both lanes, write candidates
  density                                    posting counts from the ledger
  mark-posted --url U --pattern P [--comment-file F]   record a posted reply
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REQUIRED_KEYS = ["own_login", "own_repo", "queries"]
DEFAULTS = {
    "min_stars": 300,
    "per_repo_cap": 4,
    "per_query": 15,
    "emit_cap": 100,
    "seen_retention_days": 180,
    "default_window_days": 14,
    "state_dir": "state",
    "candidates_file": "candidates.json",
    "watchlist": [],
}

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
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        sys.exit(f"config missing required keys: {missing}")
    for key, val in DEFAULTS.items():
        cfg.setdefault(key, val)
    return cfg


def state_paths(cfg):
    state_dir = Path(cfg["state_dir"])
    return state_dir, state_dir / "sweep_state.json", state_dir / "posted_ledger.jsonl"


def gh_graphql(query, **variables):
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, val in variables.items():
        flag = "-F" if isinstance(val, int) else "-f"
        cmd += [flag, f"{key}={val}"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        return None, proc.stderr.strip()[:500]
    try:
        return json.loads(proc.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"bad json: {exc}"


def load_state(state_file):
    if state_file.exists():
        return json.loads(state_file.read_text(encoding="utf-8"))
    return {"last_run": None, "seen": {}}


def posted_urls(ledger_file):
    urls = set()
    if ledger_file.exists():
        for line in ledger_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    urls.add(json.loads(line)["url"])
                except (json.JSONDecodeError, KeyError):
                    continue
    return urls


def density_counts(ledger_file):
    now = datetime.now(timezone.utc)
    counts = {30: 0, 90: 0}
    if ledger_file.exists():
        for line in ledger_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                when = datetime.fromisoformat(json.loads(line)["date"])
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            age = (now - when).days
            for window in counts:
                if age <= window:
                    counts[window] += 1
    return counts


def node_to_candidate(node, pattern, lane):
    if not node or "url" not in node:
        return None
    repo = node.get("repository") or {}
    author = (node.get("author") or {}).get("login", "")
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
        "pattern": pattern,
        "lane": lane,
    }


def search_lane(cfg, since_date, errors):
    """Lane 1: per-topic GraphQL search over issues and discussions."""
    results = []
    gql = (
        "query($q:String!,$n:Int!){ search(query:$q, type:%s, first:$n)"
        "{ nodes { %s } } }"
    )
    for pattern, group in cfg["queries"].items():
        for phrase in group["phrases"]:
            base = (
                f"{phrase} created:>{since_date} sort:created-desc "
                f"-repo:{cfg['own_repo']}"
            )
            for kind, fields in (
                ("ISSUE", ISSUE_FIELDS),
                ("DISCUSSION", DISCUSSION_FIELDS),
            ):
                qstr = f"{base} is:open is:issue" if kind == "ISSUE" else base
                data, err = gh_graphql(gql % (kind, fields), q=qstr, n=cfg["per_query"])
                if err:
                    errors.append(f"{pattern}/{kind}: {err}")
                    continue
                for node in (data.get("data", {}).get("search", {}) or {}).get(
                    "nodes", []
                ):
                    cand = node_to_candidate(node, pattern, "query")
                    if cand:
                        results.append(cand)
    return results


def watchlist_lane(cfg, since_iso, errors):
    """Lane 2: newest issues + discussions in pinned repos."""
    results = []
    gql = """
    query($owner:String!,$name:String!){
      repository(owner:$owner,name:$name){
        issues(first:20, states:OPEN, orderBy:{field:CREATED_AT, direction:DESC}){
          nodes { title url createdAt bodyText state
                  repository { nameWithOwner stargazerCount }
                  comments { totalCount } author { login } }
        }
        discussions(first:20, orderBy:{field:CREATED_AT, direction:DESC}){
          nodes { title url createdAt bodyText isAnswered
                  repository { nameWithOwner stargazerCount }
                  comments { totalCount } author { login } }
        }
      }
    }"""
    for full in cfg["watchlist"]:
        owner, name = full.split("/")
        data, err = gh_graphql(gql, owner=owner, name=name)
        if err:
            errors.append(f"watchlist {full}: {err}")
            continue
        repo = (data.get("data", {}) or {}).get("repository") or {}
        for section in ("issues", "discussions"):
            for node in (repo.get(section) or {}).get("nodes") or []:
                if node.get("createdAt", "") <= since_iso:
                    continue
                cand = node_to_candidate(node, "watchlist", "watchlist")
                if cand:
                    results.append(cand)
    return results


def cmd_scan(args):
    cfg = load_config(args.config)
    state_dir, state_file, ledger_file = state_paths(cfg)
    state = load_state(state_file)
    now = datetime.now(timezone.utc)
    if args.days:
        since = now - timedelta(days=args.days)
    elif state.get("last_run"):
        since = datetime.fromisoformat(state["last_run"])
    else:
        since = now - timedelta(days=cfg["default_window_days"])
    since_date = since.strftime("%Y-%m-%d")
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    errors = []
    raw = search_lane(cfg, since_date, errors) + watchlist_lane(cfg, since_iso, errors)

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
        if cand["author"] == cfg["own_login"] or cand["repo"] == cfg["own_repo"]:
            dropped["own"] += 1
            continue
        if cand["lane"] == "query" and cand["stars"] < cfg["min_stars"]:
            dropped["stars"] += 1
            continue
        batch_urls.add(url)
        kept.append(cand)

    kept.sort(key=lambda c: (c["stars"], c["created"]), reverse=True)
    per_repo, capped = {}, []
    for cand in kept:
        repo = cand["repo"]
        if per_repo.get(repo, 0) >= cfg["per_repo_cap"]:
            dropped["repo_cap"] = dropped.get("repo_cap", 0) + 1
            continue
        per_repo[repo] = per_repo.get(repo, 0) + 1
        capped.append(cand)
    limit = args.limit or cfg["emit_cap"]
    kept = capped[:limit]

    if not args.dry_run:
        today = now.date().isoformat()
        for cand in kept:
            seen[cand["url"]] = today
        cutoff = (now - timedelta(days=cfg["seen_retention_days"])).date().isoformat()
        state["seen"] = {u: d for u, d in seen.items() if d >= cutoff}
        state["last_run"] = now.isoformat()
        state_dir.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state, indent=1), encoding="utf-8")

    payload = {
        "scanned_at": now.isoformat(),
        "window_since": since_date,
        "posting_density": density_counts(ledger_file),
        "dropped": dropped,
        "errors": errors,
        "candidates": kept,
    }
    out = Path(cfg["candidates_file"])
    out.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    dens = payload["posting_density"]
    mode = " DRY-RUN (state untouched)" if args.dry_run else ""
    print(
        f"THREAD_SWEEP_OK{mode} window>{since_date} raw={len(raw)} kept={len(kept)} "
        f"dropped={dropped} errors={len(errors)}"
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
    state_dir, _state, ledger_file = state_paths(cfg)
    comment = ""
    if args.comment_file:
        comment = Path(args.comment_file).read_text(encoding="utf-8").strip()
    state_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "date": datetime.now(timezone.utc).isoformat(),
        "url": args.url,
        "pattern": args.pattern,
        "comment": comment,
    }
    with ledger_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
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
