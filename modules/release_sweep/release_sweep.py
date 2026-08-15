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

`brief` is what enforces that. It reads its own ledger before assembling
anything, so a channel already recorded for this repo and version drops out of
the material, and a release announced everywhere is reported as such rather
than re-scaffolded. `--force` re-admits those channels, clearly marked, for a
deliberate redraft.

The ledger sits beside the module, so one checkout holds one ledger for every
repo it is run against. Entries therefore record the repo, and the guard keys
on (repo, version, channel) - otherwise announcing org/a's v1.0.0 would
suppress a genuinely new org/b v1.0.0. See announced_channels for how a
pre-repo ledger line is migrated.

Subcommands (all take --config; the default is the channels.json beside this
script, so the module reads its own state and config from any directory):
  brief [--repo O/R] [--tag vX] [--since vY] [--force]   assemble release material
  mark-announced --version vX --channel NAME [--repo O/R] [--note ...]
                                               record a posted announcement
  log                                          show what's been announced
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sweepcore import append_ledger, gh, resolve_module_path  # noqa: E402

# Module-anchored, not CWD-anchored: the announced-ledger and the assembled
# brief belong to this module wherever it is invoked from. Anchoring these on
# the CWD meant a run from the repo root read an empty ledger and wrote the
# brief somewhere the module would never read it back.
LEDGER_PATH = resolve_module_path(__file__, "state/announced_log.jsonl")
BRIEF_PATH = resolve_module_path(__file__, "release_brief.json")
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


def ledger_entries(ledger_file):
    """Announcement records from the JSONL ledger, oldest first.

    A blank or unparseable line is skipped rather than failing the read: the
    ledger is append-only history and one bad line must not blind the
    double-announce guard to every good one.
    """
    entries = []
    path = Path(ledger_file)
    if not path.exists():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def normalise_key(value):
    """Fold one ledger key field (repo / version / channel) to its match form.

    The three key fields are operator-typed free text, so they drift: a channel
    recorded as "Show HN" against a registry that calls it "show hn ", a tag
    typed "V1.0.0". Comparing raw strings made that drift fail OPEN - no match,
    so an already-announced channel came back into the material and invited a
    second announcement. Whitespace is collapsed and case folded on both the
    write and the read side, so drift fails CLOSED instead.

    Case folding the version deliberately treats "V1.0.0" and "v1.0.0" as one
    release. Two real tags differing only in case is vanishingly rare; a typo
    is not, and suppressing (recoverable with --force) beats double-posting.
    Repo names are already case-insensitive on GitHub. A non-string folds to
    "", which never matches a real key.
    """
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).casefold()


def announced_channels(version, ledger_file=None, repo=None):
    """Channels already recorded for this repo + version.

    Returns {normalised channel name: "repo" | "legacy"}, where "legacy" marks
    a match made by a ledger line written before entries carried a repo.

    The dedup key is (repo, version, channel), not (version, channel): the
    ledger lives beside the module, so one checkout shares one ledger across
    every repo it is run against. Keying without the repo meant announcing
    org/a's v1.0.0 everywhere silently suppressed a genuinely new org/b v1.0.0
    - a tag this common collides across repos almost immediately.

    MIGRATION - a legacy line (no "repo" field) applies to EVERY repo. It fails
    closed: the repo that wrote it is still guarded, at the cost of
    over-matching others. The alternative (apply to no repo) would silently
    re-open the double-announce hole for the repo that actually posted. Callers
    report a legacy match rather than acting on it silently, and either --force
    or adding a "repo" field to that line clears it.
    """
    entries = ledger_entries(LEDGER_PATH if ledger_file is None else ledger_file)
    want_version = normalise_key(version)
    want_repo = normalise_key(repo)
    found = {}
    for entry in entries:
        channel = normalise_key(entry.get("channel"))
        if not channel or normalise_key(entry.get("version")) != want_version:
            continue
        entry_repo = normalise_key(entry.get("repo"))
        if not entry_repo:
            found.setdefault(channel, "legacy")
        elif entry_repo == want_repo:
            # An exact repo match outranks a legacy one for the same channel.
            found[channel] = "repo"
    return found


