#!/usr/bin/env python3
"""Foundry control-plane CLI. Stdlib only; Python 3.9+.

Subcommands:
  ledger      Regenerate LEDGER.md from projects/*/state/STATE.json + inbox/
  new         Scaffold a new project from templates/
  validate    Check every state file and repo invariant; exit 1 on any failure
  touch       Update a project's state safely (always bumps last_touched,
              always regenerates the ledger)

State discipline lives here so no session can get it subtly wrong by hand.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

STATUSES = [
    "inbox", "interviewing", "spec-locked", "building", "review",
    "shipped", "maintenance", "paused", "archived",
]
ACTIVE = {"interviewing", "spec-locked", "building", "review"}
NEEDS_LOCKED_SPEC = {"spec-locked", "building", "review", "shipped", "maintenance"}
MILESTONE_STATUSES = {"todo", "in-progress", "done"}
WARN_DAYS = 7
FIRE_DAYS = 21
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,50}$")
LOCK_RE = re.compile(r"LOCKED:\s*\d{4}-\d{2}-\d{2}")
REQUIRED_KEYS = {
    "schema", "project", "title", "status", "example", "created",
    "last_touched", "repo", "deploy_url", "milestones", "current_milestone",
    "next_action", "blockers", "open_questions", "sessions", "tags",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def default_root() -> Path:
    return Path(__file__).resolve().parents[1]


def state_path(root: Path, slug: str) -> Path:
    return root / "projects" / slug / "state" / "STATE.json"


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"{path}: invalid JSON ({e})") from e


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2) + "\n")


def project_dirs(root: Path) -> list[Path]:
    proj = root / "projects"
    if not proj.is_dir():
        return []
    return sorted(
        p for p in proj.iterdir()
        if p.is_dir() and (p / "state" / "STATE.json").exists()
    )


# ---------------------------------------------------------------- validate

def validate(root: Path) -> list[str]:
    """Return a list of human-readable errors; empty list means healthy."""
    errors: list[str] = []

    for d in project_dirs(root):
        rel = d.relative_to(root)
        try:
            st = load_state(d / "state" / "STATE.json")
        except ValueError as e:
            errors.append(str(e))
            continue

        missing = REQUIRED_KEYS - set(st)
        if missing:
            errors.append(f"{rel}: STATE.json missing keys: {sorted(missing)}")
            continue
        if st["project"] != d.name:
            errors.append(f"{rel}: slug '{st['project']}' != directory name '{d.name}'")
        if not SLUG_RE.match(str(st["project"])):
            errors.append(f"{rel}: slug '{st['project']}' is not kebab-case")
        if st["status"] not in STATUSES:
            errors.append(f"{rel}: unknown status '{st['status']}' (valid: {STATUSES})")
        if not isinstance(st["example"], bool):
            errors.append(f"{rel}: 'example' must be true/false")
        if not isinstance(st["sessions"], int) or st["sessions"] < 0:
            errors.append(f"{rel}: 'sessions' must be a non-negative integer")
        for field in ("blockers", "open_questions", "tags", "milestones"):
            if not isinstance(st[field], list):
                errors.append(f"{rel}: '{field}' must be a list")

        try:
            parse_ts(st["last_touched"])
        except Exception:
            errors.append(f"{rel}: last_touched '{st['last_touched']}' is not ISO-8601")

        ids = []
        for m in st["milestones"] if isinstance(st["milestones"], list) else []:
            if not isinstance(m, dict):
                errors.append(f"{rel}: milestone entries must be objects")
                continue
            if not isinstance(m.get("id"), int):
                errors.append(f"{rel}: milestone missing integer 'id': {m}")
                continue
            ids.append(m["id"])
            if not str(m.get("name", "")).strip():
                errors.append(f"{rel}: milestone {m['id']} has no name")
            if m.get("status") not in MILESTONE_STATUSES:
                errors.append(f"{rel}: milestone {m['id']} status '{m.get('status')}' "
                              f"(valid: {sorted(MILESTONE_STATUSES)})")
            if not str(m.get("acceptance", "")).strip():
                errors.append(f"{rel}: milestone {m['id']} has no acceptance criteria")
        if len(ids) != len(set(ids)):
            errors.append(f"{rel}: duplicate milestone ids")
        cm = st["current_milestone"]
        if cm is not None and cm not in ids:
            errors.append(f"{rel}: current_milestone {cm} is not a milestone id")

        if st["status"] in ACTIVE and not str(st["next_action"]).strip():
            errors.append(f"{rel}: active project with empty next_action "
                          f"(the Prime Directive violation)")

        spec = d / "spec" / "SPEC.md"
        if st["status"] in NEEDS_LOCKED_SPEC:
            if not spec.exists():
                errors.append(f"{rel}: status '{st['status']}' but spec/SPEC.md missing")
            elif not LOCK_RE.search(spec.read_text()):
                errors.append(f"{rel}: status '{st['status']}' but SPEC.md has no "
                              f"'LOCKED: YYYY-MM-DD' line")
        if st["status"] in {"building", "review", "shipped", "maintenance"}:
            if not (d / "state" / "BUILDLOG.md").exists():
                errors.append(f"{rel}: status '{st['status']}' but state/BUILDLOG.md missing")

    tmpl = root / "templates" / "STATE.json"
    if tmpl.exists():
        try:
            json.loads(tmpl.read_text())
        except json.JSONDecodeError as e:
            errors.append(f"templates/STATE.json: invalid JSON ({e})")
    else:
        errors.append("templates/STATE.json missing")

    skills_dir = root / ".claude" / "skills"
    if skills_dir.is_dir():
        for sd in sorted(skills_dir.iterdir()):
            if sd.is_dir() and not (sd / "SKILL.md").exists():
                errors.append(f".claude/skills/{sd.name}: missing SKILL.md")

    if not (root / "inbox").is_dir():
        errors.append("inbox/ directory missing")

    return errors


# ------------------------------------------------------------------ ledger

def _idle_days(st: dict, now: datetime) -> int:
    try:
        return max(0, (now - parse_ts(st["last_touched"])).days)
    except Exception:
        return 0


def _flag(st: dict, now: datetime) -> str:
    if st["status"] not in ACTIVE:
        return ""
    d = _idle_days(st, now)
    if d >= FIRE_DAYS:
        return " 🔥"
    if d >= WARN_DAYS:
        return " ⚠️"
    return ""


def _cell(text: str, limit: int = 110) -> str:
    text = " ".join(str(text).split())
    text = text.replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _name(st: dict) -> str:
    label = f"**{st['title']}** (`{st['project']}`)"
    if st.get("example"):
        label += " *(example)*"
    return label


def _milestone_cell(st: dict) -> str:
    ms = st.get("milestones") or []
    if not ms:
        return "—"
    done = sum(1 for m in ms if m.get("status") == "done")
    return f"{done}/{len(ms)} done"


def ledger(root: Path, now: datetime | None = None) -> str:
    now = now or utcnow()
    states = []
    for d in project_dirs(root):
        try:
            states.append(load_state(d / "state" / "STATE.json"))
        except ValueError:
            continue  # validate reports it; the ledger stays generable

    lines = [
        "# Foundry Ledger",
        "",
        f"*Generated {iso(now)} by `scripts/foundry.py ledger` — do not edit by hand.*",
        "",
    ]

    needs_you = [s for s in states
                 if s["status"] != "archived" and (s["blockers"] or s["open_questions"])]
    if needs_you:
        lines += ["## 🙋 Needs you", ""]
        for s in needs_you:
            for b in s["blockers"]:
                lines.append(f"- {_name(s)} — **blocked:** {b}")
            for q in s["open_questions"]:
                lines.append(f"- {_name(s)} — **question:** {q}")
        lines.append("")

    in_flight = sorted((s for s in states if s["status"] in ACTIVE),
                       key=lambda s: -_idle_days(s, now))
    lines += ["## 🔨 In flight", ""]
    if in_flight:
        lines += ["| Project | Status | Milestones | Idle | Next action |",
                  "|---|---|---|---|---|"]
        for s in in_flight:
            lines.append(
                f"| {_name(s)} | {s['status']}{_flag(s, now)} | {_milestone_cell(s)} "
                f"| {_idle_days(s, now)}d | {_cell(s['next_action'])} |")
    else:
        lines.append('Nothing in flight — say **"intake"** in a Foundry session to start one.')
    lines.append("")

    inbox_dir = root / "inbox"
    loose = [] if not inbox_dir.is_dir() else sorted(
        p.name for p in inbox_dir.iterdir()
        if p.is_file() and not p.name.startswith(".") and p.name != "README.md")
    inbox_projects = [s for s in states if s["status"] == "inbox"]
    if loose or inbox_projects:
        lines += ["## 📥 Inbox", ""]
        for f in loose:
            lines.append(f'- `inbox/{f}` — unprocessed. Say: **"intake {f}"**')
        for s in inbox_projects:
            lines.append(f"- {_name(s)} — awaiting interview. "
                         f'Say: **"intake {s["project"]}"**')
        lines.append("")

    shipped = [s for s in states if s["status"] in {"shipped", "maintenance"}]
    if shipped:
        lines += ["## 🚢 Shipped & maintained", "",
                  "| Project | Status | Idle | Where |",
                  "|---|---|---|---|"]
        for s in shipped:
            where = s.get("deploy_url") or s.get("repo") or f"`projects/{s['project']}/src/`"
            lines.append(f"| {_name(s)} | {s['status']} | {_idle_days(s, now)}d | {_cell(where)} |")
        lines.append("")

    for title, key in (("## ⏸️ Paused", "paused"), ("## 🗄️ Archived", "archived")):
        group = [s for s in states if s["status"] == key]
        if group:
            lines += [title, ""]
            for s in group:
                lines.append(f"- {_name(s)} — next: {_cell(s['next_action'] or '—', 80)}")
            lines.append("")

    active_count = len(in_flight)
    stale = sum(1 for s in in_flight if _idle_days(s, now) >= WARN_DAYS)
    lines += ["---",
              f"*{len(states)} project(s) · {active_count} in flight · "
              f"{stale} stale (≥{WARN_DAYS}d) · {len(loose)} unprocessed inbox doc(s).*",
              ""]

    content = "\n".join(lines)
    (root / "LEDGER.md").write_text(content)
    return content


# --------------------------------------------------------------------- new

def new(root: Path, slug: str, title: str, now: datetime | None = None) -> Path:
    now = now or utcnow()
    if not SLUG_RE.match(slug):
        raise ValueError(f"slug '{slug}' must be kebab-case: [a-z0-9-], starting alphanumeric")
    pdir = root / "projects" / slug
    if pdir.exists():
        raise ValueError(f"project '{slug}' already exists at {pdir}")

    subs = {
        "{{SLUG}}": slug,
        "{{TITLE}}": title,
        "{{DATE}}": now.strftime("%Y-%m-%d"),
        "{{TIMESTAMP}}": iso(now),
    }

    def render(name: str) -> str:
        text = (root / "templates" / name).read_text()
        for k, v in subs.items():
            text = text.replace(k, v)
        return text

    (pdir / "spec" / "original").mkdir(parents=True)
    (pdir / "state").mkdir()
    (pdir / "notes").mkdir()
    (pdir / "src").mkdir()
    (pdir / "spec" / "SPEC.md").write_text(render("SPEC.md"))
    (pdir / "spec" / "INTERVIEW.md").write_text(render("INTERVIEW.md"))
    (pdir / "state" / "STATE.json").write_text(render("STATE.json"))
    (pdir / "state" / "BUILDLOG.md").write_text(render("BUILDLOG.md"))
    (pdir / "state" / "DECISIONS.md").write_text(render("DECISIONS.md"))
    for keep in ("spec/original", "notes", "src"):
        (pdir / keep / ".gitkeep").write_text("")

    ledger(root, now)
    return pdir


# ------------------------------------------------------------------- touch

def touch(root: Path, slug: str, *, next_action: str | None = None,
          status: str | None = None, milestone: int | None = None,
          milestone_status: str | None = None, current_milestone: int | None = None,
          add_blocker: str | None = None, clear_blockers: bool = False,
          add_question: str | None = None, clear_questions: bool = False,
          repo: str | None = None, deploy_url: str | None = None,
          session_end: bool = False, now: datetime | None = None) -> dict:
    now = now or utcnow()
    path = state_path(root, slug)
    if not path.exists():
        raise ValueError(f"no such project '{slug}' (looked at {path})")
    st = load_state(path)

    if status is not None:
        if status not in STATUSES:
            raise ValueError(f"invalid status '{status}' (valid: {STATUSES})")
        st["status"] = status
    if next_action is not None:
        if not next_action.strip():
            raise ValueError("next_action must not be empty")
        st["next_action"] = next_action.strip()
    if milestone is not None:
        if milestone_status not in MILESTONE_STATUSES:
            raise ValueError(f"--milestone requires --milestone-status "
                             f"in {sorted(MILESTONE_STATUSES)}")
        hit = [m for m in st["milestones"] if m.get("id") == milestone]
        if not hit:
            raise ValueError(f"no milestone id {milestone} in '{slug}'")
        hit[0]["status"] = milestone_status
    if current_milestone is not None:
        st["current_milestone"] = current_milestone
    if add_blocker:
        st["blockers"].append(add_blocker)
    if clear_blockers:
        st["blockers"] = []
    if add_question:
        st["open_questions"].append(add_question)
    if clear_questions:
        st["open_questions"] = []
    if repo is not None:
        st["repo"] = repo
    if deploy_url is not None:
        st["deploy_url"] = deploy_url
    if session_end:
        st["sessions"] = int(st.get("sessions", 0)) + 1

    st["last_touched"] = iso(now)
    save_state(path, st)
    ledger(root, now)
    return st


# -------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="foundry", description=__doc__)
    ap.add_argument("--root", type=Path, default=None, help="repo root (default: auto)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ledger", help="regenerate LEDGER.md")
    sub.add_parser("validate", help="validate all state; exit 1 on failure")

    p_new = sub.add_parser("new", help="scaffold a project")
    p_new.add_argument("slug")
    p_new.add_argument("--title", required=True)

    p_t = sub.add_parser("touch", help="update project state (bumps last_touched)")
    p_t.add_argument("slug")
    p_t.add_argument("--next-action")
    p_t.add_argument("--status", choices=STATUSES)
    p_t.add_argument("--milestone", type=int)
    p_t.add_argument("--milestone-status", choices=sorted(MILESTONE_STATUSES))
    p_t.add_argument("--current-milestone", type=int)
    p_t.add_argument("--add-blocker")
    p_t.add_argument("--clear-blockers", action="store_true")
    p_t.add_argument("--add-question")
    p_t.add_argument("--clear-questions", action="store_true")
    p_t.add_argument("--repo")
    p_t.add_argument("--deploy-url")
    p_t.add_argument("--session-end", action="store_true",
                     help="also increment the session counter")

    args = ap.parse_args(argv)
    root = args.root or default_root()

    try:
        if args.cmd == "ledger":
            ledger(root)
            print(f"LEDGER.md regenerated ({root / 'LEDGER.md'})")
        elif args.cmd == "validate":
            errs = validate(root)
            if errs:
                print(f"FAIL — {len(errs)} problem(s):", file=sys.stderr)
                for e in errs:
                    print(f"  ✗ {e}", file=sys.stderr)
                return 1
            print(f"OK — {len(project_dirs(root))} project(s) validated, "
                  f"state discipline holding.")
        elif args.cmd == "new":
            pdir = new(root, args.slug, args.title)
            print(f"Scaffolded {pdir.relative_to(root)} (status: interviewing). "
                  f"Fill spec/SPEC.md via the intake interview, then lock it.")
        elif args.cmd == "touch":
            st = touch(
                root, args.slug, next_action=args.next_action, status=args.status,
                milestone=args.milestone, milestone_status=args.milestone_status,
                current_milestone=args.current_milestone, add_blocker=args.add_blocker,
                clear_blockers=args.clear_blockers, add_question=args.add_question,
                clear_questions=args.clear_questions, repo=args.repo,
                deploy_url=args.deploy_url, session_end=args.session_end,
            )
            print(f"{args.slug}: status={st['status']} "
                  f"sessions={st['sessions']} next={st['next_action']!r}")
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
