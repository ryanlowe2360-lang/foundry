# Intake Interview — Pocket Notes

**Date:** 2026-06-28 · **Source doc(s):** none — interviewed from a one-line idea
*(example project: this file demonstrates the interview record format)*

## What the planning doc already answered

No doc — the idea arrived as "I want a tiny notes thing that just works." Everything
below came from the interview.

## Questions asked & answers

### Round 1

**Q1 — Persistence:** Where should notes live: browser storage, a local file the app
reads/writes, or a hosted database?
**A:** Browser storage — zero setup beats everything.
**Consequence:** localStorage as primary store; export/import promoted to a must-have
(M4) as the data-loss escape hatch.

**Q2 — Scope:** Rich text, images, or plain text?
**A:** Plain text. "If I want formatting I'll use something else."
**Consequence:** Rich text added to the cut list; keeps the single-file constraint viable.

**Q3 — Findability:** How do you expect to find old notes?
**A:** Search box, and maybe simple tags.
**Consequence:** M3 = live search + comma-separated tags with chip filtering.

**Q4 — Deploy target:** Where does this run?
**A:** Just a file I double-click.
**Consequence:** No hosting, no build step; the deliverable is `index.html` itself.

### Round 2

Not needed — round 1 closed every rubric gap.

## Rationale summary

Pocket Notes is deliberately minimal: one HTML file, browser-local persistence, search
and tags for retrieval, JSON export as insurance. The main trade-off Ryan chose was
zero-setup over durability guarantees, which is why export/import is a locked must-have
rather than a nice-to-have.
