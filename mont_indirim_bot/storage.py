"""Atomic JSON persistence for the watchlist and the runtime state.

The bot has no database: everything it remembers between GitHub Actions runs
(measured prices, notification history, Telegram update offset) lives in two
JSON files inside the repository. All writes go through :func:`write_json`,
which writes a temporary file in the same directory and then :func:`os.replace`
it, so an interrupted run can never leave a half-written file behind.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mont_indirim_bot.prices import parse_price
from mont_indirim_bot.scrapers.base import normalize_url

LOGGER = logging.getLogger(__name__)

__all__ = [
    "MAX_HISTORY_ENTRIES",
    "SCHEMA_VERSION",
    "STATE_FILENAME",
    "WATCHLIST_FILENAME",
    "BotState",
    "HistoryEntry",
    "ItemState",
    "StorageError",
    "Store",
    "WatchItem",
    "Watchlist",
    "from_iso",
    "generate_item_id",
    "read_json",
    "to_iso",
    "utc_now",
    "write_json",
]

WATCHLIST_FILENAME = "watchlist.json"
STATE_FILENAME = "state.json"
SCHEMA_VERSION = 1

#: Upper bound for the per-item price history, keeping `data/state.json` small.
MAX_HISTORY_ENTRIES = 500

#: Length of a generated watchlist id (short enough to type in a chat command).
ITEM_ID_LENGTH = 6


class StorageError(RuntimeError):
    """Raised when a state file cannot be read or written."""


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def to_iso(moment: datetime) -> str:
    """Serialize a datetime to an ISO-8601 UTC string."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def from_iso(value: object) -> datetime | None:
    """Parse an ISO-8601 string back into a timezone-aware UTC datetime."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        LOGGER.warning("Ignoring unparsable timestamp %r", value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


# --------------------------------------------------------------------------- #
# Tolerant coercion helpers (the JSON files are hand editable)
# --------------------------------------------------------------------------- #
def _as_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _as_opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _as_bool(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _as_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return default
    return default


def _as_opt_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            # Hand edited files may contain Turkish prices such as "1.299,00".
            return parse_price(value)
    return None


def _as_mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    return []


def generate_item_id(existing: Sequence[str]) -> str:
    """Return a short random id that is not used yet."""
    taken = set(existing)
    for _ in range(100):
        candidate = secrets.token_hex(ITEM_ID_LENGTH // 2)[:ITEM_ID_LENGTH]
        if candidate not in taken:
            return candidate
    # Astronomically unlikely; keeps the signature honest.
    return secrets.token_hex(8)


# --------------------------------------------------------------------------- #
# JSON IO
# --------------------------------------------------------------------------- #
async def read_json(path: Path, default: Any) -> Any:
    """Read JSON from `path`, falling back to `default` when unusable.

    A missing file is normal (first run). A corrupt file is logged and treated
    as missing so a bad manual edit cannot take the whole workflow down.
    """
    return await asyncio.to_thread(_read_json_sync, path, default)


def _read_json_sync(path: Path, default: Any) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        LOGGER.info("%s does not exist yet, starting from defaults", path)
        return default
    except OSError as exc:
        raise StorageError(f"Could not read {path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        LOGGER.error("%s is not valid JSON (%s); starting from defaults", path, exc)
        return default


async def write_json(path: Path, payload: Any) -> None:
    """Write `payload` to `path` atomically (temp file + `os.replace`)."""
    await asyncio.to_thread(_write_json_sync, path, payload)


def _write_json_sync(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    handle_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        Path(tmp_name).replace(path)
    except OSError as exc:
        Path(tmp_name).unlink(missing_ok=True)
        raise StorageError(f"Could not write {path}: {exc}") from exc


# --------------------------------------------------------------------------- #
# Watchlist
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class WatchItem:
    """One tracked product."""

    id: str
    url: str
    site: str
    title: str = ""
    target_price: float | None = None
    min_discount_pct: float | None = None
    added_at: str = field(default_factory=lambda: to_iso(utc_now()))
    active: bool = True

    @property
    def normalized_url(self) -> str:
        """URL without query string or fragment, used for duplicate checks."""
        return normalize_url(self.url)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the `watchlist.json` shape."""
        return {
            "id": self.id,
            "url": self.url,
            "site": self.site,
            "title": self.title,
            "target_price": self.target_price,
            "min_discount_pct": self.min_discount_pct,
            "added_at": self.added_at,
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WatchItem:
        """Build an item from a possibly incomplete mapping."""
        return cls(
            id=_as_str(data.get("id")),
            url=_as_str(data.get("url")),
            site=_as_str(data.get("site")),
            title=_as_str(data.get("title")),
            target_price=_as_opt_float(data.get("target_price")),
            min_discount_pct=_as_opt_float(data.get("min_discount_pct")),
            added_at=_as_str(data.get("added_at"), to_iso(utc_now())),
            active=_as_bool(data.get("active"), True),
        )


@dataclass(slots=True)
class Watchlist:
    """The set of tracked products."""

    items: list[WatchItem] = field(default_factory=list)

    def find(self, item_id: str) -> WatchItem | None:
        """Return the item with `item_id`, or `None`."""
        wanted = item_id.strip().lower()
        return next((item for item in self.items if item.id.lower() == wanted), None)

    def find_by_url(self, url: str) -> WatchItem | None:
        """Return the item already tracking `url` (query string ignored)."""
        wanted = normalize_url(url)
        return next((item for item in self.items if item.normalized_url == wanted), None)

    def add(self, item: WatchItem) -> WatchItem:
        """Append `item`, assigning an id when it does not have one yet."""
        if not item.id:
            item.id = generate_item_id([existing.id for existing in self.items])
        self.items.append(item)
        return item

    def remove(self, item_id: str) -> WatchItem | None:
        """Remove and return the item with `item_id`, or `None`."""
        item = self.find(item_id)
        if item is None:
            return None
        self.items.remove(item)
        return item

    def active_items(self) -> list[WatchItem]:
        """Items that should be scanned by the scheduler."""
        return [item for item in self.items if item.active and item.url]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the `watchlist.json` shape."""
        return {"version": SCHEMA_VERSION, "items": [item.to_dict() for item in self.items]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Watchlist:
        """Build a watchlist from a possibly incomplete mapping."""
        raw_items = _as_sequence(_as_mapping(data).get("items"))
        parsed = [WatchItem.from_dict(_as_mapping(raw)) for raw in raw_items]
        return cls(items=[item for item in parsed if item.url])


# --------------------------------------------------------------------------- #
# Runtime state
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class HistoryEntry:
    """One price observation."""

    at: str
    price: float
    list_price: float | None = None
    in_stock: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the `state.json` shape."""
        return {
            "at": self.at,
            "price": self.price,
            "list_price": self.list_price,
            "in_stock": self.in_stock,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HistoryEntry:
        """Build a history entry from a possibly incomplete mapping."""
        return cls(
            at=_as_str(data.get("at"), to_iso(utc_now())),
            price=_as_opt_float(data.get("price")) or 0.0,
            list_price=_as_opt_float(data.get("list_price")),
            in_stock=_as_bool(data.get("in_stock"), True),
        )


@dataclass(slots=True)
class ItemState:
    """Everything remembered about one tracked product."""

    last_price: float | None = None
    last_list_price: float | None = None
    last_currency: str = "TRY"
    title: str = ""
    in_stock: bool = True
    last_checked_at: str | None = None
    last_success_at: str | None = None
    lowest_price: float | None = None
    highest_price: float | None = None
    consecutive_errors: int = 0
    error_alerted: bool = False
    last_error: str | None = None
    last_notified_at: str | None = None
    last_notified_price: float | None = None
    last_notified_reasons: list[str] = field(default_factory=list)
    armed_target: bool = True
    armed_drop: bool = True

    @property
    def last_notified_datetime(self) -> datetime | None:
        """Timestamp of the last notification, parsed for cooldown maths."""
        return from_iso(self.last_notified_at)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the `state.json` shape."""
        return {
            "last_price": self.last_price,
            "last_list_price": self.last_list_price,
            "last_currency": self.last_currency,
            "title": self.title,
            "in_stock": self.in_stock,
            "last_checked_at": self.last_checked_at,
            "last_success_at": self.last_success_at,
            "lowest_price": self.lowest_price,
            "highest_price": self.highest_price,
            "consecutive_errors": self.consecutive_errors,
            "error_alerted": self.error_alerted,
            "last_error": self.last_error,
            "last_notified_at": self.last_notified_at,
            "last_notified_price": self.last_notified_price,
            "last_notified_reasons": list(self.last_notified_reasons),
            "armed_target": self.armed_target,
            "armed_drop": self.armed_drop,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ItemState:
        """Build an item state from a possibly incomplete mapping."""
        reasons = [
            _as_str(reason)
            for reason in _as_sequence(_as_mapping(data).get("last_notified_reasons"))
        ]
        return cls(
            last_price=_as_opt_float(data.get("last_price")),
            last_list_price=_as_opt_float(data.get("last_list_price")),
            last_currency=_as_str(data.get("last_currency"), "TRY"),
            title=_as_str(data.get("title")),
            in_stock=_as_bool(data.get("in_stock"), True),
            last_checked_at=_as_opt_str(data.get("last_checked_at")),
            last_success_at=_as_opt_str(data.get("last_success_at")),
            lowest_price=_as_opt_float(data.get("lowest_price")),
            highest_price=_as_opt_float(data.get("highest_price")),
            consecutive_errors=_as_int(data.get("consecutive_errors")),
            error_alerted=_as_bool(data.get("error_alerted"), False),
            last_error=_as_opt_str(data.get("last_error")),
            last_notified_at=_as_opt_str(data.get("last_notified_at")),
            last_notified_price=_as_opt_float(data.get("last_notified_price")),
            last_notified_reasons=[reason for reason in reasons if reason],
            armed_target=_as_bool(data.get("armed_target"), True),
            armed_drop=_as_bool(data.get("armed_drop"), True),
        )


@dataclass(slots=True)
class BotState:
    """The whole `state.json` document."""

    telegram_offset: int = 0
    last_error_alert_at: str | None = None
    items: dict[str, ItemState] = field(default_factory=dict)
    history: dict[str, list[HistoryEntry]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the `state.json` shape."""
        return {
            "version": SCHEMA_VERSION,
            "telegram_offset": self.telegram_offset,
            "last_error_alert_at": self.last_error_alert_at,
            "items": {item_id: state.to_dict() for item_id, state in sorted(self.items.items())},
            "history": {
                item_id: [entry.to_dict() for entry in entries]
                for item_id, entries in sorted(self.history.items())
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BotState:
        """Build the bot state from a possibly incomplete mapping."""
        raw_items = _as_mapping(_as_mapping(data).get("items"))
        raw_history = _as_mapping(_as_mapping(data).get("history"))
        return cls(
            telegram_offset=_as_int(data.get("telegram_offset")),
            last_error_alert_at=_as_opt_str(data.get("last_error_alert_at")),
            items={
                str(item_id): ItemState.from_dict(_as_mapping(state))
                for item_id, state in raw_items.items()
            },
            history={
                str(item_id): [
                    HistoryEntry.from_dict(_as_mapping(entry)) for entry in _as_sequence(entries)
                ]
                for item_id, entries in raw_history.items()
            },
        )


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
class Store:
    """In-memory view over `data/watchlist.json` and `data/state.json`."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.watchlist = Watchlist()
        self.state = BotState()
        self.loaded = False

    @property
    def watchlist_path(self) -> Path:
        """Absolute path of the watchlist file."""
        return self.data_dir / WATCHLIST_FILENAME

    @property
    def state_path(self) -> Path:
        """Absolute path of the state file."""
        return self.data_dir / STATE_FILENAME

    async def load(self) -> Store:
        """Read both files, tolerating missing or corrupt documents."""
        raw_watchlist = await read_json(
            self.watchlist_path, {"version": SCHEMA_VERSION, "items": []}
        )
        raw_state = await read_json(self.state_path, {"version": SCHEMA_VERSION})
        self.watchlist = Watchlist.from_dict(_as_mapping(raw_watchlist))
        self.state = BotState.from_dict(_as_mapping(raw_state))
        self.loaded = True
        LOGGER.info(
            "Loaded %d watchlist item(s) and %d state entry(ies) from %s",
            len(self.watchlist.items),
            len(self.state.items),
            self.data_dir,
        )
        return self

    async def save_watchlist(self) -> None:
        """Persist the watchlist atomically."""
        await write_json(self.watchlist_path, self.watchlist.to_dict())

    async def save_state(self) -> None:
        """Persist the runtime state atomically."""
        await write_json(self.state_path, self.state.to_dict())

    async def save_all(self) -> None:
        """Persist both documents."""
        await self.save_watchlist()
        await self.save_state()

    def state_for(self, item_id: str) -> ItemState:
        """Return (creating if needed) the state of `item_id`."""
        state = self.state.items.get(item_id)
        if state is None:
            state = ItemState()
            self.state.items[item_id] = state
        return state

    def history_for(self, item_id: str) -> list[HistoryEntry]:
        """Return the recorded price history of `item_id` (oldest first)."""
        return self.state.history.setdefault(item_id, [])

    def record_history(self, item_id: str, entry: HistoryEntry) -> None:
        """Append a price observation, trimming the oldest entries."""
        entries = self.history_for(item_id)
        entries.append(entry)
        if len(entries) > MAX_HISTORY_ENTRIES:
            del entries[: len(entries) - MAX_HISTORY_ENTRIES]

    def forget_item(self, item_id: str) -> None:
        """Drop every trace of a removed item from the state."""
        self.state.items.pop(item_id, None)
        self.state.history.pop(item_id, None)
