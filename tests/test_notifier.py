"""Tests for the Turkish MarkdownV2 message builders."""

from __future__ import annotations

import json
from typing import Any

import httpx

from mont_indirim_bot.services.notifier import (
    MARKDOWN_V2_SPECIALS,
    Alert,
    CheckSummary,
    Notifier,
    WatchlistRow,
    format_added,
    format_alert,
    format_check_summary,
    format_help,
    format_history,
    format_scraper_warning,
    format_watchlist,
    md_code,
    md_escape,
    md_pre,
)
from mont_indirim_bot.storage import HistoryEntry
from mont_indirim_bot.telegram_client import TelegramClient
from tests.helpers import TRENDYOL_URL, assert_markdownv2_safe, make_client

SPECIAL_TITLE = "Mont (indirim!) %50 - 1.299,00 TL #yeni_x"


def sample_alert(**overrides: Any) -> Alert:
    base = Alert(
        item_id="aaa111",
        title="Columbia Pike Lake Mont",
        site="Trendyol",
        url=TRENDYOL_URL,
        new_price=899.0,
        old_price=1000.0,
        currency="TRY",
        discount_pct=10.1,
        target_price=900.0,
        lowest_price=899.0,
        in_stock=True,
        reasons=("target",),
        detected_at="2026-10-18T12:00:00+00:00",
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


# --------------------------------------------------------------------------- #
# Escaping primitives
# --------------------------------------------------------------------------- #
def test_md_escape_covers_every_special_character():
    assert md_escape("düz metin") == "düz metin"
    for char in MARKDOWN_V2_SPECIALS:
        assert md_escape(char) == f"\\{char}"


def test_md_code_escapes_backticks_and_backslashes():
    assert md_code("1.299,00 TL") == "`1.299,00 TL`"
    assert md_code("a`b") == "`a\\`b`"
    assert md_code("a\\b") == "`a\\\\b`"


def test_md_pre_wraps_in_a_code_block():
    assert md_pre("a-b.c") == "```\na-b.c\n```"


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #
def test_alert_escapes_the_title_and_keeps_the_link_clickable():
    alert = sample_alert(title=SPECIAL_TITLE)
    message = format_alert(alert)

    assert md_escape(SPECIAL_TITLE) in message
    assert SPECIAL_TITLE not in message
    assert f"[Ürüne git]({TRENDYOL_URL})" in message
    assert_markdownv2_safe(message)


def test_alert_shows_old_and_new_price_with_the_drop():
    message = format_alert(sample_alert())

    assert "`1.000,00 TL`" in message
    assert f"*{md_escape('899,00 TL')}*" in message
    assert md_escape("Düşüş: %10,1") in message
    assert "hedef fiyata ulaşıldı" in message
    assert "hedefin" in message  # 899 TL is below the 900 TL target
    assert md_escape("18.10.2026 12:00 UTC") in message


def test_alert_handles_a_missing_previous_price_and_target():
    alert = sample_alert(old_price=None, target_price=None, discount_pct=None, reasons=("drop",))
    message = format_alert(alert)

    assert "Eski fiyat" not in message
    assert "Hedef" not in message
    assert "büyük fiyat düşüşü" in message
    assert_markdownv2_safe(message)


def test_alert_reports_the_gap_when_the_target_is_not_reached_yet():
    message = format_alert(sample_alert(new_price=950.0, target_price=900.0, lowest_price=950.0))

    assert md_escape("hedefe 50,00 TL kaldı") in message


def test_alert_flags_an_out_of_stock_product():
    message = format_alert(sample_alert(in_stock=False))

    assert "stokta görünmüyor" in message


# --------------------------------------------------------------------------- #
# Lists, history and summaries
# --------------------------------------------------------------------------- #
def test_help_mentions_every_command_and_is_escaped():
    message = format_help(("Trendyol", "Hepsiburada"))

    for command in ("/ekle", "/liste", "/sil", "/esik", "/gecmis", "/kontrol", "/yardim"):
        assert command in message
    assert "Hepsiburada" in message
    assert_markdownv2_safe(message)


def test_watchlist_is_empty_message():
    assert "boş" in format_watchlist([])


def test_watchlist_renders_a_monospace_table_and_truncates_titles():
    long_title = "Columbia Erkek Pike Lake Mont Su Geçirmez Uzun Kol"
    message = format_watchlist(
        [
            WatchlistRow(
                item_id="aaa111", title=long_title, price=1000.0, lowest=900.0, drop_pct=25.0
            )
        ]
    )

    assert "aaa111" in message
    assert "1.000,00 TL" in message
    assert "900,00 TL" in message
    assert "%25,0" in message
    assert long_title[:23] + "..." in message
    assert long_title not in message
    assert "```" in message
    assert_markdownv2_safe(message)


def test_watchlist_handles_unknown_prices():
    message = format_watchlist([WatchlistRow(item_id="aaa111", title="")])

    assert "(ad okunamadı)" in message
    assert "-" in message


def test_history_lists_the_newest_entry_first():
    entries = [
        HistoryEntry(at="2026-10-18T09:00:00+00:00", price=1000.0, list_price=1400.0),
        HistoryEntry(at="2026-10-18T11:00:00+00:00", price=900.0, list_price=1400.0),
    ]
    message = format_history("aaa111", "Mont", entries)

    assert message.index("900,00 TL") < message.index("1.000,00 TL")
    assert "TARİH" in message
    assert "```" in message


def test_history_without_records():
    assert "fiyat kaydı yok" in format_history("aaa111", "Mont", [])


def test_check_summary_reports_alerts_and_errors():
    summary = CheckSummary(
        checked=3,
        ok=2,
        errors=1,
        alerts=["Mont → 899,00 TL"],
        error_messages=["Bere: HTTP 403"],
    )
    message = format_check_summary(summary)

    assert "3 ürün tarandı" in message
    assert "1 indirim bulundu" in message
    assert "Hatalar" in message
    assert_markdownv2_safe(message)


def test_check_summary_without_alerts():
    message = format_check_summary(CheckSummary(checked=2, ok=2))

    assert "indirim yok" in message


def test_added_confirmation_escapes_the_url_and_shows_the_target():
    message = format_added("aaa111", TRENDYOL_URL, SPECIAL_TITLE, 1299.0, 999.9, None)

    assert "aaa111" in message
    assert "`999,90 TL`" in message
    assert md_escape(SPECIAL_TITLE) in message
    assert f"[{md_escape(TRENDYOL_URL)}]({TRENDYOL_URL})" in message
    assert_markdownv2_safe(message)


def test_scraper_warning_contains_the_error_and_attempts():
    message = format_scraper_warning("aaa111", "Mont", TRENDYOL_URL, "HTTP 403 (forbidden)", 3)

    assert "`HTTP 403 (forbidden)`" in message
    assert "3 denemedir" in message
    assert_markdownv2_safe(message)


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #
async def test_notifier_sends_to_the_configured_chat():
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async with make_client(handler) as http:
        notifier = Notifier(TelegramClient("123456:TEST-TOKEN", client=http), 4242)
        assert await notifier.send("merhaba") is True
        assert await notifier.send_alert(sample_alert()) is True

    assert len(sent) == 2
    assert sent[0]["chat_id"] == 4242
    assert sent[1]["parse_mode"] == "MarkdownV2"


async def test_notifier_logs_instead_of_raising():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "description": "can't parse entities"})

    async with make_client(handler) as http:
        notifier = Notifier(TelegramClient("123456:TEST-TOKEN", client=http), 4242)
        assert await notifier.send("bozuk") is False
