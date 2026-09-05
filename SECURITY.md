# Security policy

## Reporting a vulnerability

Report privately. Do not open a public issue for a security problem.

Use GitHub's private vulnerability reporting on this repository: the **Security** tab, then **Report a vulnerability**. If that form is not available to you, open an issue titled `security contact request` containing nothing but that phrase, and a maintainer will set up a private channel before you send any detail.

Two maintainers run this project best-effort. Expect a first reply within about a week. For a valid report we will tell you what we plan to do and when, and credit you in the release notes unless you would rather we did not.

## Supported versions

Fixes land on `main` and ship in the next tagged release. Older tags are not patched. Python 3.10 is the floor and CI tests 3.10, 3.13 and 3.14. (3.10 reaches end-of-life in October 2026; the floor moves to 3.11 in the first release after.)

## What this tool touches

Worth knowing before you assess it, and worth checking if you are looking for something to report.

- **It runs on your machine, under your account.** There is no hosted component and nothing phones home.
- **It shells out to the GitHub CLI with your live token.** `sweepcore.gh` runs `gh` as a subprocess, so every GitHub read carries whatever scopes you granted at `gh auth login`. A path that lets fetched content reach that argument list would be a real finding.
- **Everything it fetches is untrusted text.** Thread titles, snippets, blurbs, README bodies and forum posts come from strangers. The modules store that text for a human to read and never execute it or treat it as instruction. A path where fetched content reaches a shell or an agent prompt as a directive is a finding, and so is a stored field that escapes the human-readable framing.
- **The gate is a security property.** No module posts or submits on its own. Any code path that could produce an outbound action without a per-item human approval belongs in a vulnerability report rather than a feature request.
- **Ledgers hold your posting history.** `state/`, `candidates.json` and the live configs are gitignored for that reason. A change that writes real posting history to a tracked path is a finding.

## Out of scope

- Rate limits, 403s and 429s from the venues the scanners read. Those are the venue's own controls working.
- Anything that first requires write access to your machine or your `gh` token.
- Vulnerabilities in `gh` itself. Report those to [cli/cli](https://github.com/cli/cli/security).
- Missing hardening in the example configs. They are worked examples, meant to be copied and edited.
