#!/usr/bin/env python3
"""newsletter-sweep - track newsletter outlets worth pitching, without burning any of them.

Tech newsletters (TLDR, Console, Changelog News, the *Weekly family) are
high-leverage distribution: one mention reaches thousands of readers who
opted in specifically to hear about tools like yours. But each outlet runs
its own submission channel, format, and cadence, and pitching one twice in a
short window (or off-format) burns it permanently - editors remember, and
unsubscribing is one click away for a reader who feels spammed.

This script does the deterministic half only. It holds a registry of outlets
with their submission mechanics (web form / email / GitHub PR / unknown),
best-effort checks whether each outlet's registered submission info still
looks current, and keeps a per-outlet cooldown ledger so a freshly-pitched
outlet is not pitched again inside its window. Every registry outlet is
checked and surfaced on every scan - the same "hand-curated, never silently
dropped" treatment cfp-sweep gives its own venue watchlist - and only the
cooldown removes one from candidates.json.

Per-outlet pitch drafting is NOT here. That is a human, with whatever
assistant they choose, behind a per-submission approval gate (see
SKILL.example.md). This script never drafts a pitch and never submits
anything, anywhere - there is no code path that sends an email, opens a web
form, or files a PR. issue #4 asked how to handle the email channel
specifically; the answer is draft-only, always. An email-channel outlet's
pitch is prepared as text for a human to send from their own mail client,
exactly like a web-form outlet's pitch is prepared as text for a human to
paste into that outlet's own form.

The freshness check's fetched page text is UNTRUSTED EXTERNAL CONTENT: it is
scanned only for a small set of marker phrases, never executed or followed.
A page that fetches fine but no longer carries its registered markers
reports `changed` rather than a guess at what moved - the same "never guess"
rule cfp-sweep's CFP-window detection follows for conference pages.

Requires: Python 3.10+. No `gh` CLI needed - the freshness check is a plain
public HTTP read of each outlet's own page.

Subcommands (all take --config; the default is the config.json beside this
script, so the module reads its own state and config from any directory):
  scan [--dry-run]                                     check the registry, write candidates
  mark-submitted --outlet NAME [--url U] [--note ...]   record a submission
  log                                                   show recorded submissions
  density                                               submission counts from the ledger
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sweepcore import (  # noqa: E402
    LaneReport,
    append_ledger,
    density_counts,
    earned_stamp,
    hold_reason,
    http_get,
    load_state,
    note_fetch_ok,
    resolve_module_path,
    window_start,
    write_json_atomic,
)

REQUIRED_KEYS = ["subject", "outlets"]
DEFAULTS = {
    "default_cooldown_days": 90,
    "default_window_days": 30,
    "state_dir": "state",
    "candidates_file": "candidates.json",
}

REQUIRED_OUTLET_KEYS = ["name", "url", "submit_channel", "submit_url_or_address"]
ALLOWED_CHANNELS = {"web-form", "email", "github-pr", "unknown"}

USER_AGENT = (
    "signal-sweep newsletter-sweep (https://github.com/signal-sweep/signal-sweep)"
)
HTTP_TIMEOUT = 15

# Fallback markers used only when a registry entry carries no curated
# alive_markers of its own (e.g. a submit_channel: unknown entry whose
# mechanics were never public enough to quote a specific phrase from). A
# match here is weaker evidence than a curated per-outlet marker - see
# check_outlet - so it is a fallback, never the first thing tried.
GENERIC_SUBMISSION_PHRASES = (
    "submit a link",
    "suggest a link",
    "suggest a story",
    "suggest a tool",
    "submit a tool",
    "submit a story",
    "recommend a tool",
    "share your project",
    "submission form",
    "send us a tip",
    "pitch us",
)

# Higher ranks first in the candidate sort - see _sort_key.
STATUS_RANK = {"alive": 2, "changed": 1, "unreachable": 0}

# An outlet with no ledger history at all has been waiting, in the sense the
# ranking cares about, longer than any outlet with an actual last-contact
# date - it sorts ahead of all of them rather than pretending to a contact
# date it doesn't have.
NEVER_CONTACTED_SENTINEL = 10**9


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
    if not isinstance(cfg["outlets"], list) or not cfg["outlets"]:
        sys.exit("config 'outlets' must be a non-empty list of outlet registry entries")
    for key, val in DEFAULTS.items():
        cfg.setdefault(key, val)
    return cfg


def load_config_for_dry_run(path):
    """Dry-run resolves to config.example.json when the live config.json is
    absent, so the registry can be previewed (no calls, no writes) before a
    project copies the example. Mirrors cfp-sweep so `scan --dry-run` works
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
    return (
        state_dir,
        state_dir / "newsletter_state.json",
        state_dir / "submitted_log.jsonl",
    )


