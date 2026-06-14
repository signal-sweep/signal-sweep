# release-sweep

Turn a release into ready-to-draft announcement material, one set per channel, without re-reading your own diff five times.

Every release, the same chore: restate what changed for Hacker News, for a social post, for a changelog entry, for a newsletter curator, each with its own length and tone. release-sweep does the deterministic half. It pulls the real release (tag, notes, and the commit diff since the previous tag) and pairs it with your channel registry, so an assistant drafts from exact material instead of inventing it.

## The gate applies here

Unlike `placement-health`, this module's output is outbound: announcements you post. So the gate is in force. The script does two safe things only: assembles material (`brief`) and records what went out (`mark-announced`). It does not draft the final copy and it does not post. Drafting is judgment; posting is a per-channel human decision. Nothing reaches a channel without your approval.

## How it works

1. **`brief`** pulls the release via `gh` (tag, notes, URL, and commit subjects + file-count since the previous tag) and writes `release_brief.json`, pairing the material with each channel's constraints from `channels.json`.
2. An assistant drafts one announcement per channel from that brief (the [SKILL example](SKILL.example.md) wires this up).
3. You approve and post each one individually.
4. **`mark-announced --version vX --channel NAME`** records it, so the ledger shows what's out and you never double-announce the same release to the same channel.

## Usage

```bash
cp channels.example.json channels.json     # edit for your channels
python release_sweep.py brief --repo OWNER/NAME      # latest release; auto-detects previous tag
python release_sweep.py brief --tag v1.2.0 --since v1.1.0   # explicit
python release_sweep.py mark-announced --version v1.2.0 --channel show-hn
python release_sweep.py log
```

Run from inside the repo and `--repo` auto-detects from the GitHub remote. Requires the GitHub CLI (`gh auth login`); standard library otherwise.

## Channel registry reference

| Field | Meaning |
|---|---|
| `name` | channel label (used in the ledger) |
| `kind` | `social` / `longform` / `newsletter` — informational |
| `char_limit` | length budget the drafter must respect (`null` for none) |
| `notes` | the channel's format and tone rules — the drafter treats these as constraints |
| `active` | set `false` to pause a channel: it stays in the registry but `brief` skips it (and lists it as paused). Defaults to active. |

The example registry covers Show HN, X, Reddit, a changelog post, and a newsletter blurb. Edit it to the channels you actually use; the brief only scaffolds what's listed and not paused. Pausing (rather than deleting) keeps a channel's constraints around for when you want it back.
