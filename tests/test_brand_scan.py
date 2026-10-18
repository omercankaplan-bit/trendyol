"""Tests for the brand level scan (`services.brand_scan`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mont_indirim_bot.scrapers.hepsiburada_listing import ListingProduct
from mont_indirim_bot.services.brand_scan import BRAND_RETENTION_DAYS, BrandScanner, ListingReader
from mont_indirim_bot.services.notifier import Alert, NotifierProtocol, format_brand_digest
from mont_indirim_bot.storage import (
    BrandScanConfig,
    BrandSource,
    ItemState,
    Store,
    WatchItem,
    Watchlist,
    to_iso,
)
from tests.helpers import FakeClock, RecordingNotifier, assert_markdownv2_safe, make_config

AVVA = BrandSource(
    id="avva", name="AVVA", query="avva erkek mont", match=("avva",), category="jacket"
)
LUFIAN = BrandSource(id="lufian", name="Lufian", query="lufian erkek mont", match=("lufian",))


def listing_card(code: str, name: str, price: str) -> str:
    """One Hepsiburada product card, in the markup the parser expects."""
    return (
        '<article class="productCard-module_article__x">'
        f'<a title="{name}" href="/urun-{code.lower()}-p-{code}">'
        '<h2 class="title-module_titleRoot__z">baslik</h2></a>'
        f'<div class="price-module_finalPrice__y" data-test-id="final-price-1">{price}'
        '<span class="price-module_finalPriceFraction__z">,00 TL</span></div>'
        "</article>"
    )


def listing_page(*cards: str) -> str:
    """A listing page carrying `cards`."""
    return "<html><body>" + "".join(cards) + "</body></html>"


class FakeListingClient:
    """`ListingReader` that serves scripted pages (or raises)."""

    def __init__(self, pages: list[str] | Exception) -> None:
        self.pages = pages
        self.calls: list[str] = []
        self.closed = False

    async def fetch(self, url: str) -> str:
        self.calls.append(url)
        if isinstance(self.pages, Exception):
            raise self.pages
        index = min(len(self.calls) - 1, len(self.pages) - 1)
        return self.pages[index]

    async def aclose(self) -> None:
        self.closed = True


def make_store(
    tmp_path: Path, *, brands: list[BrandSource] | None = None, budget: float | None = 10_000.0
) -> Store:
    """An in-memory store with the brand configuration set."""
    store = Store(tmp_path / "data")
    store.brands = BrandScanConfig(
        budget=budget,
        pages=1,
        brands=[AVVA] if brands is None else brands,
    )
    return store


def make_scanner(
    store: Store,
    *,
    client: ListingReader,
    notifier: NotifierProtocol | None = None,
    clock: Any = None,
) -> BrandScanner:
    """A scanner wired to the fakes, with no request delays."""
    config = make_config(store.data_dir)
    return BrandScanner(
        store=store,
        config=config,
        client=client,
        notifier=notifier or RecordingNotifier(),
        clock=clock or FakeClock(),
    )


async def test_first_sighting_is_remembered_without_alerting(tmp_path):
    store = make_store(tmp_path)
    client = FakeListingClient([listing_page(listing_card("HBCV0000000001", "AVVA Mont", "4.499"))])
    scanner = make_scanner(store, client=client)

    outcomes = await scanner.run()

    assert [outcome.status for outcome in outcomes] == ["ok"]
    assert outcomes[0].products == 1
    assert outcomes[0].alerts == []
    assert store.state.brand_items["HBCV0000000001"].last_price == 4499.0
    assert client.closed is False


async def test_small_drop_is_reported_too(tmp_path):
    store = make_store(tmp_path)
    client = FakeListingClient(
        [
            listing_page(listing_card("HBCV0000000001", "AVVA Mont", "4.999")),
            listing_page(listing_card("HBCV0000000001", "AVVA Mont", "4.899")),
        ]
    )
    notifier = RecordingNotifier()
    scanner = make_scanner(store, client=client, notifier=notifier)

    assert (await scanner.run())[0].alerts == []
    second = await scanner.run()

    assert len(second[0].alerts) == 1
    assert second[0].alerts[0].new_price == 4899.0
    store = make_store(tmp_path)
    client = FakeListingClient(
        [
            listing_page(listing_card("HBCV0000000001", "AVVA Mont", "4.999")),
            listing_page(listing_card("HBCV0000000001", "AVVA Mont", "3.499")),
        ]
    )
    notifier = RecordingNotifier()
    scanner = make_scanner(store, client=client, notifier=notifier)

    assert (await scanner.run())[0].alerts == []
    second = await scanner.run()

    assert len(second[0].alerts) == 1
    alert = second[0].alerts[0]
    assert alert.old_price == 4999.0
    assert alert.new_price == 3499.0
    assert alert.discount_pct is not None and round(alert.discount_pct, 1) == 30.0
    assert alert.url == "https://www.hepsiburada.com/urun-hbcv0000000001-p-HBCV0000000001"
    assert len(notifier.messages) == 1
    assert "AVVA" in notifier.messages[0]


async def test_second_drop_within_the_cooldown_is_suppressed(tmp_path):
    store = make_store(tmp_path)
    client = FakeListingClient(
        [
            listing_page(listing_card("HBCV0000000001", "AVVA Mont", "4.999")),
            listing_page(listing_card("HBCV0000000001", "AVVA Mont", "3.499")),
            listing_page(listing_card("HBCV0000000001", "AVVA Mont", "2.499")),
        ]
    )
    clock = FakeClock()
    notifier = RecordingNotifier()
    scanner = make_scanner(store, client=client, notifier=notifier, clock=clock)

    await scanner.run()
    clock.advance(hours=13)
    await scanner.run()
    clock.advance(minutes=30)
    third = await scanner.run()

    assert third[0].alerts == []
    assert len(notifier.messages) == 1


async def test_drops_outside_the_budget_are_not_reported(tmp_path):
    store = make_store(tmp_path, budget=10_000.0)
    client = FakeListingClient(
        [
            listing_page(listing_card("HBCV0000000001", "AVVA Mont", "20.000")),
            listing_page(listing_card("HBCV0000000001", "AVVA Mont", "14.000")),
        ]
    )
    notifier = RecordingNotifier()
    scanner = make_scanner(store, client=client, notifier=notifier)

    await scanner.run()
    second = await scanner.run()

    assert second[0].alerts == []
    assert notifier.messages == []
    state = store.state.brand_items["HBCV0000000001"]
    assert state.last_price == 14000.0
    assert state.armed_drop is True


async def test_products_on_the_watchlist_are_left_to_the_watchlist(tmp_path):
    store = make_store(tmp_path)
    store.watchlist = Watchlist(
        items=[
            WatchItem(
                id="aaaaaa",
                site="Hepsiburada",
                url="https://www.hepsiburada.com/urun-p-HBCV0000000001",
                title="AVVA Mont",
            )
        ]
    )
    client = FakeListingClient(
        [
            listing_page(listing_card("HBCV0000000001", "AVVA Mont", "4.999")),
            listing_page(listing_card("HBCV0000000001", "AVVA Mont", "1.999")),
        ]
    )
    scanner = make_scanner(store, client=client)

    await scanner.run()
    second = await scanner.run()

    assert second[0].alerts == []
    assert "HBCV0000000001" not in store.state.brand_items


async def test_other_brands_and_other_garments_are_skipped(tmp_path):
    store = make_store(tmp_path)
    client = FakeListingClient(
        [
            listing_page(
                listing_card("HBCV0000000001", "Lufian Kaz Tüyü Mont", "3.999"),
                listing_card("HBCV0000000002", "AVVA Erkek Basic Sweatshirt", "899"),
            )
        ]
    )
    scanner = make_scanner(store, client=client)

    outcomes = await scanner.run()

    assert outcomes[0].products == 2
    assert store.state.brand_items == {}


async def test_a_failing_brand_does_not_break_the_other_brands(tmp_path):
    store = make_store(tmp_path, brands=[AVVA, LUFIAN])
    client = FakeListingClient(RuntimeError("403"))
    scanner = make_scanner(store, client=client)

    outcomes = await scanner.run()

    assert [outcome.status for outcome in outcomes] == ["error", "error"]
    assert outcomes[0].error is not None and "RuntimeError" in outcomes[0].error


async def test_scan_is_skipped_when_no_brand_is_active(tmp_path):
    store = make_store(tmp_path, brands=[])
    client = FakeListingClient([listing_page()])
    scanner = make_scanner(store, client=client)

    outcomes = await scanner.run()

    assert outcomes == []
    assert client.calls == []


async def test_watchlist_only_store_leaves_the_listing_client_alone(tmp_path):
    store = make_store(tmp_path, brands=[])
    client = FakeListingClient([listing_page()])

    outcomes = await make_scanner(store, client=client).run()

    assert outcomes == []
    assert client.closed is False


async def test_stale_brand_products_are_forgotten(tmp_path):
    store = make_store(tmp_path)
    old = FakeClock().now - timedelta(days=BRAND_RETENTION_DAYS + 1)
    store.state.brand_items["HBCV0000000009"] = ItemState(
        last_price=1.0, last_checked_at=to_iso(old)
    )
    client = FakeListingClient([listing_page(listing_card("HBCV0000000001", "AVVA Mont", "4.499"))])
    scanner = make_scanner(store, client=client)

    await scanner.run()

    assert "HBCV0000000009" not in store.state.brand_items
    assert "HBCV0000000001" in store.state.brand_items


async def test_a_missing_price_never_reaches_the_alert(tmp_path):
    store = make_store(tmp_path)
    client = FakeListingClient(
        [
            listing_page(listing_card("HBCV0000000001", "AVVA Mont", "4.999")),
            listing_page(
                '<article class="x"><a title="AVVA Mont" href="/a-p-HBCV0000000001"></a></article>'
            ),
        ]
    )
    scanner = make_scanner(store, client=client)

    await scanner.run()
    second = await scanner.run()

    assert second[0].products == 0
    assert second[0].alerts == []
    assert store.state.brand_items["HBCV0000000001"].last_price == 4999.0


def test_digest_is_markdown_safe_and_lists_every_drop():
    empty = format_brand_digest("AVVA", [])
    assert "0 indirim" in empty

    digest = format_brand_digest(
        "AVVA",
        [
            Alert(
                item_id="HBCV1",
                title="AVVA Erkek Şişme Mont (indirim!)",
                site="Hepsiburada",
                url="https://www.hepsiburada.com/x-p-HBCV1",
                new_price=3499.0,
                old_price=4999.0,
                discount_pct=30.0,
                detected_at=to_iso(datetime(2026, 10, 18, 12, 0, tzinfo=UTC)),
            )
        ],
    )

    assert "AVVA" in digest
    assert "3\\.499,00 TL" in digest
    assert "4.999,00 TL" in digest
    assert "Eski fiyat" in digest
    assert "Ürüne git" in digest
    assert_markdownv2_safe(digest)


def test_listing_products_are_compared_by_code():
    product = ListingProduct(code="HBCV1", name="AVVA Mont", url="/x-p-HBCV1", price=1.0)

    assert product.code == "HBCV1"
