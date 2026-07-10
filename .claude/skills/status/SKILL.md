---
name: status
description: >
  This skill should be used when the user wants a dashboard view of their Foundry —
  trigger phrases include "status", "show the ledger", "what's in the foundry",
  "how are my projects doing", "what's stale", or "foundry overview".
metadata:
  version: "1.0.0"
---

# Foundry Status — the dashboard

## Locate the Foundry (shared bootstrap)

1. If `./foundry/` exists in the workspace (or the cwd contains `CLAUDE.md` +
   `scripts/foundry.py`), use it. `git pull origin main` first if a remote exists.
2. Else if the `FOUNDRY_REPO` environment variable is set, `git clone "$FOUNDRY_REPO" foundry`.
3. Else if a GitHub tool/connector is available (ToolSearch "github"), find the repo
   named `foundry` on the user's account and clone or read/write it through the tools.
4. Else ask the user for the repo URL (https; may include an access token for private
   repos), then clone it.

Then read `CLAUDE.md` at the repo root and follow it.

## Procedure

1. `python3 scripts/foundry.py validate` — if it fails, report the problems and fix
   them before anything else; a lying dashboard is worse than none.
2. `python3 scripts/foundry.py ledger` — regenerates `LEDGER.md`.
3. Present the dashboard, most urgent first:
   - 🙋 anything that needs Ryan (blockers, open questions),
   - 🔥 / ⚠️ stale in-flight projects with idle time and their exact next actions,
   - the rest of in-flight, inbox docs awaiting intake, shipped & maintained.
4. End with **one recommended action** — a single sentence, e.g. "recipe-app has been
   idle 12 days and is one milestone from done; say 'resume recipe-app' to finish it."
5. Commit the regenerated ledger (`git add LEDGER.md && git commit -m "ledger: refresh"`,
   push with retries). Status is read-only otherwise — it never modifies project state.

## Rules

- The ledger file is generated; never hand-edit it or paraphrase numbers it doesn't
  contain.
- If validation failures were fixed, that's a working session: full end-of-session
  ritual applies.
