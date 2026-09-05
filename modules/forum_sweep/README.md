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

## The eight source lanes

One adapter per source, all returning the same candidate schema (`url`, `title`, `created`, `source`, `score_or_stars`, `comments`, `snippet`, `pattern`, `lane`). Pick lanes with `--source` (default `all`). Each is configured under `sources` in `config.json`:

**Discourse (primary lane).** For each configured instance, each query phrase runs against `<instance>/search.json?q=<term>` and the `topics` array becomes candidates. Discourse vendor forums (Cursor, OpenAI, Hugging Face, LangChain, LlamaIndex) are the most on-topic audience in the whole set, and their own written rules prescribe answer-first / link-as-garnish. No auth. A Cloudflare or login wall on an instance degrades gracefully: that instance is skipped, the scan carries on.

```json
"discourse": { "instances": ["forum.cursor.com", "community.openai.com", "discuss.huggingface.co"] }
```

Anonymous Discourse search is rate-limited per instance, and an unpaced sweep 429s across every instance at once, which holds the primary lane's window run after run. So the lane keeps a floor of 1 second **per host** whatever `request_delay_seconds` says. Per host rather than module-wide because the limit is per instance: the time spent reading `community.openai.com` is time `forum.cursor.com` has already waited, so a rotating sweep pays the floor once, not once per instance.

**Hacker News.** The free Algolia API (`search_by_date`), one query per phrase over stories and comments, windowed by `created_at_i`. Hits below `thresholds.hn_min_points` are dropped. Maps to `news.ycombinator.com/item?id=`.

```json
"hn": { "enabled": true }
```

**Lobsters.** `https://lobste.rs/t/<tag>.json` per configured tag. Tag-based, not phrase-based, so you get the venue's hottest recent stories in your tags. Open JSON, no auth.

```json
"lobsters": { "tags": ["ai", "vibecoding"] }
```

**Reddit (opt-in, discovery-only).** Off by default. A public `r/<sub>/search.rss` read for *finding* threads only, never for posting. The `.json` form this lane used through v0.4.0 now returns a hard HTTP 403 to any non-browser user agent, and `old.reddit.com` redirects the same query to a login wall, so the transport is the per-subreddit Atom feed: same query, same time bucket, parsed with the stdlib XML parser like the Medium lane. Read the etiquette section before you flip `enabled` to `true`.

```json
"reddit": {
  "enabled": false,
  "subs": ["LocalLLaMA", "ClaudeAI", "AI_Agents", "LLMDevs"],
  "groups": ["context-budget", "memory-hygiene", "agent-autonomy"]
}
```

Two things the feed transport changes, both worth knowing before you read a reddit digest.

**No engagement numbers.** An Atom entry carries no score and no comment count, so every reddit candidate arrives with `score_or_stars: 0` and `comments: 0`. A zero comment count reads as an answer-gap signal in the shared ranking, and it reads that way for every candidate in the lane at once. Ordering *within* reddit is unaffected. The only thing that moves is reddit's standing against the lanes that do report real counts, so open the thread before trusting a reddit tier.

**A tight request budget.** Anonymous feed reads start returning HTTP 429 after roughly 20 quick requests. Two guards: the lane paces itself at a floor of 2 seconds between requests whatever `request_delay_seconds` says (no other lane is slowed), and the optional `groups` key narrows it to a subset of your `query_groups`. One request is spent per sub per phrase, so trimming groups is what actually keeps the lane inside the budget. Omit `groups` to run every group, as before.

