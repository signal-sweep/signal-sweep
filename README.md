# signal-sweep

Human-gated presence tooling for open-source projects.

Maintainers answer the same questions over and over in scattered GitHub threads, while the people asking never find the docs that already answer them. signal-sweep closes that gap without becoming a spam cannon: a deterministic scanner discovers candidate threads, a human (optionally working with an AI assistant) judges fit and drafts replies, nothing posts without explicit per-comment approval, and a ledger guarantees no thread is ever answered twice.

Built by [James Ross](https://github.com/jimy-r) and [Justin Zingsheim](https://github.com/jzingsheimdes).

## Design principles

1. **Deterministic discovery, human judgment.** Code retrieves and filters (search queries, dedup, venue floors, per-repo caps). People decide what deserves an answer. If an LLM helps with scoring and drafting, it works from the scanner's output and its drafts go through the same gate as everything else.
2. **The gate is the feature.** No comment is posted without explicit, individual approval. There is no batch mode, no auto-post flag, and no schedule. Tools that post on their own become the reason maintainers ban link-droppers.
3. **The answer is the payload; the link is garnish.** A reply must fully resolve the thread standing alone. If removing your link would make it a worse comment, don't post it.
4. **A ledger, so nothing doubles up.** Every posted reply is recorded (thread URL, date, final text). The scanner excludes ledgered threads forever, and posting density is reported on every run so restraint stays visible.
5. **Scarcity is the spam defence.** A few genuinely useful replies a month build standing. More than that spends it.

## Modules

| Module | What it does | Status |
|---|---|---|
| [`thread-sweep`](modules/thread_sweep/) | Finds open GitHub issues and discussions whose problem your project's docs already solve. Two lanes: per-topic search queries across all of GitHub, plus a pinned watchlist of high-overlap repos. | v0 |

The shape is deliberately modular. Candidate siblings: a mention sweep (where your project is being discussed), a curated-list submission tracker, a release radar for repos you cite. Ideas and PRs welcome.

## Quick start

Requires Python 3.10+ and an authenticated [GitHub CLI](https://cli.github.com/) (`gh auth login`). No other dependencies.

```bash
cd modules/thread_sweep
cp config.example.json config.json   # then edit: your repo, your topics, your queries
python thread_sweep.py scan --dry-run --days 7   # preview, no state written
python thread_sweep.py scan                      # real run: writes candidates + marks seen
python thread_sweep.py density                   # how much you've posted lately
```

The scanner emits `candidates.json`. Read it, judge fit, draft replies, post the ones that clear your bar, then record each one:

```bash
python thread_sweep.py mark-posted --url <thread-url> --pattern <topic> --comment-file <reply.md>
```

The module [README](modules/thread_sweep/README.md) covers the lanes, the scoring bar, and the etiquette rules in detail. If you drive it with an agent, [SKILL.example.md](modules/thread_sweep/SKILL.example.md) is a working Claude Code skill wrapper with the approval gate spelled out.

## License

[MIT](LICENSE).
