---
name: build
description: >
  This skill should be used when the user wants to execute a locked Foundry spec —
  trigger phrases include "build <project>", "start building", "keep building",
  "continue the build", "work on <project>", or "next milestone". It builds milestone
  by milestone with verification evidence and durable state.
metadata:
  version: "1.0.0"
---

# Foundry Build — execute the spec, milestone by milestone

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

1. **Load context.** Read `projects/<slug>/spec/SPEC.md`, `state/STATE.json`, the top
   of `state/BUILDLOG.md`, and `state/DECISIONS.md`. If no slug was given, run the
   ledger and ask which in-flight project to build.

2. **Gate on status.** `spec-locked`, `building`, or `review` → proceed.
   `interviewing`/`inbox` → route to intake instead. `shipped`/`maintenance` with new
   feature requests → route to improve.

3. **Announce the plan.** 1–3 sentences: which milestone, what gets implemented, how
   it will be verified. Then set status `building` and go.

4. **Implement the milestone** in `projects/<slug>/src/` (or the graduated repo if
   `STATE.json.repo` is set — clone it alongside). Follow the spec's tech decisions;
   deviations require a DECISIONS.md entry, not silence. Never commit secrets; ask
   for keys at runtime.

5. **Verify — no evidence, no done.** Logic gets tests written and run. Web UIs get
   served and screenshotted (Chromium + Playwright are preinstalled in cloud
   sessions). CLIs/scripts get real invocations with captured output. Check the
   milestone's acceptance criteria line by line.

6. **Close the milestone.**
   - Append a BUILDLOG entry: did / verified (with the evidence) / stopped at.
   - `python3 scripts/foundry.py touch <slug> --milestone <id> --milestone-status done --current-milestone <next-id> --next-action "Start M<next>: <name> — <first concrete step>"`
   - Commit: `git add -A && git commit -m "<slug>: M<id> <name> — done"`.
   - If Ryan asked for more (or said "keep going"), continue to the next milestone.

7. **Finishing the last milestone** → status `review`. Walk every acceptance
   criterion in the spec against the artifact and show Ryan the result (screenshots,
   test output, the running thing). On his approval → status `shipped`, set
   `deploy_url`/`repo` if applicable, next_action "—", and note the maintenance plan
   is now live for weekly upkeep.

8. **End-of-session ritual — always**, including interrupted or mid-milestone stops:
   touch with `--session-end` and a next_action capturing the exact resume point
   (file, function, what's left, how to verify), buildlog entry, validate, commit,
   push with retries.

## Rules

- One milestone at a time; small is fine, unverified is not.
- Mid-milestone stops are normal — losing the resume point is the only failure.
- Graduation: when `src/` needs its own repo (≈20+ files, own CI/deploys), create it,
  move the code, set `--repo` on the state, keep spec/state in the Foundry.
- Deploys (e.g. Vercel) happen only when the spec's deploy target says so.
