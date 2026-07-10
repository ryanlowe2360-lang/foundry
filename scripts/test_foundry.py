#!/usr/bin/env python3
"""Tests for the Foundry control-plane CLI.

Run from the repo root:  python3 -m unittest discover -s scripts -v
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import foundry  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)


def make_root(tmp: Path) -> Path:
    """Build a minimal foundry root inside tmp, using the real templates."""
    root = tmp / "foundry"
    (root / "projects").mkdir(parents=True)
    (root / "inbox").mkdir()
    shutil.copytree(REPO_ROOT / "templates", root / "templates")
    return root


def lock_spec(root: Path, slug: str) -> None:
    spec = root / "projects" / slug / "spec" / "SPEC.md"
    text = spec.read_text().replace(
        "**Lock status:** DRAFT — do not build until this line reads `LOCKED: <date>`",
        "**Lock status:** LOCKED: 2026-07-01",
    )
    spec.write_text(text)


class FoundryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_root(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    # ------------------------------------------------------------- new

    def test_new_scaffolds_valid_project(self):
        pdir = foundry.new(self.root, "test-app", "Test App", now=NOW)
        for rel in ("spec/SPEC.md", "spec/INTERVIEW.md", "state/STATE.json",
                    "state/BUILDLOG.md", "state/DECISIONS.md", "src/.gitkeep"):
            self.assertTrue((pdir / rel).exists(), rel)
        st = json.loads((pdir / "state" / "STATE.json").read_text())
        self.assertEqual(st["project"], "test-app")
        self.assertEqual(st["title"], "Test App")
        self.assertEqual(st["status"], "interviewing")
        self.assertEqual(st["last_touched"], "2026-07-10T12:00:00Z")
        self.assertEqual(foundry.validate(self.root), [])
        self.assertTrue((self.root / "LEDGER.md").exists())

    def test_new_rejects_bad_slug(self):
        for bad in ("Has Caps", "spaces here", "-leading", "under_score", ""):
            with self.assertRaises(ValueError, msg=bad):
                foundry.new(self.root, bad, "X", now=NOW)

    def test_new_rejects_duplicate(self):
        foundry.new(self.root, "dupe", "Dupe", now=NOW)
        with self.assertRaises(ValueError):
            foundry.new(self.root, "dupe", "Dupe", now=NOW)

    # ----------------------------------------------------------- touch

    def test_touch_updates_state_and_bumps_timestamp(self):
        foundry.new(self.root, "app", "App", now=NOW)
        later = NOW + timedelta(days=2)
        st = foundry.touch(self.root, "app",
                           next_action="Implement M1 parser in src/parse.py",
                           session_end=True, now=later)
        self.assertEqual(st["last_touched"], "2026-07-12T12:00:00Z")
        self.assertEqual(st["sessions"], 1)
        self.assertIn("parser", st["next_action"])
        on_disk = json.loads(foundry.state_path(self.root, "app").read_text())
        self.assertEqual(on_disk, st)

    def test_touch_milestone_and_status_flow(self):
        foundry.new(self.root, "app", "App", now=NOW)
        path = foundry.state_path(self.root, "app")
        st = json.loads(path.read_text())
        st["milestones"] = [
            {"id": 1, "name": "Skeleton", "status": "todo", "acceptance": "It renders"},
            {"id": 2, "name": "CRUD", "status": "todo", "acceptance": "Round-trips"},
        ]
        st["current_milestone"] = 1
        path.write_text(json.dumps(st, indent=2))
        lock_spec(self.root, "app")

        foundry.touch(self.root, "app", status="building",
                      milestone=1, milestone_status="done",
                      current_milestone=2,
                      next_action="Start M2: CRUD ops in src/store.js", now=NOW)
        st2 = json.loads(path.read_text())
        self.assertEqual(st2["status"], "building")
        self.assertEqual(st2["milestones"][0]["status"], "done")
        self.assertEqual(st2["current_milestone"], 2)
        self.assertEqual(foundry.validate(self.root), [])

    def test_touch_rejects_empty_next_action_and_bad_ids(self):
        foundry.new(self.root, "app", "App", now=NOW)
        with self.assertRaises(ValueError):
            foundry.touch(self.root, "app", next_action="   ", now=NOW)
        with self.assertRaises(ValueError):
            foundry.touch(self.root, "app", milestone=9,
                          milestone_status="done", now=NOW)
        with self.assertRaises(ValueError):
            foundry.touch(self.root, "missing", next_action="x", now=NOW)

    # -------------------------------------------------------- validate

    def test_validate_catches_violations(self):
        foundry.new(self.root, "app", "App", now=NOW)
        path = foundry.state_path(self.root, "app")
        st = json.loads(path.read_text())

        st["next_action"] = ""          # active project, empty next action
        st["status"] = "interviewing"
        path.write_text(json.dumps(st))
        errs = foundry.validate(self.root)
        self.assertTrue(any("next_action" in e for e in errs), errs)

        st["status"] = "not-a-status"   # bad status enum
        path.write_text(json.dumps(st))
        errs = foundry.validate(self.root)
        self.assertTrue(any("unknown status" in e for e in errs), errs)

        st["status"] = "building"       # building without LOCKED spec
        st["next_action"] = "do things"
        path.write_text(json.dumps(st))
        errs = foundry.validate(self.root)
        self.assertTrue(any("LOCKED" in e for e in errs), errs)

        st["project"] = "wrong-slug"    # slug/dir mismatch
        path.write_text(json.dumps(st))
        errs = foundry.validate(self.root)
        self.assertTrue(any("directory name" in e for e in errs), errs)

        path.write_text("{not json")    # corrupt file
        errs = foundry.validate(self.root)
        self.assertTrue(any("invalid JSON" in e for e in errs), errs)

    def test_template_state_json_parses(self):
        json.loads((self.root / "templates" / "STATE.json").read_text())

    # ---------------------------------------------------------- ledger

    def _set_idle(self, slug: str, days: int):
        path = foundry.state_path(self.root, slug)
        st = json.loads(path.read_text())
        st["last_touched"] = foundry.iso(NOW - timedelta(days=days))
        path.write_text(json.dumps(st, indent=2))

    def test_ledger_sections_flags_and_sorting(self):
        foundry.new(self.root, "fresh", "Fresh", now=NOW)
        foundry.new(self.root, "stale", "Stale", now=NOW)
        foundry.new(self.root, "dead", "Dead", now=NOW)
        self._set_idle("stale", 10)
        self._set_idle("dead", 25)
        (self.root / "inbox" / "big-idea.md").write_text("# an idea")

        text = foundry.ledger(self.root, now=NOW)

        self.assertIn("## 🔨 In flight", text)
        self.assertIn("⚠️", text)                      # 10d idle warns
        self.assertIn("🔥", text)                      # 25d idle burns
        self.assertIn("big-idea.md", text)             # loose inbox doc listed
        self.assertLess(text.index("Dead"), text.index("Stale"))   # stalest first
        self.assertLess(text.index("Stale"), text.index("Fresh"))
        self.assertIn("2 stale", text)                 # footer counts
        self.assertEqual((self.root / "LEDGER.md").read_text(), text)

    def test_ledger_needs_you_and_shipped_sections(self):
        foundry.new(self.root, "app", "App", now=NOW)
        lock_spec(self.root, "app")
        foundry.touch(self.root, "app", status="building",
                      add_blocker="Need Ryan's Stripe test key",
                      next_action="Wire checkout once key arrives", now=NOW)
        foundry.new(self.root, "done-app", "Done App", now=NOW)
        lock_spec(self.root, "done-app")
        foundry.touch(self.root, "done-app", status="shipped",
                      deploy_url="https://done.example.com",
                      next_action="—", now=NOW)

        text = foundry.ledger(self.root, now=NOW)
        self.assertIn("## 🙋 Needs you", text)
        self.assertIn("Stripe test key", text)
        self.assertIn("## 🚢 Shipped & maintained", text)
        self.assertIn("https://done.example.com", text)

    def test_ledger_empty_repo_still_generates(self):
        text = foundry.ledger(self.root, now=NOW)
        self.assertIn("Nothing in flight", text)
        self.assertIn("0 project(s)", text)

    # ------------------------------------------------------------- cli

    def test_cli_validate_exit_codes(self):
        rc = foundry.main(["--root", str(self.root), "validate"])
        self.assertEqual(rc, 0)
        foundry.new(self.root, "app", "App", now=NOW)
        path = foundry.state_path(self.root, "app")
        st = json.loads(path.read_text())
        st["status"] = "bogus"
        path.write_text(json.dumps(st))
        rc = foundry.main(["--root", str(self.root), "validate"])
        self.assertEqual(rc, 1)

    def test_cli_new_and_touch_roundtrip(self):
        rc = foundry.main(["--root", str(self.root), "new", "cli-app",
                           "--title", "CLI App"])
        self.assertEqual(rc, 0)
        rc = foundry.main(["--root", str(self.root), "touch", "cli-app",
                           "--next-action", "Draft the spec", "--session-end"])
        self.assertEqual(rc, 0)
        st = json.loads(foundry.state_path(self.root, "cli-app").read_text())
        self.assertEqual(st["sessions"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
