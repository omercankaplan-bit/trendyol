"""Telegram message formatting.

Two rules shape this module:

* All user facing text is Turkish (the operator is Turkish), while the code
  and docstrings stay English.
* Everything that reaches the Bot API is escaped for `MarkdownV2`. The static
  literals are passed through :func:`md_escape` as well, because characters
  like `.`, `-`, `(` and `!` are special in that dialect and a missed escape
  makes Telegram reject the whole message with HTTP 400.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from mont_indirim_bot.prices import format_percent, format_price
from mont_indirim_bot.storage import HistoryEntry, from_iso
from mont_indirim_bot.telegram_client import TelegramClient, TelegramError

LOGGER = logging.getLogger(__name__)

__all__ = [
    "MARKDOWN_V2_SPECIALS",
    "Alert",
    "Notifier",
    "NotifierProtocol",
    "WatchlistRow",
    "format_added",
    "format_alert",
    "format_brand_digest",
    "format_check_summary",
    "format_help",
    "format_history",
    "format_scraper_warning",
    "format_watchlist",
    "md_code",
    "md_escape",
    "md_escape_url",
    "md_pre",
]

#: Characters Telegram's MarkdownV2 dialect reserves.
MARKDOWN_V2_SPECIALS = "_*[]()~`>#+-=|{}.!"

_CODE_TRANSLATION = str.maketrans({"\\": "\\\\", "`": "\\`"})

#: Human readable labels for the trigger names used in `state.json`.
REASON_LABELS: dict[str, str] = {
    "target": "hedef fiyata ulaşıldı",
    "drop": "büyük fiyat düşüşü",
}


def md_escape(text: str) -> str:
    """Escape every MarkdownV2 special character in `text`."""
    return "".join(f"\\{char}" if char in MARKDOWN_V2_SPECIALS else char for char in text)


def md_escape_url(url: str) -> str:
    """Escape a URL used as a MarkdownV2 link target (only `\\` and `)` matter)."""
    return url.replace("\\", "\\\\").replace(")", "\\)")


def md_code(text: str) -> str:
    """Wrap `text` in a MarkdownV2 inline code entity."""
    return f"`{text.translate(_CODE_TRANSLATION)}`"


def md_pre(text: str) -> str:
    """Wrap `text` in a MarkdownV2 pre (code block) entity."""
    return f"```\n{text.translate(_CODE_TRANSLATION)}\n```"


def _format_timestamp(value: str | None) -> str | None:
    moment: datetime | None = from_iso(value)
    if moment is None:
        return None
    return moment.strftime("%d.%m.%Y %H:%M UTC")


@dataclass(slots=True)
class Alert:
    """One notification-worthy price movement."""

    item_id: str
    title: str
    site: str
    url: str
    new_price: float
    old_price: float | None = None
    currency: str = "TRY"
    discount_pct: float | None = None
    target_price: float | None = None
    lowest_price: float | None = None
    in_stock: bool = True
    reasons: tuple[str, ...] = ()
    detected_at: str | None = None


@dataclass(slots=True)
class WatchlistRow:
    """One line of the `/liste` table."""

    item_id: str
    title: str
    price: float | None = None
    lowest: float | None = None
    drop_pct: float | None = None
    target: float | None = None
    currency: str = "TRY"


@dataclass(slots=True)
class CheckSummary:
    """Aggregated result of one scanning pass, used by `/kontrol`."""

    checked: int = 0
    ok: int = 0
    errors: int = 0
    alerts: list[str] = field(default_factory=list)
    error_messages: list[str] = field(default_factory=list)


class NotifierProtocol(Protocol):
    """The part of :class:`Notifier` the checker and the command layer need.

    Using a protocol keeps the domain services testable without a live Telegram
    session while still type checking the real implementation.
    """

    async def send(self, text: str, *, disable_notification: bool = False) -> bool:
        """Send one MarkdownV2 message to the owner."""
        ...

    async def send_alert(self, alert: Alert) -> bool:
        """Format and send one discount alert."""
        ...

    async def send_scraper_warning(
        self, item_id: str, title: str, url: str, error: str, attempts: int
    ) -> bool:
        """Warn the owner about a product that cannot be read."""
        ...


# --------------------------------------------------------------------------- #
# Message builders
# --------------------------------------------------------------------------- #
def format_alert(alert: Alert) -> str:
    """Render one discount notification."""
    lines: list[str] = [f"🚨 *{md_escape('İNDİRİM')}* 🚨", ""]
    lines.append(f"📦 *{md_escape(alert.title or 'Bilinmeyen ürün')}*")
    lines.append(f"🏪 {md_escape(alert.site)} · {md_escape('ID')}: {md_code(alert.item_id)}")
    lines.append("")

    if alert.old_price is not None:
        old_price = md_code(format_price(alert.old_price, alert.currency))
        lines.append(f"💸 {md_escape('Eski fiyat')}: {old_price}")
    new_price = md_escape(format_price(alert.new_price, alert.currency))
    lines.append(f"✅ {md_escape('Yeni fiyat')}: *{new_price}*")
    if alert.discount_pct is not None:
        drop_text = format_percent(alert.discount_pct)
        lines.append(f"📉 {md_escape(f'Düşüş: {drop_text}')}")

    if alert.target_price is not None:
        gap = alert.new_price - alert.target_price
        if gap <= 0:
            gap_text = f"hedefin {format_price(abs(gap), alert.currency)} altında"
        else:
            gap_text = f"hedefe {format_price(gap, alert.currency)} kaldı"
        lines.append(
            f"🎯 {md_escape('Hedef')}: {md_code(format_price(alert.target_price, alert.currency))}"
            f" · {md_escape(gap_text)}"
        )

    if alert.lowest_price is not None:
        lowest = md_code(format_price(alert.lowest_price, alert.currency))
        lines.append(f"📊 {md_escape('En düşük')}: {lowest}")
    if not alert.in_stock:
        lines.append(
            f"⚠️ {md_escape('Ürün stokta görünmüyor, sepete eklemek için geç kalmış olabilirsin')}"
        )

    if alert.reasons:
        labels = [REASON_LABELS.get(reason, reason) for reason in alert.reasons]
        lines.append(f"ℹ️ {md_escape('Tetikleyici')}: {md_escape(', '.join(labels))}")

    lines.append("")
    lines.append(f"🔗 [{md_escape('Ürüne git')}]({md_escape_url(alert.url)})")
    stamp = _format_timestamp(alert.detected_at)
    if stamp:
        lines.append(f"🕒 {md_escape(stamp)}")
    return "\n".join(lines)


def format_brand_digest(brand_name: str, alerts: Sequence[Alert]) -> str:
    """Render one brand's discounts as a single message.

    The watchlist answers "what does this product cost now"; this digest answers
    "what went on sale at this brand", so it groups every movement of a brand
    into one message instead of sending one alert per product.
    """
    lines: list[str] = [
        f"🏷 *{md_escape(brand_name or 'Marka')}* · {md_escape(f'{len(alerts)} indirim')}",
    ]
    for alert in alerts:
        lines.append("")
        lines.append(f"📦 *{md_escape(_shorten(alert.title, 70) or 'Bilinmeyen ürün')}*")
        if alert.old_price is not None:
            old_price = md_code(format_price(alert.old_price, alert.currency))
            lines.append(f"💸 {md_escape('Eski fiyat')}: {old_price}")
        new_price = md_escape(format_price(alert.new_price, alert.currency))
        drop = (
            md_escape(f" ({format_percent(alert.discount_pct)})")
            if alert.discount_pct is not None
            else ""
        )
        lines.append(f"✅ {md_escape('Yeni fiyat')}: *{new_price}*{drop}")
        if not alert.in_stock:
            lines.append(f"⚠️ {md_escape('Stok bilgisi belirsiz')}")
        lines.append(f"🔗 [{md_escape('Ürüne git')}]({md_escape_url(alert.url)})")
    return "\n".join(lines)


def _shorten(text: str, limit: int) -> str:
    """Trim `text` to `limit` characters, keeping the start."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def format_help(site_names: Sequence[str]) -> str:
    """Render the `/start` and `/yardim` text."""
    sites = ", ".join(site_names) if site_names else "Trendyol, Hepsiburada"
    lines = [
        f"🤖 *{md_escape('Mont İndirim Botu')}*",
        "",
        md_escape(
            "Sunucu gerektirmez: GitHub Actions üzerinde 10 dakikada bir çalışır, "
            f"{sites} ürünlerinin fiyatını izler ve indirim olunca haber verir."
        ),
        "",
        f"*{md_escape('Komutlar')}*",
        md_escape("/ekle <link> [hedef fiyat] - ürünü takibe al"),
        md_escape("/liste - id, ürün, güncel ve en düşük fiyat"),
        md_escape("/sil <id> - ürünü takipten çıkar"),
        md_escape("/esik <id> <fiyat> - hedef fiyatı güncelle"),
        md_escape("/gecmis <id> - son 10 fiyat kaydı"),
        md_escape("/kontrol - şimdi kontrol et ve özet al"),
        md_escape("/yardim - bu mesaj"),
        "",
        f"*{md_escape('Bildirim kuralları')}*",
        md_escape("• Fiyat, hedef fiyatının altına indiğinde"),
        md_escape("• Önceki ölçüme göre en az %20 düştüğünde (eşik değiştirilebilir)"),
        md_escape("Aynı ürün için 12 saatte en fazla bir bildirim gönderilir."),
    ]
    return "\n".join(lines)


