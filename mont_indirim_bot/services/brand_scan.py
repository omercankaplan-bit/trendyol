"""Brand level scanning.

The watchlist tracks products the owner picked one by one; the brand scan does
the opposite and walks the listing pages of the brands they follow. That covers
new arrivals automatically and costs nothing when a product is delisted, which
is exactly why a per-product watchlist alone is not enough.

Every product found on a listing is remembered in `state.json` under
`brand_items`. Those entries reuse :class:`~mont_indirim_bot.storage.ItemState`,
so the drop rule, the cooldown and the re-arm behave exactly like they do for
the watchlist - only the message differs: one digest per brand instead of one
message per product. Products that are already on the watchlist are skipped so
the owner never gets the same discount twice.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Protocol

from mont_indirim_bot.config import Config
from mont_indirim_bot.prices import DEFAULT_CURRENCY
from mont_indirim_bot.scrapers.base import product_id
from mont_indirim_bot.scrapers.hepsiburada_listing import (
    JACKET_KEYWORDS,
    ListingClient,
    ListingProduct,
    build_listing_url,
    fold_turkish,
    matches_brand,
    parse_listing,
)
from mont_indirim_bot.services.notifier import Alert, NotifierProtocol, format_brand_digest
from mont_indirim_bot.services.rules import (
    cooldown_elapsed,
    drop_percent,
    mark_notified,
    rearm_if_recovered,
    record_measurement,
    trigger_reasons,
)
from mont_indirim_bot.storage import BrandSource, Store, from_iso, to_iso, utc_now

LOGGER = logging.getLogger(__name__)

__all__ = [
    "BRAND_RETENTION_DAYS",
    "SITE_NAME",
    "BrandOutcome",
    "BrandScanner",
    "ListingReader",
]

#: Name of the shop in the digest (the listing reader is Hepsiburada only).
SITE_NAME = "Hepsiburada"

#: Brand products that have not shown up for this long are forgotten, which
#: keeps `state.json` from growing forever with delisted products.
BRAND_RETENTION_DAYS = 60

#: Keywords applied per category. "jacket" keeps only outerwear; "any"
#: accepts everything the query already targets.
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {"jacket": JACKET_KEYWORDS}


class ListingReader(Protocol):
    """The part of the listing client the brand scan depends on."""

    async def fetch(self, url: str) -> str:
        """Download one listing page as HTML."""
        ...

    async def aclose(self) -> None:
        """Release the underlying connections."""
        ...


CheckStatus = Literal["ok", "error"]


@dataclass(slots=True)
class BrandOutcome:
    """Result of scanning one brand."""

    brand_id: str
    brand_name: str
    status: CheckStatus
    products: int = 0
    alerts: list[Alert] = field(default_factory=list)
    error: str | None = None

    @property
    def failed(self) -> bool:
        """`True` when the brand listing could not be read."""
        return self.status == "error"


class BrandScanner:
    """Reads the listings of the followed brands and digests their drops."""

    def __init__(
        self,
        *,
        store: Store,
        config: Config,
        client: ListingReader | None = None,
        notifier: NotifierProtocol | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """
        Args:
            store: Loaded store; also carries the brand configuration.
            config: Runtime configuration (thresholds, cooldown, delays).
            client: Listing reader; one is created (and closed) per pass when
                omitted.
            notifier: Sink for the digests; `None` disables sending (tests).
            clock: Injectable time source so cooldown logic is testable.
        """
        self._store = store
        self._config = config
        self._client = client
        self._notifier = notifier
        self._clock = clock

    async def run(self, *, send_alerts: bool = True) -> list[BrandOutcome]:
        """Scan every enabled brand once and return one outcome per brand."""
        brands = self._store.brands.active_brands()
        if not brands:
            LOGGER.info("Brand scan skipped: no active brand in the configuration")
            return []

        client = self._client or self._make_client()
        processed: set[str] = set()
        try:
            outcomes = [
                await self._scan_brand(client, brand, send_alerts=send_alerts, processed=processed)
                for brand in brands
            ]
        finally:
            if self._client is None:
                await client.aclose()

        self._forget_stale(self._clock())
        await self._store.save_state()

        alerts = sum(len(outcome.alerts) for outcome in outcomes)
        failures = sum(1 for outcome in outcomes if outcome.failed)
        LOGGER.info(
            "Scanned %d brand(s): %d alert(s), %d failure(s)",
            len(outcomes),
            alerts,
            failures,
        )
        return outcomes

    # -- internals --------------------------------------------------------- #
    def _make_client(self) -> ListingClient:
        return ListingClient(
            min_delay_s=self._config.request_min_delay_s,
            max_delay_s=self._config.request_max_delay_s,
            max_retries=self._config.max_retries,
            timeout_s=self._config.request_timeout_s,
            proxy=self._config.scraper_proxy,
        )

    async def _scan_brand(
        self,
        client: ListingReader,
        brand: BrandSource,
        *,
        send_alerts: bool,
        processed: set[str] | None = None,
    ) -> BrandOutcome:
        pages = self._store.brands.pages
        products: list[ListingProduct] = []
        try:
            for page in range(1, pages + 1):
                html = await client.fetch(build_listing_url(brand.query, page))
                found = parse_listing(html)
                if not found:
                    # An empty page means "no more results" (or new markup).
                    LOGGER.info("Brand %s: page %d had no readable product", brand.id, page)
                    break
                products.extend(found)
        except Exception as exc:  # one brand must never take the whole pass down
            message = f"{type(exc).__name__}: {exc}"
            LOGGER.warning("Brand %s could not be read: %s", brand.id, message)
            return BrandOutcome(
                brand_id=brand.id,
                brand_name=brand.name,
                status="error",
                error=message,
            )

        alerts = self._evaluate(brand, products, self._clock(), processed or set())
        if alerts and send_alerts and self._notifier is not None:
            sent = await self._notifier.send(format_brand_digest(brand.name, alerts))
            if not sent:
                LOGGER.warning("Could not send the %s digest", brand.id)
        return BrandOutcome(
            brand_id=brand.id,
            brand_name=brand.name,
            status="ok",
            products=len(products),
            alerts=alerts,
        )

    def _evaluate(
        self,
        brand: BrandSource,
        products: Sequence[ListingProduct],
        now: datetime,
        processed: set[str],
    ) -> list[Alert]:
        """Update the brand bookkeeping and return the discounts worth a message."""
        tokens = tuple(fold_turkish(token) for token in brand.match)
        keywords = CATEGORY_KEYWORDS.get(brand.category or "any", ())
        tracked = self._tracked_codes()
        budget = self._store.brands.budget
        alerts: list[Alert] = []
        seen: set[str] = set()

        for product in products:
            if product.code in seen or product.code in processed or product.code in tracked:
                continue
            seen.add(product.code)
            if keywords and not any(keyword in fold_turkish(product.name) for keyword in keywords):
                continue
            if tokens and not matches_brand(product.name, tokens):
                LOGGER.debug("%s: skipping another brand's product %s", brand.id, product.name)
                continue

            state = self._store.brand_state_for(product.code)
            previous_price = state.last_price
            processed.add(product.code)
            record_measurement(
                state,
                price=product.price,
                list_price=None,
                currency=DEFAULT_CURRENCY,
                in_stock=True,
                title=product.name,
                now=now,
            )
            if rearm_if_recovered(state, product.price, rearm_pct=self._config.rearm_pct):
                LOGGER.info("Brand product %s recovered; triggers re-armed", product.code)

            reasons = trigger_reasons(
                state,
                price=product.price,
                previous_price=previous_price,
                threshold_pct=0.0,  # any drop counts as "went on sale"
                target_price=None,
            )
            if not reasons:
                continue
            if budget is not None and product.price > budget:
                # The drop is real but still out of budget: keep the trigger
                # armed so a further fall is reported the moment it fits.
                LOGGER.info("Brand product %s is still above the budget", product.code)
                continue
            if not cooldown_elapsed(
                state, now, cooldown_seconds=self._config.cooldown_hours_seconds
            ):
                LOGGER.info("Brand product %s suppressed by the cooldown", product.code)
                continue

            alerts.append(
                Alert(
                    item_id=product.code,
                    title=product.name,
                    site=SITE_NAME,
                    url=product.absolute_url,
                    new_price=product.price,
                    old_price=previous_price,
                    currency=DEFAULT_CURRENCY,
                    discount_pct=drop_percent(previous_price, product.price),
                    target_price=budget,
                    lowest_price=state.lowest_price,
                    in_stock=True,
                    reasons=tuple(reasons),
                    detected_at=to_iso(now),
                )
            )
            mark_notified(state, price=product.price, reasons=reasons, now=now)
        return alerts

    def _tracked_codes(self) -> set[str]:
        """Product codes already covered by the watchlist (they alert on their own)."""
        codes: set[str] = set()
        for item in self._store.watchlist.items:
            code = product_id(item.url)
            if code:
                codes.add(code)
        return codes

    def _forget_stale(self, now: datetime) -> int:
        """Drop brand products that have not been seen for a long time."""
        cutoff = now - timedelta(days=BRAND_RETENTION_DAYS)
        stale = [
            code
            for code, state in self._store.state.brand_items.items()
            if (seen := from_iso(state.last_checked_at)) is not None and seen < cutoff
        ]
        for code in stale:
            del self._store.state.brand_items[code]
        if stale:
            LOGGER.info("Forgot %d brand product(s) not seen since %s", len(stale), cutoff.date())
        return len(stale)
