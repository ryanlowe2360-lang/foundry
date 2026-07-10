---
name: intake
description: >
  This skill should be used when the user wants to turn a project idea or planning
  document into a locked, buildable spec in their Foundry — trigger phrases include
  "intake this", "new project", "add this to the foundry", "process my planning doc",
  or when the user attaches a project plan and asks to get it ready to build.
metadata:
  version: "1.0.0"
---

# Foundry Intake — planning doc → locked spec

Turn one of Ryan's planning documents into a spec so complete that any future session
can build from it without asking him anything.

## Locate the Foundry (shared bootstrap)

1. If `./foundry/` exists in the workspace (or the cwd contains `CLAUDE.md` +
   `scripts/foundry.py`), use it. `git pull origin main` first if a remote exists.
2. Else if the `FOUNDRY_REPO` environment variable is set, `git clone "$FOUNDRY_REPO" foundry`.
3. Else if a GitHub tool/connector is available (ToolSearch "github"), find the repo
   named `foundry` on the user's account and clone or read/write it through the tools.
4. Else ask the user for the repo URL (https; may include an access token for private
   repos), then clone it.

Then read `CLAUDE.md` at the repo root and follow it — its state-discipline rules are
mandatory, including the end-of-session ritual.

## Procedure

1. **Find the source material.** In priority order: a document attached to this
   conversation (copy it into `inbox/` first), a file the user named, or the contents
   of `inbox/` (list them and ask which). If there is no doc at all, run the interview
   from scratch — the doc is an accelerant, not a requirement.

2. **Deep-read and extract.** Ryan's planning docs often already contain prompts,
   architecture notes, setup steps, or a project-level CLAUDE.md. Treat these as
   authoritative. Build a summary of every rubric item the doc already answers —
   those questions are never re-asked.

3. **Gap analysis against the rubric.** The rubric (from the repo's CLAUDE.md):
   purpose & user · v1 scope vs. cut list · testable acceptance per must-have ·
   tech stack, persistence, auth, integrations/keys, deploy target · risks & the
   hardest part · what maintenance means post-ship.

4. **Interview.** Use AskUserQuestion: at most 2 rounds of at most 4 questions, only
   on genuine gaps. Prefer concrete options over open questions ("SQLite file vs.
   Supabase vs. in-memory" beats "how should data work?"). This is the deep chat that
   makes sure goals are understood — take it seriously, but do not pad it.

5. **Draft the spec.** Scaffold first:
   `python3 scripts/foundry.py new <slug> --title "<Title>"`
   then fill `projects/<slug>/spec/SPEC.md` from the template. Milestones sized to
   roughly one session each, every one with testable acceptance criteria. Write
   `spec/INTERVIEW.md` (what the doc answered, Q&A, rationale summary). Move the
   original doc(s) into `spec/original/`.

6. **Review and lock.** Show Ryan the spec (milestone list + cut list at minimum).
   Iterate until he approves. On approval, change the lock line to
   `**Lock status:** LOCKED: <YYYY-MM-DD>`, add the milestones to STATE.json
   (`milestones` array: `{"id", "name", "status": "todo", "acceptance"}`,
   `current_milestone: 1`), then:
   `python3 scripts/foundry.py touch <slug> --status spec-locked --next-action "Start milestone 1: <name> — <first concrete step>" --session-end`

7. **Offer to build now.** If Ryan says yes, hand off to the build procedure in the
   same session. Either way, finish with the end-of-session ritual: validate, commit,
   push.

## Rules

- No building before LOCKED. `spec-locked` status requires the lock line — the
  validator enforces it.
- Never discard Ryan's original doc; it lives in `spec/original/` verbatim.
- If the idea is really several projects, say so and split it into multiple intakes
  rather than one bloated spec.
