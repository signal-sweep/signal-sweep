# Contributing

Issues and pull requests are welcome, module ideas especially. Two maintainers run this best-effort, so a reply may take a week.

## The rule that is not negotiable

Nothing that weakens the human approval gate gets merged. No auto-post path, no batch-approve flag, no scheduler hook, no per-module override. The gate is the project's identity and the reason maintainers tolerate the tool at all, so a PR that adds one gets closed on principle however good the rest of it is.

[`CLAUDE.md`](CLAUDE.md) carries the working conventions in full. This file covers the mechanics.

## Setting up

Python 3.10 or newer and an authenticated [GitHub CLI](https://cli.github.com/). Nothing else is needed. The modules are stdlib plus `gh`.

```bash
git clone https://github.com/signal-sweep/signal-sweep
cd signal-sweep
gh auth status                 # the modules shell out to gh
python modules/run_tests.py    # the test gate; every suite, all offline
```

Each module runs standalone from its own directory.

```bash
cd modules/thread_sweep
cp config.example.json config.json
python thread_sweep.py scan --dry-run --days 30   # writes nothing at all
```

## Before you push

```bash
python modules/run_tests.py      # what CI runs: each suite in its own subprocess
ruff check .
ruff format --check .
```

Lint pins live in `.github/requirements-ci.txt` and the explicit select lives in `ruff.toml`, so a ruff bump cannot silently change what is linted. Install the pinned version if a finding looks unfamiliar.

Tests are offline by design. Mock the fetch. Never hit a live venue from a test.

## Changing a module

- Stdlib plus `gh` only, unless a dependency earns its keep in the PR description.
- State stays out of git. `state/`, `candidates.json` and the live configs are gitignored. Never commit a real ledger, because it is your posting history.
- Paths anchor on the module, never on the CWD. Use `sweepcore.resolve_module_path`. Every module is expected to appear in the tables of `modules/test_module_paths.py`, which is the regression guard.
- A helper stays inside its module until a second module genuinely needs it. It moves into `sweepcore.py` on the second use, not the first.
- Venue text is untrusted. Store it for a human to read, and keep it away from shells, `eval`, and agent instructions.

## Adding a module

New modules arrive when a real need pulls one in, so open an issue describing the need before you build. A module directory holds these files.

```
modules/<name>/
  <name>.py            the script; standalone, no package __init__
  config.example.json  a real worked example, not a stub
  README.md            what it does, the lanes, the etiquette
  test_<name>.py       offline suite
  SKILL.example.md     the agent wrapper; every module ships one
```

Register it in the module table in [`README.md`](README.md) and in [`ROADMAP.md`](ROADMAP.md), and give it a [`CHANGELOG.md`](CHANGELOG.md) entry in the same change as the tag.

## Commits and PRs

[Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `chore:`), one logical change per commit. Agent-assisted commits carry a `Co-Authored-By:` trailer.

A PR description says what changed and why, names the module, and reports the test run. If the change touches a gate, say so in the first line so a reviewer looks there first.

## Security

Do not report a vulnerability in an issue or a PR. [`SECURITY.md`](SECURITY.md) has the private path.
