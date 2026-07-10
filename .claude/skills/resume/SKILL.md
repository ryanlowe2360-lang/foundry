---
name: resume
description: >
  This skill should be used when the user wants to pick up Foundry work without
  re-explaining anything — trigger phrases include "resume", "where was I",
  "continue <project>", "what's next", "pick up where we left off", or when the user
  opens a session and mentions getting back to their projects.
metadata:
  version: "1.0.0"
---

# Foundry Resume — never lose your place

The whole point of the Foundry: Ryan should never have to remember where he left off,
because the repo remembers for him.

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

**With a project named** (e.g. "resume recipe-app"):

1. Read its `STATE.json`, `SPEC.md`, and the newest BUILDLOG entry.
2. Give a 3-line brief — goal (the spec's one-liner) · done so far (milestones
   complete, sessions) · **next action** (verbatim from state).
3. Continue immediately under the operation matching its status: `building`/
   `spec-locked`/`review` → the build procedure; `interviewing` → finish intake;
   `shipped`/`maintenance` → ask whether this is an improvement (→ improve) or a
   health concern (→ upkeep). Do not wait for re-confirmation of things the state
   already records.

**Without a project named:**

1. `python3 scripts/foundry.py validate && python3 scripts/foundry.py ledger`
2. Present: anything in 🙋 Needs you, then the top 3 stalest in-flight projects —
   each as one line: name · idle time · its exact next action.
3. Ask which one via AskUserQuestion (options: the top candidates + "show me
   everything"). Then proceed as above.

## Rules

- Never ask Ryan to re-explain a project. If the state files can't answer a question,
  that is a state bug — fix it by writing better state, then continue.
- A resume session is a working session: it ends with the full end-of-session ritual
  (touch --session-end, buildlog, validate, commit, push).
- If the workspace copy has uncommitted changes from a crashed session, commit them
  first ("recovered work-in-progress: <summary>") before pulling or building.
