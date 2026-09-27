"""Logging with secret redaction. Any known secret value appearing in a log record is masked, and the tastytrade
SDK's chatter (it forces its own logger to DEBUG at import time, logging every websocket frame) is held at WARNING."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

NOISY = ("tastytrade", "httpx2", "httpcore2", "anyio", "asyncio")


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


class QuietSdkFilter(logging.Filter):
    """Handler-level guard: drop sub-WARNING records from third-party loggers even if they re-raise their own level."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.levelno < logging.WARNING and record.name.split(".")[0] in NOISY)


def quiet_sdk_loggers() -> None:
    """Call after importing tastytrade (its __init__ does logger.setLevel(DEBUG))."""
    for name in NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)


def setup_logging(level: str = "INFO", secrets: list[str] | None = None, log_dir: Path | None = None,
                  run_id: str | None = None) -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    redact = RedactFilter(secrets or [])
    quiet = QuietSdkFilter()
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.addFilter(quiet)
    sh.addFilter(redact)
    root.addHandler(sh)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / f"daemon-{run_id or 'run'}.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        fh.addFilter(quiet)
        fh.addFilter(redact)
        root.addHandler(fh)
    quiet_sdk_loggers()
    try:  # if the SDK is importable, import it now so its DEBUG reset happens before we set the level again
        import tastytrade  # noqa: F401
        quiet_sdk_loggers()
    except Exception:  # noqa: BLE001
        pass
    return logging.getLogger("saa")
