"""Pytest fixtures shared by the test modules."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from mont_indirim_bot.config import Config
from mont_indirim_bot.storage import Store, WatchItem
from tests import helpers


@pytest.fixture
def read_fixture() -> Callable[[str], str]:
    """Read a saved HTML/JSON fixture as text."""
    return helpers.read_fixture


@pytest.fixture
def read_json_fixture() -> Callable[[str], Any]:
    """Read a saved JSON fixture as parsed data."""
    return helpers.read_json_fixture


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """A `Config` pointing at a temporary data directory."""
    return helpers.make_config(tmp_path)


@pytest.fixture
def make_config(tmp_path: Path) -> Callable[..., Config]:
    """Factory for `Config` objects with per-test overrides."""

    def _make(**overrides: Any) -> Config:
        return helpers.make_config(tmp_path, **overrides)

    return _make


@pytest.fixture
def store(tmp_path: Path) -> Store:
    """An empty store rooted in the temporary directory (not loaded yet)."""
    return Store(tmp_path)


@pytest.fixture
async def loaded_store(store: Store) -> Store:
    """A store loaded from disk (creating the default documents)."""
    return await store.load()


@pytest.fixture
def watch_item() -> WatchItem:
    """A watchlist item pointing at the fake scraper's host."""
    return WatchItem(
        id="aaa111",
        url="https://shop.example/mont-p-1",
        site="fake",
        title="Columbia Pike Lake Mont",
        target_price=None,
    )
