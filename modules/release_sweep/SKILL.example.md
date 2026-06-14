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
3. **Never double-announce.** Check the ledger (`release_sweep.py log`); skip any channel already recorded for this version.
4. **The script does not post.** Posting is a per-channel human action (the channels differ too much to automate safely). After the user posts, record it with `mark-announced`.

## Procedure

1. **Assemble:** `python release_sweep.py brief --repo OWNER/NAME` (or `--tag`/`--since` to be explicit). Report the `RELEASE_BRIEF_OK` line. Read `release_brief.json`.
2. **Check the ledger:** `python release_sweep.py log` — drop any channel already announced for this version.
3. **Draft per channel:** for each remaining channel, draft one announcement from the brief, respecting its `char_limit` and `notes`. Apply the writing-style rules — these are public copy. Lead each with the single most interesting change in the release, not a feature list.
4. **Gate:** present the drafts one channel at a time; the user approves, edits, or skips each.
5. **Post + record (approved only):** the user posts to the channel (you provide the final text); then `python release_sweep.py mark-announced --version vX --channel NAME`.

## Rules

- Never run on a schedule — a release announcement is a deliberate act.
- If the release notes are thin, lean on the commit subjects in the brief, but still don't invent — if there isn't material for a channel, say so rather than padding.
