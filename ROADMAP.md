# Roadmap

Direction, not a promise. Every module runs the same five-stage pipeline (**signal → judge → gate → act → ledger**), and the gate is non-negotiable in all of them: discovery, scoring, and drafting automate fully, but nothing outbound happens without per-artifact human approval.

## Shared core

The per-module pipeline sits on `modules/sweepcore.py` — the dedup, ledger, state, `gh`, HTTP (with 429/503 Retry-After backoff), and relevance-tiering primitives. Each module imports the ones it needs, so where two modules do the same job (dedup, ledger, tiering) they do it identically. New modules reuse it instead of copying it.

## Next

The build candidates, each with an open issue for discussion and claiming:

1. ~~**list-sweep**~~ ([#2](https://github.com/signal-sweep/signal-sweep/issues/2)). **Built ([modules/list_sweep/](modules/list_sweep/))**: curated-list discovery, intake-mechanics detection (PR / issue-form / web-form / human-only), drafted entries, submission tracking; dedups against placement-health.
2. ~~**newsletter-sweep**~~ ([#4](https://github.com/signal-sweep/signal-sweep/issues/4)). **Built ([modules/newsletter_sweep/](modules/newsletter_sweep/))**: verified outlet registry, alive/changed/unreachable freshness checks, per-outlet cooldown ledger; pitching and submission stay human-gated, email is draft-only.
3. **directory-sweep** — list-sweep's off-GitHub twin (tool directories / registries). Scoped to a watchlist-only classifier. A 2026 channel health-check found standalone tool-directory discovery is a weak, AI-displaced channel whose best targets are human-only (hand-flagged), so a broad crawler is not worth building.

(`stack-sweep`, the former #2, is now **Not planned** — see below.)

## Build order

Shipped on top of the shared core: release-sweep gained Conventional-Commit bucketed highlights, mention-sweep gained body-corroboration so coincidental-namesake hits rank last, and a deterministic high/med/low relevance tier now ranks thread/forum candidates for faster human triage.

Promotion leverage leads the rest. The get-listed family (`list-sweep`, built; `directory-sweep`, watchlist-only) and the answer-the-question venues already built (thread/forum) reach the people asking the questions. The inward, no-gate modules (`citation-sweep`, `audience-sweep`, `adjacent-sweep`) measure presence rather than build it, so they wait. Anything needing a per-run login (Reddit / X / LinkedIn posting) stays out entirely (see Not planned).

## Explored, unscheduled

Ideas that fit the pipeline and may graduate to issues when someone wants to build them.

**Inbound answers** (thread-sweep's mechanics, new sources)
- ~~`benchmark-sweep`~~. **Built ([modules/benchmark_sweep/](modules/benchmark_sweep/))**: threads + arXiv papers where a benchmark of the project's domain properties would settle the argument, tallied per property. Discovery-only; the offer-the-run act stage stays dormant until a benchmark exists.
- ~~`forum-sweep`~~. **Built ([modules/forum_sweep/](modules/forum_sweep/))**: Discourse vendor forums (primary) + Hacker News + Lobsters + an opt-in discovery-only Reddit lane, via per-source adapters on the shared gated pipeline.
- ~~`mention-sweep`~~. **Built ([modules/mention_sweep/](modules/mention_sweep/))**: entity-first discovery of where the project is named or misdescribed (GitHub issues/discussions + `gh search code`); gated engage / correct / amplify.

**Submission and distribution**
- ~~`cfp-sweep`~~. **Built ([modules/cfp_sweep/](modules/cfp_sweep/))**: conference-data dataset lane + venue watchlist, open/closed/unknown window detection, per-venue cooldown ledger; drafting and submission stay human-gated per submission.
- ~~`release-sweep`~~. **Built ([modules/release_sweep/](modules/release_sweep/))**: per-channel announcement material assembled from the real release diff; gated drafting + posting.
- `directory-sweep` — non-GitHub tool directories and registries, same registry-plus-gate shape as list-sweep. Promoted to Next in watchlist-only form (a 2026 channel health-check narrowed it from a broad crawler).

**Content creation**
- ~~`teardown-sweep`~~. **Built ([modules/teardown_sweep/](modules/teardown_sweep/))**: ranked, inward-facing discovery of published agent architectures worth a respectful written teardown on your own property.
- `qa-content-sweep` — turn the questions people actually ask (including other modules' rejected candidates) into FAQ/docs pages on your own property. Inbound, zero spam surface.
- `trend-sweep` — survey what's trending on YouTube/podcasts in your domain; draft micro-learning artifacts that teach the trending concept using your project as the worked example. Drafting automates; publishing cadence stays a human decision, per artifact.
- `digest-sweep` — periodic state-of-the-niche digest assembled from sweep data. Carries a cadence commitment; adopt deliberately.

**Presence intelligence** (inward-facing, no gate needed because nothing is outbound)
- ~~`response-sweep`~~ (reply tracking). **Built ([modules/response_sweep/](modules/response_sweep/))**: re-reads the threads you already answered, straight out of the posted ledgers, and surfaces replies you have not seen. GitHub issues, discussions, and Hacker News; per-thread baseline plus seen-state dedup, and a pending queue that survives a re-run.
- ~~`placement-health`~~. **Built ([modules/placement_health/](modules/placement_health/))**: verify your listings stayed live, links unbroken.
- `adjacent-sweep` — watch neighbouring tools' trackers for unmet needs; roadmap signal plus future thread candidates.
- `audience-sweep` — cluster stargazers/forkers by org, aggregate only.
- `citation-sweep` — academic mentions via arXiv/Semantic Scholar.

Several modules feed each other: thread-sweep's rejects feed qa-content-sweep, list-sweep feeds placement-health, adjacent-sweep feeds thread-sweep's queries, and thread-sweep plus forum-sweep feed response-sweep their posted ledgers. The toolkit compounds; that's the point of the shared shape.

## Not planned

Anything that weakens the gate: auto-post paths, batch approval, schedulers that act unattended. Modules that can't work inside the gate don't belong in this project.

**stack-sweep** ([#3](https://github.com/signal-sweep/signal-sweep/issues/3)) — pointing the pipeline at Stack Overflow / Stack Exchange. Dropped after a 2026 review: Stack Overflow's public-question flow has fallen roughly 95% from its peak as developers moved to LLM chat and private communities, and the public Q&A venues that absorbed the spillover (GitHub Discussions, Discourse) are already covered by thread-sweep and forum-sweep. That thin adapter now exists: forum-sweep ships an opt-in Stack Exchange lane (#25) for the residual long tail; a dedicated module remains not worth building.
