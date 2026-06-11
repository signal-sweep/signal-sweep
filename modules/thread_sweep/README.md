# thread-sweep

Find the open GitHub issues and discussions whose problem your project's docs already solve, then answer them — one at a time, through a human gate.

## How discovery works

**Lane 1 (query-first).** Each topic your project can answer gets 2–3 search phrasings (GitHub search syntax: bare terms AND together, quotes are exact). The scanner runs them across all of GitHub's issues *and* discussions, windowed from the last run, so input scales with what's new rather than re-crawling anything.

**Lane 2 (watchlist).** A few repos whose audience overlaps yours so strongly they deserve a look even without keyword hits. The scanner pulls their newest threads since the last run.

Filters before anything reaches you: a venue floor (`min_stars`, watchlist exempt), a per-repo cap so one mega-repo can't flood the output, your own repo and account excluded, everything previously surfaced excluded (seen-store), everything previously *answered* excluded forever (ledger).

## The judgment half is yours

The scanner is recall; you are precision. For each candidate ask:

- **Direct fit:** does a specific page of your docs answer *this exact question*? Adjacency ("they're discussing our general area") is a skip.
- **Venue:** is the repo notable, and is its audience your audience? The `pattern` field is a hint from loose search matching, not a verdict.
- **Answer-gap:** if the thread has comments, read them first. If a good answer exists, skip; never duplicate.

## Etiquette, the part that matters

- **The reply must stand without the link.** Full mechanism in-thread. If deleting your link makes it a worse comment, don't post.
- **Attribute honestly, first person:** "I maintain a reference for this exact pattern: <link>" reads better than fake-neutral linking, and survives moderation.
- **Stay scarce.** A handful of genuinely useful replies a month builds standing; more spends it. `density` prints your recent posting counts on every scan so restraint stays visible.
- **Record every post** with `mark-posted` — the ledger is what guarantees you never answer the same thread twice.

## Usage

```bash
cp config.example.json config.json    # edit for your project
python thread_sweep.py scan --dry-run --days 7   # preview, state untouched
python thread_sweep.py scan                      # real run
python thread_sweep.py density
python thread_sweep.py mark-posted --url <thread-url> --pattern <topic> --comment-file reply.md
```

## Config reference

| Key | Meaning | Default |
|---|---|---|
| `own_login` / `own_repo` | excluded from results | required |
| `queries` | topic → `{answers_with, phrases[]}` | required |
| `watchlist` | repos pulled without keywords | `[]` |
| `min_stars` | lane-1 venue floor | `300` |
| `per_repo_cap` | max candidates per repo per scan | `4` |
| `per_query` | results fetched per phrasing per type | `15` |
| `emit_cap` | recall ceiling on emitted candidates | `100` |
| `seen_retention_days` | seen-store pruning horizon | `180` |
| `default_window_days` | first-run window | `14` |
| `state_dir` / `candidates_file` | where state and output live | `state` / `candidates.json` |

The example config is a real one: the topic groups the [agent-workspace-architecture](https://github.com/jimy-r/agent-workspace-architecture) project actually sweeps with.

## Driving it with an agent

[SKILL.example.md](SKILL.example.md) is a working Claude Code skill that wraps this module: scan, score against the fit bar, draft replies, then a hard per-comment approval gate before anything posts. Port the same shape to any agent runtime; the load-bearing parts are the gate and the ledger, not the assistant.