def format_watchlist(rows: Sequence[WatchlistRow]) -> str:
    """Render the `/liste` table as a monospace block."""
    if not rows:
        return md_escape("Takip listen boş. /ekle <link> [hedef fiyat] ile ürün ekleyebilirsin.")

    header = f"{'ID':<6} {'ÜRÜN':<26} {'GÜNCEL':>12} {'EN DÜŞÜK':>12} {'DÜŞÜŞ':>7}"
    table = [header, "-" * len(header)]
    for row in rows:
        title = row.title or "(ad okunamadı)"
        title = title if len(title) <= 26 else title[:23] + "..."
        table.append(
            f"{row.item_id:<6} {title:<26} "
            f"{format_price(row.price, row.currency):>12} "
            f"{format_price(row.lowest, row.currency):>12} "
            f"{format_percent(row.drop_pct):>7}"
        )
    return "\n".join(
        [
            f"📋 *{md_escape(f'Takip listesi ({len(rows)} ürün)')}*",
            md_pre("\n".join(table)),
            md_escape("Detay için: /gecmis <id> · hedef değiştirmek için: /esik <id> <fiyat>"),
        ]
    )


def format_history(item_id: str, title: str, entries: Sequence[HistoryEntry]) -> str:
    """Render the last price observations of one product."""
    heading = f"🕘 *{md_escape(title or item_id)}* · {md_code(item_id)}"
    if not entries:
        return f"{heading}\n{md_escape('Bu ürün için henüz fiyat kaydı yok.')}"

    header = f"{'TARİH':<18} {'FİYAT':>12} {'LİSTE':>12}"
    table = [header, "-" * len(header)]
    for entry in reversed(entries[-10:]):
        stamp = _format_timestamp(entry.at) or entry.at
        table.append(
            f"{stamp:<18} {format_price(entry.price):>12} {format_price(entry.list_price):>12}"
        )
    return "\n".join([heading, md_pre("\n".join(table))])


