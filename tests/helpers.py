"""Helpers shared by the test modules.

They live here instead of in `conftest.py` so that `mypy` can follow them and
so that the test modules can import them directly.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import httpx

from mont_indirim_bot.config import Config
from mont_indirim_bot.scrapers import PriceResult, Scraper
from mont_indirim_bot.services.notifier import MARKDOWN_V2_SPECIALS, Alert

FIXTURES_DIR = Path(__file__).parent / "fixtures"

TRENDYOL_URL = "https://www.trendyol.com/columbia/pike-lake-mont-p-123456789"
HEPSIBURADA_URL = "https://www.hepsiburada.com/tnf/resolve-mont-p-HBCV0000123456"


def read_fixture(name: str) -> str:
    """Return the text of a saved HTML/JSON fixture."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def read_json_fixture(name: str) -> Any:
    """Return the parsed content of a saved JSON fixture."""
    return json.loads(read_fixture(name))


def make_config(data_dir: Path, **overrides: Any) -> Config:
    """Build a `Config` with test friendly defaults (no network delays)."""
    base = Config(
        telegram_bot_token="123456:TEST-TOKEN",
        telegram_chat_id=42,
        min_discount_pct=20.0,
        cooldown_hours=12.0,
        rearm_pct=2.0,
        request_min_delay_s=0.0,
        request_max_delay_s=0.0,
        max_retries=3,
        request_timeout_s=5.0,
        max_parallel_requests=2,
        data_dir=data_dir,
    )
    return replace(base, **overrides) if overrides else base


def price_result(price: float, **overrides: Any) -> PriceResult:
    """Build a `PriceResult` for the fake scraper."""
    base = PriceResult(
        product_name="Columbia Erkek Pike Lake Mont",
        price=price,
        list_price=price * 1.4,
        currency="TRY",
        in_stock=True,
        url=TRENDYOL_URL,
        source="json",
    )
    return replace(base, **overrides) if overrides else base


class FakeClock:
    """Deterministic clock so cooldown windows can be travelled instantly."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 10, 18, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class RecordingNotifier:
    """`NotifierProtocol` implementation that records instead of sending."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.alerts: list[Alert] = []
        self.warnings: list[tuple[str, str, str, int]] = []

    async def send(self, text: str, *, disable_notification: bool = False) -> bool:
        del disable_notification
        self.messages.append(text)
        return True

    async def send_alert(self, alert: Alert) -> bool:
        self.alerts.append(alert)
        return True

    async def send_scraper_warning(
        self, item_id: str, title: str, url: str, error: str, attempts: int
    ) -> bool:
        del url
        self.warnings.append((item_id, title, error, attempts))
        return True


class FakeScraper(Scraper):
    """Scraper that replays a scripted list of results (or exceptions).

    The last script entry is repeated forever, so a test can say "always 899".
    """

    name: ClassVar[str] = "fake"
    display_name: ClassVar[str] = "Fake Site"
    host_patterns: ClassVar[tuple[str, ...]] = ("shop.example", "*.shop.example")

    def __init__(self, script: Sequence[PriceResult | Exception]) -> None:
        self.script: list[PriceResult | Exception] = list(script)
        self.calls: list[str] = []

    async def fetch(self, url: str, *, client: httpx.AsyncClient | None = None) -> PriceResult:
        del client
        self.calls.append(url)
        if not self.script:
            msg = "FakeScraper script is empty"
            raise AssertionError(msg)
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, Exception):
            raise step
        return step


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    """Build a client whose transport answers from `handler` (no real network)."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def _link_end(text: str, start: int) -> int:
    """Index of the closing `)` of a `[label](target)` link, or -1."""
    index = start
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == ")":
            return index
        index += 1
    return -1


def assert_markdownv2_safe(text: str) -> None:
    """Assert that no MarkdownV2 special character is left unescaped.

    `*` (bold), `` ` `` (code) and `[label](target)` links are markup and are
    skipped; everything else must be backslash escaped, otherwise Telegram
    answers with HTTP 400 `can't parse entities`.
    """
    index = 0
    in_code = False
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "`":
            in_code = not in_code
            index += 1
            continue
        if not in_code:
            if char == "*":
                index += 1
                continue
            if char == "[":
                close = text.find("](", index)
                end = _link_end(text, close + 2) if close != -1 else -1
                if end != -1:
                    index = end + 1
                    continue
            if char in MARKDOWN_V2_SPECIALS:
                context = text[max(0, index - 40) : index + 40]
                msg = f"unescaped MarkdownV2 character {char!r} near {context!r}"
                raise AssertionError(msg)
        index += 1
