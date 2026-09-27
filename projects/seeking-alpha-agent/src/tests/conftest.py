"""pytest setup: make `saa_daemon` importable from src/daemon and share fixtures."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]
DAEMON = SRC / "daemon"
if str(DAEMON) not in sys.path:
    sys.path.insert(0, str(DAEMON))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    """A complete dummy .env (values are placeholders; tests never touch real credentials)."""
    p = tmp_path / ".env"
    p.write_text(
        "# dummy\n"
        "TT_PROD_CLIENT_ID=prod-client-id\n"
        "TT_PROD_CLIENT_SECRET=prod-secret-value-0123456789\n"
        "TT_PROD_REFRESH_TOKEN=prod-refresh-token-abcdefghij\n"
        "TT_SANDBOX_CLIENT_ID=sbx-client-id\n"
        "TT_SANDBOX_CLIENT_SECRET=\"sbx-secret-value-0123456789\"\n"
        "TT_SANDBOX_REFRESH_TOKEN='sbx-refresh-token-abcdefghij'\n"
        "TELEGRAM_BOT_TOKEN=123456:telegram-bot-token-value\n"
        "TELEGRAM_CHAT_ID=987654321\n"
        "FINNHUB_API_KEY=finnhub-key-value\n"
        "SUPABASE_URL=https://example.supabase.co/\n"
        "SUPABASE_SERVICE_ROLE_KEY=service-role-key-value-xyz\n",
        encoding="utf-8",
    )
    return p
