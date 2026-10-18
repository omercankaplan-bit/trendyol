"""Service layer: scanning, notification formatting and command handling."""

from __future__ import annotations

from mont_indirim_bot.services.commands import (
    ALIASES,
    KNOWN_COMMANDS,
    Command,
    CommandProcessor,
    parse_command,
)
from mont_indirim_bot.services.notifier import (
    Alert,
    CheckSummary,
    Notifier,
    WatchlistRow,
    format_alert,
    format_check_summary,
    format_help,
    format_history,
    format_watchlist,
    md_code,
    md_escape,
    md_escape_url,
    md_pre,
)
from mont_indirim_bot.services.price_checker import (
    ERROR_ALERT_THRESHOLD,
    CheckOutcome,
    PriceChecker,
    drop_percent,
)

__all__ = [
    "ALIASES",
    "ERROR_ALERT_THRESHOLD",
    "KNOWN_COMMANDS",
    "Alert",
    "CheckOutcome",
    "CheckSummary",
    "Command",
    "CommandProcessor",
    "Notifier",
    "PriceChecker",
    "WatchlistRow",
    "drop_percent",
    "format_alert",
    "format_check_summary",
    "format_help",
    "format_history",
    "format_watchlist",
    "md_code",
    "md_escape",
    "md_escape_url",
    "md_pre",
    "parse_command",
]
