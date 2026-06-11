# CLAUDE.md (contributor context)

Human-gated presence tooling. Two maintainers (@jimy-r, @jzingsheimdes), best-effort.

## Working principles

- **Branch for changes once the repo has users; direct commits to `main` are fine while it's just us bootstrapping.** Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`).
- **The approval gate is load-bearing.** Never add an auto-post path, a batch-approve flag, or a scheduler hook to any module. PRs that weaken the human gate get closed on principle — it's the project's whole identity.
- **Stdlib + `gh` only** for module code unless a dependency earns its keep in the PR description.
- **State stays out of git.** `state/`, `candidates.json`, and ledgers are gitignored; never commit a real ledger (it contains your posting history).
- Include a `Co-Authored-By:` trailer on agent-assisted commits.

## Layout

- `modules/<name>/`: one self-contained module per directory (script, `config.example.json`, module README, optional agent-skill example).
- Shared helpers stay inside the module until two modules genuinely need them; extract on the second use, not the first.
