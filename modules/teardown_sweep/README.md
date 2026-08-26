# teardown-sweep

Find published agent architectures worth a public teardown, and rank them so the best reading surfaces first.

A teardown here means a respectful written analysis of someone's *published, credited* work: what the design gets right, what it trades off, what you would do differently. This module does the deterministic discovery half only. It never writes the teardown, and it never contacts anyone.

## Not a gate, because nothing goes out

The rest of this toolkit gates outbound actions behind per-artifact human approval. teardown-sweep has nothing outbound to gate, the same way [placement-health](../placement_health/README.md) does. It reads public search results, public READMEs, and public configuration files, scores them, and writes a ranked reading list. That is the whole module: discovery *intelligence*, not outreach. A human reads `candidates.json` and decides, entirely outside this tool, whether and how to write anything.

## How discovery works

**Lane 1 (GitHub repos).** `gh search repos` runs two query shapes: a free-text phrase per configured `search_queries` entry, and a `--topic` filter per configured `topics` entry. Both are scoped to the same fixed activity floor - pushed within `active_within_days` of right now, recomputed every run - and a star floor (`min_stars`, watchlist-free: every lane-1 hit came from a real query, so there is no hand-curated exemption to make here).

**Lane 2 (Hacker News, via Algolia).** A phrase search per configured `hn_queries` entry against `search_by_date`, `tags=story`, with a combined points-floor + since-last-run time filter (`numericFilters=points>=N,created_at_i>T`). This is the recall net for architecture write-ups that never touch a GitHub repo search: blog posts, "how I built my agent" threads, Show HN posts.

**Lane 3 (artefact code search).** Lanes 1 and 2 find frameworks and write-ups. Lane 3 finds real *configured workspaces* by searching for the configuration artefacts themselves via GitHub's REST code-search resource (`gh api search/code?q=...`): `CLAUDE.md`, `AGENTS.md`, `.claude/settings.json`, `.cursor/rules`, `copilot-instructions.md`. Each entry in `artefact_queries` is a `{label, query}` pair, so a new convention (a fifth agent-instructions filename, say) is a config edit, not a code change. Every default query carries a server-side `size:>N` floor, because a two-line `CLAUDE.md` is by far the dominant false positive on an unfloored search - sampled live during build, `filename:CLAUDE.md size:>4000` still returns hundreds of thousands of hits but the actual matches are substantive files, not stubs.

*Budget.* `code_search` is rate-limited to 10 requests/min (live-verified via `gh api rate_limit`). Calls per scan = `len(artefact_queries) * artefact_pages_per_query` - 5 queries at the shipped default of 1 page each, 5 calls total - sleeping `ARTEFACT_SLEEP_SECONDS` (6s) between successive calls, so a default run spends about 24 seconds and stays comfortably inside one rolling 60-second window. Pass `--no-artefacts` to skip this lane entirely for a quick lanes-1/2 run. A 403 from this endpoint is treated as the rate limit rather than a real failure: the lane stops issuing further queries for the rest of that run and reports it as advisory (never fails the scan), and the next run tries again from the top.

