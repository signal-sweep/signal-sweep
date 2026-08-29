# Changelog

> Backfilled 2026-08-27 from the GitHub release notes (audit finding e56d8999: four tagged
> releases existed with notes living only on GitHub). From here, each release adds its entry
> at the top in the same action as the tag.

## v0.5.0 — reddit lane on RSS, per-lane query budgets — 2026-08-29

Reddit's public `.json` read stopped answering this tool. The lane moves to the Atom feed, and every phrase lane gains a way to spend fewer requests.

## forum-sweep

- **Reddit lane rebuilt on `search.rss`.** The unauthenticated `r/<sub>/search.json` read now returns a hard HTTP 403 to non-browser user agents, and `old.reddit.com` redirects the same query to a login wall, so the lane was fetching nothing. `r/<sub>/search.rss` answers 200 for the module's own descriptive UA (verified live 2026-08-29) with the same query, the same `t` time bucket, and the same held-window behaviour on failure. Parsed with the stdlib XML parser, like the Medium lane. Still discovery-only, still opt-in, still no posting path.
- **What the feed costs.** An Atom entry carries no score and no comment count, so reddit candidates now emit `score_or_stars: 0` and `comments: 0`. A zero comment count reads as an answer-gap signal for every candidate in the lane at once, so ranking within reddit is unchanged; only reddit's standing against the lanes that report real counts moves. Documented in the adapter, the module README, and the example config rather than faked with placeholder numbers.
- **Pacing floor.** Anonymous feed reads start returning HTTP 429 after roughly 20 quick requests, so the reddit lane now sleeps at least 2 seconds between its own requests whatever `request_delay_seconds` is set to. No other lane is slowed by it.
- **`sources.<lane>.groups`.** An optional per-lane whitelist of `query_groups` slugs, wired into every phrase-driven lane (`discourse`, `hn`, `reddit`, `stackexchange`, `lemmy`; the tag-driven lanes ignore it). Omit it and the lane runs every group exactly as before. An unknown slug warns once on stderr and is skipped, so a typo cannot quietly narrow a scan. This is what holds reddit's sub-by-phrase request count inside its budget without shrinking `query_groups` for the lanes that have no such limit.

## Fixes

- `.gitignore` now covers the underscore form of the private-profile patterns (`candidates_*.json`, `state_*/`, `config_*.json`). Only the dotted form was ignored, and live profiles are named with underscores, so a real targeting config and its state were committable.

forum-sweep's suite grows from 121 tests to 152, all green.

## v0.4.0 — four new modules, eight forum lanes — 2026-08-26

The toolkit grows from six modules to ten, and forum coverage from four lanes to eight. Discovery automates; every outbound act stays individually human-gated, unchanged.

## New modules

