"""Tests for the browser-fingerprinted transport in `scrapers.http`."""

from __future__ import annotations

from typing import Any, ClassVar

import httpx
import pytest
from curl_cffi.requests.errors import RequestsError

from mont_indirim_bot.scrapers import http as http_module
from mont_indirim_bot.scrapers.http import NETWORK_ERRORS, BrowserClient


class FakeResponse:
    """Minimal stand-in for a curl_cffi response."""

    status_code: int = 200
    headers: ClassVar[dict[str, str]] = {"content-type": "text/html"}
    text: str = "<html></html>"

    def json(self) -> Any:
        return {"ok": True}


class FakeSession:
    """Stands in for `curl_cffi.requests.AsyncSession`."""

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs: dict[str, Any] = kwargs
        self.get_calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.get_calls.append((url, kwargs))
        return FakeResponse()

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_session(monkeypatch: pytest.MonkeyPatch) -> FakeSession:
    """Replace the real curl_cffi session before `BrowserClient` builds one."""
    session = FakeSession()

    def factory(**kwargs: Any) -> FakeSession:
        session.init_kwargs = kwargs
        return session

    monkeypatch.setattr(http_module, "AsyncSession", factory)
    return session


def test_network_errors_covers_both_transports():
    assert httpx.HTTPError in NETWORK_ERRORS
    assert RequestsError in NETWORK_ERRORS


async def test_browser_client_impersonates_chrome(fake_session: FakeSession):
    client = BrowserClient(timeout_s=12.5)

    assert fake_session.init_kwargs == {"impersonate": "chrome", "timeout": 12.5, "proxies": None}

    await client.aclose()

    assert fake_session.closed is True


async def test_browser_client_maps_follow_redirects(fake_session: FakeSession):
    client = BrowserClient(timeout_s=5.0)

    response = await client.get(
        "https://example.test/x", headers={"A": "b"}, follow_redirects=False
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert fake_session.get_calls == [
        ("https://example.test/x", {"headers": {"A": "b"}, "allow_redirects": False})
    ]


async def test_browser_client_omits_empty_headers(fake_session: FakeSession):
    client = BrowserClient(timeout_s=5.0)

    await client.get("https://example.test/y")

    assert fake_session.get_calls == [
        ("https://example.test/y", {"headers": None, "allow_redirects": True})
    ]


async def test_browser_client_routes_both_schemes_through_the_proxy(fake_session: FakeSession):
    BrowserClient(timeout_s=5.0, proxy="http://user:pw@proxy.test:8080")

    assert fake_session.init_kwargs["proxies"] == {
        "http": "http://user:pw@proxy.test:8080",
        "https": "http://user:pw@proxy.test:8080",
    }


async def test_browser_client_connects_directly_without_a_proxy(fake_session: FakeSession):
    BrowserClient(timeout_s=5.0)

    assert fake_session.init_kwargs["proxies"] is None
