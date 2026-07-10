# The Foundry — Operating Manual for Claude

This repository is Ryan's **project factory**. It turns planning documents into shipped,
maintained software. Every Claude session that touches this repo — interactive or
scheduled — follows this manual. It is self-sufficient: even without the Foundry plugin
installed, this file tells you everything you need to run the pipeline.

Ryan's standing preference: **ship the complete thing.** Do it right, with tests, with
documentation. Never leave a dangling thread, never present a workaround when the real
fix exists.

## Why this exists (read once, act always)

Ryan plans projects in chats. Chats forget. This repo is the memory: specs, build state,
decisions, and logs all live here as files under git. The single most important rule:

## The Prime Directive: state discipline

**Never end a working session without completing the end-of-session ritual.** A session
whose work isn't captured in state files may as well not have happened.

The ritual, in order:

1. **Update state** — `python3 scripts/foundry.py touch <slug> --next-action "..." [--status ...] [--session-end]`
   The `next_action` must be one concrete sentence a total stranger could execute:
   file, function, what's left, how to verify. "Keep working on search" is a violation;
   "Implement the tag-filter dropdown in `src/index.html` `render()`; acceptance: clicking
   a tag shows only matching notes" is correct.
2. **Append to the build log** — add an entry to `projects/<slug>/state/BUILDLOG.md`:
   date, what was done, what was verified (with evidence), where you stopped and why.
3. **Record decisions** — anything with a rationale worth remembering goes in
   `projects/<slug>/state/DECISIONS.md`.
4. **Validate** — `python3 scripts/foundry.py validate` must pass.
5. **Commit and push** — `git add -A && git commit -m "<slug>: <what happened>"`, then
   `git push -u origin main` (retry up to 4 times with backoff: 2s/4s/8s/16s). If no
   remote is configured yet, still commit, and remind Ryan once per session to finish
   the GitHub setup in `README.md` § Setup.

`touch` also regenerates `LEDGER.md` automatically. Never edit `LEDGER.md` by hand.

## Session start ritual

1. If a remote exists: `git pull origin main` first.
2. Read `LEDGER.md`. Open with a 3-line brief: what's in flight, what's stalest, what
   needs Ryan. Then do what he asked — or if he just said "resume", follow § Resume.
3. Never ask Ryan to re-explain a project. Everything needed is in `spec/` and `state/`.
   If it isn't, that's a state bug: fix it by writing a better `next_action`, not by
   making Ryan repeat himself.

## Repo map

```
CLAUDE.md            ← you are here
README.md            ← Ryan's manual (setup, usage, automation)
KICKOFF.md           ← prompt Ryan pastes into any new session
LEDGER.md            ← generated dashboard: every project, status, idle time, next action
inbox/               ← raw planning docs land here, unprocessed
projects/<slug>/
  spec/SPEC.md       ← the locked build spec (milestones + acceptance criteria)
  spec/INTERVIEW.md  ← the intake Q&A and rationale
  spec/original/     ← Ryan's original planning docs, preserved verbatim
  state/STATE.json   ← machine state: status, milestones, next_action (source of truth)
  state/BUILDLOG.md  ← append-only session log with verification evidence
  state/DECISIONS.md ← decision log (lightweight ADRs)
  notes/             ← scratch
  src/               ← the build itself (until it graduates to its own repo)
templates/           ← blank versions of every file above
scripts/foundry.py   ← control-plane CLI: ledger | new | validate | touch
scripts/test_foundry.py
reports/             ← upkeep run reports
```

## Lifecycle

`inbox → interviewing → spec-locked → building → review → shipped → maintenance`
(plus `paused` and `archived` at any point).

Transitions are earned, not assumed: **no building until the spec is LOCKED**, no
`shipped` until every milestone's acceptance criteria have logged verification evidence.

## The six operations

### 1. Intake (`intake`) — planning doc → locked spec
1. Source: a doc Ryan attached (copy it into `inbox/` first), named in his message, or
   picked from `inbox/`.
2. Deep-read it. Extract everything it already answers. Ryan's docs often include
   prompts, setup instructions, or a project CLAUDE.md — treat those as authoritative
   input and preserve them in `spec/original/`.
3. Gap analysis against the interview rubric (below). Then interview Ryan with
   AskUserQuestion — **at most 2 rounds of at most 4 questions**, covering only what the
   doc doesn't answer. This is the "deep chat to make sure goals are understood."
4. Draft `SPEC.md` from the template. Milestones must each be completable in roughly one
   session and each must have testable acceptance criteria.
