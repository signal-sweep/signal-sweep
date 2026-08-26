# cfp-sweep

Find the conference and meetup CFPs worth pitching, work out which are still open, and never burn a venue by pitching it twice. One submission at a time, through a human gate.

cfp-sweep is the other end of the discoverability funnel from list-sweep. Where list-sweep gets a project *listed*, cfp-sweep gets a person *speaking about it*. This module does the deterministic half: it discovers candidate CFPs, detects whether each one is still open, and keeps a cooldown ledger so a venue that just heard from you isn't pitched again next month. It never drafts an abstract and it never submits anything.

## How discovery works

**Lane 1 (conference-data).** Each topic in your config becomes a scan of the public [tech-conferences/conference-data](https://github.com/tech-conferences/conference-data) dataset, reading `conferences/<year>/<topic>.json` for the current year and the next, over plain HTTPS. Not every topic exists for every year (next year's files are seeded gradually; 2027 had 6 of 2026's 30 topic files at verification time), so a missing file is treated as "not there yet," never as a failure. A CFP is kept when it has a tracked deadline that hasn't passed and its event hasn't happened yet, and clears your region filter. A conference indexed under more than one of your configured topics collapses into a single candidate with an accumulated topic-match count, which is the real basis for the topic-match ranking below (a real, verified case: the same event appears byte-identical in more than one topic file).

**Lane 2 (watchlist).** A pinned list of venues you want tracked directly, such as a conference that recurs every year but isn't (yet) indexed by the dataset, or one you want closer scrutiny on. Each venue's own CFP page is fetched best-effort and scanned for a small set of open/closed phrases and dated-deadline patterns, then classified `open` / `closed` / `unknown`. A watchlist entry is always classified and always surfaced, whatever the result, the same "hand-curated, never dropped" treatment thread-sweep and list-sweep give their own watchlists.

Filters before anything reaches you: a region filter on lane-1 hits (`countries` include-list, empty means no restriction; `online` entries always included when `include_online` is set, watchlist exempt), everything previously surfaced excluded (seen-store), and every venue still inside its submission cooldown excluded (below).

## Submission-window detection, and why it stays honest

Lane 2's fetched page text is scanned only for phrases and dates. It is never executed or followed, and never trusted blindly:

- An explicit **closed** phrase (`"submissions are closed"`, `"cfp has closed"`, …) wins outright, even if the page also carries generic "call for papers" language elsewhere on the same page. A real example from Berlin Buzzwords' 2026 CFP page: the copy reads *"is open to new ideas"* two paragraphs above *"Submissions are closed"*. The second one is the truth, and closed beats open by design.
- Failing that, an explicit **open** phrase (`"submit your talk"`, `"cfp is open"`, …).
- Failing that, a **dated deadline** found near deadline-context words (`deadline`, `due by`, `closes`, …), but only when the date carries an explicit year. A future date infers `open`; a past one infers `closed`. This is not a guess: KubeCon + CloudNativeCon Europe's own CFP page (verified live) states its close date as *"CFP Closes: Sunday, 11 October"* with no year anywhere nearby, and this module reports that venue `unknown` rather than assume which October. A day-and-month with no year is common in the wild, and guessing the year is exactly the kind of guess this module refuses to make. `cfp_end: null` alongside `detected_state: "unknown"` is the honest result.
- Otherwise `unknown`, with a `detection_note` explaining why. Either no CFP-shaped language was found at all (a real cause: React Summit's GitNation CFP page is a client-rendered SPA, and a plain server-side fetch returns no scannable text, so `unknown` there is by design, not a bug), or CFP language is present but no open/closed signal or dated deadline to go on.

Every watchlist page fetch is best-effort: a fetch failure is recorded as an advisory and the venue reports `unknown`, and one flaky venue never blocks the rest of the scan.

## Ranking

Kept candidates get a coarse `high`/`med`/`low` tier from `sweepcore.relevance_tier` (fed only the one signal that has honest meaning here: a real topic match versus a generic watchlist pull, the same distinction thread-sweep and forum-sweep use their own `pattern` field for). Because a CFP carries no analogue for `relevance_tier`'s other signals (answered/comments/match-type/stars), `high` is never reached in this module. That split alone doesn't say enough, so the real ranking sits on two domain-specific keys, applied inside each tier: **topic-match count** (a CFP matching more of your configured topics ranks first) and then **deadline proximity** (a closer open deadline ranks first). A candidate with an unknown deadline ranks last within its bracket rather than pretending to an urgency it cannot support.

## The cooldown ledger, and why it is correctness, not caution

Burning a venue is permanent in a way a missed list-sweep placement is not. A program committee that gets pitched the same idea twice in one cycle remembers, and organizers talk to each other. So every submission is recorded, keyed by venue name, and a venue with a ledger entry newer than its cooldown window is excluded from candidates entirely and counted in `dropped.cooldown` rather than silently vanishing. The default cooldown is `default_cooldown_days` (180); a watchlist entry can override it per-venue with its own `cooldown_days`. A rate limit for its own sake isn't the point. The cooldown keeps a recurring venue's goodwill intact across cycles.

## The gate

The scanner produces `candidates.json`: discovered CFPs, their detected state and deadline, a topic-match count, and a tier. That is a proposal, not an action. Drafting the pitch or abstract and deciding whether to submit are **not here**. That work belongs to a human, with whatever assistant they choose, behind a per-submission approval gate. [SKILL.example.md](SKILL.example.md) spells out that flow. There is no auto-submit, no batch-approve, no scheduler, and no code path in this module that reaches a submission form. When you do submit (by hand, through the venue's own process), `mark-submitted` records it so the cooldown and the seen-store both take effect.

## Usage

```bash
cp config.example.json config.json    # edit for your project
python cfp_sweep.py scan --dry-run                 # preview fetch targets, no network/state
python cfp_sweep.py scan                            # real run: writes candidates, marks seen
python cfp_sweep.py mark-submitted --venue "PyConf Hyderabad" --url <cfp-url> --note "talk accepted"
python cfp_sweep.py log                             # show recorded submissions
python cfp_sweep.py density                         # how much you've submitted lately
```

`scan` prints a one-line summary of what it swept and kept:

```
CFP_SWEEP_OK topics=data,devops,python,javascript raw=41 kept=17 dropped={'malformed': 0, 'no_cfp': 9, 'cfp_closed': 3, 'past_event': 0, 'region': 2, 'dup': 1, 'seen': 6, 'cooldown': 2} errors=0
  fit tiers: 0 high / 11 med / 6 low
candidates -> candidates.json
```

`--dry-run` makes no network calls and writes nothing; it prints every `conferences/<year>/<topic>.json` URL lane 1 would fetch and every venue lane 2 would classify, so you can sanity-check your config before spending a real run.

## Config reference

| Key | Meaning | Default |
|---|---|---|
| `subject` | `{name, url}` of the project/speaker context a drafted pitch would represent (carried through for the agent skill; this script does not draft) | required |
| `topics` | conference-data topic names to scan (lane 1) | required |
| `countries` | region include-list for lane-1 hits; empty means no restriction | `[]` |
| `include_online` | always include `online: true` lane-1 hits regardless of `countries` | `true` |
| `watchlist` | pinned venues (lane 2): `name`, `cfp_url`, `topics` required; optional `url`, `cadence_note`, `format_note`, `open_markers`, `closed_markers`, `cooldown_days` | `[]` |
| `default_cooldown_days` | days a venue stays excluded after a recorded submission (per-entry `cooldown_days` overrides it) | `180` |
| `seen_retention_days` | seen-store pruning horizon | `180` |
| `default_window_days` | first-run default for the coverage marker (see below); does not bound what lane 1 fetches | `30` |
| `emit_cap` | ceiling on emitted candidates | `60` |
| `state_dir` / `candidates_file` | where state and output live | `state` / `candidates.json` |

The example config is real: verified-live topic names, and three watchlist venues chosen to show the three outcomes best-effort scanning actually produces: an explicit closed phrase (Berlin Buzzwords), a page whose close date lacks a year so best-effort detection honestly falls back to `unknown` despite the CFP being open (KubeCon + CloudNativeCon Europe), and a client-rendered SPA where a plain fetch returns no scannable text at all (React Summit).

### Topic names

`conference-data` topic files do not track "ai" as a category. The topic files that existed for 2026 at verification time (2026-08-26) were: `accessibility`, `android`, `api`, `clojure`, `cpp`, `css`, `data`, `devops`, `dotnet`, `general`, `graphql`, `groovy`, `ios`, `iot`, `java`, `javascript`, `kotlin`, `leadership`, `networking`, `opensource`, `performance`, `php`, `product`, `python`, `rust`, `security`, `sre`, `testing`, `typescript`, `ux`. `data` is the closest real category to AI/ML conferences (sampled live: AI DBA, betterCode() GenAI, AI DevWorld, AgentCon Bangkok all file under it) and is what the example config uses in place of the non-existent `ai`. Next year's files seed in gradually over the year rather than appearing all at once, so the module treats a missing topic/year file as "not published yet," not an error.

### On the coverage marker

Every other scanning module in this toolkit uses `last_run` to bound an incremental query (`pushed:>floor`, "issues created after X"). cfp-sweep is different: lane 1 always re-reads the *entire* current + next year dataset for your configured topics on every scan. There is nothing for a day-count window to narrow, so `scan` takes no `--days` override. `last_run`/`window_held` still exist and still follow the same earned-marker mechanics as the rest of the toolkit (`sweepcore.window_start` / `earned_stamp`): the marker only advances when every topic/year fetch in lane 1 came back and none failed outright (200 and 404 both count as "came back," since a 404 usually just means that year's file hasn't been seeded yet). A held run is reported in the digest (`window_held: true`) and on stderr; it changes nothing about what gets fetched next time (lane 1 always fetches everything regardless), so its only job is to tell you the last scan didn't fully land.

## Driving it with an agent

[SKILL.example.md](SKILL.example.md) is a working Claude Code skill that wraps this module: scan, draft a pitch per venue from your own talking points, then a hard per-submission approval gate before anything is submitted by hand through the venue's own form. Port the same shape to any agent runtime; the load-bearing parts are the gate and the ledger, not the assistant.
