# placement-health

Watch the places your project is listed, and find out when one quietly drops you.

Curated lists prune entries. Links rot. A submission you spent effort landing disappears in a cleanup six months later, and nobody tells you. placement-health keeps a registry of your placements and checks each one on demand: is your entry still there, does the link still resolve.

## Not a gate, because nothing goes out

The rest of this toolkit gates outbound actions behind per-artifact human approval. placement-health has nothing outbound to gate. It reads public URLs and reports. That is the whole module: presence *intelligence*, not outreach.

## How it works

A registry (`placements.json`) lists each placement with a `url` to fetch and an `expect` string that must be present if you are still listed. For a GitHub list, point `url` at the raw README and set `expect` to your repo name or URL. The checker fetches each and reports:

- **LIVE** — 200, and your `expect` string is present.
- **DROPPED** — 200, but the string is gone. You were pruned. This is the finding the module exists for.
- **BROKEN** — the URL failed to load (non-200 or network error).
- **PENDING_OK / PENDING_MERGED** — for entries marked `"status": "pending"` (a submission not yet merged). A pending entry that 404s is normal. Once the `expect` string appears, it flags **PENDING_MERGED** so you promote it to `live`.

Exit code is non-zero if any DROPPED or BROKEN, so it also works as a cron or CI check.

## Usage

```bash
cp placements.example.json placements.json    # edit for your placements
python placement_health.py check              # human-readable report
python placement_health.py check --json       # machine-readable
python placement_health.py check --log        # also append to state/health_log.jsonl
```

No dependencies beyond the Python standard library, and no GitHub CLI needed (it reads arbitrary public URLs).

[SKILL.example.md](SKILL.example.md) is a working Claude Code skill that wraps this module: check, separate DROPPED from BROKEN, and hand back a triage list. It carries no gate because nothing goes out, so its load-bearing rules are the ones that stop an agent re-submitting on your behalf. Port the same shape to any agent runtime.

## Registry reference

| Field | Meaning |
|---|---|
| `url` | the page to fetch (raw README for a GitHub list; the page URL otherwise) — required |
| `expect` | substring that must be present if you are still listed — required |
| `name` | human label for the report (defaults to the URL) |
| `kind` | `list` / `directory` / `page` — informational |
| `status` | `live` (default) or `pending` (submission not yet merged) |

The example registry is real: the [agent-workspace-architecture](https://github.com/jimy-r/agent-workspace-architecture) project's own placements, including a pending list submission so you can see how that state reads.