def format_added(
    item_id: str,
    url: str,
    title: str,
    price: float | None,
    target: float | None,
    warning: str | None,
) -> str:
    """Render the `/ekle` confirmation."""
    lines = [
        f"✅ *{md_escape('Takibe alındı')}*",
        "",
        f"📦 {md_escape(title or 'Ürün adı ilk kontrolde doldurulacak')}",
        f"🆔 {md_code(item_id)}",
        f"🔗 [{md_escape(url)}]({md_escape_url(url)})",
    ]
    if price is not None:
        lines.append(f"💰 {md_escape('Güncel fiyat')}: {md_code(format_price(price))}")
    if target is not None:
        lines.append(f"🎯 {md_escape('Hedef fiyat')}: {md_code(format_price(target))}")
    if warning:
        lines.append("")
        lines.append(f"⚠️ {md_escape(warning)}")
    return "\n".join(lines)


def format_check_summary(summary: CheckSummary) -> str:
    """Render the `/kontrol` summary."""
    lines = [
        f"🔍 *{md_escape('Kontrol tamamlandı')}*",
        md_escape(
            f"{summary.checked} ürün tarandı: {summary.ok} başarılı, {summary.errors} hatalı."
        ),
    ]
    if summary.alerts:
        lines.append("")
        lines.append(f"*{md_escape(f'{len(summary.alerts)} indirim bulundu')}*")
        lines.extend(md_escape(f"• {line}") for line in summary.alerts)
    else:
        lines.append(md_escape("Şu an bildirim koşullarını sağlayan indirim yok."))
    if summary.error_messages:
        lines.append("")
        lines.append(f"*{md_escape('Hatalar')}*")
        lines.extend(md_escape(f"• {line}") for line in summary.error_messages)
    return "\n".join(lines)


def format_scraper_warning(item_id: str, title: str, url: str, error: str, attempts: int) -> str:
    """Render the owner warning sent after repeated scraping failures."""
    lines = [
        f"⚠️ *{md_escape('Fiyat okunamıyor')}*",
        f"📦 {md_escape(title or item_id)} · {md_code(item_id)}",
        md_escape(f"{attempts} denemedir ürün okunamıyor"),
        f"🧾 {md_escape('Son hata')}: {md_code(error)}",
        f"🔗 [{md_escape(url)}]({md_escape_url(url)})",
        md_escape("Site yapısı değişmiş olabilir; scraper sabitlerini güncellemek gerekebilir."),
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #
class Notifier:
    """Sends (already formatted) MarkdownV2 messages to the configured chat."""

    def __init__(self, telegram: TelegramClient, chat_id: int) -> None:
        self._telegram = telegram
        self._chat_id = chat_id

    @property
    def chat_id(self) -> int:
        """Chat the bot talks to; also the only chat whose commands are obeyed."""
        return self._chat_id

    async def send(self, text: str, *, disable_notification: bool = False) -> bool:
        """Send raw MarkdownV2 text, logging instead of raising on failure."""
        try:
            await self._telegram.send_message(
                self._chat_id, text, disable_notification=disable_notification
            )
        except TelegramError as exc:
            LOGGER.error("Could not deliver a Telegram message: %s", exc)
            return False
        return True

    async def send_alert(self, alert: Alert) -> bool:
        """Format and send one discount alert."""
        return await self.send(format_alert(alert))

    async def send_scraper_warning(
        self, item_id: str, title: str, url: str, error: str, attempts: int
    ) -> bool:
        """Tell the owner that a product has been unreadable for a while."""
        return await self.send(format_scraper_warning(item_id, title, url, error, attempts))
