"""Settings: `.env` loading, secret redaction, validation.

Secrets are wrapped in :class:`Secret` so they can never leak through ``repr``/``str``/logging.
The only way to read one is ``.value``. Nothing in this module prints a value.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_INDEX_SYMBOLS = ("SPY", "QQQ", "IWM")
KNOWN_INDEX_SYMBOLS = {"SPX", "XSP", "NDX", "RUT", "VIX", "DJX", "OEX"}  # Cboe cash indices (REST type 'index')

# Keys the daemon knows about (names only — documented in README / SETUP.md §4).
TT_KEYS = {
    "prod": ("TT_PROD_CLIENT_ID", "TT_PROD_CLIENT_SECRET", "TT_PROD_REFRESH_TOKEN"),
    "sandbox": ("TT_SANDBOX_CLIENT_ID", "TT_SANDBOX_CLIENT_SECRET", "TT_SANDBOX_REFRESH_TOKEN"),
}
SECRET_KEYS = (
    "TT_PROD_CLIENT_SECRET", "TT_PROD_REFRESH_TOKEN", "TT_SANDBOX_CLIENT_SECRET", "TT_SANDBOX_REFRESH_TOKEN",
    "TELEGRAM_BOT_TOKEN", "FINNHUB_API_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SECRET_KEY", "ANTHROPIC_API_KEY",
)


class Secret:
    """A string that refuses to be printed."""

    __slots__ = ("_v",)

    def __init__(self, value: str):
        self._v = value

    @property
    def value(self) -> str:
        return self._v

    def __bool__(self) -> bool:
        return bool(self._v)

    def __len__(self) -> int:
        return len(self._v)

    def __repr__(self) -> str:
        return f"<secret len={len(self._v)}>"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:  # never compare by value in logs; identity semantics are enough
        return self is other

    __hash__ = object.__hash__


class ConfigError(Exception):
    """Raised for a missing or invalid setting. Messages name keys, never values."""


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines (comments, blanks, optional quotes, `export ` prefix)."""
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        if s.startswith("export "):
            s = s[7:].strip()
        k, v = s.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k] = v
    return out


