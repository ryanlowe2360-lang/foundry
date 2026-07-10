# {{TITLE}}

**Slug:** `{{SLUG}}` · **Created:** {{DATE}}
**Lock status:** DRAFT — do not build until this line reads `LOCKED: <date>`

## One-liner

<What this is, in one sentence.>

## Problem & user

<Who is this for, what pain does it remove, what does success look like in one sentence?>

## Goals (ranked)

1. <Most important outcome>
2. <Next>

## Non-goals — the cut list

Explicitly out of scope for v1 (prevents scope rot; revisit via Improve addenda only):

- <Cut feature and one-line reason>

## Milestones

Each milestone is completable in roughly one session and has testable acceptance
criteria. Build order is the numbered order unless a decision log entry says otherwise.

### M1 — <name>
- **Builds:** <what gets implemented>
- **Acceptance:** <how we prove it works — the test, the screenshot, the invocation>

### M2 — <name>
- **Builds:**
- **Acceptance:**

## Tech decisions

- **Stack:** <language/framework and why — "Claude picks" resolutions get recorded here>
- **Persistence:** <where data lives>
- **Auth:** <none / method>
- **Integrations & keys:** <external APIs and which secrets Ryan must provide>
- **Deploy target:** <local file / Vercel / server / none>

## Risks & unknowns

- <Hardest part, external dependencies, open technical questions>

## Maintenance plan

<What weekly upkeep should check once this ships: tests to run, deps to watch,
links/APIs that can rot.>

---

## Addenda

Post-lock changes append here as `## Addendum vN (<date>)` with their own goals,
acceptance criteria, and continued milestone numbering. The original spec above is
never edited after locking.
