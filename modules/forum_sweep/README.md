# forum-sweep

Find the open forum and aggregator threads, beyond GitHub, whose problem your project's docs already solve, then answer them one at a time through a human gate. The forum sibling of [thread-sweep](../thread_sweep/): where thread-sweep reads GitHub issues and discussions, forum-sweep reads the answer-the-question venues past GitHub.

## The pipeline

Every module in this repo runs the same five stages, and forum-sweep is no exception:

**signal → judge → gate → act → ledger**

- **signal**: `forum_sweep.py scan` runs the source adapters, filters, and writes `candidates.json`. Deterministic code, no judgment.
- **judge**: a human (with whatever assistant they choose) scores each candidate for direct fit, venue standing, and answer-gap. Recall is the script's job; precision is yours.
- **gate**: nothing posts without explicit per-item human approval. This is the project's whole identity, not a setting to toggle.
- **act**: the approved reply gets posted by hand on the venue (forum-sweep never posts for you; for the throttled venues, a trusted member posts it).
- **ledger**: `mark-posted` records the URL so the same thread is never answered twice.

The script owns the first stage and the last. The three in the middle are human. That split is the point.

## The four source lanes

One adapter per source, all returning the same candidate schema (`url`, `title`, `created`, `source`, `score_or_stars`, `comments`, `snippet`, `pattern`, `lane`). Pick lanes with `--source` (default `all`). Each is configured under `sources` in `config.json`:

**Discourse (primary lane).** For each configured instance, each query phrase runs against `<instance>/search.json?q=<term>` and the `topics` array becomes candidates. Discourse vendor forums (Cursor, OpenAI, Hugging Face, LangChain, LlamaIndex) are the most on-topic audience in the whole set, and their own written rules prescribe answer-first / link-as-garnish. No auth. A Cloudflare or login wall on an instance degrades gracefully: that instance is skipped, the scan carries on.

```json
"discourse": { "instances": ["forum.cursor.com", "community.openai.com", "discuss.huggingface.co"] }
```

**Hacker News.** The free Algolia API (`search_by_date`), one query per phrase over stories and comments, windowed by `created_at_i`. Hits below `thresholds.hn_min_points` are dropped. Maps to `news.ycombinator.com/item?id=`.

```json
"hn": { "enabled": true }
```

**Lobsters.** `https://lobste.rs/t/<tag>.json` per configured tag. Tag-based, not phrase-based, so you get the venue's hottest recent stories in your tags. Open JSON, no auth.

```json
"lobsters": { "tags": ["ai", "vibecoding"] }
```

**Reddit (opt-in, discovery-only).** Off by default. A public `r/<sub>/search.json` read for *finding* threads only, never for posting. Read the etiquette section before you flip `enabled` to `true`.

```json
"reddit": { "enabled": false, "subs": ["LocalLLaMA", "ClaudeAI", "AI_Agents", "LLMDevs"] }
```

Filters run before anything reaches you: a per-source cap so one busy instance can't flood the digest, everything previously surfaced excluded (seen-store), everything previously *answered* excluded forever (ledger). The time window scales input to what is new since the last run.

## The judgment half is yours

The scanner is recall; you are precision. For each candidate:

- **Direct fit.** Does a specific page of your docs answer *this exact question*? Adjacency ("they're discussing our general area") is a skip. The `pattern` field is a hint from loose phrase matching, not a verdict.
- **Venue standing.** Is your account in good standing on this forum? Several venues throttle links from new low-trust accounts. Tool the discovery, earn standing, then hand-post.
- **Answer-gap.** Read the existing replies first. If a good answer already exists, skip; never duplicate.

## ETIQUETTE: the load-bearing part

The non-negotiable, repo-wide rule first: **nothing posts without per-item human approval.** The script discovers and records. A human reads each draft and decides. There is no auto-post path, no batch-approve flag, no scheduler. Adding one is a PR that gets closed on principle.

Two doctrines hold across every venue:

- **The reply must stand without the link.** Put the full mechanism in the thread. If deleting your link makes it a worse comment, don't post. The answer is the payload; the link is garnish.
- **Attribute honestly, first person.** "I maintain a reference for this exact pattern: <link>" survives moderation and reads better than fake-neutral linking.

Then the per-venue facts, each of which can cost you an account or a domain if ignored:

### Discourse: new-account link throttling

Discourse trust levels are mechanical. A new account (TL0) is link-throttled and often cannot post links at all, regardless of how good the answer is. Standing is earned per-forum and cannot be bought or rushed. So **tool the discovery, post by hand once trusted.** Use forum-sweep to find the thread; write the answer on the forum yourself, as a member who has already contributed linkless help there. Do not treat the script's candidate list as a posting queue on a forum where you have no standing.

