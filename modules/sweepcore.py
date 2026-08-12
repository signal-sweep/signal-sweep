#!/usr/bin/env python3
"""sweepcore — shared primitives for the signal-sweep modules.

Every module runs the same five-stage pipeline (signal -> judge -> gate -> act
-> ledger). The judge/gate/act logic and each module's config schema stay in
the module; what lives here is the genuinely-identical plumbing that was being
copy-pasted into every new module (the fourth copy tripped CLAUDE.md's "extract
on the second use, not the first" rule):

  - paths:   resolve_module_path  (module-anchored, never CWD-anchored)
  - state:   load_state, write_json_atomic
  - ledger:  posted_urls, density_counts, append_ledger
  - gh:      gh, gh_graphql   (auth failure -> exit with a 'gh auth login' hint)
  - http:    http_get         (public read with 429/503 Retry-After backoff)
  - window:  LaneReport, note_fetch_ok, parse_stamp, window_start, earned_stamp,
             hold_reason     (the earned-marker rule every scanning module obeys)

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
from datetime import datetime, timedelta, timezone
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


# --- paths -------------------------------------------------------------------


def resolve_module_path(module_file, value):
    """Anchor a module-owned relative path on the MODULE's own directory.

    A module's state dir, candidates file and default config belong to that
    module, not to whatever directory the process happened to start in. Reading
    them relative to the CWD meant that running two modules from the repo root
    pointed both at one shared ./candidates.json and one shared ./state/: the
    second scan overwrote the first's candidates, and each module then read a
    brand-new (empty) state file, silently re-windowing to the first-run default
    instead of its real last_run. Anchoring on __file__ gives every module one
    canonical location, so `python modules/<name>/<name>.py` from anywhere reads
    and writes exactly what `cd modules/<name> && python <name>.py` always did.

    `module_file` is the calling module's __file__. An absolute `value` is
    returned unchanged, so an explicit override still wins.

    The join is normalised lexically (os.path.normpath, no filesystem access and
    no symlink rewriting) so a cross-module value like
    "../placement_health/placements.json" prints and compares as the real
    location rather than a path with a `..` still in the middle of it.
    """
    path = Path(value)
    if path.is_absolute():
        return path
    return Path(os.path.normpath(Path(module_file).resolve().parent / path))


# --- state -------------------------------------------------------------------


def load_state(state_file):
    """Read a module's {"last_run", "seen"} state; reset cleanly if corrupt.

    Corrupt covers two cases, because callers only ever treat the result as a
    mapping (state.get("last_run"), state["seen"]): JSON that does not parse,
    and JSON that parses to something other than an object (a bare list, string,
    number or null). Both reset to an empty state with a warning on stderr, so
    the reset is loud rather than a silent data loss.
    """
    if state_file.exists():
        try:
            loaded = json.loads(state_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            reason = "was corrupt"
        else:
            if isinstance(loaded, dict):
                return loaded
            reason = f"held a JSON {type(loaded).__name__}, not an object"
        print(
            f"WARN state file {state_file} {reason} — resetting to empty state",
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


# --- window markers ----------------------------------------------------------
#
# Every scanning module stores a last_run marker and starts its next window
# there. That marker is a claim about COVERAGE — "everything published after
# this instant has been looked at" — so it may only move when this run actually
# did the looking. A run that errored, was skipped, ran a disabled or
# unconfigured lane, or re-windowed because the stored marker no longer parses
# has NOT covered that stretch, and stamping `now` over it loses the stretch
# silently and permanently because nothing downstream ever revisits it.
#
# The opposite failure is just as real: a marker that never advances re-scans a
# widening window forever. A fetch that came back holding NOTHING is a genuine
# empty window and earns the advance. The rule is therefore about proof of
# coverage, not about finding something:
#
#   advance only on a completed fetch that reached back to the prior marker;
#   hold otherwise, and never invent a marker a failed run did not earn.


class LaneReport(list):
    """A lane's (or a run's) record of its own fetching.

    Doubles as the plain `errors` list the lane helpers already take — they only
    ever append a string to it — and adds the one count a length-zero result set
    cannot supply on its own: whether any request was answered at all. "Covered
    the window and found nothing" and "never got an answer" both produce zero
    candidates, and only the first has earned a new marker.
    """

    def __init__(self):
        super().__init__()
        self.fetches_ok = 0

    def fetch_ok(self):
        """Record one request that came back. An empty result set still counts —
        that is a covered window, not a missing one."""
        self.fetches_ok += 1

    @property
    def clean(self):
        """True only if at least one request came back and nothing failed."""
        return self.fetches_ok > 0 and not self


def note_fetch_ok(errors):
    """Record a completed fetch when the caller passed a LaneReport.

    Lane helpers are also called with a plain list (tests, ad-hoc use), which
    carries no counter to bump; this is a no-op there rather than an error.
    """
    recorder = getattr(errors, "fetch_ok", None)
    if recorder is not None:
        recorder()


def hold_reason(report):
    """Short phrase for the stderr warning when a run did not earn its marker.

    Two shapes: something was tried and failed, or nothing was ever tried (the
    lane is off, was skipped, or has nothing configured to query). Kept short
    because callers embed it in a longer sentence, sometimes alongside the names
    of the lanes it applies to.
    """
    return "requests failed" if report else "no request completed"


def parse_stamp(stamp):
    """Parse a stored ISO marker into an aware datetime; None if absent or
    unreadable. A naive stamp reads as UTC, which is what the modules write."""
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def window_start(stamp, default_days, now, days_override=None, label="last_run"):
    """Where this run's window begins: an explicit --days override, else the
    stored marker, else the first-run default window.

    A marker that will not parse falls back to the default window WITH a warning
    on stderr. Falling back silently would re-window the lane without anyone
    noticing the marker had rotted; the matching earned_stamp call leaves the
    rotted value on disk, so the warning keeps firing until a human clears it.
    """
    if days_override is not None:
        return now - timedelta(days=days_override)
    since = parse_stamp(stamp)
    if since is not None:
        return since
    if stamp:
        print(
            f"WARN unreadable last_run for {label} ({stamp!r}) — falling back to "
            f"the {default_days}-day default window",
            file=sys.stderr,
        )
    return now - timedelta(days=default_days)


def earned_stamp(prior_raw, since, now):
    """The marker a cleanly-fetched run earns, given the marker it started from.

    `now`, but only when the run reached back to the previous marker. Two things
    start a window AFTER a stored marker, and both leave a stretch this run never
    looked at:

    * `--days 3` against a marker 30 days old — the 27 days in front of the
      window are uncovered.
    * a marker that no longer parses, which drops the run back to the default
      window with no way to tell whether that reaches the marker or stops short.

    Stamping `now` for either swallows the uncovered stretch silently and
    permanently, so the stored marker stands instead: after a narrowed run the
    next default run re-covers the gap, and after a rotted one the warning from
    window_start keeps firing until a human clears the marker — loud and
    recoverable, rather than quiet and lost.

    Nothing stored at all is a genuine first run: there is no earlier marker to
    fall short of, so a completed fetch earns `now` (one that came back empty
    included). Without that branch no marker is ever laid down and the run
    re-scans the default window forever, which is the same bug facing the other
    way. The caller only reaches here on a clean run; a failed one must not call
    this at all, so it cannot invent a marker it never had.
    """
    prior = parse_stamp(prior_raw)
    if prior is None:
        return prior_raw if prior_raw else now.isoformat()
    if since > prior:
        return prior_raw
    return now.isoformat()


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
                if when.tzinfo is None:
                    # A hand-edited or migrated ledger line may carry a naive
                    # date; treat it as UTC rather than crashing the whole run.
                    when = when.replace(tzinfo=timezone.utc)
                age = (now - when).days
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                continue
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
    scheme = url.split(":", 1)[0].lower() if ":" in url else ""
    if scheme not in ("http", "https"):
        # Defence in depth: nothing in this codebase should ever ask for a
        # file://, ftp:// or other non-web scheme; refuse rather than let
        # urllib service it.
        return None, "", f"unsupported url scheme: {scheme or '(none)'}"
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
