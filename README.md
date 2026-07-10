# The Foundry 🏭

Your project factory. You bring planning docs; it interviews you until the goals are
locked, builds milestone by milestone with tests and evidence, remembers exactly where
every project stands, and keeps shipped things healthy — so ideas stop dying in old
chats.

**The core trick is boring on purpose:** chats forget, git doesn't. Every spec,
decision, build step, and "here's exactly what to do next" lives in this repo as a
file. Any Claude session — today, next month, on your phone — can pick up any project
mid-stride by reading it.

## How it works

Every project moves through a lifecycle:

`inbox → interviewing → spec-locked → building → review → shipped → maintenance`

Two rules make it stick. First, **nothing gets built until its spec is locked** — the
intake interview (a deep chat, at most a couple of rounds of pointed questions) closes
every gap first, and the validator physically refuses `building` status without a
`LOCKED:` line. Second, **no session ends without writing state** — status, a
concrete next action, a build-log entry with verification evidence, commit, push. The
`LEDGER.md` dashboard is regenerated from those state files by a tested script, and
anything idle a week gets flagged ⚠️ (three weeks: 🔥) so nothing silently dies.

A worked example, **Pocket Notes**, ships in `projects/pocket-notes/` — a real
half-built app with a locked spec, interview record, decision log, and a build log
whose evidence (a Playwright smoke test + screenshot) actually ran. It's there so
every file format has a living reference, and it's deliberately 8 days stale so you
can see the ⚠️ flag working. Delete it whenever:
`git rm -r projects/pocket-notes && python3 scripts/foundry.py ledger`

## Setup — one-time, ~5 minutes

**1. Put this repo on GitHub** (it was built in a cloud session; it needs a permanent
home). Pick whichever is least annoying:

- **No terminal needed:** connect the GitHub connector to Claude (Settings →
  Connectors), then in a session with this folder say: *"create a private GitHub repo
  named `foundry` and push this folder to it."*
- **On your computer:** unzip, then
  `cd foundry && gh repo create foundry --private --source=. --push`
  (or create an empty repo on github.com and `git remote add origin … && git push -u
  origin main`).

**2. Pin the address.** Edit `KICKOFF.md` and replace the placeholder with your real
repo URL. For private repos, mint a fine-grained personal access token scoped to just
this repo (Contents: read/write) — cloning then works from any session via
`https://x-access-token:<TOKEN>@github.com/YOU/foundry.git`.

**3. Install the plugin.** The `foundry.plugin` file you received alongside this repo
adds the six skills to every Cowork session, so "resume" or "intake this" just works
without pasting anything. (Without the plugin, `KICKOFF.md` does the same job by
hand; in Claude Code the skills also live natively in `.claude/skills/`.)

**4. Re-point the automation.** Two scheduled tasks exist, with push + email
summaries:

- **Weekly upkeep & nudge** (Mondays ~9am ET) — live now. Safe maintenance on
  shipped projects, staleness nudges with exact next actions. Until it can reach
  your repo it only sends setup reminders.
- **Daily auto-build** (~6am ET) — created **disabled** so it can't nag you before
  setup. Once enabled, each morning it picks your stalest unblocked in-flight
  project and builds **one verified milestone** unattended: tests run, UIs
  screenshotted, work committed. When it genuinely needs you (a key, a taste call),
  it queues a question on the ledger instead of guessing, and anything finishing its
  last milestone lands in `review` for your approval — the robot never ships without
  you.

Once the repo is pushed, tell any Claude session: *"rewire my Foundry automation
with repo URL `<your URL>`"* — it recreates both schedules from
`automation/TRIGGERS.md` with the address baked in and enables auto-build.

> For Claude, when rewiring: follow the protocol at the top of
> `automation/TRIGGERS.md`. The live tasks are named "Foundry weekly upkeep & nudge"
> and "Foundry daily auto-build" (`list_triggers` finds them by name).

## Daily use

Start any session (plugin installed, or paste `KICKOFF.md`) and say what you want:

| You say | What happens |
|---|---|
| **"intake this"** (+ attach your planning doc) | Deep-read → gap interview → locked spec with milestones → ready to build |
| **"build \<project\>"** / "keep going" | Executes the next milestone(s) with tests/screenshots as evidence, commits each one |
| **"resume"** / "where was I?" | 3-line brief per project and continues exactly at the recorded next action |
| **"status"** | The dashboard: what's in flight, what's stale, what needs you, one recommended action |
| **"improve \<project\>"** | Your feedback becomes a spec addendum with new milestones — then gets built |
| **"upkeep"** | Dependency bumps, test runs, health checks on shipped projects; stale-project triage |

Your existing workflow doesn't change: keep ideating with Claude anywhere, have it
produce the planning doc like always — then drop that doc into a Foundry session with
"intake this". The doc is preserved verbatim in `projects/<slug>/spec/original/`.

## Where things live

```
inbox/                  unprocessed planning docs (listed in the ledger until intaken)
projects/<slug>/spec/   SPEC.md (locked), INTERVIEW.md, original/ (your docs, verbatim)
projects/<slug>/state/  STATE.json, BUILDLOG.md, DECISIONS.md — the memory
projects/<slug>/src/    the build itself
LEDGER.md               the dashboard (generated — never hand-edited)
CLAUDE.md               the operating manual every Claude session follows
scripts/foundry.py      ledger | new | validate | touch  (stdlib-only, tested)
reports/                weekly upkeep reports
```

Small projects live their whole lives in `src/`. When one outgrows the nest (~20+
files, needs its own deploys), it **graduates** to its own repo and the Foundry keeps
the spec + state as the control plane.

## FAQ

**What if a session dies mid-build?** The ritual commits per milestone and records a
mid-milestone resume point, so the blast radius is minutes, not projects. The next
"resume" picks up from the last committed state.

**Multiple devices?** It's git — any session anywhere clones the same truth. Sessions
even recover uncommitted work-in-progress they find in a workspace before continuing.

**Do I have to babysit the upkeep runs?** No. Scheduled runs only make safe changes
(patch/minor bumps with green tests, security patches). Anything bigger lands in
🙋 Needs you on the ledger and in your weekly summary.

**Tests?** `python3 -m unittest discover -s scripts -v` — 13 tests cover the
scaffolder, state updates, validation rules, and ledger generation.
