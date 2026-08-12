# release-sweep

Turn a release into ready-to-draft announcement material, one set per channel, without re-reading your own diff five times.

Every release, the same chore: restate what changed for Hacker News, for a social post, for a changelog entry, for a newsletter curator, each with its own length and tone. release-sweep does the deterministic half. It pulls the real release (tag, notes, and the commit diff since the previous tag) and pairs it with your channel registry, so an assistant drafts from exact material instead of inventing it.

## The gate applies here

Unlike `placement-health`, this module's output is outbound: announcements you post. So the gate is in force. The script does two safe things only: assembles material (`brief`) and records what went out (`mark-announced`). It does not draft the final copy and it does not post. Drafting is judgment; posting is a per-channel human decision. Nothing reaches a channel without your approval.

## How it works

1. **`brief`** pulls the release via `gh` (tag, notes, URL, and commit subjects + file-count since the previous tag) and writes `release_brief.json`, pairing the material with each channel's constraints from `channels.json`. It reads the ledger first and leaves out any channel already announced for this repo and version.
2. An assistant drafts one announcement per channel from that brief (the [SKILL example](SKILL.example.md) wires this up).
3. You approve and post each one individually.
4. **`mark-announced --version vX --channel NAME`** records it, so the ledger shows what's out and the next `brief` for that repo and version skips the channel.

Step 4 is what makes step 1 stick. Announce `v1.2.0` to `show-hn`, record it, and the next `brief --tag v1.2.0` scaffolds the remaining channels only. Once every active channel is recorded, the run prints `RELEASE_BRIEF_SKIP` and hands back no drafting material. `brief --force` re-admits the recorded channels, each flagged `already_announced` in the brief, for a deliberate redraft (a post that got deleted, say). It changes what gets assembled, never what gets sent. Posting stays a per-channel human action either way, and `brief` only ever reads the ledger.

## What the ledger keys on

The ledger lives beside the module, so one checkout keeps one ledger for every repo you run it against. An entry records `repo`, `version` and `channel`, and all three make up the key. Announcing `v1.0.0` for `org/a` therefore leaves a genuinely new `org/b` `v1.0.0` fully scaffolded — a first-release tag collides across repos almost immediately, so keying on the tag alone would silently swallow the second one.

`mark-announced` records the repo it detects from the current directory; pass `--repo OWNER/NAME` when you record an announcement from somewhere else. The three key fields are stored folded to lowercase with whitespace collapsed, and read back the same way, so `Show HN`, `show hn` and ` show  hn ` are one channel rather than three chances to announce twice. `log` prints the repo alongside each entry.

**Upgrading an existing ledger.** Lines written before entries carried a repo have no `repo` field, and those apply to *every* repo. That direction is deliberate: the repo that wrote the line stays guarded, and an over-match is visible and recoverable where a missed match is a duplicate post. `brief` names any channel it suppressed on a pre-repo line and tells you so; add a `"repo": "OWNER/NAME"` field to those lines (or delete them) to scope them, and use `--force` if you need the channel back in the meantime.

## Usage

```bash
cp channels.example.json channels.json     # edit for your channels
python release_sweep.py brief --repo OWNER/NAME      # latest release; auto-detects previous tag
python release_sweep.py brief --tag v1.2.0 --since v1.1.0   # explicit
python release_sweep.py brief --tag v1.2.0 --force           # redraft channels already recorded
python release_sweep.py mark-announced --version v1.2.0 --channel show-hn
python release_sweep.py mark-announced --version v1.2.0 --channel show-hn --repo OWNER/NAME
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
