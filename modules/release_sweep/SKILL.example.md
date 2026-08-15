---
name: release-sweep
description: On a new release, assemble per-channel announcement material and draft one announcement per channel, posting only on explicit per-channel user approval. Never auto-posts.
---

> Example Claude Code skill wrapping the release-sweep module. Replace the
> bracketed placeholders with your project's specifics and drop this in
> `.claude/skills/release-sweep/SKILL.md`. The load-bearing parts are the
> Iron Laws.

## Purpose

When a release is cut, draft its announcements for each channel from the real release material, and post each only after the user approves it. The script (`release_sweep.py`) assembles the material deterministically; drafting is judgment; posting is gated.

## Iron Laws

1. **No announcement is posted without explicit per-channel user approval.** One approval per channel — no "post them all" blanket yes.
2. **Draft only from the brief.** Every claim in an announcement must trace to the release notes, commit subjects, or stats in `release_brief.json`. Do not invent features, numbers, or fixes the release didn't ship.
3. **Never double-announce.** `brief` drops any channel already recorded in the ledger for this repo and version. Never re-add one by hand, and never pass `--force` unless the user asks for a redraft.
4. **The script does not post.** Posting is a per-channel human action (the channels differ too much to automate safely). After the user posts, record it with `mark-announced`.

## Procedure

1. **Assemble:** `python release_sweep.py brief --repo OWNER/NAME` (or `--tag`/`--since` to be explicit). Report the `RELEASE_BRIEF_OK` line. Read `release_brief.json`. A `RELEASE_BRIEF_SKIP` line means this release is already announced everywhere: stop and tell the user.
2. **Check the ledger:** the brief's `already_announced` list is what `brief` filtered out for you; `python release_sweep.py log` shows the full history if you want to confirm it. Anything also listed in `already_announced_legacy` was matched by a pre-repo ledger line that applies to every repo, so confirm with the user that it really was announced for *this* repo before treating it as done.
3. **Draft per channel:** for each remaining channel, draft one announcement from the brief, respecting its `char_limit` and `notes`. Apply the writing-style rules — these are public copy. Lead each with the single most interesting change in the release, not a feature list.
4. **Gate:** present the drafts one channel at a time; the user approves, edits, or skips each.
5. **Post + record (approved only):** the user posts to the channel (you provide the final text); then `python release_sweep.py mark-announced --version vX --channel NAME` (add `--repo OWNER/NAME` when the working directory is not that repo, so the ledger scopes the record correctly).

## Rules

- Never run on a schedule — a release announcement is a deliberate act.
- If the release notes are thin, lean on the commit subjects in the brief, but still don't invent — if there isn't material for a channel, say so rather than padding.
