# teardown-sweep

Find published agent architectures worth a public teardown, and rank them so the best reading surfaces first.

A teardown here means a respectful written analysis of someone's *published, credited* work: what the design gets right, what it trades off, what you would do differently. This module does the deterministic discovery half only. It never writes the teardown, and it never contacts anyone.

## Not a gate, because nothing goes out

The rest of this toolkit gates outbound actions behind per-artifact human approval. teardown-sweep has nothing outbound to gate, the same way [placement-health](../placement_health/README.md) does. It reads public search results and public READMEs, scores them, and writes a ranked reading list. That is the whole module: discovery *intelligence*, not outreach. A human reads `candidates.json` and decides, entirely outside this tool, whether and how to write anything.

## How discovery works

**Lane 1 (GitHub repos).** `gh search repos` runs two query shapes: a free-text phrase per configured `search_queries` entry, and a `--topic` filter per configured `topics` entry. Both are scoped to the same fixed activity floor - pushed within `active_within_days` of right now, recomputed every run - and a star floor (`min_stars`, watchlist-free: every lane-1 hit came from a real query, so there is no hand-curated exemption to make here).

**Lane 2 (Hacker News, via Algolia).** A phrase search per configured `hn_queries` entry against `search_by_date`, `tags=story`, with a combined points-floor + since-last-run time filter (`numericFilters=points>=N,created_at_i>T`). This is the recall net for architecture write-ups that never touch a GitHub repo search: blog posts, "how I built my agent" threads, Show HN posts.

Filters before anything reaches you: your own repos excluded (`own_repos`), everything previously surfaced excluded (seen-store), everything already torn down excluded (the covered ledger, below).

## Teardown-worthiness ranking

Each candidate gets a deterministic `richness_score` from cheap, explainable signals, so the ranking is reproducible and inspectable rather than a black box:

- **Keyword hits** - how many of `richness_keywords` (defaults: memory, context, hooks, subagent, provenance, verification, orchestration, eval, retrieval, guardrail) appear in the repo's description + README (lane 1) or the story title (lane 2).
- **README length band** - a longer README is a floor signal that real docs exist, not a linear quality score: under 500 chars scores nothing, 500-2999 scores +1, 3000+ scores +2.
- **docs/ directory presence** - a repo with a dedicated `docs/` folder scores +2. Checked with one small `gh api .../contents/docs` call (existence only, no content fetched).
- **Stars / points band** - `min_stars`/`hn_min_points` and up scores +1, 1000+ (stars) or 100+ (points) scores +2.
- **Recency band** (lane 1 only) - pushed within 30 days scores +2, within 90 days scores +1.

Every candidate also gets a coarse `tier` (`high`/`med`/`low`) from `sweepcore.relevance_tier` - the same tiering primitive `thread-sweep`/`mention-sweep` use - fed with the fields that apply here (`stars`/`score_or_stars`, `pattern`). Sort within the digest is `(tier, richness_score)` descending. Because every candidate in this module comes from a real query match (there is no watchlist lane pulling in unscored noise), `relevance_tier` naturally settles into `high`/`med` for this data; `richness_score` is the finer-grained signal doing the real ranking work.

Every candidate carries a one-line `why` built from these same signals (e.g. `"312 stars; pushed 4d ago; 3 arch keyword(s) (hooks, memory, provenance); has docs/"`), so the reasoning behind the rank is visible without opening the repo.

**README/title text is untrusted external content.** It is scanned for keyword hits and stored (truncated) for you to read; this tool never executes it or treats anything inside it as an instruction, no matter what it says.

## Etiquette, the part that matters

