"""Watchlist scanning.

One pass over the watchlist is fully deterministic given the fetched prices:

1. Fetch the current price of every active product (bounded parallelism).
2. Update the bookkeeping in `state.json` (last/lowest/highest price, history).
3. Decide whether the movement deserves a notification.
4. Apply the notification bookkeeping (cooldown + re-arm) and send.

A failure of a single product never aborts the pass: it is counted, stored in
`state.json`, and only after `ERROR_ALERT_THRESHOLD` consecutive failures does
the owner get a warning.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from mont_indirim_bot.config import Config
from mont_indirim_bot.scrapers import (
    PriceResult,
    Scraper,
    ScraperError,
    default_scrapers,
    find_scraper,
)
from mont_indirim_bot.services.notifier import Alert, NotifierProtocol
from mont_indirim_bot.storage import (
    HistoryEntry,
    ItemState,
    Store,
    WatchItem,
    to_iso,
    utc_now,
)

LOGGER = logging.getLogger(__name__)

__all__ = ["ERROR_ALERT_THRESHOLD", "CheckOutcome", "PriceChecker"]

#: Consecutive failures after which the owner is warned about a broken scraper.
ERROR_ALERT_THRESHOLD = 3

CheckStatus = Literal["ok", "error"]


def drop_percent(old_price: float | None, new_price: float) -> float | None:
    """Return the percentage drop from `old_price` to `new_price`.

    Returns `None` when there is no usable previous price or when the price did
    not actually fall.
    """
    if old_price is None or old_price <= 0 or new_price >= old_price:
        return None
    return (old_price - new_price) / old_price * 100.0


@dataclass(slots=True)
class CheckOutcome:
    """Result of checking one watchlist item."""

    item_id: str
    title: str
    status: CheckStatus
    price: float | None = None
    alerts: list[Alert] = field(default_factory=list)
    error: str | None = None

    @property
    def failed(self) -> bool:
        """`True` when the product could not be measured."""
        return self.status == "error"


class PriceChecker:
    """Scans the watchlist and notifies on real discounts."""

    def __init__(
        self,
        *,
        store: Store,
        config: Config,
        scrapers: Sequence[Scraper] | None = None,
        notifier: NotifierProtocol | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """
        Args:
            store: Loaded watchlist/state store.
            config: Runtime configuration (thresholds, cooldown, delays).
            scrapers: Scraper pool; defaults to every known site with the
                configured delays and retries.
            notifier: Sink for alerts; `None` disables sending (tests).
            clock: Injectable time source so cooldown logic is testable.
        """
        self._store = store
        self._config = config
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
        self._notifier = notifier
        self._clock = clock
        self._watchlist_dirty = False

    async def run(
        self,
        *,
        item_ids: Sequence[str] | None = None,
        send_alerts: bool = True,
    ) -> list[CheckOutcome]:
        """Check the watchlist (or just `item_ids`) and return one outcome per item."""
        items = self._select_items(item_ids)
        if not items:
            LOGGER.info("Nothing to check: the watchlist is empty")
            return []

        semaphore = asyncio.Semaphore(max(1, self._config.max_parallel_requests))

        async def guarded(item: WatchItem) -> CheckOutcome:
            async with semaphore:
                return await self._guarded_check(item, send_alerts=send_alerts)

        outcomes = await asyncio.gather(*(guarded(item) for item in items))
        if self._watchlist_dirty:
            await self._store.save_watchlist()
            self._watchlist_dirty = False

        alerts = sum(len(outcome.alerts) for outcome in outcomes)
        failures = sum(1 for outcome in outcomes if outcome.failed)
        LOGGER.info(
            "Checked %d product(s): %d alert(s), %d failure(s)",
            len(outcomes),
            alerts,
            failures,
        )
        return list(outcomes)

    async def check_item(self, item: WatchItem, *, send_alerts: bool = True) -> CheckOutcome:
        """Fetch and evaluate a single watchlist item."""
        return await self._guarded_check(item, send_alerts=send_alerts)

    # -- internals --------------------------------------------------------- #
    async def _guarded_check(self, item: WatchItem, *, send_alerts: bool) -> CheckOutcome:
        try:
            return await self._check_item(item, send_alerts=send_alerts)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.exception("Unexpected failure while checking %s", item.url)
            message = f"unexpected {type(exc).__name__}: {exc}"
            return await self._register_error(item, message)

    async def _check_item(self, item: WatchItem, *, send_alerts: bool) -> CheckOutcome:
        scraper = find_scraper(item.url, self._scrapers)
        if scraper is None:
            return await self._register_error(item, "unsupported site for this URL")

        try:
            result = await scraper.fetch(item.url)
        except ScraperError as exc:
            LOGGER.warning("[%s] %s: %s", scraper.name, item.url, exc)
            return await self._register_error(item, f"{scraper.name}: {exc}")
        except Exception as exc:
            LOGGER.exception("[%s] unexpected error for %s", scraper.name, item.url)
            return await self._register_error(
                item, f"{scraper.name}: unexpected {type(exc).__name__}"
            )

        return await self._apply_success(item, scraper, result, send_alerts=send_alerts)

    async def _apply_success(
        self,
        item: WatchItem,
        scraper: Scraper,
        result: PriceResult,
        *,
        send_alerts: bool,
    ) -> CheckOutcome:
        now = self._clock()
        state = self._store.state_for(item.id)
        previous_price = state.last_price

        self._update_price_state(item, state, result, now)
        self._update_history(item, state, result, now)
        self._rearm_if_recovered(state, result.price)

        reasons = self._trigger_reasons(item, state, result.price, previous_price)
        alerts: list[Alert] = []
        if reasons and self._cooldown_elapsed(state, now):
            alert = self._build_alert(item, state, scraper, result, previous_price, reasons, now)
            alerts.append(alert)
            self._mark_notified(state, result.price, reasons, now)
            LOGGER.info("Alert for %s: %s", item.id, ", ".join(reasons))
            if send_alerts and self._notifier is not None:
                await self._notifier.send_alert(alert)
        elif reasons:
            LOGGER.info(
                "Alert for %s (%s) suppressed by the %.1fh cooldown",
                item.id,
                ", ".join(reasons),
                self._config.cooldown_hours,
            )

        await self._store.save_state()
        return CheckOutcome(
            item_id=item.id,
            title=item.title or state.title,
            status="ok",
            price=result.price,
            alerts=alerts,
        )

    async def _register_error(self, item: WatchItem, message: str) -> CheckOutcome:
        now = self._clock()
        state = self._store.state_for(item.id)
        state.consecutive_errors += 1
        state.last_checked_at = to_iso(now)
        state.last_error = message

        if state.consecutive_errors >= ERROR_ALERT_THRESHOLD and not state.error_alerted:
            state.error_alerted = True
            self._store.state.last_error_alert_at = to_iso(now)
            LOGGER.error(
                "Product %s failed %d times in a row: %s",
                item.id,
                state.consecutive_errors,
                message,
            )
            if self._notifier is not None:
                await self._notifier.send_scraper_warning(
                    item.id,
                    item.title or state.title,
                    item.url,
                    message,
                    state.consecutive_errors,
                )

        await self._store.save_state()
        return CheckOutcome(
            item_id=item.id,
            title=item.title or state.title,
            status="error",
            error=message,
        )

    # -- state machine ------------------------------------------------------ #
    def _select_items(self, item_ids: Sequence[str] | None) -> list[WatchItem]:
        if item_ids is None:
            return self._store.watchlist.active_items()
        selected: list[WatchItem] = []
        for item_id in item_ids:
            item = self._store.watchlist.find(item_id)
            if item is None:
                LOGGER.warning("Unknown watchlist id %s", item_id)
                continue
            selected.append(item)
        return selected

    def _update_price_state(
        self, item: WatchItem, state: ItemState, result: PriceResult, now: datetime
    ) -> None:
        state.last_price = result.price
        state.last_list_price = result.list_price
        state.last_currency = result.currency
        state.in_stock = result.in_stock
        state.last_checked_at = to_iso(now)
        state.last_success_at = to_iso(now)
        state.consecutive_errors = 0
        state.error_alerted = False
        state.last_error = None
        if result.product_name and result.product_name != state.title:
            state.title = result.product_name
            item.title = result.product_name
            self._watchlist_dirty = True
        state.lowest_price = (
            result.price if state.lowest_price is None else min(state.lowest_price, result.price)
        )
        state.highest_price = (
            result.price if state.highest_price is None else max(state.highest_price, result.price)
        )

    def _update_history(
        self, item: WatchItem, state: ItemState, result: PriceResult, now: datetime
    ) -> None:
        entries = self._store.history_for(item.id)
        if entries and entries[-1].price == result.price:
            # Same price as the previous sample: keep the history meaningful.
            return
        self._store.record_history(
            item.id,
            HistoryEntry(
                at=to_iso(now),
                price=result.price,
                list_price=result.list_price,
                in_stock=result.in_stock,
            ),
        )

    def _rearm_if_recovered(self, state: ItemState, price: float) -> None:
        """Re-arm a trigger once the price climbed back above the notified level."""
        reference = state.last_notified_price
        if reference is None:
            return
        threshold = reference * (1.0 + max(0.0, self._config.rearm_pct) / 100.0)
        if price < threshold:
            return
        if not state.armed_target or not state.armed_drop:
            LOGGER.info("Price recovered above the notified level, triggers re-armed")
        state.armed_target = True
        state.armed_drop = True

    def _trigger_reasons(
        self,
        item: WatchItem,
        state: ItemState,
        price: float,
        previous_price: float | None,
    ) -> list[str]:
        reasons: list[str] = []
        if item.target_price is not None and state.armed_target and price <= item.target_price:
            reasons.append("target")
        threshold = (
            item.min_discount_pct
            if item.min_discount_pct is not None
            else self._config.min_discount_pct
        )
        drop = drop_percent(previous_price, price)
        if state.armed_drop and drop is not None and drop >= threshold:
            reasons.append("drop")
        return reasons

    def _cooldown_elapsed(self, state: ItemState, now: datetime) -> bool:
        last_notified = state.last_notified_datetime
        if last_notified is None:
            return True
        elapsed = (now - last_notified).total_seconds()
        return elapsed >= self._config.cooldown_hours_seconds

    def _mark_notified(
        self, state: ItemState, price: float, reasons: Sequence[str], now: datetime
    ) -> None:
        state.last_notified_at = to_iso(now)
        state.last_notified_price = price
        state.last_notified_reasons = list(reasons)
        if "target" in reasons:
            state.armed_target = False
        if "drop" in reasons:
            state.armed_drop = False

    def _build_alert(
        self,
        item: WatchItem,
        state: ItemState,
        scraper: Scraper,
        result: PriceResult,
        previous_price: float | None,
        reasons: Sequence[str],
        now: datetime,
    ) -> Alert:
        return Alert(
            item_id=item.id,
            title=item.title or state.title or "Bilinmeyen ürün",
            site=scraper.display_name,
            url=item.url,
            new_price=result.price,
            old_price=previous_price,
            currency=result.currency,
            discount_pct=drop_percent(previous_price, result.price),
            target_price=item.target_price,
            lowest_price=state.lowest_price,
            in_stock=result.in_stock,
            reasons=tuple(reasons),
            detected_at=to_iso(now),
        )
