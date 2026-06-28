#!/usr/bin/env python3
"""release-sweep - turn a release into per-channel announcement material.

When you cut a release, the announcement work is repetitive: restate what
changed for Hacker News, for a social post, for a changelog entry, for a
newsletter curator - each with its own length and tone. This module does the
deterministic half: it pulls the real release (tag, notes, and the commit
diff since the previous tag) and pairs it with your channel registry, so an
assistant has exact material to draft from instead of inventing it.

It does NOT post, and it does NOT draft the final copy. Drafting is judgment
(a human, or an assistant working for one); posting is an outbound action and
goes through per-channel human approval. The script only assembles material
and records what was announced, so a release is never announced twice to the
same channel.

Requires: Python 3.10+, an authenticated GitHub CLI (`gh auth login`).

Subcommands (all take --config, default ./channels.json):
  brief [--repo O/R] [--tag vX] [--since vY]   assemble release material + scaffolds
  mark-announced --version vX --channel NAME [--note ...]   record a posted announcement
  log                                          show what's been announced
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sweepcore import gh  # noqa: E402

LEDGER_PATH = Path("state/announced_log.jsonl")
BRIEF_PATH = Path("release_brief.json")
MAX_COMMITS = 50

# Conventional-Commit prefix: type, optional (scope), optional ! bang, then colon.
_CC_PREFIX = re.compile(
    r"^(?P<type>[a-z]+)(?:\([^)]*\))?(?P<bang>!)?:\s*", re.IGNORECASE
)


def bucket_commits(subjects):
    """Group commit subject lines by Conventional-Commit prefix.

    Returns a dict with four ordered buckets: breaking, feat, fix, other.
    A subject lands in exactly one bucket (breaking > feat > fix > other).
    The `type(scope):` prefix is stripped so the human-readable change shows.
    """
    buckets = {"breaking": [], "feat": [], "fix": [], "other": []}
    for subject in subjects:
        if not subject:
            continue
        m = _CC_PREFIX.match(subject)
        ctype = m.group("type").lower() if m else None
        bang = bool(m.group("bang")) if m else False
        stripped = subject[m.end() :] if m else subject
        if bang or "BREAKING CHANGE" in subject:
            buckets["breaking"].append(stripped)
        elif ctype == "feat":
            buckets["feat"].append(stripped)
        elif ctype == "fix":
            buckets["fix"].append(stripped)
        else:
            buckets["other"].append(stripped)
    return buckets


def load_channels(path):
    cfg_path = Path(path)
    if not cfg_path.exists():
        sys.exit(
            f"config not found: {cfg_path}\n"
            "Copy channels.example.json to channels.json and edit it."
        )
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"config is not valid JSON ({cfg_path}): {exc}")
    chans = cfg.get("channels")
    if not isinstance(chans, list) or not chans:
        sys.exit("config must have a non-empty 'channels' list")
    return chans


def resolve_repo(arg):
    if arg:
        return arg
    data, err = gh(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
    if err or not data:
        sys.exit("could not detect repo - pass --repo OWNER/NAME")
    return data if isinstance(data, str) else data.get("nameWithOwner", "")


def previous_tag(repo, current):
    data, err = gh(
        ["release", "list", "--repo", repo, "--limit", "10", "--json", "tagName"]
    )
    if err or not isinstance(data, list):
        return None
    tags = [r.get("tagName") for r in data if r.get("tagName")]
    for i, t in enumerate(tags):
        if t == current and i + 1 < len(tags):
            return tags[i + 1]
    return None


def cmd_brief(args):
    all_channels = load_channels(args.config)
    paused = [c.get("name", "?") for c in all_channels if c.get("active") is False]
    channels = [c for c in all_channels if c.get("active") is not False]
    if not channels:
        sys.exit("every channel is paused (active:false) - nothing to assemble")
    repo = resolve_repo(args.repo)

    fields = "tagName,name,publishedAt,body,url"
    view_args = ["release", "view", "--repo", repo, "--json", fields]
    if args.tag:
        view_args.insert(2, args.tag)
    rel, err = gh(view_args)
    if err or not isinstance(rel, dict):
        sys.exit(f"could not read release for {repo}: {err or 'no release found'}")

    tag = rel.get("tagName", "")
    prev = args.since or previous_tag(repo, tag)
    commits, stats = [], {}
    if prev:
        cmp_data, cmp_err = gh(["api", f"repos/{repo}/compare/{prev}...{tag}"])
        if not cmp_err and isinstance(cmp_data, dict):
            raw = cmp_data.get("commits", []) or []
            commits = [
                c.get("commit", {}).get("message", "").splitlines()[0]
                for c in raw
                if c.get("commit")
            ][:MAX_COMMITS]
            stats = {
                "commits": cmp_data.get("total_commits", len(raw)),
                "files": len(cmp_data.get("files", []) or []),
            }

    brief = {
        "repo": repo,
        "version": tag,
        "name": rel.get("name", ""),
        "date": rel.get("publishedAt", ""),
        "url": rel.get("url", ""),
        "previous_tag": prev,
        "notes": rel.get("body", ""),
        "commit_subjects": commits,
        "highlights": bucket_commits(commits),
        "stats": stats,
        "channels": channels,
    }
    BRIEF_PATH.write_text(json.dumps(brief, indent=1), encoding="utf-8")

    print(
        f"RELEASE_BRIEF_OK {repo} {tag}"
        + (f" (since {prev})" if prev else " (first release)")
    )
    if stats:
        print(
            f"  {stats.get('commits', '?')} commits, {stats.get('files', '?')} files changed"
        )
    print(
        f"  {len(commits)} commit subjects captured; notes {len(brief['notes'])} chars"
    )
    h = brief["highlights"]
    print(
        f"  highlights: {len(h['breaking'])} breaking / {len(h['feat'])} feat"
        f" / {len(h['fix'])} fix / {len(h['other'])} other"
    )
    print(f"  channels to draft for: {', '.join(c.get('name', '?') for c in channels)}")
    if paused:
        print(f"  paused (active:false, skipped): {', '.join(paused)}")
    print(f"  material -> {BRIEF_PATH}")
    print("  Next: an assistant drafts one announcement per channel from the brief;")
    print("  you approve and post each individually, then `mark-announced`.")
    return 0


def cmd_mark_announced(args):
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "date": datetime.now(timezone.utc).isoformat(),
        "version": args.version,
        "channel": args.channel,
        "note": args.note or "",
    }
    with LEDGER_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    print(f"LEDGER_OK {args.version} -> {args.channel}")
    return 0


def cmd_log(args):
    if not LEDGER_PATH.exists():
        print("no announcements recorded yet")
        return 0
    for line in LEDGER_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        print(
            f"  {e.get('date', '')[:10]}  {e.get('version', ''):<10} {e.get('channel', '')}"
        )
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="channels.json")
    sub = parser.add_subparsers(dest="cmd", required=True)

    brief = sub.add_parser(
        "brief", help="assemble release material + channel scaffolds"
    )
    brief.add_argument(
        "--repo", default=None, help="OWNER/NAME (default: detect from cwd)"
    )
    brief.add_argument("--tag", default=None, help="release tag (default: latest)")
    brief.add_argument(
        "--since", default=None, help="previous tag (default: auto-detect)"
    )
    brief.set_defaults(func=cmd_brief)

    mark = sub.add_parser("mark-announced", help="record a posted announcement")
    mark.add_argument("--version", required=True)
    mark.add_argument("--channel", required=True)
    mark.add_argument("--note", default=None)
    mark.set_defaults(func=cmd_mark_announced)

    log = sub.add_parser("log", help="show what's been announced")
    log.set_defaults(func=cmd_log)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