- **cfp-sweep** (#26) — conference/meetup CFP discovery from the tech-conferences dataset + a venue watchlist, open/closed/unknown window detection that refuses to guess missing years, and a per-venue cooldown ledger so no program committee is pitched twice in a cycle.
- **newsletter-sweep** (#28, closes #4) — outlet registry with live-verified submission mechanics, alive/changed/unreachable freshness checks, cooldown ledger. Email is draft-only, never send.
- **teardown-sweep** (#24, #31) — ranked, inward-facing discovery of published agent architectures worth a written teardown. v2 adds an artefact code-search lane (CLAUDE.md, AGENTS.md, .claude/, .cursor/rules with server-side size floors inside the 10 req/min budget), pattern-density scoring that peaks where a teardown has the most to say, and fork/template/content-hash flood guards.
- **benchmark-sweep** (#23) — threads and papers where a benchmark of your domain's properties would settle a live argument, tallied per property. Discovery-only; the act stage stays dormant by design.

## forum-sweep: eight lanes

- Thin **Stack Exchange** adapter and **dev.to** lane (#25) — the roadmap's stack-sweep carve-out, opt-in, with quota/backoff handling and the Stack Overflow AI-answer-policy caveat spelled out.
- **Medium RSS tag lane** and **Lemmy** adapter (#29) — opt-in, tracking-param-stable seen keys, federated-permalink handling.

## Fixes and docs

- Earned window markers and per-module state isolation (#21), correctness sweep (#20), test-gate and sweepcore docs (#22), module-table and roadmap registration (#27, #30).

Full pipeline count: 11 offline test suites, all green on 3.10 and 3.13.

## v0.3.1 — correctness sweep — 2026-07-12

A scan where every request failed used to advance the last-run stamp anyway, silently skipping that window forever; all four scan modules now hold the stamp and re-cover the window next run. Same batch: thread-sweep's query lane uses an inclusive date boundary (GitHub date qualifiers are day-granular, so the old form dropped same-day threads), Discourse snippets are read from the posts array because many instances return no topic excerpt, list-sweep keeps reading intake docs past a generic CONTRIBUTING.md, and the NoAutoPost gate guard now covers sweepcore itself — the one shared file every per-module guard was blind to. CI pins ruff and tests the advertised Python 3.10 floor. Suite grows 124 to 142 tests. Full list in #20 and the PR description.

## v0.3.0 — the toolkit fills out + a shared core — 2026-07-01

## v0.3.0 — the toolkit fills out + a shared core

Three new modules, a shared core every module now sits on, and sharper ranking. Everything keeps the same non-negotiable: nothing posts without your explicit per-comment approval.

### New modules

- **forum-sweep** — thread-sweep's job beyond GitHub: Discourse vendor forums (primary), Hacker News, Lobsters, and an opt-in discovery-only Reddit lane, each a thin adapter on the shared gated pipeline.
- **mention-sweep** — entity-first discovery of where your project is already named or misdescribed across issues, discussions, and code. Body-corroboration ranks coincidental-namesake hits last.
- **list-sweep** — finds curated lists and directories you could be listed in, and detects each one's intake mechanics (PR / issue-form / web-form / human-only).

### Under the hood

- **Shared core (`modules/sweepcore.py`)** — dedup, ledger, state, `gh`, HTTP with 429/503 Retry-After backoff, and relevance tiering, imported by every module. Gate and ledger semantics are now identical across the toolkit by construction.
- **Relevance tiering** — a deterministic high / med / low fit band ranks thread and forum candidates so the human triage starts at the most likely hits.
- **release-sweep** now buckets highlights by Conventional-Commit type from the real release diff.

### Tests

124 tests across the modules plus the shared core, with a CI test job.

Full history: [`v0.2.0...v0.3.0`](https://github.com/signal-sweep/signal-sweep/compare/v0.2.0...v0.3.0)

## v0.2.0 - placement-health + release-sweep — 2026-06-14

Two new modules since v0.1.0, both on the same signal/judge/gate/act/ledger shape.

**placement-health.** Watches the places your project is listed (awesome-lists, directories, your own pages) and reports LIVE / DROPPED / BROKEN / PENDING per entry. Presence intelligence: it only reads public URLs and reports, so there is no outbound action and no approval gate. Standard library only.

**release-sweep.** On a new release, assembles per-channel announcement material (tag, notes, and the commit diff since the previous tag) for an assistant to draft from. The output is outbound, so it is gated: the script assembles and ledgers only, and nothing posts without per-channel approval.

Also since v0.1.0: thread-sweep gained a mentions query group (#6) plus input/error-handling hardening and ruff CI (#7, #9); the repo gained ROADMAP.md (#5) and the build candidates as issues (#2 list-sweep, #3 stack-sweep, #4 newsletter-sweep).

Full module list and roadmap in the README.

## v0.1.0 — thread-sweep — 2026-06-11

First usable cut: the thread-sweep module. Two-lane GitHub discovery (per-topic search queries + a pinned watchlist) over issues and discussions, with a seen-store so nothing resurfaces, a posted-reply ledger so nothing is answered twice, a venue floor, per-repo caps, and a dry-run mode. Config-driven, stdlib + GitHub CLI only; ships with a real worked-example config and a portable agent-skill wrapper.

The contract that defines the project: discovery is deterministic, judgment is human, and nothing posts without explicit per-comment approval.
