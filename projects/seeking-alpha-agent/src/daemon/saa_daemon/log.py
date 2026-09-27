"""Logging with secret redaction. Any known secret value appearing in a log record is masked."""
from __future__ import annotations

import logging
import sys
from pathlib import Path


class RedactFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def _scrub(self, text: str) -> str:
        for s in self._secrets:
            if s in text:
                text = text.replace(s, "<redacted>")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - malformed format args
            msg = str(record.msg)
        record.msg = self._scrub(msg)
        record.args = ()
        return True


def setup_logging(level: str = "INFO", secrets: list[str] | None = None, log_dir: Path | None = None,
                  run_id: str | None = None) -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    redact = RedactFilter(secrets or [])
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.addFilter(redact)
    root.addHandler(sh)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / f"daemon-{run_id or 'run'}.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        fh.addFilter(redact)
        root.addHandler(fh)
    # Third-party chatter: the SDK logs every websocket frame at DEBUG; keep it quiet.
    for noisy in ("tastytrade", "httpx2", "httpcore2", "anyio", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger("saa")