# --- registry parsing --------------------------------------------------


def _validate_outlet(entry, advisory):
    """Validate + normalize one registry entry. Returns a normalized dict, or
    None (with an advisory message) if the entry is too broken to check at
    all. A malformed registry entry is a config typo, not a coverage gap, so
    it goes to advisory rather than the coverage-marker LaneReport - the same
    reasoning cfp-sweep and list-sweep apply to their own watchlists."""
    if not isinstance(entry, dict):
        advisory.append(f"registry entry is not an object, skipped: {entry!r}")
        return None
    missing = [
        k
        for k in REQUIRED_OUTLET_KEYS
        if not isinstance(entry.get(k), str) or not entry.get(k).strip()
    ]
    if missing:
        advisory.append(f"registry entry missing/invalid {missing}, skipped: {entry!r}")
        return None
    channel = entry["submit_channel"]
    if channel not in ALLOWED_CHANNELS:
        advisory.append(
            f"{entry['name']}: submit_channel {channel!r} not one of "
            f"{sorted(ALLOWED_CHANNELS)}, skipped"
        )
        return None

    cooldown_days = entry.get("cooldown_days")
    if (
        not isinstance(cooldown_days, int)
        or isinstance(cooldown_days, bool)
        or cooldown_days <= 0
    ):
        cooldown_days = None  # falls back to the config default at candidate time

    check_url = entry.get("check_url")
    if not isinstance(check_url, str) or not check_url.strip():
        check_url = entry["url"]

    markers = entry.get("alive_markers")
    if not isinstance(markers, list) or not all(
        isinstance(m, str) and m.strip() for m in markers
    ):
        markers = []

    format_note = entry.get("format_note")
    format_note = format_note if isinstance(format_note, str) else ""
    audience_note = entry.get("audience_note")
    audience_note = audience_note if isinstance(audience_note, str) else ""

    return {
        "name": entry["name"],
        "url": entry["url"],
        "submit_channel": channel,
        "submit_url_or_address": entry["submit_url_or_address"],
        "format_note": format_note,
        "audience_note": audience_note,
        "cooldown_days": cooldown_days,
        "check_url": check_url,
        "alive_markers": markers,
    }


# --- freshness check -----------------------------------------------------


def check_outlet(entry, report, advisory):
    """Best-effort fetch + classify one registry outlet's freshness-check page.

    Returns (status, note). status is alive/changed/unreachable. A request
    that never reaches a server (status is None: DNS failure, timeout,
    connection refused) is the only case that withholds the coverage marker -
    see note_fetch_ok below. Any HTTP response, even a 404 or a 500, is a
    completed request: the outlet's check_url just failed to answer usefully,
    which is exactly what `unreachable` honestly reports, not a sign this run
    failed to look.

    Fetched page text is UNTRUSTED EXTERNAL CONTENT: scanned only for marker
    phrase membership, never executed or followed.
    """
    status, body, err = http_get(
        entry["check_url"], timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}
    )
    if status is None:
        report.append(f"{entry['name']}: {err}")
        return "unreachable", f"fetch failed: {err}"
    note_fetch_ok(report)
    if status != 200:
        advisory.append(f"{entry['name']}: HTTP {status} on the freshness check")
        return "unreachable", f"HTTP {status}"

    low = body.lower()
    configured = entry["alive_markers"]
    markers = configured or GENERIC_SUBMISSION_PHRASES
    for marker in markers:
        if marker.lower() in low:
            kind = "outlet-specific" if configured else "generic"
            return "alive", f"{kind} alive marker matched ({marker!r})"
    return (
        "changed",
        "page fetched, no alive markers found - submission info may have moved",
    )


# --- ledger, cooldown, ranking --------------------------------------------


def last_contact_by_outlet(ledger_file):
    """Most recent contact date per normalized outlet name, from the ledger."""
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
        outlet = (entry.get("outlet") or "").strip().lower()
        if not outlet:
            continue
        day = when.date()
        if outlet not in latest or day > latest[outlet]:
            latest[outlet] = day
    return latest


def build_candidate(entry, status, note, last_contact, cfg, today):
    cooldown_days = entry["cooldown_days"] or cfg["default_cooldown_days"]
    last = last_contact.get(entry["name"].strip().lower())
    days_since = (today - last).days if last is not None else None
    return {
        "name": entry["name"],
        "url": entry["url"],
        "submit_channel": entry["submit_channel"],
        "submit_url_or_address": entry["submit_url_or_address"],
        "format_note": entry["format_note"],
        "audience_note": entry["audience_note"],
        "cooldown_days": cooldown_days,
        "status": status,
        "detection_note": note,
        "days_since_last_contact": days_since,
    }


