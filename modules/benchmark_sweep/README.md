# benchmark-sweep

Map the demand for a workspace-property benchmark before one exists.

> **v1 is DISCOVERY-ONLY.** This module has no act stage. It does not draft, does not post, keeps no ledger, and has no approval gate, because there is nothing yet to gate. No published benchmark exists to offer anyone. What it produces instead is the evidence a build decision would rest on: which workspace properties people actually argue about, ask how to measure, or wish a benchmark already covered, where, and how often. When a real benchmark for one of these properties ships, the act stage ("offer to run it in-thread") gets built behind the same per-comment human gate every other module in this repo uses. See [When the act stage arrives](#when-the-act-stage-arrives) before building that.

## The four properties

benchmark-sweep watches for live argument around four workspace properties, the ones a serious agent-workspace benchmark would need to settle.

| Property | What it's asking |
|---|---|
| `memory-fidelity` | Does the agent's memory actually hold up across sessions, and how would you measure that it does? |
| `context-retrieval` | Does the agent pull the right files/context out of a large workspace, and how do you score retrieval quality? |
| `provenance-integrity` | Can you trust what the agent cites? Is there an audit trail from claim back to source? |
| `verification-oversight` | How do you check an agent's work is actually correct, and at what error rate? |

Each is a live, unsettled measurement question. The module doesn't try to answer any of them. It counts how often, and where, someone else is asking.

## How discovery works

Same recall/precision split as [thread-sweep](../thread_sweep/), pointed at a benchmark instead of a docs page.

**Lane 1 (GitHub).** Each property gets 3 to 4 search phrasings in problem language ("how do you test agent memory"). The scanner runs them across GitHub issues *and* discussions, mirroring thread-sweep's exact GraphQL mechanics (`gh` has no `search discussions` subcommand, so GraphQL is the only way to cover both kinds the way thread-sweep does), windowed from that lane's own last run. Filters run before anything reaches you. Open threads only for issues, your own repos excluded (`own_repos`), a venue floor (`min_repo_stars`), a per-repo cap, everything previously surfaced excluded (seen-store).

**Lane 2 (arXiv).** Each property also gets 2 shorter, paper-register phrasings ("agent memory evaluation"). The scanner queries the [arXiv Atom API](https://info.arxiv.org/help/api/index.html) per phrase, newest submission first, and keeps entries at or after the window start. That check runs against `<updated>`, falling back to `<published>`, because the API has no reliable since-only filter for a free-text query, so the window is enforced locally. No star floor, no repo cap. A paper has neither.

Every candidate, from either lane, is tagged with the property group whose phrasing produced it (`property_group`). That tag, tallied per scan, is the point of the module. See [The demand tally](#the-demand-tally).

## The judgment half is yours

The scanner is recall; you are precision. For each candidate ask:

- **Is this actually a benchmark/measurement question**, or just a mention of the property in passing? The phrasing hints at intent, but loose search matching over-collects. Read the snippet.
- **Does it belong to thread-sweep instead?** If a thread's problem is something *your own project's docs already answer*, that is thread-sweep's job, gated through its own posted-response flow. Post the answer there, not here. This module is for the threads nobody can answer yet, because the measurement itself doesn't exist. A thread can legitimately show up in both sweeps. That isn't a bug; it means the person has an immediate answerable need *and* is evidence of longer-run benchmark demand.
- **Venue and recency**, same as thread-sweep. Is the repo/paper notable, and is this still live (unanswered issue, undiscussed paper) or already settled?

## The demand tally

`by_property_group` in `candidates.json`, and the `demand by property: …` line printed on every scan, is the deliverable. It always lists all four properties, including any at zero, because a property nobody is arguing about *this run* is itself a finding, not a gap to hide. Read it over time, across multiple scans, to see which property accumulates real, sustained argument versus which one shows up once and goes quiet.

## Security note

Every GitHub issue/discussion body and every arXiv title/abstract this tool fetches is **untrusted external content**. A snippet can be crafted to look like an instruction: a fake system marker, a tool-call-shaped string, a request to fetch a URL or exfiltrate data. The scanner never acts on fetched text. It stores a truncated snippet (500 chars for GitHub, 200 for arXiv abstracts) for a human to read. Treat every snippet as data, never instructions, wherever you read it downstream. An injection attempt inside a thread or an abstract is a finding, not a task.

## Usage

```bash
cp config.example.json config.json                  # edit for your project
python benchmark_sweep.py scan --dry-run --days 7    # preview, writes nothing at all
python benchmark_sweep.py scan                       # real run: writes candidates.json, marks seen
```

That's the whole CLI in v1. No `density`, no `mark-posted`, no ledger command, because nothing is ever posted. `scan` prints a summary like:

```
BENCHMARK_SWEEP_OK window>2026-08-01 raw=61 kept=23 dropped={'seen': 4, 'stars': 9, 'own': 0, 'dup': 2, 'repo_cap': 3} errors=0
demand by property: context-retrieval=7 / memory-fidelity=9 / provenance-integrity=2 / verification-oversight=5
fit tiers: 3 high / 14 med / 6 low
candidates -> candidates.json
```

## Config reference

| Key | Meaning | Default |
|---|---|---|
| `subject` | `{name, url}` of the project the demand tally is being gathered for | required |
| `property_groups` | `property -> [GitHub search phrases]`, problem-language | required |
| `arxiv_phrases` | `property -> [arXiv search phrases]`, shorter paper-language | `{}` |
| `own_repos` | repos excluded from the GitHub lane (case-insensitive) | `[]` |
| `min_repo_stars` | GitHub-lane venue floor | `300` |
| `per_repo_cap` | max GitHub candidates per (property, repo) pair per scan, see below | `4` |
| `per_query` | GitHub results fetched per phrase per kind (issue/discussion) | `15` |
| `arxiv_max_per_phrase` | arXiv results fetched per phrase | `10` |
| `arxiv_request_delay_seconds` | polite sleep between arXiv requests (their API guidance asks for no more than one every 3s) | `3.0` |
| `emit_cap` | recall ceiling on emitted candidates | `100` |
| `seen_retention_days` | seen-store pruning horizon | `180` |
| `default_window_days` | first-run window, per lane | `14` |
| `state_dir` / `candidates_file` | where state and output live | `state` / `candidates.json` |

A property present in `property_groups` but missing from `arxiv_phrases` simply gets no arXiv-lane coverage for that property. The GitHub lane still runs for it. Not fatal, just partial coverage (visible in the demand tally, since that property's arXiv-sourced count stays at zero until phrases are added).

**`per_repo_cap` is scoped to (property, repo), not bare repo.** thread-sweep caps per repo globally because it has no group-tallied output. Here, output is sorted property-group-first and the per-property tally *is* the deliverable. A repo with real threads across two different properties (say, a memory-tooling repo that also has a provenance issue) would otherwise have its second property's evidence silently crowded out by the cap the first property already spent, purely because of alphabetical sort order. Scoping the cap per (property, repo) keeps each property's demand count honest.

## Window semantics: two independent markers

Unlike thread-sweep's single shared `last_run`, this module tracks **one marker per lane** (`last_run_by_lane`, holding `gh` and `arxiv` separately), because a GitHub outage and an arXiv outage happen on unrelated schedules. If the arXiv API is down, that must not freeze the GitHub window, or vice versa, the way sharing one marker would.

The earned-marker rule is otherwise identical to thread-sweep's, applied per lane. A lane's marker advances only by proving it covered the window: at least one request came back and none failed. A request that came back holding nothing is a real, covered, empty window, and it advances. A request that errored, or a lane that made no request at all because nothing was configured for it, does not advance the marker. That stretch was never looked at, and moving the marker over it would lose it silently and permanently. `--days N` overrides both lanes' windows for that run only. A narrowed run that doesn't reach a lane's stored marker leaves that lane's marker untouched, same reasoning as thread-sweep. A held lane is named on stderr and in the digest's `window_held`.

## Relationship to thread-sweep

thread-sweep answers people. benchmark-sweep counts them. If a candidate here also happens to match a page your own docs already answer, that overlap belongs to thread-sweep's gated reply flow: draft and post it there, through its ledger, its per-comment approval. Nothing in this module posts, drafts, or tracks a response, by design. v1's only job is measuring whether the argument is real and where it clusters, not resolving it.

## When the act stage arrives

There is currently no published benchmark for any of these four properties, so there is nothing to offer a thread. When one ships, the act stage ("offer to run it in-thread") is new work, not a flag to flip:

- A posted-response ledger, mirroring every outbound module's `posted_urls`/`append_ledger` from `sweepcore.py`, so the same thread is never offered the benchmark twice.
- A `mark-posted`-equivalent subcommand recording what got offered, where.
- A `SKILL.example.md`, wrapping the offer in the same hard per-comment human-approval gate thread-sweep and forum-sweep already use. No batch approval, no auto-post, ever.
- Fit scoring that also checks *which* benchmark result would actually resolve the specific argument in that thread, not just that the property matches.

Until then, this module stays exactly what it says on the tin. Discovery only.