def find_env_file(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """Locate `.env`: explicit arg → $SAA_ENV_FILE → walk up from this package (max 5 levels) → cwd."""
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    env = os.environ.get("SAA_ENV_FILE")
    if env:
        p = Path(env).expanduser()
        return p if p.is_file() else None
    here = Path(__file__).resolve().parent
    for _ in range(6):
        cand = here / ".env"
        if cand.is_file():
            return cand
        if here.parent == here:
            break
        here = here.parent
    cand = Path.cwd() / ".env"
    return cand if cand.is_file() else None


def _bool(v: str | None, default: bool) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _float(v: str | None, default: float) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except ValueError as e:
        raise ConfigError(f"expected a number: {v!r}") from e


def _int(v: str | None, default: int) -> int:
    try:
        return int(v) if v not in (None, "") else default
    except ValueError as e:
        raise ConfigError(f"expected an integer: {v!r}") from e


def _csv(v: str | None) -> tuple[str, ...]:
    if not v:
        return ()
    return tuple(s.strip().upper() for s in v.split(",") if s.strip())


@dataclass
class Settings:
    env_file: Path | None
    # tastytrade — data (market data) and broker (account) environments
    data_env: str = "prod"          # prod | sandbox   (sandbox has no DXLink data; prod is the default per D11)
    broker_env: str = "sandbox"     # sandbox only until M5
    tt_prod_client_id: str = ""
    tt_prod_client_secret: Secret = field(default_factory=lambda: Secret(""))
    tt_prod_refresh_token: Secret = field(default_factory=lambda: Secret(""))
    tt_sandbox_client_id: str = ""
    tt_sandbox_client_secret: Secret = field(default_factory=lambda: Secret(""))
    tt_sandbox_refresh_token: Secret = field(default_factory=lambda: Secret(""))
    # Supabase mirror (optional but required for the M2 acceptance run)
    supabase_url: str = ""
    supabase_key: Secret = field(default_factory=lambda: Secret(""))
    # Telegram fallback (outbox is the primary path)
    telegram_bot_token: Secret = field(default_factory=lambda: Secret(""))
    telegram_chat_id: str = ""
    # behaviour
    state_dir: Path = Path("state")
    log_level: str = "INFO"
    index_symbols: tuple[str, ...] = DEFAULT_INDEX_SYMBOLS
    index_symbols_explicit: bool = False   # True when SAA_INDEX_SYMBOLS was set → overrides saa.settings.index_symbols
    extra_symbols: tuple[str, ...] = ()
    max_single_names: int = 25
    strike_window_pct_index: float = 3.0
    strike_window_pct_single: float = 8.0
    max_strikes_per_side: int = 20
    expirations_index: int = 2
    expirations_single: int = 1
    snapshot_minutes: int = 5
    record_events: bool = True
    mirror_enabled: bool = True
    mirror_ticks: bool = False       # also feed saa.price_ticks (M1 scorer) — off by default, M1 pipeline untouched
    halts_poll_seconds: int = 60
    vix_poll_minutes: int = 5

    # ----- derived -----
    @property
    def mirror_configured(self) -> bool:
        return bool(self.supabase_url) and bool(self.supabase_key)

    @property
    def telegram_fallback_configured(self) -> bool:
        return bool(self.telegram_bot_token) and bool(self.telegram_chat_id)

    def tt_credentials(self, env: str) -> tuple[str, Secret, Secret]:
        if env == "prod":
            return self.tt_prod_client_id, self.tt_prod_client_secret, self.tt_prod_refresh_token
        if env == "sandbox":
            return self.tt_sandbox_client_id, self.tt_sandbox_client_secret, self.tt_sandbox_refresh_token
        raise ConfigError(f"unknown tastytrade environment {env!r}")

    def missing_tt_keys(self, env: str) -> list[str]:
        names = TT_KEYS[env]
        cid, sec, tok = self.tt_credentials(env)
        return [n for n, v in zip(names, (cid, sec, tok)) if not v]

    def is_index_symbol(self, symbol: str) -> bool:
        return symbol.upper() in self.index_symbols

    def rest_instrument_kind(self, symbol: str) -> str:
        """'index' for Cboe cash indices, else 'equity' (ETFs like SPY are equities)."""
        return "index" if symbol.upper() in KNOWN_INDEX_SYMBOLS or symbol.startswith("$") else "equity"

    def redacted(self) -> dict[str, object]:
        """Everything a log line may show. Secrets appear as <secret len=N>."""
        out: dict[str, object] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            out[f.name] = repr(v) if isinstance(v, Secret) else (str(v) if isinstance(v, Path) else v)
        return out

    def secret_values(self) -> list[str]:
        """Raw secret values, for the log redaction filter only."""
        vals = [self.tt_prod_client_secret, self.tt_prod_refresh_token, self.tt_sandbox_client_secret,
                self.tt_sandbox_refresh_token, self.supabase_key, self.telegram_bot_token]
        return [s.value for s in vals if s and len(s) >= 8]


def load_settings(env_file: str | os.PathLike[str] | None = None, *, environ: dict[str, str] | None = None,
                  state_dir: str | os.PathLike[str] | None = None) -> Settings:
    """Build Settings from `.env` (file values) overlaid by process environment variables."""
    path = find_env_file(env_file)
    values: dict[str, str] = parse_env_file(path) if path else {}
    env = os.environ if environ is None else environ
    for k, v in env.items():
        if k.startswith(("TT_", "SUPABASE_", "TELEGRAM_", "SAA_", "FINNHUB_")):
            values[k] = v

    def g(k: str, default: str = "") -> str:
        return values.get(k, default)

    data_env = g("SAA_DATA_ENV", "prod").strip().lower() or "prod"
    broker_env = g("SAA_BROKER_ENV", "sandbox").strip().lower() or "sandbox"
    if data_env not in ("prod", "sandbox"):
        raise ConfigError("SAA_DATA_ENV must be 'prod' or 'sandbox'")
    if broker_env != "sandbox":
        raise ConfigError("SAA_BROKER_ENV must be 'sandbox' — M2–M4 are sandbox-only; production brokerage is M5")

    sd = Path(state_dir or g("SAA_STATE_DIR") or (Path(__file__).resolve().parent.parent / "state")).expanduser()
    explicit_idx = _csv(g("SAA_INDEX_SYMBOLS"))
    index_symbols = explicit_idx or DEFAULT_INDEX_SYMBOLS
    supabase_key = g("SUPABASE_SERVICE_ROLE_KEY") or g("SUPABASE_SECRET_KEY")

    return Settings(
        env_file=path,
        data_env=data_env,
        broker_env=broker_env,
        tt_prod_client_id=g("TT_PROD_CLIENT_ID"),
        tt_prod_client_secret=Secret(g("TT_PROD_CLIENT_SECRET")),
        tt_prod_refresh_token=Secret(g("TT_PROD_REFRESH_TOKEN")),
        tt_sandbox_client_id=g("TT_SANDBOX_CLIENT_ID"),
        tt_sandbox_client_secret=Secret(g("TT_SANDBOX_CLIENT_SECRET")),
        tt_sandbox_refresh_token=Secret(g("TT_SANDBOX_REFRESH_TOKEN")),
        supabase_url=g("SUPABASE_URL").rstrip("/"),
        supabase_key=Secret(supabase_key),
        telegram_bot_token=Secret(g("TELEGRAM_BOT_TOKEN")),
        telegram_chat_id=g("TELEGRAM_CHAT_ID"),
        state_dir=sd,
        log_level=g("SAA_LOG_LEVEL", "INFO").upper() or "INFO",
        index_symbols=index_symbols,
        index_symbols_explicit=bool(explicit_idx),
        extra_symbols=_csv(g("SAA_EXTRA_SYMBOLS")),
        max_single_names=_int(g("SAA_MAX_SINGLE_NAMES"), 25),
        strike_window_pct_index=_float(g("SAA_STRIKE_WINDOW_PCT_INDEX"), 3.0),
        strike_window_pct_single=_float(g("SAA_STRIKE_WINDOW_PCT_SINGLE"), 8.0),
        max_strikes_per_side=_int(g("SAA_MAX_STRIKES_PER_SIDE"), 20),
        expirations_index=_int(g("SAA_EXPIRATIONS_INDEX"), 2),
        expirations_single=_int(g("SAA_EXPIRATIONS_SINGLE"), 1),
        snapshot_minutes=_int(g("SAA_SNAPSHOT_MINUTES"), 5),
        record_events=_bool(g("SAA_RECORD_EVENTS"), True),
        mirror_enabled=_bool(g("SAA_MIRROR"), True),
        mirror_ticks=_bool(g("SAA_MIRROR_TICKS"), False),
        halts_poll_seconds=_int(g("SAA_HALTS_POLL_SECONDS"), 60),
        vix_poll_minutes=_int(g("SAA_VIX_POLL_MINUTES"), 5),
    )


def readiness(settings: Settings) -> list[str]:
    """Human-readable readiness report (key names only). Empty list = fully configured for M2."""
    problems: list[str] = []
    for env in ("prod", "sandbox"):
        miss = settings.missing_tt_keys(env)
        if miss:
            problems.append(f"tastytrade {env}: missing {', '.join(miss)}")
    if not settings.mirror_configured:
        problems.append("Supabase mirror: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY not both set — data stays in SQLite only")
    if not settings.telegram_fallback_configured:
        problems.append("Telegram fallback: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not both set (outbox path still works)")
    return problems