**Stack Exchange (opt-in, thin adapter).** Off by default. [ROADMAP.md](../../ROADMAP.md) explains why a dedicated `stack-sweep` module isn't planned — Stack Overflow's public-question volume is down roughly 95% off its peak, and the venues that absorbed the spillover are already covered by thread-sweep and forum-sweep — but a thin adapter here still catches the residual long tail. Each configured `site` (the API's short slug, e.g. `stackoverflow`, `ai` for ai.stackexchange.com — not the hostname) runs every query phrase against `search/excerpts`, windowed server-side by `fromdate` and floored by `min_score`. Candidates surface both questions and answers; the `is_answered` field on each feeds the same unanswered-preferred ranking the other lanes get for free.

```json
"stackexchange": { "enabled": false, "sites": ["stackoverflow", "ai"], "min_score": 0 }
```

Two things to read before enabling: the anonymous IP quota is small (roughly 300 requests/day, shared across every site queried), and a response can carry a `backoff` field demanding N seconds before the next request — this lane honours it automatically and prints a `NOTE` on stderr when it fires, without holding the lane's window (the fetch that carried the hint still succeeded). Separately, and non-negotiably: **Stack Overflow's own policy prohibits AI-generated answer content.** This lane is discovery recall only, same as every lane in this module — any reply a human chooses to write there must be genuinely human-authored and policy-compliant.

**dev.to / Forem (opt-in, discovery-only).** Off by default. `GET /api/articles?tag=<tag>` per configured tag — the documented, stable public lane (dev.to's keyword-search endpoint is undocumented and returned 404 during this build, so tag-based discovery is what's wired up). A tag alone is broad, so results are also floored by `min_reactions` and filtered through the existing `query_groups` phrases via a cheap token-overlap check before they reach you.

```json
"devto": { "enabled": false, "tags": ["ai", "llm", "agenticai", "claudeai"], "min_reactions": 3 }
```

dev.to comment etiquette parallels the rest of this set: the reply must stand alone, and drive-by link-drops burn the account.

**Medium (opt-in, RSS-by-tag).** Off by default. `GET /feed/tag/<tag>` per configured tag — an RSS 2.0 feed, no auth, no pagination. Windowed locally by `pubDate` and floored through the existing `query_groups` phrases via the same token-overlap check dev.to uses (title, snippet, and category tags all feed the overlap). Tracking query strings on Medium links (`?source=rss...`) are stripped before the URL becomes the candidate/seen-store key.

```json
"medium": { "enabled": false, "tags": ["ai-agents", "claude", "llm", "agentic-ai"] }
```

Medium is a **discovery** lane in a stronger sense than every other lane above: a Medium response (comment) is a manual human act on medium.com, and there is no reply API this module could call even if the posting gate allowed it. The value is knowing which posts are pulling the conversation in your patterns — feeding replies on the other lanes and outreach decisions, not a reply on Medium itself.

**Lemmy (opt-in).** Off by default. For each configured instance, each query phrase runs against `GET /api/v3/search?q=<phrase>&type_=Posts&sort=New`, floored by `min_score`. Windowed locally by `published`. The candidate URL is the post's local permalink on the instance you queried (`https://<instance>/post/<id>`), not the post's external submitted link and not `ap_id` — a federated post's `ap_id` points at its origin instance, which is often not the instance you configured; `ap_id` still rides along on the candidate since it's cheap to carry.

```json
"lemmy": { "enabled": false, "instances": ["programming.dev"], "min_score": 2 }
```

Lemmy is a small, federated network: an unreachable or slow instance degrades gracefully like every other multi-instance lane here, a held window for that instance next run rather than a failed scan.

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

Reddit is **disabled by default, discovery-only, manual.** Two reasons. First, shadowbans are invisible: a removed comment still looks live to the account that posted it, so a "posted" ledger entry for Reddit can be a lie. **Always verify a Reddit post out-of-band** (open the comment in a logged-out browser) before trusting the ledger. Second, the full Reddit Data API is OAuth-gated and pre-approval-gated; the public `search.rss` feed read this tool uses is unauthenticated, best-effort, rate-limited (hence the 2-second pacing floor), and may be blocked at any time, exactly as the `.json` read it replaced eventually was. Posting through it is never automated. Per-sub self-promotion rules are strict and the link is usually best omitted entirely.

### Lobsters: invite-gate + <25% self-promo ceiling

Posting on Lobsters is invite-gated; you cannot self-register. And the site enforces a hard **under-25% self-promotion ceiling** (across your whole history, fewer than one in four of your submissions may point at your own properties), plus active anti-AI-slop moderation. Fold the `ai` and `vibecoding` tags into discovery, hand-post rarely, and keep your linkless-to-self-link ratio comfortably inside the ceiling.

### Stack Exchange: AI-generated-content ban + a small quota

Two facts, both load-bearing. First, the anonymous IP quota this lane uses is small (roughly 300 requests/day, shared across every configured site) and can shrink further mid-run: a response can carry a `backoff` field telling you to wait N seconds before the next request, which this lane honours automatically. Second, and non-negotiable: **Stack Overflow's own policy prohibits AI-generated answer content.** This lane finds threads; it does not draft or post to them. Any reply a human posts on Stack Overflow or any Stack Exchange site must be genuinely their own writing and compliant with that site's policy, full stop — there is no assistant-drafted shortcut here the way there might be on a venue without that rule.

### dev.to: same gate, ordinary community norms

dev.to carries no unusual structural gate (no invite wall, no domain-level enforcement, no shadowban risk), but the module's universal rule still applies in full: the reply must stand alone, and drive-by link-drops burn the account just as fast as anywhere else. Treat it as a normal, moderate-trust venue, not a free pass because the mechanics are lighter.

### Medium: discovery only, there is no posting path

Medium is different in kind from every venue above: this lane never becomes a `mark-posted` candidate, because there is nothing to post *to*. A Medium response is a comment left by hand on medium.com, and this module has no reply API to call for it even in principle. Treat the digest as pure signal — which posts, and which authors, are already pulling the exact conversation your patterns describe — and let that inform where you post on the *other* lanes, or who's worth reaching out to directly. There is no venue-standing or self-promotion-ratio risk to manage here because there is no posting action here at all.

### Lemmy: federated, per-community norms — read before posting

Lemmy has no single site-wide policy the way Stack Overflow or Lobsters do; it's a federation of independently-run instances and communities, each with its own moderation norms, closer in spirit to the per-subreddit variation on Reddit than to a single Discourse instance. An instance being small or quiet is normal, not a signal the lane is broken — a slow or unreachable instance degrades gracefully (see above) rather than failing the scan. Before posting anywhere Lemmy surfaces, read that specific community's rules and recent threads the way you would a subreddit you don't already know; there is no repo-verified numeric ceiling to cite here the way there is for Lobsters' <25% rule, so err conservative.

The self-reference ratio is the real governor everywhere. Venues differ in their rules; the universal defence is the same: mix genuinely-helpful linkless answers in over time so the account never reads as ~100% self-link. **Record every post** with `mark-posted`. The ledger is what guarantees you never answer the same thread twice (modulo the Reddit-shadowban caveat above), and `density` keeps your recent posting count visible so restraint stays honest.

## Security note

Every forum and aggregator response this tool fetches is **untrusted external content**. A thread title or blurb can carry text engineered to look like an instruction: a fake system marker, a tool-call-shaped string, a request to fetch a URL or reveal data. The scanner never acts on fetched text; it stores a truncated snippet for a human to read. Treat every snippet as data, never instructions, wherever you read it downstream. An injection attempt inside a thread is a finding, not a task.

## Usage

```bash
cp config.example.json config.json               # edit for your project
python forum_sweep.py scan --dry-run --days 7     # preview, writes nothing at all
python forum_sweep.py scan --source lobsters      # one lane (most reliably open)
python forum_sweep.py scan                        # all enabled lanes, real run
python forum_sweep.py density                     # recent posting counts
python forum_sweep.py mark-posted --url <thread-url> --pattern <slug> --comment-file reply.md
```

Requires Python 3.10+. Stdlib only: `urllib.request` for the HTTP-JSON sources, `xml.etree.ElementTree` for the Medium and Reddit feed lanes, no third-party deps and no auth for the Discourse / HN / Lobsters lanes.

## Config reference

| Key | Meaning | Default |
|---|---|---|
| `subject` | `{name, url}` of the project you're answering for | required |
| `query_groups` | `pattern-slug → [phrases]`, reusable across sources | required |
| `sources.<lane>.groups` | narrow one phrase lane to these `query_groups` slugs (`discourse`, `hn`, `reddit`, `stackexchange`, `lemmy`; the tag-driven lanes ignore it). An unknown slug warns on stderr and is skipped | all groups |
| `sources.discourse.instances` | Discourse hosts to search | `[]` |
| `sources.hn.enabled` | run the Hacker News lane | `false` |
| `sources.lobsters.tags` | Lobsters tags to pull | `[]` |
| `sources.reddit.enabled` | run the opt-in discovery-only Reddit lane | `false` |
| `sources.reddit.subs` | subreddits to search when enabled | `[]` |
| `sources.stackexchange.enabled` | run the opt-in, thin Stack Exchange lane | `false` |
| `sources.stackexchange.sites` | SE API site slugs to search (e.g. `stackoverflow`, `ai` — not hostnames) | `[]` |
| `sources.stackexchange.min_score` | drop SE hits below this score | `0` |
| `sources.devto.enabled` | run the opt-in dev.to (Forem) lane | `false` |
| `sources.devto.tags` | dev.to tags to pull articles from | `[]` |
| `sources.devto.min_reactions` | drop dev.to articles below this reaction count | `3` |
| `sources.medium.enabled` | run the opt-in Medium (RSS-by-tag) lane | `false` |
| `sources.medium.tags` | Medium tags to pull `/feed/tag/<tag>` from | `[]` |
| `sources.lemmy.enabled` | run the opt-in Lemmy lane | `false` |
| `sources.lemmy.instances` | Lemmy instance hosts to search | `[]` |
| `sources.lemmy.min_score` | drop Lemmy posts below this score | `2` |
| `thresholds.per_source_cap` | max candidates per instance/site per scan | `4` |
| `thresholds.hn_min_points` | drop HN hits below this point count | `2` |
| `emit_cap` | recall ceiling on emitted candidates | `100` |
| `seen_retention_days` | seen-store pruning horizon | `180` |
| `default_window_days` | first-run window | `14` |
| `request_delay_seconds` | polite sleep between HTTP requests (the 429 throttle; `0` disables). The discourse and reddit lanes hold their own higher floors on top of it | `0.5` |
| `state_dir` / `candidates_file` | where state and output live | `state` / `candidates.json` |

State lives in `state/forum_sweep_state.json` (a per-source last_run map + seen) and `state/forum_sweep_log.jsonl` (the posted ledger). Both are gitignored: the ledger is your posting history; never commit it.

Each source carries its own `last_run`, so `--source hn` advances only the HN window — the other lanes that did not run keep theirs and lose nothing published in the gap. A state file from an older version with one shared `last_run` is migrated on the next scan by seeding every source with that value.

A lane earns a new `last_run` only by completing a fetch cleanly, because being asked to scan is not proof the scan happened. A request that came back holding nothing is a real, covered, empty window, and it advances. Everything else keeps the old stamp: a request that failed, an adapter that crashed, or a lane that never made a request at all because the source is off or has no instances or tags configured. The next run then re-covers that stretch instead of stepping over it. Partial failure counts as failure. If one Discourse instance 503s while the others answer, the whole lane holds, and the seen-store keeps the already-surfaced threads out of the re-scan. Held lanes are named on stderr and in the digest's `sources_held`.

Two things stop a clean run from advancing the marker, and it is the same reason twice. The window started after the marker, so a stretch in front of it went unread. `--days 3` against a 30-day-old marker leaves 27 uncovered days, and stamping `now` would swallow them silently, so the old marker stands and the next default run picks the gap back up. A marker that no longer parses does the same damage from the other end. The lane falls back to the default window with no way to tell whether that reaches far enough back, so the unreadable stamp is kept and the `WARN unreadable last_run` line repeats every run until you fix or clear it.

## Driving it with an agent

[SKILL.example.md](SKILL.example.md) is a working Claude Code skill that wraps this module: scan, score against the fit bar, draft replies, then a hard per-comment approval gate before anything posts. Port the same shape to any agent runtime; the load-bearing parts are the gate and the ledger, not the assistant.
