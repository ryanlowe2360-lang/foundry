"""The kill switch — a file flag (`state/HALT`) plus the Telegram `/halt` command, both ending in the same place.

Engaged = the file exists. It survives a restart on purpose: a halted daemon stays halted until Ryan clears it
(`/resume` or `./run.sh resume`), and the Supabase mirror of the flag (`saa.settings.halt`) is kept in step by the
executor so the M1 `/halt` path (the telegram-send edge function sets that setting) is honoured on the next poll.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("saa.killswitch")

HALT_FILE = "HALT"


class KillSwitch:
    def __init__(self, state_dir: Path | str):
        self.path = Path(state_dir) / HALT_FILE
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def engaged(self) -> bool:
        return self.path.exists()

    def info(self) -> dict[str, Any] | None:
        if not self.engaged:
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            return {"reason": "halt file present (unreadable)"}

    def engage(self, reason: str, by: str, now: datetime) -> bool:
        """Returns True when this call engaged it (False if it already was)."""
        if self.engaged:
            return False
        self.path.write_text(json.dumps({"reason": reason, "by": by, "at": now.isoformat()}, indent=1), encoding="utf-8")
        log.warning("KILL SWITCH ENGAGED by %s: %s", by, reason)
        return True

    def clear(self, by: str) -> bool:
        if not self.engaged:
            return False
        self.path.unlink(missing_ok=True)
        log.warning("kill switch cleared by %s", by)
        return True


__all__ = ["KillSwitch", "HALT_FILE"]