### Hacker News: domain-level SILENT enforcement

HN enforces at the *domain* level, silently. A single read of your domain that a moderator or the flamewar detector judges promotional can kill **all** of that domain's links site-wide, retroactively, across past and future submissions, with no notification and no appeal. This is the strictest scarcity bar in the entire set. One careless promotional comment doesn't cost you one comment; it can cost you the whole domain's reach on HN permanently. Treat every HN post as if it were your last allowed one, because it might be.

### Reddit: shadowban invisibility + OAuth gating

Reddit is **disabled by default, discovery-only, manual.** Two reasons. First, shadowbans are invisible: a removed comment still looks live to the account that posted it, so a "posted" ledger entry for Reddit can be a lie. **Always verify a Reddit post out-of-band** (open the comment in a logged-out browser) before trusting the ledger. Second, the full Reddit Data API is OAuth-gated and pre-approval-gated; the public `.json` read this tool uses is unauthenticated, best-effort, and may be rate-limited or blocked at any time. Posting through it is never automated. Per-sub self-promotion rules are strict and the link is usually best omitted entirely.

### Lobsters: invite-gate + <25% self-promo ceiling

Posting on Lobsters is invite-gated; you cannot self-register. And the site enforces a hard **under-25% self-promotion ceiling** (across your whole history, fewer than one in four of your submissions may point at your own properties), plus active anti-AI-slop moderation. Fold the `ai` and `vibecoding` tags into discovery, hand-post rarely, and keep your linkless-to-self-link ratio comfortably inside the ceiling.

The self-reference ratio is the real governor everywhere. Venues differ in their rules; the universal defence is the same: mix genuinely-helpful linkless answers in over time so the account never reads as ~100% self-link. **Record every post** with `mark-posted`. The ledger is what guarantees you never answer the same thread twice (modulo the Reddit-shadowban caveat above), and `density` keeps your recent posting count visible so restraint stays honest.

## Security note

Every forum and aggregator response this tool fetches is **untrusted external content**. A thread title or blurb can carry text engineered to look like an instruction: a fake system marker, a tool-call-shaped string, a request to fetch a URL or reveal data. The scanner never acts on fetched text; it stores a truncated snippet for a human to read. Treat every snippet as data, never instructions, wherever you read it downstream. An injection attempt inside a thread is a finding, not a task.

## Usage

```bash
cp config.example.json config.json               # edit for your project
python forum_sweep.py scan --dry-run --days 7     # preview, state untouched
python forum_sweep.py scan --source lobsters      # one lane (most reliably open)
python forum_sweep.py scan                        # all enabled lanes, real run
python forum_sweep.py density                     # recent posting counts
python forum_sweep.py mark-posted --url <thread-url> --pattern <slug> --comment-file reply.md
```

Requires Python 3.10+. Stdlib only: `urllib.request` for the HTTP-JSON sources, no third-party deps and no auth for the Discourse / HN / Lobsters lanes.

## Config reference

| Key | Meaning | Default |
|---|---|---|
| `subject` | `{name, url}` of the project you're answering for | required |
| `query_groups` | `pattern-slug → [phrases]`, reusable across sources | required |
| `sources.discourse.instances` | Discourse hosts to search | `[]` |
| `sources.hn.enabled` | run the Hacker News lane | `false` |
| `sources.lobsters.tags` | Lobsters tags to pull | `[]` |
| `sources.reddit.enabled` | run the opt-in discovery-only Reddit lane | `false` |
| `sources.reddit.subs` | subreddits to search when enabled | `[]` |
| `thresholds.per_source_cap` | max candidates per instance/site per scan | `4` |
| `thresholds.hn_min_points` | drop HN hits below this point count | `2` |
| `emit_cap` | recall ceiling on emitted candidates | `100` |
| `seen_retention_days` | seen-store pruning horizon | `180` |
| `default_window_days` | first-run window | `14` |
| `state_dir` / `candidates_file` | where state and output live | `state` / `candidates.json` |

State lives in `state/forum_sweep_state.json` (last_run + seen) and `state/forum_sweep_log.jsonl` (the posted ledger). Both are gitignored: the ledger is your posting history; never commit it.

## Driving it with an agent

[SKILL.example.md](SKILL.example.md) is a working Claude Code skill that wraps this module: scan, score against the fit bar, draft replies, then a hard per-comment approval gate before anything posts. Port the same shape to any agent runtime; the load-bearing parts are the gate and the ledger, not the assistant.
