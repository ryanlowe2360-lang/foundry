# Scheduled Task Prompts — canonical copies

These are the canonical prompts for the Foundry's two scheduled tasks. They live in
the repo so any Claude session can recreate the schedules deterministically.

## Rewire protocol (for Claude)

When Ryan says **"rewire my Foundry automation with repo URL `<url>`"**:

1. Take each prompt below and replace `<REPO_URL>` with the URL he gave (including
   any access token he embedded).
2. Create the replacements with `create_trigger` — same names, same cron expressions,
   notifications `{push: true, email: true}` — **auto-build enabled** this time.
3. Find the old tasks with `list_triggers` (by name) and delete them.
4. Confirm to Ryan: both schedules live, addresses baked in.

Current live tasks (created 2026-07-10, discovery-based prompts because no URL
existed yet): "Foundry weekly upkeep & nudge" (enabled) and "Foundry daily
auto-build" (created **disabled** so it doesn't nag before setup — rewiring enables
it).

---

## Task 1 — "Foundry weekly upkeep & nudge" · cron `0 13 * * 1` (Mon ~9am ET)

> You are running Ryan's Foundry weekly upkeep session, unattended. The Foundry is
> Ryan's project-factory git repo. Clone it: `git clone <REPO_URL> foundry` (fall
> back to the FOUNDRY_REPO env var or a connected GitHub tool if that fails).
>
> If the repo is unreachable by every path: send Ryan a short nudge (3 sentences
> max): the weekly run couldn't reach the repo; check that the URL/token in the
> rewire is still valid (see the Foundry README, Setup). Then stop.
>
> Once cloned: read CLAUDE.md at its root and follow it exactly, running the Upkeep
> operation in unattended mode — validate state first; only SAFE maintenance on
> shipped/maintenance projects (patch/minor bumps that keep tests green, security
> patches, lockfile refresh, re-run suites); never write code for mid-build
> projects; never do ask-first operations (major bumps, migrations, deletions,
> auth/billing/data) — queue those with `scripts/foundry.py touch --add-question`;
> write `reports/upkeep-YYYY-MM-DD.md`; regenerate the ledger; commit and push with
> retries.
>
> End your final message with the three-line summary CLAUDE.md specifies:
> ✅ what upkeep did · ⚠️ each stale in-flight project with idle days and its exact
> recorded next action · ▶️ "open a Foundry session and say: resume <project>".

## Task 2 — "Foundry daily auto-build" · cron `0 10 * * *` (daily ~6am ET)

> You are running Ryan's Foundry unattended build session. The Foundry is Ryan's
> project-factory git repo. Clone it: `git clone <REPO_URL> foundry` (fall back to
> the FOUNDRY_REPO env var or a connected GitHub tool if that fails). If the repo is
> unreachable by every path, end with one quiet sentence saying so — no long nudge,
> the weekly task handles setup reminders.
>
> Once cloned: read CLAUDE.md at its root and follow the **Unattended build mode**
> section to the letter. In short: pick the stalest in-flight project with no
> blockers and no open questions; build exactly one milestone to the full
> verification standard (tests run, UIs screenshotted, evidence in the build log);
> if Ryan's input is genuinely needed, `touch --add-question` and move to the next
> qualifying project instead of guessing; a run that finishes a project's last
> milestone sets status `review` — never `shipped`; full end-of-session ritual
> (state, build log, ledger, validate, commit, push with retries).
>
> If no project qualifies, build nothing and say what's waiting on Ryan.
>
> End your final message with the ✅/⚠️/▶️ summary — it becomes Ryan's morning
> notification: what got built overnight, what's stale, what needs him.