*Fork-flood guard.* Code search surfaces forks, template instantiations, and archived repos at real volume - a popular workspace template can flood a query with dozens of copies that add nothing. For every unique repository among the hits, one `gh api repos/{full_name}` metadata call (cheap core quota, not the rate-limited resource) fetches stars, push date, and `fork` / `is_template` / `archived`; any of the three drops the candidate (counted in `dropped` as `fork` / `template` / `archived`), and the survivors are floored by `artefact_min_stars` (a lower bar than lane 1's `min_stars` - practitioner workspaces run smaller than the frameworks lane 1 targets) and `active_within_days` on the push date.

*Near-duplicate content guard.* The matched artefact's own content is fetched once per repo+path (`gh api repos/{full}/contents/{path}`) for pattern scoring, and doubles as a dedup signal: a sha1 of the first ~2KB with whitespace runs collapsed. A hit whose hash matches one already seen this run, or already in the state file from a prior run, is dropped as `dup_content` - the guard against copies of the same public template flooding the digest under different repo names.

Filters before anything reaches you: your own repos excluded (`own_repos`, all three lanes), everything previously surfaced excluded (seen-store), everything already torn down excluded (the covered ledger, below).

## Teardown-worthiness ranking

Each candidate gets a deterministic `richness_score` from cheap, explainable signals, so the ranking is reproducible and inspectable rather than a black box:

- **Keyword hits** - how many of `richness_keywords` (defaults: memory, context, hooks, subagent, provenance, verification, orchestration, eval, retrieval, guardrail) appear in the repo's description + README (lane 1) or the story title (lane 2).
- **README length band** - a longer README is a floor signal that real docs exist, not a linear quality score: under 500 chars scores nothing, 500-2999 scores +1, 3000+ scores +2.
- **docs/ directory presence** - a repo with a dedicated `docs/` folder scores +2. Checked with one small `gh api .../contents/docs` call (existence only, no content fetched).
- **Stars / points band** - `min_stars`/`hn_min_points` and up scores +1, 1000+ (stars) or 100+ (points) scores +2.
- **Recency band** (lane 1 only) - pushed within 30 days scores +2, within 90 days scores +1.

Every candidate also gets a coarse `tier` (`high`/`med`/`low`). Lanes 1 and 2 get theirs from `sweepcore.relevance_tier` - the same tiering primitive `thread-sweep`/`mention-sweep` use - fed with the fields that apply here (`stars`/`score_or_stars`, `pattern`). Sort within the digest is `(tier, richness_score)` descending. Because every candidate in this module comes from a real query match (there is no watchlist lane pulling in unscored noise), `relevance_tier` naturally settles into `high`/`med` for lane-1/2 data; `richness_score` is the finer-grained signal doing the real ranking work.

Every candidate carries a one-line `why` built from these same signals (e.g. `"312 stars; pushed 4d ago; 3 arch keyword(s) (hooks, memory, provenance); has docs/"`), so the reasoning behind the rank is visible without opening the repo.

**README/title text is untrusted external content.** It is scanned for keyword hits and stored (truncated) for you to read; this tool never executes it or treats anything inside it as an instruction, no matter what it says.

## Pattern-density ranking (lane 3 only)

Lane 3 ranks differently, because the question it answers is different. Lanes 1-2 ask "is this worth reading"; lane 3 asks "how much would a teardown of this workspace have to *say*". Each candidate's fetched artefact text is scored against `pattern_signals`, a config map of 15 labels (`p01`..`p15`), each a short list of lowercase indicator phrases for one pattern of the reference architecture this toolkit itself follows: role composition, classify-then-act, a freshness/sentinel check, tiering by impact, memory pointers, credentials kept out of files, a pre-tool-use hook guard, an audit cadence, a context budget, gated self-edits, a scaffold register, loop selection, a divergent-critic pass, a delegation queue, model tiering by cost. A label counts as present on a single indicator hit - a coverage signal, not a density-within-pattern one.

**The ranking philosophy, and why it is not "more patterns = higher score".** A candidate that implements a handful of these patterns while conspicuously lacking others outranks one that matches everything or matches nothing. Matching nothing gives a teardown nothing to praise; matching everything gives it nothing to contrast, and reads more like a template clone than a workspace that made real trade-offs. The sweet spot - `patterns_present` in the 4-9 range - is mid-density with clear gaps: exactly the shape of "here is what works, and here is what is conspicuously missing" that makes a teardown worth writing. `score_pattern_density` encodes this directly: it peaks flat across 4-9 patterns present and decays on both sides, so a 6-pattern artefact always outranks an all-15 one.

Each lane-3 candidate carries `patterns_present` / `patterns_absent` (the label lists) and `pattern_score` (the density score above); `tier` is a small dedicated band map over `pattern_score` (`>=8` high, `>=3` med, else low) rather than `relevance_tier`, so lane 3 sorts on the same `(tier, richness_score)` key as lanes 1-2 - `richness_score` for lane 3 is `pattern_score` plus the same stars/recency bands lane 1 uses (`score_repo_signals`), so pattern density dominates the rank but stars and freshness still break ties.

**Artefact text is untrusted external content**, exactly like README/title text above: scanned for indicator hits and hashed for dedup, never executed or treated as an instruction, no matter what it says.

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
python teardown_sweep.py scan --no-artefacts     # skip lane 3 (rate-limited), lanes 1-2 only
python teardown_sweep.py mark-covered --url <repo-or-story-url> --note "published 2026-08-26"
python teardown_sweep.py log                     # show recorded teardowns
```

`--dry-run` makes no network or `gh` calls and writes nothing; it prints the exact queries it would run. `--no-artefacts` skips lane 3 (the rate-limited code-search lane) for a quick lanes-1/2-only run; lanes 1-2 make no `gh` calls that are rate-limited the same way, so this is the fast path when you only want frameworks and write-ups.

## Config reference

| Key | Meaning | Default |
|---|---|---|
| `own_repos` | your `owner/name` list, excluded from results (all three lanes) | required |
| `search_queries` | lane-1 free-text phrases | see config.example.json |
| `topics` | lane-1 `--topic` filters | `["ai-agents", "agentic-ai"]` |
| `hn_queries` | lane-2 Algolia phrase searches | see config.example.json |
| `artefact_queries` | lane-3 `{label, query}` code-search entries | see config.example.json |
| `richness_keywords` | architecture-depth keywords scored in lane-1/2 text | see config.example.json |
| `pattern_signals` | lane-3 `p01`..`p15` label -> indicator-phrase map | see config.example.json |
| `min_stars` | lane-1 star floor | `50` |
| `artefact_min_stars` | lane-3 star floor (lower - practitioner workspaces run smaller) | `20` |
| `active_within_days` | lane-1 activity floor (pushed within N days of now); also lane-3's recency floor on the metadata fetch's push date | `365` |
| `hn_min_points` | lane-2 points floor | `10` |
| `artefact_pages_per_query` | lane-3 pages fetched per query (per_page is fixed at 30) | `1` |
| `per_query` | results fetched per query, lanes 1-2 | `20` |
| `emit_cap` | ceiling on emitted candidates | `60` |
| `seen_retention_days` | seen-store and content-hash-store pruning horizon | `180` |
| `default_window_days` | lane-2 first-run window | `30` |
| `state_dir` / `candidates_file` | where state and output live | `state` / `candidates.json` |

## When the window advances (lane 2 only)

`last_run` is a claim about **lane-2 (HN) coverage**: everything published there after it has been looked at. A scan earns a new marker only by proving lane 2 covered the window - at least one HN query came back and none failed.

This is the mirror image of `list-sweep`, where the query lane governs the marker and a watchlist lane is untimed. Here it is the query-shaped HN lane that is windowed, and the two repo lanes are not: lane 1's floor is `active_within_days` back from **right now**, recomputed every run, never a since-last-run claim, and lane 3 has no date qualifier to window against at all - GitHub's code-search API does not support one. There is no "stretch since last time" for a `gh search repos` or `gh api search/code` failure to lose, so lane-1 and lane-3 errors are always visible in `errors` but never hold the marker. This means a scan can advance its window even while every GitHub query that run failed, as long as HN was clean - that is by design, not an oversight. Lane 3's repeat-guard is entirely the static floors above plus the seen-store and covered ledger, the same way lane 1's would be without its query-side date filter.

An HN search that came back matching nothing is a real, covered, empty window, and it advances. Everything else keeps the old stamp: an HN query that errored, or a run where `hn_queries` is empty and no query was ever issued. A held run says so on stderr and sets `window_held` in the digest; the seen-store still keeps already-surfaced candidates out of the re-scan either way.

## candidates.json shape

Lane-1 (`github`) candidates carry `repo` (owner/name) and `stars`; lane-2 (`hn`) candidates carry `title` and `score_or_stars` (points) plus `hn_comments`; lane-3 (`artefact`) candidates carry `repo`, `stars`, `pushed_at`, `fork`/`is_template`/`archived`, `artefact_label`/`artefact_path`/`artefact_url` (which query matched, where, and a direct link to the file), `patterns_present`/`patterns_absent`, `pattern_score`, and `content_hash`. All three carry `lane`, `url`, `richness_score`, `tier`, and `why`; lanes 1-2 also carry `pattern` (which query/topic matched) and `matched_keywords`.

The summary line's `dropped` counts extend the same way lane by lane: `dup`/`own`/`seen`/`covered` apply across all three lanes, `stars`/`points` are lane-1/2's floors, and `fork`/`template`/`archived`/`stale`/`dup_content` are lane-3's fork-flood and near-duplicate guards (see Lane 3 above). The `TEARDOWN_SWEEP_OK` line also reports `artefacts=N repos=N`: how many raw artefact hits lane 3 built this run, across how many distinct repositories, before any filtering.

## Recording a teardown

`mark-covered --url <repo-or-story-url> [--note "..."]` appends to a ledger (`state/covered_log.jsonl`, gitignored - it is your posting history). Every future scan checks that ledger before a candidate is surfaced, the same way `thread-sweep` checks its posted-reply ledger, so a project you have already covered never resurfaces. `log` prints what is recorded.
