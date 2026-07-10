# Build Log — Pocket Notes

Append-only. Newest entry on top. Every session that touches this project adds one.

## 2026-07-01 — session 2

- **Did:** M2 — full note CRUD in `src/index.html`: create (＋ New), edit with
  autosave on input, delete; persistence via `localStorage` under versioned key
  `pocket-notes.v1` (decision D2). List re-sorts by last-updated.
- **Verified:** `notes/verify.py` (Playwright, headless Chromium) — creates two
  notes, reloads the page, asserts both survive, deletes one, reloads, asserts the
  delete persisted, and checks zero console errors. Result: **PASS**. Evidence
  screenshot: `notes/verify-m1-m2.png`. M2 acceptance ("reload and notes survive")
  met.
- **Stopped at:** M2 complete and committed. Next is M3 — add
  `<input id="search">` above the note list (placeholder comment already marks the
  spot) and filter `renderList()` by title/body substring, then tag chips.

## 2026-06-28 — session 1

- **Did:** M1 — single-file skeleton: sidebar (note list + ＋ New) and editor pane
  (title input, body textarea, Delete button), empty state, full styling. Locked the
  spec earlier the same session (see `spec/INTERVIEW.md`).
- **Verified:** opened in headless Chromium — both panes render, empty state shows,
  zero console errors. M1 acceptance met.
- **Stopped at:** M1 complete. Next: M2 CRUD + localStorage.
