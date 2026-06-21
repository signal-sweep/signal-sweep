# mention-sweep

Find where your project is already being talked about, then engage or correct it — one mention at a time, through a human gate.

## Entity-first, not problem-first

thread-sweep starts from a problem: who has the question your docs answer? mention-sweep starts from the entity: where is your project's name or repo URL already showing up? The two are complements. thread-sweep recruits new readers from cold threads; mention-sweep defends and amplifies the conversations that already name you.

Why that distinction earns its own module: a passive mention is a different kind of opportunity. Someone listing your project in an awesome-list, asking how it works, or saying it's abandoned when it isn't — none of those are questions your docs "answer." They're places your reputation is being set by other people, and you may want a say.

## How discovery works

**Lane 1 (threads).** Each match string runs as a quoted GitHub search across issues and discussions, windowed from the last run. Match strings are your distinctive names and repo URLs, not problem phrasings.

**Lane 2 (code).** Each match string runs through `gh search code`, which finds your name or URL inside files: awesome-list entries, README references, dependency manifests, config snippets. GitHub's code search has no date filter, so the seen-store is the freshness backstop for this lane rather than a time window.

Filters before anything reaches you: your own repos excluded (set them in `own_repos`), an optional star floor on the thread lane (`min_stars`, default 0 so nothing is dropped by accident), a per-repo cap so one busy repo can't flood the digest, everything previously surfaced excluded (seen-store), everything previously engaged excluded forever (ledger).

Each hit gets a heuristic class to triage faster:

- **favorable-mention** — the default; your project named in passing or in a list.
- **question** — the text reads like someone asking how it works.
- **possible-misdescription** — markers like "abandoned", "deprecated", "broken", "don't use" appear. This is the one to read first; it's where a fair correction does the most good.

The class is a hint from string matching, never a verdict. A human reads the snippet and decides.

## Match confidence

Each candidate also carries a `match_type`: `url` when the hit came from a repo URL or `owner/name` path, `name` when it came from a bare project name. URL matches are high-confidence, because only a real reference produces them. Bare-name matches are noisier: a name like `agent-workspace-architecture` is a plausible generic phrase, so many name-only hits are coincidental rather than genuine references. Candidates are sorted url-first and the scan prints a `by_match_type` count, so triage the `url` hits first and treat `name`-only hits as low-confidence until you read them.

## The gate

Discovery, classification, and draft stubs are automated. Engaging, correcting, amplifying, or converting a mention is not. Nothing goes outbound without per-artifact human approval. There is no auto-post path, no batch-approve flag, no scheduler that acts. `mark-posted` only records a reply you already posted by hand, so the ledger never surfaces that mention again.

## Four promotion uses

1. **Defend reputation.** A `possible-misdescription` ("project X is unmaintained") is a chance to correct the record where readers will see it.
2. **Amplify advocates.** Someone already recommending you is worth a thank-you or a useful follow-up that strengthens the recommendation.
3. **Convert passive mentions.** A bare listing or an offhand reference can become an engaged reader with one helpful, link-free comment.
4. **Catch listing threats.** A code-lane hit in an awesome-list or directory can show your entry being moved, downgraded, or dropped before it disappears quietly.

## Reddit and X

Both are auth-degraded with no clean public read path: Reddit's full Data API is OAuth- and approval-gated, and its shadowban removals are invisible (a removed comment still looks live to the poster, so a ledger entry for it can be a lie); X has no usable free search tier. Neither is a posting lane here. If you track mentions on those venues, do it by hand and keep the engagement out of this tool's ledger. This mirrors forum-sweep, which keeps Reddit discovery-only and opt-in for the same reasons.

## Etiquette

- **The comment must stand without the link.** Say the useful thing in the thread; a link with no substance is spam. If removing your link makes it a worse comment, don't post.
- **Attribute honestly, first person.** "I maintain this project — the abandoned label is out of date, last release was last week" reads better than fake-neutral correction and survives moderation.
- **Corrections are facts, not defensiveness.** State what's true and let it stand. Arguing tone loses even when the facts win.
- **Stay scarce.** A few genuinely useful engagements a month build standing; more spends it. `density` prints your recent posting counts on every scan.
- **Record every reply** with `mark-posted` — the ledger is what guarantees you never engage the same mention twice.

## Usage

```bash
cp config.example.json config.json    # edit for your project
python mention_sweep.py scan --dry-run --days 7   # preview the queries, no calls
python mention_sweep.py scan                      # real run
python mention_sweep.py density
python mention_sweep.py mark-posted --url <mention-url> --kind correct --comment-file reply.md
```

## Config reference

| Key | Meaning | Default |
|---|---|---|
| `display_name` | label for the project set | required |
| `match_strings` | distinctive names + repo URLs to search for | required |
| `own_repos` | `owner/name` repos excluded from results | required |
| `min_stars` | thread-lane star floor (code lane exempt) | `0` |
| `per_repo_cap` | max candidates per repo per scan | `4` |
| `per_query` | results fetched per match string per type | `20` |
| `emit_cap` | recall ceiling on emitted candidates | `100` |
| `seen_retention_days` | seen-store pruning horizon | `180` |
| `default_window_days` | first-run window | `30` |
| `scan_code_lane` | run lane 2 (`gh search code`) | `true` |
| `state_dir` / `candidates_file` | where state and output live | `state` / `candidates.json` |

The example config is a real one: the match strings and own-repo exclusions for the [agent-workspace-architecture](https://github.com/jimy-r/agent-workspace-architecture) and [signal-sweep](https://github.com/signal-sweep/signal-sweep) projects.

## Driving it with an agent

[SKILL.example.md](SKILL.example.md) is a working Claude Code skill that wraps this module: scan, classify, draft engage/correct stubs, then a hard per-comment approval gate before anything posts. Port the same shape to any agent runtime; the load-bearing parts are the gate and the ledger, not the assistant.
