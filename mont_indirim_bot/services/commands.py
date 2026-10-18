"""Telegram command parsing and execution.

Only the chat configured as `TELEGRAM_CHAT_ID` is obeyed; updates from anybody
else are consumed (so they do not pile up) and silently ignored. Command names
and arguments are parsed by a pure function so the whole grammar is unit
tested, and every handler returns MarkdownV2 ready Turkish text.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from mont_indirim_bot.config import Config
from mont_indirim_bot.prices import format_price, parse_price
from mont_indirim_bot.scrapers import (
    Scraper,
    ScraperError,
    default_scrapers,
    find_scraper,
    supported_site_names,
)
from mont_indirim_bot.services.notifier import (
    CheckSummary,
    NotifierProtocol,
    WatchlistRow,
    format_added,
    format_check_summary,
    format_help,
    format_history,
    format_watchlist,
    md_escape,
)
from mont_indirim_bot.services.price_checker import PriceChecker
from mont_indirim_bot.storage import HistoryEntry, Store, WatchItem, to_iso, utc_now
from mont_indirim_bot.telegram_client import TelegramClient, TelegramError

LOGGER = logging.getLogger(__name__)

__all__ = ["ALIASES", "KNOWN_COMMANDS", "Command", "CommandProcessor", "parse_command"]

#: Commands the bot answers (after alias resolution).
KNOWN_COMMANDS = frozenset({"yardim", "ekle", "liste", "sil", "esik", "gecmis", "kontrol"})

#: Accepted spellings that map onto a canonical command.
ALIASES: dict[str, str] = {
    "start": "yardim",
    "help": "yardim",
    "yardim": "yardim",
    "liste": "liste",
    "kontrol": "kontrol",
}


@dataclass(frozen=True, slots=True)
class Command:
    """A parsed `/command arg1 arg2` message."""

    name: str
    args: tuple[str, ...] = ()
    raw: str = ""


def parse_command(text: object) -> Command | None:
    """Parse `text` into a :class:`Command`, or `None` when it is not one.

    Handles the `/ekle@MyBot` form Telegram uses inside groups.
    """
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split()
    head = parts[0][1:]
    if "@" in head:
        head = head.split("@", 1)[0]
    name = head.strip().lower()
    if not name:
        return None
    return Command(name=ALIASES.get(name, name), args=tuple(parts[1:]), raw=stripped)


class CommandProcessor:
    """Consumes Telegram updates and answers the owner's commands."""

    def __init__(
        self,
        *,
        store: Store,
        config: Config,
        checker: PriceChecker,
        notifier: NotifierProtocol,
        scrapers: Sequence[Scraper] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._config = config
        self._checker = checker
        self._notifier = notifier
        self._scrapers = (
            list(scrapers)
            if scrapers is not None
            else default_scrapers(
                min_delay_s=config.request_min_delay_s,
                max_delay_s=config.request_max_delay_s,
                max_retries=config.max_retries,
                timeout_s=config.request_timeout_s,
            )
        )
        self._clock = clock
        self._did_run_check = False

    @property
    def did_run_check(self) -> bool:
        """`True` when `/kontrol` already scanned the watchlist in this process."""
        return self._did_run_check

    @property
    def site_names(self) -> tuple[str, ...]:
        """Display names of the supported storefronts."""
        return supported_site_names(self._scrapers)

    async def poll_once(self, telegram: TelegramClient) -> int:
        """Process every pending update once and persist the new offset."""
        state = self._store.state
        offset = state.telegram_offset or None
        updates = await telegram.get_updates(offset=offset)
        if not updates:
            LOGGER.debug("No pending Telegram updates")
            return 0

        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                # Advance first: a failing reply must not replay the update.
                state.telegram_offset = max(state.telegram_offset, update_id + 1)
            try:
                await self.handle_update(update)
            except TelegramError as exc:
                LOGGER.error("Could not answer update %s: %s", update_id, exc)
            except Exception:
                LOGGER.exception("Unexpected error while handling update %s", update_id)

        await self._store.save_state()
        LOGGER.info("Processed %d Telegram update(s)", len(updates))
        return len(updates)

    async def handle_update(self, update: Mapping[str, Any]) -> str | None:
        """Handle one update, returning the reply that was sent (if any)."""
        message = update.get("message")
        if not isinstance(message, Mapping):
            return None
        chat = message.get("chat")
        chat_id = chat.get("id") if isinstance(chat, Mapping) else None
        if chat_id != self._config.telegram_chat_id:
            LOGGER.warning(
                "Ignoring update from chat %r: it is not the configured owner. "
                "Set TELEGRAM_CHAT_ID to this value if this is your chat.",
                chat_id,
            )
            return None

        command = parse_command(message.get("text"))
        if command is None:
            return None

        reply = await self.handle_command(command)
        if reply:
            await self._notifier.send(reply)
        return reply

    async def handle_command(self, command: Command) -> str:
        """Dispatch `command` to its handler and return the reply text."""
        handlers: dict[str, Callable[[Sequence[str]], Awaitable[str]]] = {
            "yardim": self._cmd_help,
            "liste": self._cmd_list,
            "ekle": self._cmd_add,
            "sil": self._cmd_remove,
            "esik": self._cmd_threshold,
            "gecmis": self._cmd_history,
            "kontrol": self._cmd_check,
        }
        handler = handlers.get(command.name)
        if handler is None:
            return md_escape(
                f"Bilinmeyen komut: /{command.name}. Komut listesi için /yardim yazabilirsin."
            )
        return await handler(command.args)

    # -- command handlers --------------------------------------------------- #
    async def _cmd_help(self, args: Sequence[str]) -> str:
        del args
        return format_help(self.site_names)

    async def _cmd_list(self, args: Sequence[str]) -> str:
        del args
        rows: list[WatchlistRow] = []
        for item in self._store.watchlist.items:
            state = self._store.state.items.get(item.id)
            price = state.last_price if state else None
            lowest = state.lowest_price if state else None
            highest = state.highest_price if state else None
            # "Düşüş" shows how far the current price sits below the highest
            # price ever observed for this product.
            drop_pct = (
                (highest - price) / highest * 100.0
                if highest and price is not None and highest > price
                else (0.0 if price is not None else None)
            )
            rows.append(
                WatchlistRow(
                    item_id=item.id,
                    title=item.title or (state.title if state else ""),
                    price=price,
                    lowest=lowest,
                    drop_pct=drop_pct,
                    target=item.target_price,
                    currency=state.last_currency if state else "TRY",
                )
            )
        return format_watchlist(rows)

    async def _cmd_add(self, args: Sequence[str]) -> str:
        usage = md_escape("Kullanım: /ekle <link> [hedef fiyat] — ör. /ekle <link> 1500")
        if not args:
            return usage

        url = args[0].strip()
        if not url.lower().startswith(("http://", "https://")):
            return md_escape("Link http:// veya https:// ile başlamalı.")

        target_price: float | None = None
        if len(args) > 1:
            target_price = parse_price(args[1])
            if target_price is None:
                return md_escape(f"Hedef fiyatı anlayamadım: {args[1]}")

        scraper = find_scraper(url, self._scrapers)
        if scraper is None:
            return md_escape(
                "Bu siteyi desteklemiyorum. Desteklenen siteler: " + ", ".join(self.site_names)
            )

        existing = self._store.watchlist.find_by_url(url)
        if existing is not None:
            return md_escape(f"Bu ürün zaten takipte (id: {existing.id}).")

        title = ""
        price: float | None = None
        currency = "TRY"
        stock = True
        warning: str | None = None
        try:
            result = await scraper.fetch(url)
        except ScraperError as exc:
            warning = f"Ürün adı ve fiyatı şimdi okunamadı ({exc}); ilk kontrolde tekrar denenecek."
            LOGGER.info("Could not pre-fetch %s while adding: %s", url, exc)
        else:
            title, price, currency, stock = (
                result.product_name,
                result.price,
                result.currency,
                result.in_stock,
            )
            if target_price is not None and price <= target_price:
                warning = (
                    f"Güncel fiyat ({format_price(price, currency)}) hedefinin altında; "
                    "sonraki kontrolde bildirim gelebilir."
                )

        item = self._store.watchlist.add(
            WatchItem(id="", url=url, site=scraper.name, title=title, target_price=target_price)
        )
        if price is not None:
            self._seed_state(item, title, price, currency, stock)
        await self._store.save_all()
        LOGGER.info("Added %s (%s) to the watchlist", item.id, scraper.name)
        return format_added(item.id, url, title, price, target_price, warning)

    async def _cmd_remove(self, args: Sequence[str]) -> str:
        if not args:
            return md_escape("Kullanım: /sil <id>")
        item = self._store.watchlist.remove(args[0])
        if item is None:
            return md_escape(f"Bu id ile takipte bir ürün yok: {args[0]}")
        self._store.forget_item(item.id)
        await self._store.save_all()
        LOGGER.info("Removed %s from the watchlist", item.id)
        return md_escape(f"Takipten çıkarıldı: {item.title or item.id} (id: {item.id})")

    async def _cmd_threshold(self, args: Sequence[str]) -> str:
        if len(args) < 2:
            return md_escape("Kullanım: /esik <id> <fiyat> — ör. /esik a1b2c3 1500")
        item = self._store.watchlist.find(args[0])
        if item is None:
            return md_escape(f"Bu id ile takipte bir ürün yok: {args[0]}")
        target_price = parse_price(args[1])
        if target_price is None:
            return md_escape(f"Fiyatı anlayamadım: {args[1]}")

        item.target_price = target_price
        state = self._store.state_for(item.id)
        # A new threshold must be able to notify even if the previous one did.
        state.armed_target = True
        await self._store.save_all()

        note = ""
        if state.last_price is not None and state.last_price <= target_price:
            note = (
                " Uyarı: güncel fiyat bu hedefin altında, bir sonraki kontrolde bildirim gelebilir."
            )
        return md_escape(
            f"{item.id} için hedef fiyat {format_price(target_price)} olarak ayarlandı.{note}"
        )

    async def _cmd_history(self, args: Sequence[str]) -> str:
        if not args:
            return md_escape("Kullanım: /gecmis <id>")
        item = self._store.watchlist.find(args[0])
        state = self._store.state.items.get(args[0].strip().lower())
        if item is None and state is None:
            return md_escape(f"Bu id ile takipte bir ürün yok: {args[0]}")
        item_id = item.id if item is not None else args[0].strip().lower()
        title = (item.title if item else "") or (state.title if state else "")
        entries: list[HistoryEntry] = self._store.history_for(item_id)
        return format_history(item_id, title, entries)

    async def _cmd_check(self, args: Sequence[str]) -> str:
        del args
        if not self._store.watchlist.active_items():
            return md_escape(
                "Takip listesi boş. /ekle <link> [hedef fiyat] ile ürün ekleyebilirsin."
            )
        outcomes = await self._checker.run()
        self._did_run_check = True
        summary = CheckSummary(
            checked=len(outcomes),
            ok=sum(1 for outcome in outcomes if not outcome.failed),
            errors=sum(1 for outcome in outcomes if outcome.failed),
        )
        for outcome in outcomes:
            label = outcome.title or outcome.item_id
            if outcome.failed:
                summary.error_messages.append(f"{label}: {outcome.error or 'bilinmeyen hata'}")
            for alert in outcome.alerts:
                summary.alerts.append(f"{label} → {format_price(alert.new_price, alert.currency)}")
        return format_check_summary(summary)

    # -- helpers ------------------------------------------------------------ #
    def _seed_state(
        self, item: WatchItem, title: str, price: float, currency: str, in_stock: bool
    ) -> None:
        """Store the price measured while adding, so `/gecmis` has a first row."""
        state = self._store.state_for(item.id)
        now = self._clock()
        state.title = title or state.title
        state.last_price = price
        state.last_currency = currency
        state.in_stock = in_stock
        state.last_checked_at = to_iso(now)
        state.last_success_at = to_iso(now)
        state.lowest_price = price if state.lowest_price is None else min(state.lowest_price, price)
        state.highest_price = (
            price if state.highest_price is None else max(state.highest_price, price)
        )
        self._store.record_history(
            item.id, HistoryEntry(at=to_iso(now), price=price, in_stock=in_stock)
        )
