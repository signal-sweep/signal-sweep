#!/usr/bin/env python3
"""placement-health - verify your project's listings stayed live.

Curated lists prune entries, links rot, and a submission you fought to land
quietly disappears months later with no notification. This module watches a
registry of placements (awesome-list entries, directory listings, your own
pages) and reports which are LIVE, which got DROPPED, and which are BROKEN.

It is *presence intelligence*, not outreach: it only reads public URLs and
reports. Nothing is posted, so there is no approval gate to clear; the gate
in this toolkit guards outbound actions, and this module has none.

Requires: Python 3.10+ (standard library only).

Subcommands (all take --config, default ./placements.json):
  check [--json] [--timeout N] [--log]   fetch every placement, report state
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

UA = "placement-health (https://github.com/signal-sweep/signal-sweep)"
DEFAULT_TIMEOUT = 15
LOG_PATH = Path("state/health_log.jsonl")

# status -> exit-affecting? LIVE/PENDING are fine; DROPPED/BROKEN are findings.
OK_STATES = {"LIVE", "PENDING_OK", "PENDING_MERGED"}


def load_config(path):
    cfg_path = Path(path)
    if not cfg_path.exists():
        sys.exit(
            f"config not found: {cfg_path}\n"
            "Copy placements.example.json to placements.json and edit it."
        )
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"config is not valid JSON ({cfg_path}): {exc}")
    items = cfg.get("placements")
    if not isinstance(items, list) or not items:
        sys.exit("config must have a non-empty 'placements' list")
    for i, p in enumerate(items):
        if not isinstance(p, dict) or "url" not in p or "expect" not in p:
            sys.exit(f"placements[{i}] needs at least 'url' and 'expect'")
    return cfg


def fetch(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body, None
    except urllib.error.HTTPError as exc:
        return exc.code, "", f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, "", str(exc)[:200]


def check_one(placement, timeout):
    """Return (state, detail). Pure of side effects beyond the network read."""
    pending = placement.get("status") == "pending"
    status, body, err = fetch(placement["url"], timeout)
    present = placement["expect"] in body if body else False
    if err:
        # A pending entry that 404s is normal (not merged yet); not a finding.
        return ("PENDING_OK", f"not yet live ({err})") if pending else ("BROKEN", err)
    if pending:
        return (
            ("PENDING_MERGED", "expect-string now present - promote to live")
            if present
            else ("PENDING_OK", "still pending")
        )
    if present:
        return "LIVE", "expect-string present"
    return "DROPPED", "200 OK but expect-string absent - pruned from the listing"


def cmd_check(args):
    cfg = load_config(args.config)
    timeout = args.timeout or DEFAULT_TIMEOUT
    results = []
    for p in cfg["placements"]:
        state, detail = check_one(p, timeout)
        results.append(
            {
                "name": p.get("name", p["url"]),
                "url": p["url"],
                "state": state,
                "detail": detail,
            }
        )

    if args.log:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat()
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            for r in results:
                fh.write(json.dumps({"checked_at": stamp, **r}) + "\n")

    findings = [r for r in results if r["state"] not in OK_STATES]
    if args.json:
        print(json.dumps({"results": results, "findings": len(findings)}, indent=1))
    else:
        for r in results:
            mark = "OK " if r["state"] in OK_STATES else "!! "
            print(f"{mark}{r['state']:<15} {r['name']}")
            if r["state"] not in OK_STATES or r["state"] == "PENDING_MERGED":
                print(f"     {r['detail']}  <- {r['url']}")
        print(
            f"\n{len(results)} checked, {len(findings)} finding(s) "
            f"(DROPPED/BROKEN). PENDING_MERGED means a submission landed - promote it."
        )
    return 1 if findings else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="placements.json")
    sub = parser.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("check", help="fetch every placement, report state")
    check.add_argument("--json", action="store_true")
    check.add_argument("--timeout", type=int, default=None)
    check.add_argument(
        "--log", action="store_true", help="append results to state/health_log.jsonl"
    )
    check.set_defaults(func=cmd_check)
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
