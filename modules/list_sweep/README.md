# list-sweep

Find the curated lists and directories where your project could be listed, work out how each one takes submissions, and draft an entry for it. One at a time, through a human gate.

Placement is the top of the discoverability funnel. An awesome-list or directory is how someone who has never heard of your project finds it. This module does the deterministic half of getting listed: it discovers candidate lists, detects each one's intake mechanics, and drafts a submission stub. It never submits anything.

## How discovery works

**Lane 1 (query-first).** Each search keyword in your config (`awesome-claude-code`, `awesome-ai-agents`, and so on) becomes a `gh search repos` query scoped to list-shaped repos, sorted by stars and windowed from the last run. This is recall: it casts wide, and you decide what actually fits.

**Lane 2 (watchlist).** A seed list of directories you already know are targets. They get classified the same way as lane-1 hits, so a list you have in mind still gets its intake detected and a stub drafted, even if a keyword search would have missed it.

Filters before anything reaches you: a star floor on lane-1 hits (watchlist exempt), a fit floor (a list that overlaps zero of your topics is dropped), your own repo excluded, everything previously surfaced excluded (seen-store), everything previously submitted to excluded (the ledger), and everything you are already listed in excluded (the optional placements registry, below).

## Intake-mechanics detection, and why some lists are flagged not handled

For each candidate, the scanner fetches `CONTRIBUTING.md` (and falls back to the README) and classifies the submission path:

- **PR** — you add yourself with a pull request. The common awesome-list path.
- **issue-form** — you open an issue from a template and a maintainer adds you.
- **web-form** — submissions go through a Google Form, Airtable, Typeform, or similar.
- **unknown** — a doc was found but no intake signal matched, or no doc was found.

A candidate is **flagged** when the intake is a web-form, or when the docs say the list bans automated or non-human submissions ("no bots", "human submissions only", "manual review only"). Flagged lists are surfaced with the reason and left for a person to handle by hand. That line is deliberate. Some directories forbid automated or non-human submissions, and the project respects that: list-sweep will draft material for a flagged list, but it will not pretend the submission can be automated, and there is no path in this module that submits anything to anywhere.

## The gate

The scanner produces `candidates.json`: discovered lists, their detected intake, a fit score, and a drafted submission stub per list. That is a proposal, not an action. Nothing is submitted. A human reviews each candidate, edits the stub, and (if they choose) makes the submission by hand through whatever the list's real intake path is. Then `mark-submitted` records it so it never resurfaces. There is no auto-submit, no batch-approve, no scheduler. That is the project's whole identity.

## How it complements placement-health

The two modules are the two ends of one funnel. list-sweep gets you **listed**: it finds the directories and drafts the entry. [placement-health](../placement_health/README.md) confirms you **stay listed**: it watches the placements you landed and tells you when one quietly drops you. Point list-sweep's `placements_path` at the same `placements.json` placement-health watches, and a list you are already in (or have a pending submission to) is excluded from discovery automatically.

## Usage

```bash
cp config.example.json config.json    # edit for your project
python list_sweep.py scan --dry-run             # preview queries, no network/gh/state
python list_sweep.py scan --days 30             # real run, 30-day window
python list_sweep.py scan                        # real run, since last run
python list_sweep.py mark-submitted --url <list-url> --list "<label>" --note "PR #123"
python list_sweep.py log                         # show recorded submissions
```

`--dry-run` makes no network or `gh` calls and writes nothing; it prints the exact queries it would run, so you can tune keywords without spending API quota.

## Config reference

| Key | Meaning | Default |
|---|---|---|
| `own_repo` | your `owner/name`, excluded from results | required |
| `own_tagline` | one-line description used in the drafted entry | `""` |
| `topics` | topic terms for fit-scoring | required |
| `search_keywords` | lane-1 `gh search repos` terms | required |
| `watchlist` | known target lists (`owner/name`) classified directly | `[]` |
| `placements_path` | path to a placement-health `placements.json` to dedup against | `null` |
| `min_stars` | lane-1 star floor (watchlist exempt) | `100` |
| `fit_floor` | minimum topic-overlap score to keep a candidate | `1` |
| `per_query` | repos fetched per search query | `20` |
| `emit_cap` | ceiling on emitted candidates | `60` |
| `seen_retention_days` | seen-store pruning horizon | `180` |
| `default_window_days` | first-run window | `30` |
| `state_dir` / `candidates_file` | where state and output live | `state` / `candidates.json` |

The example config is real: the lists the [agent-workspace-architecture](https://github.com/jimy-r/agent-workspace-architecture) project would target.

## Driving it with an agent

[SKILL.example.md](SKILL.example.md) is a working Claude Code skill that wraps this module: scan, score against the fit bar, draft an entry, then a hard per-submission approval gate before anything is submitted by hand. Port the same shape to any agent runtime; the load-bearing parts are the gate and the ledger, not the assistant.