def apply_cooldown(candidates):
    """The only exclusion from candidates.json: an outlet contacted more
    recently than its (per-outlet or default) cooldown window. Status
    (alive/changed/unreachable) never excludes an outlet on its own - a
    changed or unreachable registry entry needs a human's attention, and
    hiding it would bury exactly the drift the freshness check exists to
    surface. See build_candidate for days_since_last_contact."""
    kept, dropped = [], 0
    for cand in candidates:
        days_since = cand["days_since_last_contact"]
        if days_since is not None and days_since < cand["cooldown_days"]:
            dropped += 1
            continue
        kept.append(cand)
    return kept, dropped


def _sort_key(cand):
    days_since = cand["days_since_last_contact"]
    if days_since is None:
        days_since = NEVER_CONTACTED_SENTINEL
    return (STATUS_RANK[cand["status"]], days_since)


# --- scan ------------------------------------------------------------


def cmd_scan(args):
    now = datetime.now(timezone.utc)
    today = now.date()

    if args.dry_run:
        # No network, no state. Print exactly what we WOULD fetch.
        cfg = load_config_for_dry_run(args.config)
        print("NEWSLETTER_SWEEP_DRY-RUN (no network, no state writes)")
        print(f"  subject: {cfg['subject']}")
        print("  registry freshness checks would fetch:")
        for entry in cfg["outlets"]:
            if not isinstance(entry, dict):
                print(f"    (skipped malformed entry: {entry!r})")
                continue
            name = entry.get("name", "?")
            target = entry.get("check_url") or entry.get("url", "?")
            print(f"    {name} -> {target}")
        print(
            "  candidates would be written to: "
            f"{resolve_module_path(__file__, cfg['candidates_file'])}"
        )
        return 0

    cfg = load_config(args.config)
    state_dir, state_file, ledger_file = state_paths(cfg)
    state = load_state(state_file)
    # since/window_start feed only the earned-marker coverage proof below: the
    # registry is re-checked in full every scan rather than an incremental
    # query, so there is nothing for a day-count window to bound - the same
    # reasoning cfp-sweep's lane 1 documents for its own --days-less scan.
    since = window_start(
        state.get("last_run"),
        cfg["default_window_days"],
        now,
        None,
        label="newsletter-sweep",
    )

    report = LaneReport()
    advisory = []
    last_contact = last_contact_by_outlet(ledger_file)

    candidates = []
    for entry in cfg["outlets"]:
        parsed = _validate_outlet(entry, advisory)
        if parsed is None:
            continue
        status, note = check_outlet(parsed, report, advisory)
        candidates.append(
            build_candidate(parsed, status, note, last_contact, cfg, today)
        )

    status_counts = {"alive": 0, "changed": 0, "unreachable": 0}
    for cand in candidates:
        status_counts[cand["status"]] += 1

    kept, cooldown_dropped = apply_cooldown(candidates)
    kept.sort(key=_sort_key, reverse=True)

    # The marker advances only on a run that proved it covered the whole
    # registry: every outlet's check_url fetch reached a server (200 or a
    # non-200 both count - see check_outlet) and none came back with a true
    # network failure. A held run keeps the old marker so the registry is
    # re-checked in full next time rather than silently accepted as "already
    # covered".
    held = not report.clean
    if held:
        print(
            f"WARN {hold_reason(report)} — keeping last_run so the registry is "
            "re-checked in full next time",
            file=sys.stderr,
        )
    else:
        state["last_run"] = earned_stamp(state.get("last_run"), since, now)
    write_json_atomic(state_file, state)

    errors = list(report) + advisory
    dropped = {"cooldown": cooldown_dropped}
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
        f"NEWSLETTER_SWEEP_OK outlets={len(candidates)} alive={status_counts['alive']} "
        f"changed={status_counts['changed']} unreachable={status_counts['unreachable']} "
        f"dropped={dropped} errors={len(errors)}"
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
        "outlet": args.outlet,
        "url": args.url or "",
        "note": args.note or "",
    }
    append_ledger(ledger_file, entry)
    print(f"LEDGER_OK {args.outlet} <- submission recorded")
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
            f"  {e.get('date', '')[:10]}  {e.get('outlet', ''):<30} {e.get('url', '')}"
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

    scan = sub.add_parser("scan", help="check the registry, write candidates JSON")
    scan.add_argument(
        "--dry-run",
        action="store_true",
        help="preview only: no network calls, writes nothing, advances nothing",
    )
    scan.set_defaults(func=cmd_scan)

    mark = sub.add_parser("mark-submitted", help="record a submission in the ledger")
    mark.add_argument("--outlet", required=True, help="outlet name (registry 'name')")
    mark.add_argument("--url", default=None, help="the submission URL, if any")
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
