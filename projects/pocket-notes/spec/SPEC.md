# Pocket Notes

**Slug:** `pocket-notes` · **Created:** 2026-06-28
**Lock status:** LOCKED: 2026-06-28

> **This is the Foundry's worked example.** It exists so every file format has a living
> reference — a real spec, real state, a real half-built app demonstrating what
> "mid-build, resumable" looks like. Delete it any time:
> `git rm -r projects/pocket-notes && python3 scripts/foundry.py ledger`

## One-liner

A zero-install, single-file notes app that lives in the browser and needs no account.

## Problem & user

Ryan wants somewhere to dump quick notes without opening a heavy app or signing into
anything. Success: double-click one HTML file, write a note, close the tab, and it's
still there tomorrow.

## Goals (ranked)

1. Notes persist across browser sessions with zero setup.
2. Finding an old note takes under five seconds (search + tags).
3. Notes are never trapped: one-click export to JSON, one-click import.

## Non-goals — the cut list

- **No sync / multi-device** — this is a local tool; export/import is the escape hatch.
- **No rich text** — plain text with line breaks; formatting invites scope rot.
- **No auth** — nothing leaves the machine.

## Milestones

### M1 — App skeleton & layout
- **Builds:** single `index.html`, sidebar (note list + New button) and editor pane, styling.
- **Acceptance:** opens in a browser showing both panes; no console errors.

### M2 — Note CRUD with localStorage persistence
- **Builds:** create/edit/delete; autosave on input; storage in `localStorage`.
- **Acceptance:** create, edit, delete notes; reload the page and notes survive.

### M3 — Search & tag filtering
- **Builds:** live search box over title/body; comma-separated tags per note; tag chips filter the list.
- **Acceptance:** typing live-filters; clicking a tag shows only notes carrying it.

### M4 — Export / import JSON backup
- **Builds:** Export button downloading `pocket-notes-backup.json`; Import button restoring it.
- **Acceptance:** export then import in a fresh browser restores notes exactly.

## Tech decisions

- **Stack:** vanilla HTML/CSS/JS, one file, no build step (see D1).
- **Persistence:** `localStorage`, key `pocket-notes.v1` (see D2).
- **Auth:** none.
- **Integrations & keys:** none.
- **Deploy target:** local file — deliverable is the file itself.

## Risks & unknowns

- localStorage is per-browser-profile and clearable; mitigated by M4 export/import.
- Single-file constraint caps growth — fine, the cut list keeps v1 small.

## Maintenance plan

Weekly upkeep: open the file headlessly, run the smoke check (create → reload →
persist), confirm no console errors in current Chromium.

---

## Addenda

Post-lock changes append here as `## Addendum vN (<date>)` with their own goals,
acceptance criteria, and continued milestone numbering. The original spec above is
never edited after locking.
