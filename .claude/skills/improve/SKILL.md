---
name: improve
description: >
  This skill should be used when the user wants to change or extend a Foundry project
  that already has a locked spec — trigger phrases include "improve <project>",
  "add a feature to <project>", "change how <project> works", "feedback on
  <project>", or "v2 of <project>".
metadata:
  version: "1.0.0"
---

# Foundry Improve — feedback → addendum → build

Changes are welcome; silent scope drift is not. Improvements extend the spec with an
addendum so history stays honest, then build like any other milestone.

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

1. **Load context**: the project's spec (including prior addenda), state, recent
   buildlog, decisions.
2. **Understand the feedback.** If ambiguous, clarify with AskUserQuestion — one round,
   concrete options. Distinguish: bug (fix under the existing spec — no addendum
   needed, just a milestone-style fix with a buildlog entry) vs. change/feature
   (addendum) vs. maintenance concern (route to upkeep).
3. **Write the addendum** for changes/features: append to `SPEC.md`:
   `## Addendum vN (<date>)` with goals, acceptance criteria, and new milestones
   whose numbering continues from the last existing milestone. Add the new milestones
   to `STATE.json`. Never edit the original locked sections.
4. **Confirm scope** with Ryan in one message (milestones + acceptance). On approval:
   `python3 scripts/foundry.py touch <slug> --status building --current-milestone <first-new-id> --next-action "Start M<id>: <name> — <first concrete step>"`
5. **Build** the new milestones under the build procedure — same verification
   standard, same per-milestone commits, same ritual. When all addendum milestones
   pass acceptance → back to `shipped` (or `review` if Ryan should look first).
6. **Record the why**: a DECISIONS.md entry when the improvement changed an earlier
   decision.

## Rules

- Original locked spec text is immutable; addenda only.
- An improvement session on a shipped project must leave it deployable — if the
  change breaks the deploy target, fixing that is part of the milestone, not a
  follow-up.
