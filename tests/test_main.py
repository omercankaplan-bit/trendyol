"""Tests for the configuration loading and the CLI contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from mont_indirim_bot.config import REPO_ROOT, ConfigError, load_config
from mont_indirim_bot.main import build_parser, cli

BASE_ENV = {"TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN", "TELEGRAM_CHAT_ID": "-1001234567890"}


def test_load_config_requires_the_bot_token():
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        load_config({"TELEGRAM_CHAT_ID": "1"})


def test_load_config_requires_the_chat_id():
    with pytest.raises(ConfigError, match="TELEGRAM_CHAT_ID"):
        load_config({"TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN"})


def test_load_config_rejects_a_non_numeric_chat_id():
    with pytest.raises(ConfigError, match="numeric chat id"):
        load_config({**BASE_ENV, "TELEGRAM_CHAT_ID": "@kanalim"})


def test_load_config_defaults():
    config = load_config(BASE_ENV)

    assert config.telegram_chat_id == -1001234567890
    assert config.telegram_bot_token == "123456:TEST-TOKEN"
    assert config.min_discount_pct == 20.0
    assert config.cooldown_hours == 12.0
    assert config.rearm_pct == 2.0
    assert config.max_retries == 3
    assert config.max_parallel_requests == 2
    assert config.request_min_delay_s == 2.0
    assert config.request_max_delay_s == 6.0
    assert config.data_dir == REPO_ROOT / "data"
    assert config.watchlist_path.name == "watchlist.json"
    assert config.state_path.name == "state.json"
    assert config.cooldown_hours_seconds == 12 * 3600


def test_load_config_reads_overrides(tmp_path: Path):
    config = load_config(
        {
            **BASE_ENV,
            "MIN_DISCOUNT_PCT": "20,5",
            "COOLDOWN_HOURS": "6",
            "REARM_PCT": "5",
            "MAX_RETRIES": "5",
            "REQUEST_TIMEOUT_S": "9",
            "MAX_PARALLEL_REQUESTS": "4",
            "REQUEST_MIN_DELAY_S": "1",
            "REQUEST_MAX_DELAY_S": "3",
            "DATA_DIR": str(tmp_path),
            "LOG_LEVEL": "debug",
        }
    )

    assert config.min_discount_pct == 20.5
    assert config.cooldown_hours == 6.0
    assert config.rearm_pct == 5.0
    assert config.max_retries == 5
    assert config.request_timeout_s == 9.0
    assert config.max_parallel_requests == 4
    assert config.request_min_delay_s == 1.0
    assert config.request_max_delay_s == 3.0
    assert config.data_dir == tmp_path
    assert config.log_level == "DEBUG"


def test_load_config_resolves_a_relative_data_dir():
    config = load_config({**BASE_ENV, "DATA_DIR": "custom-state"})

    assert config.data_dir == REPO_ROOT / "custom-state"


@pytest.mark.parametrize(
    "env",
    [
        {"MIN_DISCOUNT_PCT": "abc"},
        {"MIN_DISCOUNT_PCT": "0"},
        {"COOLDOWN_HOURS": "-1"},
        {"MAX_RETRIES": "0"},
        {"MAX_PARALLEL_REQUESTS": "abc"},
        {"REQUEST_MIN_DELAY_S": "5", "REQUEST_MAX_DELAY_S": "1"},
        {"REQUEST_MIN_DELAY_S": "-1"},
        {"REQUEST_TIMEOUT_S": "soon"},
    ],
)
def test_load_config_rejects_malformed_values(env):
    with pytest.raises(ConfigError):
        load_config({**BASE_ENV, **env})


def test_parser_defaults_to_both_modes():
    assert build_parser().parse_args([]).mode == "both"
    assert build_parser().parse_args(["--mode", "check"]).mode == "check"
    assert build_parser().parse_args(["--verbose"]).verbose is True


def test_parser_rejects_an_unknown_mode():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--mode", "teleport"])


def test_cli_exits_non_zero_without_secrets(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    assert cli(["--mode", "check"]) == 1


def test_cli_exits_non_zero_with_a_malformed_chat_id(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST-TOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "kanal")

    assert cli(["--mode", "check"]) == 1