- **It's PUBLISHED work.** Only surface and analyse what the author chose to make public. Never dig through anything private, unlisted, or non-consensual - this module only ever reads public search results and public READMEs, and a teardown built from it should hold to the same line.
- **Link back and credit clearly.** A teardown is analysis of someone's work, not content mined from it. Name the author and the project up front.
- **Name what's good first.** Every design that shipped and got stars made real trade-offs under real constraints. Start there.
- **Critique the design, not the person.** "This approach breaks under X" reads very differently from a remark about the author.
- **Prefer notable, actively-maintained subjects.** A stale weekend project earns a quieter response than a maintained one with real users - the star/recency bands above are a proxy for this, not a substitute for judgment.
- **Stay scarce.** A handful of genuinely thoughtful teardowns build standing; a pile of thin ones spends it.
- **Record every teardown** with `mark-covered` - the ledger is what keeps you from covering the same project twice.

## Usage

```bash
cp config.example.json config.json    # edit own_repos for your project(s)
python teardown_sweep.py scan --dry-run          # preview queries, no network/gh/state
python teardown_sweep.py scan --days 14          # real run, 14-day HN window
python teardown_sweep.py scan                    # real run, since last run
python teardown_sweep.py mark-covered --url <repo-or-story-url> --note "published 2026-08-26"
python teardown_sweep.py log                     # show recorded teardowns
```

`--dry-run` makes no network or `gh` calls and writes nothing; it prints the exact queries it would run.

## Config reference

| Key | Meaning | Default |
|---|---|---|
| `own_repos` | your `owner/name` list, excluded from results | required |
| `search_queries` | lane-1 free-text phrases | see config.example.json |
| `topics` | lane-1 `--topic` filters | `["ai-agents", "agentic-ai"]` |
| `hn_queries` | lane-2 Algolia phrase searches | see config.example.json |
| `richness_keywords` | architecture-depth keywords scored in text | see config.example.json |
| `min_stars` | lane-1 star floor | `50` |
| `active_within_days` | lane-1 activity floor (pushed within N days of now) | `365` |
| `hn_min_points` | lane-2 points floor | `10` |
| `per_query` | results fetched per query, both lanes | `20` |
| `emit_cap` | ceiling on emitted candidates | `60` |
| `seen_retention_days` | seen-store pruning horizon | `180` |
| `default_window_days` | lane-2 first-run window | `30` |
| `state_dir` / `candidates_file` | where state and output live | `state` / `candidates.json` |

## When the window advances (lane 2 only)

`last_run` is a claim about **lane-2 (HN) coverage**: everything published there after it has been looked at. A scan earns a new marker only by proving lane 2 covered the window - at least one HN query came back and none failed.

This is the mirror image of `list-sweep`, where the query lane governs the marker and a watchlist lane is untimed. Here it is the query-shaped HN lane that is windowed, and the repo lane that is not: lane 1's floor is `active_within_days` back from **right now**, recomputed every run, never a since-last-run claim. There is no "stretch since last time" for a `gh search repos` failure to lose, so lane-1 errors are always visible in `errors` but never hold the marker. This means a scan can advance its window even while every GitHub query that run failed, as long as HN was clean - that is by design, not an oversight.

An HN search that came back matching nothing is a real, covered, empty window, and it advances. Everything else keeps the old stamp: an HN query that errored, or a run where `hn_queries` is empty and no query was ever issued. A held run says so on stderr and sets `window_held` in the digest; the seen-store still keeps already-surfaced candidates out of the re-scan either way.

## candidates.json shape

Lane-1 (github) candidates carry `repo` (owner/name) and `stars`; lane-2 (hn) candidates carry `title` and `score_or_stars` (points) plus `hn_comments`. Both carry `lane`, `url`, `pattern` (which query/topic matched), `matched_keywords`, `richness_score`, `tier`, and `why`.

## Recording a teardown

`mark-covered --url <repo-or-story-url> [--note "..."]` appends to a ledger (`state/covered_log.jsonl`, gitignored - it is your posting history). Every future scan checks that ledger before a candidate is surfaced, the same way `thread-sweep` checks its posted-reply ledger, so a project you have already covered never resurfaces. `log` prints what is recorded.