5. Review with Ryan. On approval: scaffold with
   `python3 scripts/foundry.py new <slug> --title "..."`, fill in spec + interview, move
   the source doc to `spec/original/`, set the lock line (`LOCKED: <date>`), set status
   `spec-locked`, `next_action` = "Start milestone 1: …". Offer to start building now.

**Interview rubric** — every locked spec answers all of these:
- **Purpose & user**: who is it for, what pain, success in one sentence.
- **Scope**: v1 must-haves vs. explicitly-cut non-goals (the cut list prevents scope rot).
- **Acceptance**: a testable criterion per must-have.
- **Tech**: stack (or "Claude picks" — then pick and record why), data/persistence, auth,
  integrations + keys needed, deploy target (local file / Vercel / server / none).
- **Risks**: the hardest part, external dependencies, unknowns.
- **Maintenance**: what upkeep means for this project once shipped.

### 2. Build (`build <slug>`) — execute the spec, milestone by milestone
1. Read `SPEC.md` + `STATE.json`. If status isn't `spec-locked`/`building`/`review`,
   route to the right operation instead (usually intake).
2. Announce the plan for the next milestone in 1–3 sentences, then implement.
3. **Verify before marking done** — every milestone needs evidence in the build log:
   run the tests you wrote; for web UIs, serve it and screenshot with Playwright
   (Chromium is preinstalled); for CLIs/scripts, run them on real examples and capture
   output. No evidence, no done.
4. Commit per milestone. Continue to the next milestone if Ryan asked for more or said
   "keep going"; otherwise stop cleanly.
5. Ritual — always, including mid-milestone stops. A mid-milestone `next_action`
   captures the exact resume point.

**Graduation rule**: when `src/` outgrows the nest (≈20+ files, needs its own CI or
deploys), create a dedicated repo named `<slug>`, move `src/` there, set `repo` in
STATE.json, and keep spec/state here — the Foundry remains the control plane.

**Secrets**: never commit keys or tokens. `.env` is gitignored; ask Ryan for keys at
runtime or have him configure environment secrets.

### 3. Resume (`resume [slug]`) — never lose your place
- With a slug: give a 3-line brief (goal · done so far · next action), then continue
  under the operation matching its status.
- Without: regenerate the ledger, show what needs Ryan plus the top 3 stalest in-flight
  projects with their next actions, and ask which one (AskUserQuestion).

### 4. Status (`status`) — the dashboard
Run `validate` then `ledger`; present the dashboard with staleness callouts and one
recommended action. Commit the regenerated ledger.

### 5. Improve (`improve <slug>`) — feedback → addendum → build
Never silently rewrite a LOCKED spec. Clarify the feedback if ambiguous, then append an
`## Addendum vN (<date>)` section to `SPEC.md` with its own goals, acceptance criteria,
and new milestones (numbering continues). Status returns to `building`. Then build them
exactly like any milestone. History stays honest.

### 6. Upkeep (`upkeep`) — maintenance without babysitting
For every `shipped`/`maintenance` project (and `building` projects idle 21+ days):
pull, install, run tests, audit dependencies (`npm audit` / `pip list --outdated`),
apply **safe** updates, re-run tests, log everything to the project's build log, and
write a report to `reports/upkeep-YYYY-MM-DD.md`.

- **Safe (just do it)**: patch/minor dependency bumps with green tests afterward,
  security patches, lockfile refresh, re-running test suites, regenerating the ledger.
- **Ask-first (report, don't touch)**: major version bumps, framework or storage
  migrations, feature changes, deleting anything, anything touching auth, billing,
  or user data.

For in-flight projects, upkeep only *reports* staleness — it never writes code for a
project mid-build without Ryan asking.

**Scheduled runs** (no Ryan present): do safe upkeep, then end with a summary in this
shape — it becomes his notification:

> ✅ Upkeep: pocket-notes deps bumped, tests green.
> ⚠️ Stale: recipe-app idle 12d — next: wire the auth callback in src/auth.ts.
> ▶️ To resume, open a Foundry session and say: "resume recipe-app".

## Staleness thresholds

Idle ≥ 7 days while in flight → ⚠️ flagged in the ledger. Idle ≥ 21 days → 🔥, and the
weekly summary must lead with it, including its exact next action and the one-line
resume command. Projects Ryan declares dead get `archived` with a one-line epitaph in
the build log — an honest graveyard beats a guilty backlog.

## Verification standard

"Holy shit, that's done" — not "should work". Tests written and run for logic;
screenshots for UIs; real invocations for tools; acceptance criteria checked line by
line before anything is called shipped. Log the evidence. If `validate` fails, fixing
it outranks everything else.
