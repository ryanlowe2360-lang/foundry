---
name: upkeep
description: >
  This skill should be used when the user (or a scheduled session) wants maintenance
  run across Foundry projects — trigger phrases include "upkeep", "maintenance",
  "health check", "check my projects", "weekly foundry run", or scheduled-task
  prompts that mention Foundry upkeep.
metadata:
  version: "1.0.0"
---

# Foundry Upkeep — maintenance without babysitting

Keep shipped projects healthy and forgotten projects loud. Runs interactively or as
the weekly scheduled session.

## Locate the Foundry (shared bootstrap)

1. If `./foundry/` exists in the workspace (or the cwd contains `CLAUDE.md` +
   `scripts/foundry.py`), use it. `git pull origin main` first if a remote exists.
2. Else if the `FOUNDRY_REPO` environment variable is set, `git clone "$FOUNDRY_REPO" foundry`.
3. Else if a GitHub tool/connector is available (ToolSearch "github"), find the repo
   named `foundry` on the user's account and clone or read/write it through the tools.
4. Else ask the user for the repo URL — or, in an unattended scheduled session, stop
   and send a setup nudge instead (see Unattended mode).

Then read `CLAUDE.md` at the repo root and follow it.

## Procedure

1. `python3 scripts/foundry.py validate` — fix state problems first.
2. **For each `shipped`/`maintenance` project** (in `src/` or its graduated repo):
   pull → install deps → run its test suite → audit (`npm audit` / `pip list
   --outdated` / equivalent) → apply **safe** updates → re-run tests → buildlog entry
   with evidence → commit.
   - **Safe (just do it):** patch/minor dependency bumps that keep tests green,
     security patches, lockfile refresh, re-running suites, regenerating the ledger.
   - **Ask-first (report, never touch unattended):** major version bumps, framework
     or storage migrations, feature changes, deletions, anything touching auth,
     billing, or user data.
3. **Stale triage for in-flight projects:** report ⚠️ (≥7d) and 🔥 (≥21d) with each
   one's exact next action and the one-line resume command. Never write code for a
   mid-build project during upkeep.
4. **Write the report** to `reports/upkeep-YYYY-MM-DD.md`: what was checked, what
   changed, what needs Ryan. Regenerate the ledger. Commit and push everything.

## Unattended mode (scheduled sessions)

- Make only safe changes; queue everything else in the report and, where a decision
  is needed, `touch --add-question "..."` so it surfaces in 🙋 Needs you.
- If the repo can't be reached (no clone path works), send a short setup nudge
  instead of failing silently: what was attempted, and that finishing the GitHub
  setup in the Foundry README restores automation.
- End with the notification summary, exactly this shape:

  > ✅ Upkeep: <projects checked, what changed>.
  > ⚠️ Stale: <project> idle <N>d — next: <its next_action>.
  > ▶️ To resume, open a Foundry session and say: "resume <project>".

## Rules

- Tests after every change; a bump that breaks tests gets reverted and reported, not
  left broken.
- Upkeep sessions follow the full end-of-session ritual like any other.
