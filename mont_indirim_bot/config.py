"""Environment driven configuration.

Every knob the bot needs comes from the process environment, which in
production is filled by GitHub Actions secrets/variables (see `.env.example`).
`load_config` is deliberately pure: it takes an optional mapping so tests can
exercise the validation without touching `os.environ`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DEFAULT_COOLDOWN_HOURS",
    "DEFAULT_DELAY_RANGE_S",
    "DEFAULT_MAX_PARALLEL_REQUESTS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_MIN_DISCOUNT_PCT",
    "DEFAULT_REARM_PCT",
    "DEFAULT_REQUEST_TIMEOUT_S",
    "REPO_ROOT",
    "Config",
    "ConfigError",
    "load_config",
]

#: Repository root, derived from this file's location (`<root>/mont_indirim_bot/config.py`).
REPO_ROOT = Path(__file__).resolve().parents[1]

#: Default directory holding `watchlist.json` and `state.json`.
DEFAULT_DATA_DIR = REPO_ROOT / "data"

DEFAULT_MIN_DISCOUNT_PCT = 20.0
DEFAULT_COOLDOWN_HOURS = 12.0
DEFAULT_REARM_PCT = 2.0
DEFAULT_DELAY_RANGE_S = (2.0, 6.0)
DEFAULT_MAX_RETRIES = 3
DEFAULT_REQUEST_TIMEOUT_S = 20.0
DEFAULT_MAX_PARALLEL_REQUESTS = 2

ENV_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_CHAT_ID = "TELEGRAM_CHAT_ID"


class ConfigError(RuntimeError):
    """Raised when the environment does not provide a usable configuration."""


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable runtime configuration."""

    telegram_bot_token: str
    telegram_chat_id: int
    min_discount_pct: float = DEFAULT_MIN_DISCOUNT_PCT
    cooldown_hours: float = DEFAULT_COOLDOWN_HOURS
    rearm_pct: float = DEFAULT_REARM_PCT
    request_min_delay_s: float = DEFAULT_DELAY_RANGE_S[0]
    request_max_delay_s: float = DEFAULT_DELAY_RANGE_S[1]
    max_retries: int = DEFAULT_MAX_RETRIES
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S
    max_parallel_requests: int = DEFAULT_MAX_PARALLEL_REQUESTS
    data_dir: Path = DEFAULT_DATA_DIR
    log_level: str = "INFO"

    @property
    def watchlist_path(self) -> Path:
        """Path of the tracked-products file."""
        return self.data_dir / "watchlist.json"

    @property
    def state_path(self) -> Path:
        """Path of the persisted runtime state file."""
        return self.data_dir / "state.json"

    @property
    def cooldown_hours_seconds(self) -> float:
        """Cooldown window expressed in seconds."""
        return self.cooldown_hours * 3600.0


def _raw(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _get_str(env: Mapping[str, str], key: str, default: str | None = None) -> str:
    value = _raw(env, key)
    if value is None:
        if default is None:
            msg = f"{key} is not set."
            raise ConfigError(msg)
        return default
    return value


def _get_float(env: Mapping[str, str], key: str, default: float) -> float:
    value = _raw(env, key)
    if value is None:
        return default
    try:
        # Accept both "20.5" and the Turkish "20,5" spelling.
        return float(value.replace(",", "."))
    except ValueError as exc:
        msg = f"{key} must be a number, got {value!r}."
        raise ConfigError(msg) from exc


def _get_int(env: Mapping[str, str], key: str, default: int) -> int:
    value = _raw(env, key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        msg = f"{key} must be an integer, got {value!r}."
        raise ConfigError(msg) from exc


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build a :class:`Config` from `env` (defaults to `os.environ`).

    Raises:
        ConfigError: when a required variable is missing or malformed. The
            caller turns this into a non-zero exit code so the GitHub Actions
            run turns red instead of silently doing nothing.
    """
    source: Mapping[str, str] = os.environ if env is None else env

    token = _get_str(source, ENV_BOT_TOKEN)
    chat_id_raw = _get_str(source, ENV_CHAT_ID)
    try:
        chat_id = int(chat_id_raw)
    except ValueError as exc:
        msg = (
            f"{ENV_CHAT_ID} must be a numeric chat id, got {chat_id_raw!r}. "
            "Group ids are negative, e.g. -1001234567890."
        )
        raise ConfigError(msg) from exc

    min_delay = _get_float(source, "REQUEST_MIN_DELAY_S", DEFAULT_DELAY_RANGE_S[0])
    max_delay = _get_float(source, "REQUEST_MAX_DELAY_S", DEFAULT_DELAY_RANGE_S[1])
    if min_delay < 0 or max_delay < min_delay:
        msg = (
            "REQUEST_MIN_DELAY_S/REQUEST_MAX_DELAY_S must satisfy "
            f"0 <= min <= max, got {min_delay} and {max_delay}."
        )
        raise ConfigError(msg)

    min_discount = _get_float(source, "MIN_DISCOUNT_PCT", DEFAULT_MIN_DISCOUNT_PCT)
    if min_discount <= 0:
        msg = f"MIN_DISCOUNT_PCT must be > 0, got {min_discount}."
        raise ConfigError(msg)

    cooldown = _get_float(source, "COOLDOWN_HOURS", DEFAULT_COOLDOWN_HOURS)
    if cooldown < 0:
        msg = f"COOLDOWN_HOURS must be >= 0, got {cooldown}."
        raise ConfigError(msg)

    retries = _get_int(source, "MAX_RETRIES", DEFAULT_MAX_RETRIES)
    if retries < 1:
        msg = f"MAX_RETRIES must be >= 1, got {retries}."
        raise ConfigError(msg)

    parallel = _get_int(source, "MAX_PARALLEL_REQUESTS", DEFAULT_MAX_PARALLEL_REQUESTS)
    if parallel < 1:
        msg = f"MAX_PARALLEL_REQUESTS must be >= 1, got {parallel}."
        raise ConfigError(msg)

    data_dir_raw = _raw(source, "DATA_DIR")
    data_dir = Path(data_dir_raw).expanduser() if data_dir_raw else DEFAULT_DATA_DIR
    if not data_dir.is_absolute():
        data_dir = REPO_ROOT / data_dir

    return Config(
        telegram_bot_token=token,
        telegram_chat_id=chat_id,
        min_discount_pct=min_discount,
        cooldown_hours=cooldown,
        rearm_pct=_get_float(source, "REARM_PCT", DEFAULT_REARM_PCT),
        request_min_delay_s=min_delay,
        request_max_delay_s=max_delay,
        max_retries=retries,
        request_timeout_s=_get_float(source, "REQUEST_TIMEOUT_S", DEFAULT_REQUEST_TIMEOUT_S),
        max_parallel_requests=parallel,
        data_dir=data_dir,
        log_level=_get_str(source, "LOG_LEVEL", "INFO").upper(),
    )