def print_legacy_note(legacy):
    """Report channels suppressed by a pre-repo ledger line.

    A legacy line matches every repo, so this suppression can be one repo's
    history blocking another's release. That must never be silent - the whole
    complaint about a repo-less key is that the operator cannot see why a new
    release went quiet.
    """
    if not legacy:
        return
    print(
        f"  NOTE {', '.join(legacy)}: matched by a pre-repo ledger line, which"
        " applies to every repo."
    )
    print(
        '  Add a "repo" field to that line (or delete it) to scope it;'
        " --force re-admits the channel meanwhile."
    )


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

    # The double-announce guard. `brief` reads its own ledger (never writes it -
    # recording stays the human's mark-announced step) and drops any channel
    # already announced for this exact repo + version. --force keeps them,
    # marked, for a deliberate redraft; it changes what gets assembled, never
    # what gets sent.
    announced = announced_channels(tag, repo=repo)
    already = sorted(
        c.get("name", "?")
        for c in channels
        if normalise_key(c.get("name")) in announced
    )
    legacy = sorted(
        c.get("name", "?")
        for c in channels
        if announced.get(normalise_key(c.get("name"))) == "legacy"
    )
    if args.force:
        channels = [
            {**c, "already_announced": True}
            if normalise_key(c.get("name")) in announced
            else c
            for c in channels
        ]
    else:
        channels = [
            c for c in channels if normalise_key(c.get("name")) not in announced
        ]

    prev = args.since or previous_tag(repo, tag)
    commits, stats = [], {}
    if prev:
        cmp_data, cmp_err = gh(["api", f"repos/{repo}/compare/{prev}...{tag}"])
        if not cmp_err and isinstance(cmp_data, dict):
            raw = cmp_data.get("commits", []) or []
            # `or [""]` guards commits with an empty message (git allows them
            # via --allow-empty-message); bucket_commits skips falsy subjects.
            commits = [
                (c.get("commit", {}).get("message", "").splitlines() or [""])[0]
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
        "already_announced": already,
        "already_announced_legacy": legacy,
    }
    BRIEF_PATH.write_text(json.dumps(brief, indent=1), encoding="utf-8")

    if not channels:
        # Every active channel is in the ledger for this version: say so loudly
        # instead of handing back material that invites a second announcement.
        print(
            f"RELEASE_BRIEF_SKIP {repo} {tag} - already announced to every active"
            f" channel: {', '.join(already)}"
        )
        print_legacy_note(legacy)
        print(f"  nothing to draft; ledger -> {LEDGER_PATH}")
        print("  Re-run with --force only if you deliberately need a redraft.")
        return 0

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
    if already:
        label = (
            "already announced (re-admitted by --force - redraft)"
            if args.force
            else "already announced for this version, skipped"
        )
        print(f"  {label}: {', '.join(already)}")
        print_legacy_note(legacy)
    if paused:
        print(f"  paused (active:false, skipped): {', '.join(paused)}")
    print(f"  material -> {BRIEF_PATH}")
    print("  Next: an assistant drafts one announcement per channel from the brief;")
    print("  you approve and post each individually, then `mark-announced`.")
    return 0


def cmd_mark_announced(args):
    # The repo belongs in the entry: one checkout shares one ledger, so an
    # entry that cannot say which repo it announced suppresses all of them.
    # The three key fields are stored normalised, so the file itself holds the
    # form the guard matches on and operator drift cannot re-open the hole.
    repo = resolve_repo(args.repo)
    entry = {
        "date": datetime.now(timezone.utc).isoformat(),
        "repo": normalise_key(repo),
        "version": normalise_key(args.version),
        "channel": normalise_key(args.channel),
        "note": args.note or "",
    }
    append_ledger(LEDGER_PATH, entry)
    # Echo the stored form, not the typed form: the operator should see the key
    # the guard will match on, so a surprising fold is visible at record time.
    print(f"LEDGER_OK {entry['repo']} {entry['version']} -> {entry['channel']}")
    return 0


def cmd_log(args):
    entries = ledger_entries(LEDGER_PATH)
    if not entries:
        print("no announcements recorded yet")
        return 0
    for e in entries:
        # A pre-repo line is labelled rather than blank: it matches every repo,
        # which is worth seeing in the history it is read from.
        repo = e.get("repo") or "(any repo)"
        print(
            f"  {e.get('date', '')[:10]}  {repo:<22} {e.get('version', ''):<10}"
            f" {e.get('channel', '')}"
        )
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        # Default resolves beside the module, not beside the CWD; an explicitly
        # passed --config is used verbatim (the user typed it, they meant it).
        default=str(resolve_module_path(__file__, "channels.json")),
        help="path to config (default: channels.json in the module directory)",
    )
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
    brief.add_argument(
        "--force",
        action="store_true",
        help="re-admit channels already announced for this version (marked, for "
        "a deliberate redraft); assembles material only, never posts",
    )
    brief.set_defaults(func=cmd_brief)

    mark = sub.add_parser("mark-announced", help="record a posted announcement")
    mark.add_argument("--version", required=True)
    mark.add_argument("--channel", required=True)
    mark.add_argument(
        "--repo",
        default=None,
        help="OWNER/NAME the announcement was for (default: detect from cwd). "
        "Recorded in the ledger so the guard scopes to this repo.",
    )
    mark.add_argument("--note", default=None)
    mark.set_defaults(func=cmd_mark_announced)

    log = sub.add_parser("log", help="show what's been announced")
    log.set_defaults(func=cmd_log)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
