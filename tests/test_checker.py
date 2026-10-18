"""Tests for the discount trigger state machine and the notification rules."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from mont_indirim_bot.config import Config
from mont_indirim_bot.scrapers import ScraperError, ScraperFetchError
from mont_indirim_bot.services.price_checker import (
    ERROR_ALERT_THRESHOLD,
    CheckOutcome,
    PriceChecker,
    drop_percent,
)
from mont_indirim_bot.storage import (
    BotState,
    ItemState,
    Store,
    WatchItem,
    from_iso,
    to_iso,
    utc_now,
)
from tests.helpers import FakeClock, FakeScraper, RecordingNotifier, make_config, price_result

ITEM_ID = "aaa111"


def build_store(tmp_path: Path, item: WatchItem, state: ItemState | None = None) -> Store:
    """Store holding exactly one watchlist item (plus optional state)."""
    store = Store(tmp_path)
    store.watchlist.add(item)
    if state is not None:
        store.state.items[item.id] = state
    return store


async def check(
    store: Store,
    scraper: FakeScraper,
    *,
    notifier: RecordingNotifier | None = None,
    clock: FakeClock | None = None,
    config: Config | None = None,
    item_ids: list[str] | None = None,
    send_alerts: bool = True,
) -> list[CheckOutcome]:
    checker = PriceChecker(
        store=store,
        config=config or make_config(store.data_dir),
        scrapers=[scraper],
        notifier=notifier,
        clock=clock or utc_now,
    )
    return await checker.run(item_ids=item_ids, send_alerts=send_alerts)


# --------------------------------------------------------------------------- #
# Trigger matrix
# --------------------------------------------------------------------------- #
async def test_first_sample_records_state_without_alerting(tmp_path: Path, watch_item: WatchItem):
    notifier = RecordingNotifier()
    store = build_store(tmp_path, watch_item)

    outcomes = await check(store, FakeScraper([price_result(1000.0)]), notifier=notifier)

    assert [outcome.status for outcome in outcomes] == ["ok"]
    assert notifier.alerts == []
    state = store.state.items[ITEM_ID]
    assert state.last_price == 1000.0
    assert state.lowest_price == 1000.0
    assert state.highest_price == 1000.0
    assert state.consecutive_errors == 0
    assert len(store.history_for(ITEM_ID)) == 1


async def test_price_below_target_notifies(tmp_path: Path, watch_item: WatchItem):
    watch_item.target_price = 900.0
    notifier = RecordingNotifier()
    store = build_store(tmp_path, watch_item, ItemState(last_price=1000.0))

    await check(store, FakeScraper([price_result(899.0)]), notifier=notifier)

    assert len(notifier.alerts) == 1
    alert = notifier.alerts[0]
    assert alert.item_id == ITEM_ID
    assert alert.reasons == ("target",)
    assert alert.old_price == 1000.0
    assert alert.new_price == 899.0
    assert alert.target_price == 900.0
    assert alert.discount_pct == pytest.approx(10.1)
    assert alert.site == "Fake Site"
    state = store.state.items[ITEM_ID]
    assert state.armed_target is False
    assert state.last_notified_price == 899.0
    assert state.last_notified_reasons == ["target"]


async def test_large_drop_notifies(tmp_path: Path, watch_item: WatchItem):
    notifier = RecordingNotifier()
    store = build_store(tmp_path, watch_item, ItemState(last_price=1000.0))

    await check(store, FakeScraper([price_result(790.0)]), notifier=notifier)

    assert len(notifier.alerts) == 1
    alert = notifier.alerts[0]
    assert alert.reasons == ("drop",)
    assert alert.discount_pct == pytest.approx(21.0)
    assert store.state.items[ITEM_ID].armed_drop is False


async def test_small_drop_and_no_target_does_not_notify(tmp_path: Path, watch_item: WatchItem):
    notifier = RecordingNotifier()
    store = build_store(tmp_path, watch_item, ItemState(last_price=1000.0))

    await check(store, FakeScraper([price_result(850.0)]), notifier=notifier)

    assert notifier.alerts == []
    assert store.state.items[ITEM_ID].last_price == 850.0


async def test_both_triggers_collapse_into_one_alert(tmp_path: Path, watch_item: WatchItem):
    watch_item.target_price = 900.0
    notifier = RecordingNotifier()
    store = build_store(tmp_path, watch_item, ItemState(last_price=1000.0))

    await check(store, FakeScraper([price_result(750.0)]), notifier=notifier)

    assert len(notifier.alerts) == 1
    assert set(notifier.alerts[0].reasons) == {"target", "drop"}


async def test_item_level_threshold_overrides_the_default(tmp_path: Path, watch_item: WatchItem):
    watch_item.min_discount_pct = 10.0
    notifier = RecordingNotifier()
    store = build_store(tmp_path, watch_item, ItemState(last_price=1000.0))

    await check(store, FakeScraper([price_result(890.0)]), notifier=notifier)

    assert [alert.reasons for alert in notifier.alerts] == [("drop",)]


async def test_the_same_discount_is_not_reported_twice(tmp_path: Path, watch_item: WatchItem):
    watch_item.target_price = 900.0
    notifier = RecordingNotifier()
    clock = FakeClock()
    store = build_store(tmp_path, watch_item, ItemState(last_price=1000.0))
    scraper = FakeScraper([price_result(890.0)])

    await check(store, scraper, notifier=notifier, clock=clock)
    clock.advance(hours=48)  # cooldown elapsed: only the re-arm rule can suppress
    await check(store, scraper, notifier=notifier, clock=clock)

    assert len(notifier.alerts) == 1


async def test_notification_repeats_only_after_a_recovery(tmp_path: Path, watch_item: WatchItem):
    watch_item.target_price = 900.0
    notifier = RecordingNotifier()
    clock = FakeClock()
    store = build_store(tmp_path, watch_item, ItemState(last_price=1000.0))
    scraper = FakeScraper([price_result(890.0), price_result(950.0), price_result(880.0)])

    await check(store, scraper, notifier=notifier, clock=clock)  # alert
    clock.advance(hours=24)
    await check(store, scraper, notifier=notifier, clock=clock)  # price recovered
    clock.advance(hours=24)
    await check(store, scraper, notifier=notifier, clock=clock)  # falls again

    assert len(notifier.alerts) == 2
    assert notifier.alerts[1].new_price == 880.0


# --------------------------------------------------------------------------- #
# Cooldown
# --------------------------------------------------------------------------- #
async def test_cooldown_suppresses_a_second_alert_and_keeps_it_armed(
    tmp_path: Path, watch_item: WatchItem
):
    watch_item.target_price = 900.0
    notifier = RecordingNotifier()
    clock = FakeClock()
    state = ItemState(
        last_price=920.0,
        last_notified_at=to_iso(clock.now - timedelta(hours=1)),
        last_notified_price=950.0,
    )
    store = build_store(tmp_path, watch_item, state)

    await check(store, FakeScraper([price_result(890.0)]), notifier=notifier, clock=clock)

    assert notifier.alerts == []
    assert store.state.items[ITEM_ID].armed_target is True
    assert from_iso(store.state.items[ITEM_ID].last_notified_at) == clock.now - timedelta(hours=1)


async def test_alert_fires_once_the_cooldown_expired(tmp_path: Path, watch_item: WatchItem):
    watch_item.target_price = 900.0
    notifier = RecordingNotifier()
    clock = FakeClock()
    state = ItemState(
        last_price=920.0,
        last_notified_at=to_iso(clock.now - timedelta(hours=13)),
        last_notified_price=950.0,
    )
    store = build_store(tmp_path, watch_item, state)

    await check(store, FakeScraper([price_result(890.0)]), notifier=notifier, clock=clock)

    assert len(notifier.alerts) == 1
    assert from_iso(store.state.items[ITEM_ID].last_notified_at) == clock.now


async def test_send_alerts_false_still_updates_the_bookkeeping(
    tmp_path: Path, watch_item: WatchItem
):
    watch_item.target_price = 900.0
    notifier = RecordingNotifier()
    store = build_store(tmp_path, watch_item, ItemState(last_price=1000.0))

    outcomes = await check(
        store, FakeScraper([price_result(899.0)]), notifier=notifier, send_alerts=False
    )

    assert notifier.alerts == []
    assert len(outcomes[0].alerts) == 1
    assert store.state.items[ITEM_ID].last_notified_at is not None


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #
async def test_repeated_failures_warn_the_owner_only_once(tmp_path: Path, watch_item: WatchItem):
    notifier = RecordingNotifier()
    store = build_store(tmp_path, watch_item)
    scraper = FakeScraper([ScraperFetchError("HTTP 403")])

    outcomes: list[CheckOutcome] = []
    for _ in range(ERROR_ALERT_THRESHOLD + 2):
        outcomes = await check(store, scraper, notifier=notifier)

    assert len(notifier.warnings) == 1
    item_id, _title, error, attempts = notifier.warnings[0]
    assert item_id == ITEM_ID
    assert "403" in error
    assert attempts == ERROR_ALERT_THRESHOLD
    assert outcomes[0].status == "error"
    state = store.state.items[ITEM_ID]
    assert state.consecutive_errors == ERROR_ALERT_THRESHOLD + 2
    assert state.error_alerted is True
    assert state.last_error is not None


async def test_a_success_resets_the_failure_counter(tmp_path: Path, watch_item: WatchItem):
    notifier = RecordingNotifier()
    store = build_store(tmp_path, watch_item)
    scraper = FakeScraper(
        [ScraperFetchError("HTTP 429"), ScraperFetchError("HTTP 429"), price_result(1000.0)]
    )

    await check(store, scraper, notifier=notifier)
    await check(store, scraper, notifier=notifier)
    await check(store, scraper, notifier=notifier)

    state = store.state.items[ITEM_ID]
    assert state.consecutive_errors == 0
    assert state.error_alerted is False
    assert state.last_error is None
    assert state.last_price == 1000.0


async def test_unexpected_exception_is_contained(tmp_path: Path, watch_item: WatchItem):
    notifier = RecordingNotifier()
    store = build_store(tmp_path, watch_item)
    scraper = FakeScraper([RuntimeError("boom")])

    outcomes = await check(store, scraper, notifier=notifier)

    assert outcomes[0].status == "error"
    assert outcomes[0].error is not None and "RuntimeError" in outcomes[0].error
    assert store.state.items[ITEM_ID].consecutive_errors == 1


async def test_unsupported_url_is_recorded_as_an_error(tmp_path: Path, watch_item: WatchItem):
    watch_item.url = "https://www.amazon.com.tr/mont-p-1"
    store = build_store(tmp_path, watch_item)

    outcomes = await check(store, FakeScraper([price_result(1000.0)]))

    assert outcomes[0].status == "error"
    assert outcomes[0].error == "unsupported site for this URL"


async def test_scraper_error_base_class_is_handled(tmp_path: Path, watch_item: WatchItem):
    store = build_store(tmp_path, watch_item)

    outcomes = await check(store, FakeScraper([ScraperError("no price found in the HTML")]))

    assert outcomes[0].status == "error"
    assert outcomes[0].error is not None and "no price found" in outcomes[0].error


# --------------------------------------------------------------------------- #
# Bookkeeping
# --------------------------------------------------------------------------- #
async def test_history_only_records_price_changes(tmp_path: Path, watch_item: WatchItem):
    store = build_store(tmp_path, watch_item)
    scraper = FakeScraper([price_result(1000.0), price_result(1000.0), price_result(900.0)])

    await check(store, scraper)
    await check(store, scraper)
    await check(store, scraper)

    assert [entry.price for entry in store.history_for(ITEM_ID)] == [1000.0, 900.0]


async def test_lowest_and_highest_prices_are_tracked(tmp_path: Path, watch_item: WatchItem):
    store = build_store(tmp_path, watch_item)
    scraper = FakeScraper([price_result(1000.0), price_result(900.0), price_result(1100.0)])

    await check(store, scraper)
    await check(store, scraper)
    await check(store, scraper)

    state = store.state.items[ITEM_ID]
    assert state.lowest_price == 900.0
    assert state.highest_price == 1100.0


async def test_missing_title_is_backfilled_and_saved(tmp_path: Path, watch_item: WatchItem):
    watch_item.title = ""
    store = build_store(tmp_path, watch_item)

    await check(store, FakeScraper([price_result(1000.0)]))

    assert watch_item.title == "Columbia Erkek Pike Lake Mont"
    assert store.state.items[ITEM_ID].title == "Columbia Erkek Pike Lake Mont"
    reloaded = await Store(tmp_path).load()
    assert reloaded.watchlist.items[0].title == "Columbia Erkek Pike Lake Mont"


async def test_out_of_stock_is_stored(tmp_path: Path, watch_item: WatchItem):
    store = build_store(tmp_path, watch_item)

    await check(store, FakeScraper([price_result(1000.0, in_stock=False)]))

    assert store.state.items[ITEM_ID].in_stock is False
    assert store.history_for(ITEM_ID)[0].in_stock is False


async def test_run_can_target_a_single_item(tmp_path: Path, watch_item: WatchItem):
    store = build_store(tmp_path, watch_item)
    other = store.watchlist.add(
        WatchItem(id="bbb222", url="https://shop.example/other-p-2", site="fake")
    )
    scraper = FakeScraper([price_result(1000.0)])

    outcomes = await check(store, scraper, item_ids=[other.id])

    assert [outcome.item_id for outcome in outcomes] == ["bbb222"]
    assert scraper.calls == [other.url]


async def test_empty_watchlist_returns_no_outcomes(tmp_path: Path, watch_item: WatchItem):
    del watch_item
    store = Store(tmp_path)

    assert await check(store, FakeScraper([price_result(1000.0)])) == []


async def test_state_is_written_to_disk_after_every_item(tmp_path: Path, watch_item: WatchItem):
    store = build_store(tmp_path, watch_item)

    await check(store, FakeScraper([price_result(1234.0)]))

    reloaded = await Store(tmp_path).load()
    assert reloaded.state.items[ITEM_ID].last_price == 1234.0
    assert reloaded.state.items[ITEM_ID].last_checked_at is not None


async def test_error_state_is_written_to_disk(tmp_path: Path, watch_item: WatchItem):
    store = build_store(tmp_path, watch_item)

    await check(store, FakeScraper([ScraperFetchError("HTTP 403")]))

    reloaded = await Store(tmp_path).load()
    assert reloaded.state.items[ITEM_ID].consecutive_errors == 1
    assert reloaded.state.items[ITEM_ID].last_error is not None


def test_bot_state_defaults_are_sane():
    state = BotState()
    assert state.telegram_offset == 0
    assert state.items == {} and state.history == {}


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (1000.0, 800.0, 20.0),
        (1000.0, 1000.0, None),
        (1000.0, 1100.0, None),
        (None, 800.0, None),
        (0.0, 800.0, None),
    ],
)
def test_drop_percent(old, new, expected):
    assert drop_percent(old, new) == expected
