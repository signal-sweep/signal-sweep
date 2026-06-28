#!/usr/bin/env python3
"""sweepcore — shared primitives for the signal-sweep modules.

Every module runs the same five-stage pipeline (signal -> judge -> gate -> act
-> ledger). The judge/gate/act logic and each module's config schema stay in
the module; what lives here is the genuinely-identical plumbing that was being
copy-pasted into every new module (the fourth copy tripped CLAUDE.md's "extract
on the second use, not the first" rule):

  - state:   load_state, write_json_atomic
  - ledger:  posted_urls, density_counts, append_ledger
  - gh:      gh, gh_graphql   (auth failure -> exit with a 'gh auth login' hint)
  - http:    http_get         (public read with 429/503 Retry-After backoff)

Stdlib + the GitHub CLI only, matching the project's identity. Modules import
this via a sys.path shim so each one still runs standalone
(`cd modules/<name> && python <name>.py`).

Nothing here is outbound. The human approval gate lives in each module's mark-*
path, never in this file.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# gh stderr substrings that mean "you are not authenticated". An unauthenticated
# sweep is a misconfiguration, not a soft per-item failure, so these are fatal.
AUTH_MARKERS = (
    "401",
    "bad credentials",
    "authentication",
    "gh auth login",
    "not logged in",
)

DEFAULT_UA = "signal-sweep (+https://github.com/signal-sweep/signal-sweep)"


# --- state -------------------------------------------------------------------


def load_state(state_file):
    """Read a module's {"last_run", "seen"} state; reset cleanly if corrupt."""
    if state_file.exists():
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(
                f"WARN state file {state_file} was corrupt — resetting to empty state",
                file=sys.stderr,
            )
    return {"last_run": None, "seen": {}}


def write_json_atomic(path, obj, indent=1):
    """Write JSON via a temp file + os.replace so a crash never leaves a
    half-written state file. Creates the parent directory if needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=indent), encoding="utf-8")
    os.replace(tmp, path)


# --- ledger ------------------------------------------------------------------


def posted_urls(ledger_file):
    """Set of URLs already acted on, read from a JSONL ledger (excluded
    forever from future surfacing)."""
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
    """Posted-reply counts in the trailing 30- and 90-day windows."""
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


def append_ledger(ledger_file, entry):
    """Append one JSON object as a line to a ledger, creating the dir if needed.
    The caller builds the entry; this only records it (the act itself is gated
    upstream)."""
    ledger_file = Path(ledger_file)
    ledger_file.parent.mkdir(parents=True, exist_ok=True)
    with ledger_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


# --- gh ----------------------------------------------------------------------


def gh(args):
    """Run a gh command; return (parsed-JSON-or-raw-text, error-or-None). An
    auth failure or a missing gh binary exits with a 'gh auth login' hint."""
    try:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True, encoding="utf-8"
        )
    except FileNotFoundError:
        sys.exit("gh CLI not found — install it and run 'gh auth login'")
    if proc.returncode != 0:
        err = proc.stderr.strip()[:300]
        if any(marker in err.lower() for marker in AUTH_MARKERS):
            sys.exit(f"gh authentication failed — run 'gh auth login': {err}")
        return None, err
    out = proc.stdout.strip()
    try:
        return json.loads(out), None
    except json.JSONDecodeError:
        return out, None


def gh_graphql(query, **variables):
    """Run a gh GraphQL query; return (parsed-JSON, error-or-None). Int values
    are passed with -F (typed), everything else with -f."""
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, val in variables.items():
        flag = "-F" if isinstance(val, int) else "-f"
        cmd += [flag, f"{key}={val}"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    except FileNotFoundError:
        sys.exit("gh CLI not found — install it and run 'gh auth login'")
    if proc.returncode != 0:
        err = proc.stderr.strip()[:500]
        if any(marker in err.lower() for marker in AUTH_MARKERS):
            sys.exit(f"gh authentication failed — run 'gh auth login': {err}")
        return None, err
    try:
        return json.loads(proc.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"bad json: {exc}"


# --- http --------------------------------------------------------------------


def _retry_after_seconds(exc, attempt, backoff_base):
    """Seconds to wait before a retry: honour a Retry-After header if present
    (capped), else bounded exponential backoff."""
    header = exc.headers.get("Retry-After") if exc.headers else None
    if header:
        try:
            return min(float(header), 30.0)
        except ValueError:
            pass
    return min(backoff_base**attempt, 30.0)


def http_get(url, timeout=15, headers=None, retries=2, backoff_base=2.0):
    """Public GET -> (status, body, err). Retries on 429/503 honouring
    Retry-After (a transient rate-limit on one venue must not silently drop it
    for a whole run); other failures return immediately. err is None only on a
    response that was read; non-None is a short description for the caller's
    errors[] list."""
    hdrs = {"User-Agent": DEFAULT_UA}
    if headers:
        hdrs.update(headers)
    attempt = 0
    while True:
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace"), None
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and attempt < retries:
                time.sleep(_retry_after_seconds(exc, attempt, backoff_base))
                attempt += 1
                continue
            return exc.code, "", f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return None, "", str(exc)[:200]


# --- relevance tiering -------------------------------------------------------

TIER_RANK = {"high": 2, "med": 1, "low": 0}


def relevance_tier(cand):
    """A deterministic, explainable high/med/low fit band from whatever signals
    an answer-the-question candidate carries. A triage HINT for the human-judge
    step, never a verdict and never an auto-drop — everything stays in the
    digest, this only ranks it. The score is a sum of small named signals so a
    human can see why. Tuned for thread/forum/mention candidates (it reads
    is_answered, comments, match_type, pattern, stars/score_or_stars)."""
    score = 0
    # answer-gap: an open, unanswered thread is the best place to add an answer
    if cand.get("is_answered") is False:
        score += 2
    if (cand.get("comments") or 0) == 0:
        score += 1
    # match confidence (mention lane): a url/owner-path hit beats a bare name
    match_type = cand.get("match_type")
    if match_type == "url":
        score += 2
    elif match_type == "name-unconfirmed":
        score -= 1
    # topic specificity: a real pattern match beats a generic watchlist pull
    pattern = cand.get("pattern")
    if pattern and pattern != "watchlist":
        score += 1
    # venue notability
    popularity = cand.get("stars")
    if popularity is None:
        popularity = cand.get("score_or_stars") or 0
    if popularity >= 1000:
        score += 1
    if score >= 3:
        return "high"
    if score >= 1:
        return "med"
    return "low"
