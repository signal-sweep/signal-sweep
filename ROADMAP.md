# Roadmap

Direction, not a promise. Every module here is the same five-stage pipeline — **signal → judge → gate → act → ledger** — and the gate is non-negotiable in all of them: discovery, scoring, and drafting automate fully; nothing outbound happens without per-artifact human approval.

## Next

The build candidates, each with an open issue for discussion and claiming:

1. ~~**list-sweep**~~ ([#2](https://github.com/signal-sweep/signal-sweep/issues/2)) — **built ([modules/list_sweep/](modules/list_sweep/))**: curated-list discovery, intake-mechanics detection (PR / issue-form / web-form / human-only), drafted entries, submission tracking; dedups against placement-health.
2. **stack-sweep** ([#3](https://github.com/signal-sweep/signal-sweep/issues/3)) — the thread-sweep pipeline pointed at Stack Overflow/Exchange; forces the shared-core decision.
3. **newsletter-sweep** ([#4](https://github.com/signal-sweep/signal-sweep/issues/4)) — outlet registry, submission windows, per-outlet pitch drafting, cooldown ledger.

## Build order — two lenses

Ordered by *strategic position*, placement discoverability leads — `list-sweep` (now built) and the directory family. Ordered instead by *autonomy × build effort* (cheapest to run unattended, no gate to build), the inward modules lead: `citation-sweep` (keyless arXiv + Semantic Scholar, `placement-health`-shaped, no `gh`/ledger/gate) and `audience-sweep` are the next cheap wins, ahead of the remaining outbound-gated candidates (`stack-sweep`, `newsletter-sweep`). Both orders are defensible; inward-first ships value without touching the gate. Anything needing a per-run login (Reddit/X/LinkedIn posting) stays out (see Not planned).

## Explored, unscheduled

Ideas that fit the pipeline and may graduate to issues when someone wants to build them.

**Inbound answers** (thread-sweep's mechanics, new sources)
- ~~`forum-sweep`~~ — **built ([modules/forum_sweep/](modules/forum_sweep/))**: Discourse vendor forums (primary) + Hacker News + Lobsters + an opt-in discovery-only Reddit lane, via per-source adapters on the shared gated pipeline.
- ~~`mention-sweep`~~ — **built ([modules/mention_sweep/](modules/mention_sweep/))**: entity-first discovery of where the project is named or misdescribed (GitHub issues/discussions + `gh search code`); gated engage / correct / amplify.

**Submission and distribution**
- `cfp-sweep` — conference/meetup CFP discovery matched to your topics, with drafted abstracts.
- ~~`release-sweep`~~ — **built ([modules/release_sweep/](modules/release_sweep/))**: per-channel announcement material assembled from the real release diff; gated drafting + posting.
- `directory-sweep` — non-GitHub tool directories and registries, same registry-plus-gate shape as list-sweep.

**Content creation**
- `qa-content-sweep` — turn the questions people actually ask (including other modules' rejected candidates) into FAQ/docs pages on your own property. Inbound, zero spam surface.
- `trend-sweep` — survey what's trending on YouTube/podcasts in your domain; draft micro-learning artifacts that teach the trending concept using your project as the worked example. Drafting automates; publishing cadence stays a human decision, per artifact.
- `digest-sweep` — periodic state-of-the-niche digest assembled from sweep data. Carries a cadence commitment; adopt deliberately.

**Presence intelligence** (inward-facing, no gate needed because nothing is outbound)
- ~~`placement-health`~~ — **built ([modules/placement_health/](modules/placement_health/))**: verify your listings stayed live, links unbroken.
- `adjacent-sweep` — watch neighbouring tools' trackers for unmet needs; roadmap signal plus future thread candidates.
- `audience-sweep` — cluster stargazers/forkers by org, aggregate only.
- `citation-sweep` — academic mentions via arXiv/Semantic Scholar.

Several modules feed each other: thread-sweep's rejects feed qa-content-sweep, list-sweep feeds placement-health, adjacent-sweep feeds thread-sweep's queries. The toolkit compounds; that's the point of the shared shape.

## Not planned

Anything that weakens the gate: auto-post paths, batch approval, schedulers that act unattended. Modules that can't work inside the gate don't belong in this project.
