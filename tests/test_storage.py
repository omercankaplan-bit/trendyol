"""Tests for the atomic JSON storage layer."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from mont_indirim_bot.storage import (
    MAX_HISTORY_ENTRIES,
    BotState,
    HistoryEntry,
    ItemState,
    Store,
    WatchItem,
    Watchlist,
    from_iso,
    generate_item_id,
    read_json,
    to_iso,
    utc_now,
    write_json,
)


async def test_write_and_read_roundtrip(tmp_path: Path):
    path = tmp_path / "nested" / "doc.json"
    await write_json(path, {"a": 1, "türkçe": "ığş"})
    assert await read_json(path, {}) == {"a": 1, "türkçe": "ığş"}


async def test_atomic_write_leaves_no_temporary_file(tmp_path: Path):
    path = tmp_path / "state.json"
    await write_json(path, {"x": 1})
    await write_json(path, {"x": 2})

    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []
    assert json.loads(path.read_text(encoding="utf-8")) == {"x": 2}


async def test_read_json_falls_back_on_corrupt_file(tmp_path: Path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    assert await read_json(path, {"default": True}) == {"default": True}


async def test_read_json_returns_default_for_missing_file(tmp_path: Path):
    assert await read_json(tmp_path / "nope.json", []) == []


async def test_store_load_creates_missing_documents(tmp_path: Path):
    store = await Store(tmp_path).load()

    assert store.watchlist.items == []
    assert store.state.telegram_offset == 0
    assert store.loaded is True


async def test_store_roundtrips_watchlist_state_and_history(tmp_path: Path):
    store = await Store(tmp_path).load()
    item = store.watchlist.add(
        WatchItem(
            id="",
            url="https://www.trendyol.com/columbia/pike-lake-p-123456789",
            site="trendyol",
            title="Columbia Pike Lake Mont",
            target_price=999.5,
        )
    )
    state = store.state_for(item.id)
    state.last_price = 1299.0
    state.lowest_price = 1200.0
    state.armed_target = False
    state.last_notified_at = to_iso(utc_now())
    state.last_notified_reasons = ["target"]
    store.record_history(
        item.id, HistoryEntry(at=to_iso(utc_now()), price=1299.0, list_price=1799.0)
    )
    await store.save_all()

    reloaded = await Store(tmp_path).load()
    assert len(reloaded.watchlist.items) == 1
    restored_item = reloaded.watchlist.items[0]
    assert restored_item.id == item.id
    assert restored_item.target_price == 999.5
    assert restored_item.title == "Columbia Pike Lake Mont"

    restored_state = reloaded.state_for(item.id)
    assert restored_state.last_price == 1299.0
    assert restored_state.lowest_price == 1200.0
    assert restored_state.armed_target is False
    assert restored_state.last_notified_reasons == ["target"]
    assert reloaded.state_for(item.id).last_notified_datetime is not None

    history = reloaded.history_for(item.id)
    assert len(history) == 1
    assert history[0].list_price == 1799.0


def test_watchlist_add_assigns_unique_ids_and_finds_case_insensitively():
    watchlist = Watchlist()
    first = watchlist.add(WatchItem(id="", url="https://www.trendyol.com/a-p-1", site="trendyol"))
    second = watchlist.add(WatchItem(id="", url="https://www.trendyol.com/b-p-2", site="trendyol"))

    assert len(first.id) == 6
    assert first.id != second.id
    assert watchlist.find(first.id.upper()) is first
    assert watchlist.find("missing") is None


def test_watchlist_find_by_url_ignores_query_string_and_trailing_slash():
    watchlist = Watchlist()
    item = watchlist.add(
        WatchItem(id="", url="https://www.trendyol.com/a/b-p-1?boutiqueId=61", site="trendyol")
    )

    assert watchlist.find_by_url("https://www.trendyol.com/a/b-p-1") is item
    assert watchlist.find_by_url("https://www.trendyol.com/a/b-p-1/") is item
    assert watchlist.find_by_url("https://www.trendyol.com/a/other-p-2") is None


def test_watchlist_remove_and_active_filter():
    watchlist = Watchlist()
    keep = watchlist.add(WatchItem(id="", url="https://www.trendyol.com/a-p-1", site="trendyol"))
    drop = watchlist.add(
        WatchItem(id="", url="https://www.trendyol.com/b-p-2", site="trendyol", active=False)
    )

    assert watchlist.active_items() == [keep]
    assert watchlist.remove(drop.id) is drop
    assert watchlist.remove(drop.id) is None
    assert len(watchlist.items) == 1


def test_generate_item_id_avoids_taken_ids():
    taken = [generate_item_id([]) for _ in range(5)]
    fresh = generate_item_id(taken)

    assert len(fresh) == 6
    assert fresh not in taken


async def test_history_is_trimmed_to_the_limit(tmp_path: Path):
    store = await Store(tmp_path).load()
    for index in range(MAX_HISTORY_ENTRIES + 5):
        store.record_history("aaa111", HistoryEntry(at=to_iso(utc_now()), price=float(index)))

    entries = store.history_for("aaa111")
    assert len(entries) == MAX_HISTORY_ENTRIES
    assert entries[0].price == 5.0
    assert entries[-1].price == float(MAX_HISTORY_ENTRIES + 4)


def test_item_state_tolerates_hand_edited_values():
    state = ItemState.from_dict(
        {
            "last_price": "1.299,5",
            "consecutive_errors": "2",
            "armed_drop": None,
            "last_notified_reasons": ["target", ""],
            "unknown_field": 1,
        }
    )

    assert state.last_price == 1299.5
    assert state.consecutive_errors == 2
    assert state.armed_drop is True
    assert state.last_notified_reasons == ["target"]


def test_bot_state_roundtrip_keeps_the_offset():
    state = BotState.from_dict(
        {
            "telegram_offset": 42,
            "last_error_alert_at": "2026-10-18T12:00:00+00:00",
            "items": {"aaa111": {"last_price": 100.0}},
            "history": {"aaa111": [{"at": "2026-10-18T12:00:00+00:00", "price": 100.0}]},
        }
    )
    restored = BotState.from_dict(state.to_dict())

    assert restored.telegram_offset == 42
    assert restored.last_error_alert_at == "2026-10-18T12:00:00+00:00"
    assert restored.items["aaa111"].last_price == 100.0
    assert restored.history["aaa111"][0].price == 100.0


def test_forget_item_drops_state_and_history(tmp_path: Path):
    store = Store(tmp_path)
    store.state_for("aaa111").last_price = 10.0
    store.record_history("aaa111", HistoryEntry(at=to_iso(utc_now()), price=10.0))

    store.forget_item("aaa111")

    assert "aaa111" not in store.state.items
    assert "aaa111" not in store.state.history


def test_iso_helpers_roundtrip_and_reject_garbage():
    moment = datetime(2026, 10, 18, 9, 30, tzinfo=UTC)

    assert from_iso(to_iso(moment)) == moment
    assert from_iso("2026-10-18T09:30:00") == moment
    assert from_iso("not a date") is None
    assert from_iso(None) is None
    assert to_iso(moment.replace(tzinfo=None)) == "2026-10-18T09:30:00+00:00"
