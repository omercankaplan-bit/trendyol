"""Tests for the httpx based Telegram Bot API client."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from mont_indirim_bot import telegram_client as tc
from mont_indirim_bot.telegram_client import TelegramClient, TelegramError
from tests.helpers import make_client

TOKEN = "123456:SECRET-TOKEN"


def _ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


@pytest.fixture
def instant_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace the retry sleep with a recorder so tests stay fast."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(tc, "_sleep", fake_sleep)
    return sleeps


async def test_send_message_posts_markdownv2_payload():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _ok({"message_id": 7})

    async with make_client(handler) as http:
        client = TelegramClient(TOKEN, client=http)
        result = await client.send_message(4242, "merhaba dünya")

    assert result["message_id"] == 7
    assert requests[0].url.path == f"/bot{TOKEN}/sendMessage"
    body = json.loads(requests[0].content)
    assert body["chat_id"] == 4242
    assert body["parse_mode"] == "MarkdownV2"
    assert body["link_preview_options"] == {"is_disabled": True}


async def test_send_message_truncates_over_long_text():
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _ok({"message_id": 1})

    async with make_client(handler) as http:
        client = TelegramClient(TOKEN, client=http)
        await client.send_message(1, "x" * 5000)

    assert len(bodies[0]["text"]) == 4096
    assert bodies[0]["text"].endswith("…")


async def test_get_me_returns_the_account():
    async with make_client(lambda request: _ok({"username": "mont_bot"})) as http:
        client = TelegramClient(TOKEN, client=http)
        assert await client.get_me() == {"username": "mont_bot"}


async def test_400_raises_immediately_without_retrying():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            400, json={"ok": False, "description": "Bad Request: can't parse entities"}
        )

    async with make_client(handler) as http:
        client = TelegramClient(TOKEN, client=http)
        with pytest.raises(TelegramError) as excinfo:
            await client.send_message(1, "bozuk")

    assert calls["n"] == 1
    assert excinfo.value.status_code == 400
    assert "parse entities" in str(excinfo.value)


async def test_ok_false_on_http_200_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "chat not found"})

    async with make_client(handler) as http:
        client = TelegramClient(TOKEN, client=http)
        with pytest.raises(TelegramError) as excinfo:
            await client.send_message(1, "selam")

    assert "chat not found" in str(excinfo.value)


async def test_429_is_retried_after_retry_after(instant_sleep: list[float]):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 3},
                },
            )
        return _ok({"message_id": 9})

    async with make_client(handler) as http:
        client = TelegramClient(TOKEN, client=http)
        assert await client.send_message(1, "selam") == {"message_id": 9}

    assert calls["n"] == 2
    assert instant_sleep == [3.0]


async def test_429_exhausts_the_retries(instant_sleep: list[float]):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            429,
            headers={"Retry-After": "2"},
            json={"ok": False, "description": "Too Many Requests"},
        )

    async with make_client(handler) as http:
        client = TelegramClient(TOKEN, client=http, max_retries=2)
        with pytest.raises(TelegramError) as excinfo:
            await client.call("getMe")

    assert calls["n"] == 2
    assert excinfo.value.status_code == 429
    assert instant_sleep == [2.0]


async def test_server_error_is_retried_with_exponential_backoff(instant_sleep: list[float]):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="upstream down")
        return _ok({"message_id": 3})

    async with make_client(handler) as http:
        client = TelegramClient(TOKEN, client=http)
        assert await client.send_message(1, "selam") == {"message_id": 3}

    assert calls["n"] == 2
    assert instant_sleep == [tc.RETRY_BACKOFF_S]


async def test_network_error_is_retried_and_the_token_stays_secret(instant_sleep: list[float]):
    def handler(request: httpx.Request) -> httpx.Response:
        msg = f"connection failed for {request.url}"
        raise httpx.ConnectError(msg, request=request)

    async with make_client(handler) as http:
        client = TelegramClient(TOKEN, client=http, max_retries=1)
        with pytest.raises(TelegramError) as excinfo:
            await client.call("getMe")

    assert "ConnectError" in str(excinfo.value)
    assert "SECRET-TOKEN" not in str(excinfo.value)
    assert instant_sleep == []


async def test_get_updates_forwards_parameters_and_filters_junk():
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _ok([{"update_id": 6, "message": {}}, "junk", 42])

    async with make_client(handler) as http:
        client = TelegramClient(TOKEN, client=http)
        updates = await client.get_updates(offset=5, timeout_s=10, limit=3)

    assert updates == [{"update_id": 6, "message": {}}]
    assert bodies[0]["offset"] == 5
    assert bodies[0]["timeout"] == 10
    assert bodies[0]["limit"] == 3
    assert bodies[0]["allowed_updates"] == ["message"]


async def test_get_updates_returns_empty_for_unexpected_shapes():
    async with make_client(lambda request: _ok({"unexpected": True})) as http:
        client = TelegramClient(TOKEN, client=http)
        assert await client.get_updates() == []


def test_malformed_token_is_rejected():
    with pytest.raises(TelegramError):
        TelegramClient("not-a-token")


async def test_aclose_leaves_injected_clients_open():
    async with make_client(lambda request: _ok({})) as http:
        client = TelegramClient(TOKEN, client=http)
        await client.aclose()
        assert http.is_closed is False


def test_retry_after_seconds_prefers_the_header():
    response = httpx.Response(
        429,
        headers={"Retry-After": "2"},
        json={"parameters": {"retry_after": 30}},
    )

    assert tc._retry_after_seconds(response) == 2.0
