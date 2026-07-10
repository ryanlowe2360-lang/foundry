# Decisions — Pocket Notes

Lightweight decision log. Newest on top. Record anything a future session (or future
Ryan) would otherwise re-litigate.

## D2 (2026-07-01) — localStorage key is versioned: `pocket-notes.v1`

- **Context:** M2 needed a storage key; future schema changes (tags in M3, export
  format in M4) could break old data.
- **Options:** unversioned key; versioned key with migration on bump.
- **Chose:** versioned key. If the note shape ever changes incompatibly, bump to
  `.v2` and write a one-time migration from `.v1`.
- **Revisit if:** never — this is cheap insurance.

## D1 (2026-06-28) — vanilla single-file HTML, no framework, no build step

- **Context:** spec requires "double-click one file and it works" with zero setup.
- **Options:** React + bundler; Preact via CDN; vanilla JS in one file.
- **Chose:** vanilla. A framework adds a build step or CDN dependency for an app with
  one screen and four features; the cut list keeps it small enough that vanilla stays
  readable.
- **Revisit if:** an addendum adds a second screen or real component reuse.
