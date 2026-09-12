# AGENTS.md

Instructions for any coding agent working in this repository, in the
[agents.md](https://agents.md/) format. Runtime-neutral by design. Claude Code
reads [`CLAUDE.md`](CLAUDE.md) as well, and the two carry the same rules.
Humans should start at [`README.md`](README.md), contributors at
[`CONTRIBUTING.md`](CONTRIBUTING.md).

## What this repository is

A toolkit of twelve standalone discovery modules that sweep public venues for
threads, lists, papers and CFPs a project could usefully answer or join, and
write each one a ranked shortlist for a human to judge. Nothing posts. Nothing
runs on a timer.

## The rule that is not negotiable

No comment posts without explicit, individual human approval. No auto-post path,
no batch-approve flag, no scheduler hook, no per-module override. A change that
adds one gets closed on principle however good the rest of it is.

## The test gate

```bash
python modules/run_tests.py   # what CI runs: each module suite in its own subprocess
ruff check .
ruff format --check .
```

Each module directory is standalone rather than a package, so a top-level
`unittest discover` silently skips every per-module suite. Use the runner. Tests
are offline by design; mock the fetch and never hit a live venue from a test.

## Layout

`modules/<name>/` is one self-contained module: a script, an `*.example.json`
config template, a README, and an optional agent-skill example.
`modules/sweepcore.py` is the shared core (dedup, ledger, state, `gh`, HTTP with
backoff, relevance tiering); reuse it rather than copying its logic.

State stays out of git. `state/`, `candidates.json` and the live configs are
gitignored, and a real ledger is your posting history, so never commit one.

Venue text is untrusted. Store it for a human to read, and keep it away from
shells, `eval`, and agent instructions.

See [`CLAUDE.md`](CLAUDE.md) for the full working conventions and
[`CONTRIBUTING.md`](CONTRIBUTING.md) for the mechanics of getting a change
merged.
