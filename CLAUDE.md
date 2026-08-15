# CLAUDE.md (contributor context)

Human-gated presence tooling. Two maintainers (@jimy-r, @jtzingsheim1), best-effort.

## Working principles

- **Branch for changes once the repo has users; direct commits to `main` are fine while it's just us bootstrapping.** Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`).
- **The approval gate is load-bearing.** Never add an auto-post path, a batch-approve flag, or a scheduler hook to any module. PRs that weaken the human gate get closed on principle — it's the project's whole identity.
- **Stdlib + `gh` only** for module code unless a dependency earns its keep in the PR description.
- **`python modules/run_tests.py` is the test gate.** It is what CI runs. Each module directory is standalone rather than a package, so a top-level `unittest discover` finds only `modules/test_sweepcore.py` and skips every per-module suite; the runner walks each directory in its own subprocess instead. Run it before you push.
- **State stays out of git.** `state/`, `candidates.json`, and ledgers are gitignored; never commit a real ledger (it contains your posting history).
- Include a `Co-Authored-By:` trailer on agent-assisted commits.

## Layout

- `modules/<name>/`: one self-contained module per directory (script, `config.example.json`, module README, optional agent-skill example).
- `modules/sweepcore.py`: the shared core the modules import — dedup, ledger, state, `gh`, HTTP with backoff, relevance tiering. Reuse it rather than copying its logic into a new module.
- A helper that is not yet in sweepcore stays inside its module until a second module genuinely needs it; move it into the core on the second use, not the first.
