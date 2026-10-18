"""Tests for the Telegram command grammar and the command processor."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from mont_indirim_bot.scrapers import ScraperError
from mont_indirim_bot.services.commands import Command, CommandProcessor, parse_command
from mont_indirim_bot.services.notifier import md_escape
from mont_indirim_bot.services.price_checker import PriceChecker
from mont_indirim_bot.storage import ItemState, Store, WatchItem
from mont_indirim_bot.telegram_client import TelegramClient
from tests.helpers import (
    FakeClock,
    FakeScraper,
    RecordingNotifier,
    make_client,
    make_config,
    price_result,
)

FAKE_URL = "https://shop.example/pike-lake-mont-p-1"


@pytest.mark.parametrize(
    ("text", "expected_name", "expected_args"),
    [
        ("/start", "yardim", ()),
        ("/yardim", "yardim", ()),
        ("/HELP", "yardim", ()),
        ("  /liste  ", "liste", ()),
        ("/ekle https://shop.example/x-p-1 1500", "ekle", ("https://shop.example/x-p-1", "1500")),
        ("/ekle@MontBot https://shop.example/x-p-1", "ekle", ("https://shop.example/x-p-1",)),
        ("/sil aaa111", "sil", ("aaa111",)),
        ("/bilinmeyen komut", "bilinmeyen", ("komut",)),
    ],
)
def test_parse_command(text, expected_name, expected_args):
    command = parse_command(text)

    assert command is not None
    assert command.name == expected_name
    assert command.args == expected_args


@pytest.mark.parametrize("text", [None, 42, "", "merhaba", "/", "   ", "@bot /liste"])
def test_parse_command_rejects_non_commands(text):
    assert parse_command(text) is None


def build_processor(
    store: Store,
    *,
    script: list[Any] | None = None,
    config=None,
    clock: FakeClock | None = None,
) -> tuple[CommandProcessor, FakeScraper, RecordingNotifier]:
    cfg = config or make_config(store.data_dir)
    time_source = clock or FakeClock()
    scraper = FakeScraper(script or [price_result(1299.0)])
    notifier = RecordingNotifier()
    checker = PriceChecker(
        store=store, config=cfg, scrapers=[scraper], notifier=notifier, clock=time_source
    )
    processor = CommandProcessor(
        store=store,
        config=cfg,
        checker=checker,
        notifier=notifier,
        scrapers=[scraper],
        clock=time_source,
    )
    return processor, scraper, notifier


@pytest.fixture
def processor(store: Store) -> CommandProcessor:
    processor, _scraper, _notifier = build_processor(store)
    return processor


def owner_update(text: str, update_id: int = 1, chat_id: int = 42) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {"message_id": 5, "chat": {"id": chat_id}, "text": text},
    }


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
async def test_help_lists_the_commands(processor: CommandProcessor):
    reply = await processor.handle_command(Command(name="yardim"))

    assert "/ekle" in reply
    assert "/kontrol" in reply
    assert "Fake Site" in reply


async def test_unknown_command_is_answered_with_a_hint(processor: CommandProcessor):
    reply = await processor.handle_command(Command(name="zart"))

    assert "Bilinmeyen komut" in reply
    assert "/yardim" in reply


async def test_list_reports_an_empty_watchlist(processor: CommandProcessor):
    reply = await processor.handle_command(Command(name="liste"))

    assert "boş" in reply


async def test_list_shows_items_with_prices(store: Store):
    store.watchlist.add(
        WatchItem(id="aaa111", url=FAKE_URL, site="fake", title="Columbia Pike Lake Mont")
    )
    store.state.items["aaa111"] = ItemState(
        last_price=1000.0, lowest_price=900.0, highest_price=1200.0
    )
    processor, _scraper, _notifier = build_processor(store)

    reply = await processor.handle_command(Command(name="liste"))

    assert "aaa111" in reply
    assert "Columbia Pike Lake Mont" in reply
    assert "1.000,00 TL" in reply
    assert "900,00 TL" in reply


async def test_add_requires_an_argument(processor: CommandProcessor):
    assert "Kullanım" in await processor.handle_command(Command(name="ekle"))


async def test_add_rejects_a_non_http_link(processor: CommandProcessor):
    reply = await processor.handle_command(Command(name="ekle", args=("ftp://shop.example/x",)))

    assert "http" in reply


async def test_add_rejects_an_unsupported_site(processor: CommandProcessor):
    reply = await processor.handle_command(
        Command(name="ekle", args=("https://www.amazon.com.tr/mont-p-1",))
    )

    assert "desteklemiyorum" in reply
    assert "Fake Site" in reply


async def test_add_rejects_an_unparsable_target(processor: CommandProcessor):
    reply = await processor.handle_command(Command(name="ekle", args=(FAKE_URL, "bedava")))

    assert "anlayamadım" in reply


async def test_add_stores_the_item_with_the_target_price(store: Store):
    processor, _scraper, _notifier = build_processor(store, script=[price_result(1299.0)])

    reply = await processor.handle_command(Command(name="ekle", args=(FAKE_URL, "999,90")))

    assert len(store.watchlist.items) == 1
    item = store.watchlist.items[0]
    assert item.url == FAKE_URL
    assert item.site == "fake"
    assert item.target_price == 999.9
    assert item.title == "Columbia Erkek Pike Lake Mont"
    assert item.id in reply
    assert "Takibe alındı" in reply
    assert store.state.items[item.id].last_price == 1299.0
    assert len(store.history_for(item.id)) == 1

    reloaded = await Store(store.data_dir).load()
    assert reloaded.watchlist.items[0].target_price == 999.9


async def test_add_warns_when_the_price_is_already_below_the_target(store: Store):
    processor, _scraper, _notifier = build_processor(store, script=[price_result(1299.0)])

    reply = await processor.handle_command(Command(name="ekle", args=(FAKE_URL, "1300")))

    assert "hedefinin altında" in reply


async def test_add_keeps_the_item_when_the_prefetch_fails(store: Store):
    processor, _scraper, _notifier = build_processor(store, script=[ScraperError("HTTP 403")])

    reply = await processor.handle_command(Command(name="ekle", args=(FAKE_URL,)))

    assert len(store.watchlist.items) == 1
    assert store.watchlist.items[0].title == ""
    assert "okunamadı" in reply


async def test_add_refuses_duplicates(store: Store):
    store.watchlist.add(WatchItem(id="aaa111", url=FAKE_URL, site="fake"))
    processor, _scraper, _notifier = build_processor(store)

    reply = await processor.handle_command(Command(name="ekle", args=(FAKE_URL + "?x=1",)))

    assert "zaten takipte" in reply
    assert len(store.watchlist.items) == 1


async def test_remove_deletes_item_and_state(store: Store):
    store.watchlist.add(WatchItem(id="aaa111", url=FAKE_URL, site="fake", title="Mont"))
    store.state.items["aaa111"] = ItemState(last_price=1000.0)
    processor, _scraper, _notifier = build_processor(store)

    reply = await processor.handle_command(Command(name="sil", args=("aaa111",)))

    assert "çıkarıldı" in reply
    assert store.watchlist.items == []
    assert store.state.items == {}
    reloaded = await Store(store.data_dir).load()
    assert reloaded.watchlist.items == []


async def test_remove_reports_an_unknown_id(processor: CommandProcessor):
    assert "yok" in await processor.handle_command(Command(name="sil", args=("nope",)))
    assert "Kullanım" in await processor.handle_command(Command(name="sil"))


async def test_threshold_updates_the_target_and_rearms(store: Store):
    store.watchlist.add(WatchItem(id="aaa111", url=FAKE_URL, site="fake"))
    store.state.items["aaa111"] = ItemState(last_price=2000.0, armed_target=False)
    processor, _scraper, _notifier = build_processor(store)

    reply = await processor.handle_command(Command(name="esik", args=("aaa111", "1500")))

    assert md_escape("1.500,00 TL") in reply
    assert store.watchlist.items[0].target_price == 1500.0
    assert store.state.items["aaa111"].armed_target is True
    assert store.state.items["aaa111"].last_price == 2000.0
    assert "Uyarı" not in reply  # 2000 > 1500, no immediate notification


async def test_threshold_warns_when_already_below(store: Store):
    store.watchlist.add(WatchItem(id="aaa111", url=FAKE_URL, site="fake"))
    store.state.items["aaa111"] = ItemState(last_price=1000.0)
    processor, _scraper, _notifier = build_processor(store)

    reply = await processor.handle_command(Command(name="esik", args=("aaa111", "1500")))

    assert "Uyarı" in reply


async def test_threshold_validates_arguments(store: Store):
    store.watchlist.add(WatchItem(id="aaa111", url=FAKE_URL, site="fake"))
    processor, _scraper, _notifier = build_processor(store)

    assert "Kullanım" in await processor.handle_command(Command(name="esik", args=("aaa111",)))
    assert "yok" in await processor.handle_command(Command(name="esik", args=("nope", "10")))
    assert "anlayamadım" in await processor.handle_command(
        Command(name="esik", args=("aaa111", "abc"))
    )


async def test_history_renders_the_recorded_prices(store: Store):
    processor, _scraper, _notifier = build_processor(store, script=[price_result(1299.0)])
    await processor.handle_command(Command(name="ekle", args=(FAKE_URL,)))
    item_id = store.watchlist.items[0].id

    reply = await processor.handle_command(Command(name="gecmis", args=(item_id,)))

    assert "1.299,00 TL" in reply
    assert "TARİH" in reply


async def test_history_reports_missing_data(processor: CommandProcessor):
    assert "Kullanım" in await processor.handle_command(Command(name="gecmis"))
    assert "yok" in await processor.handle_command(Command(name="gecmis", args=("nope",)))


async def test_check_reports_an_empty_watchlist(processor: CommandProcessor):
    assert "Takip listesi boş" in await processor.handle_command(Command(name="kontrol"))
    assert processor.did_run_check is False


async def test_check_summarises_the_scan(store: Store):
    store.watchlist.add(WatchItem(id="aaa111", url=FAKE_URL, site="fake", title="Mont"))
    store.state.items["aaa111"] = ItemState(last_price=2000.0)
    processor, _scraper, notifier = build_processor(store, script=[price_result(1000.0)])

    reply = await processor.handle_command(Command(name="kontrol"))

    assert "Kontrol tamamlandı" in reply
    assert "1 ürün tarandı" in reply
    assert "1 indirim bulundu" in reply
    assert len(notifier.alerts) == 1
    assert processor.did_run_check is True


async def test_check_reports_failures(store: Store):
    store.watchlist.add(WatchItem(id="aaa111", url=FAKE_URL, site="fake", title="Mont"))
    processor, _scraper, _notifier = build_processor(store, script=[ScraperError("HTTP 403")])

    reply = await processor.handle_command(Command(name="kontrol"))

    assert "1 hatalı" in reply
    assert "Hatalar" in reply


# --------------------------------------------------------------------------- #
# Update handling
# --------------------------------------------------------------------------- #
async def test_owner_commands_are_answered(store: Store):
    processor, _scraper, notifier = build_processor(store)

    reply = await processor.handle_update(owner_update("/liste"))

    assert reply is not None
    assert notifier.messages == [reply]


@pytest.mark.parametrize(
    "update",
    [
        owner_update("/liste", chat_id=999),
        {"update_id": 1, "callback_query": {"id": "1"}},
        {"update_id": 1, "message": {"chat": {"id": 42}}},
        {"update_id": 1, "message": {"chat": {"id": 42}, "text": "selam"}},
        {"update_id": 1},
    ],
)
async def test_other_updates_are_ignored(store: Store, update):
    processor, _scraper, notifier = build_processor(store)

    assert await processor.handle_update(update) is None
    assert notifier.messages == []


async def test_poll_once_consumes_updates_and_stores_the_offset(store: Store):
    processor, _scraper, notifier = build_processor(store)
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path.endswith("/getUpdates"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        owner_update("/liste", update_id=100),
                        owner_update("/liste", update_id=101, chat_id=999),
                    ],
                },
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async with make_client(handler) as http:
        telegram = TelegramClient("123456:TEST-TOKEN", client=http)
        processed = await processor.poll_once(telegram)

    assert processed == 2
    assert store.state.telegram_offset == 102
    assert requests == ["/bot123456:TEST-TOKEN/getUpdates"]
    assert len(notifier.messages) == 1  # only the owner's command was answered

    reloaded = await Store(store.data_dir).load()
    assert reloaded.state.telegram_offset == 102


async def test_poll_once_does_nothing_without_updates(store: Store):
    processor, _scraper, notifier = build_processor(store)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": []})

    async with make_client(handler) as http:
        processed = await processor.poll_once(TelegramClient("123456:TEST-TOKEN", client=http))

    assert processed == 0
    assert notifier.messages == []
