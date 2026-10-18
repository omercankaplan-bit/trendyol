"""Minimal asynchronous client for the Telegram Bot API.

`python-telegram-bot` assumes a long-running process with a polling loop; the
serverless design of this bot needs two calls per run at most, so the raw HTTP
API through `httpx` is the leaner choice here.

Only `getUpdates` (with an offset) and `sendMessage` are used, next to a
`getMe` health check. Rate limits (HTTP 429) and transient 5xx errors are
retried with exponential backoff; 4xx errors are raised immediately because
retrying them only wastes the workflow budget.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

LOGGER = logging.getLogger(__name__)

__all__ = ["TELEGRAM_API_BASE", "TelegramClient", "TelegramError"]

TELEGRAM_API_BASE = "https://api.telegram.org"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_RETRIES = 3
RETRY_BACKOFF_S = 1.5
MAX_RETRY_AFTER_S = 60.0
MAX_MESSAGE_LENGTH = 4096


class TelegramError(RuntimeError):
    """Raised when the Bot API answers with an error or is unreachable."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        description: str | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.description = description
        self.retry_after_s = retry_after_s


async def _sleep(seconds: float) -> None:
    """Sleep between retries (tests patch this to keep the suite instant)."""
    await asyncio.sleep(seconds)


class TelegramClient:
    """Thin wrapper around `api.telegram.org/bot<token>/<method>`."""

    def __init__(
        self,
        token: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """
        Args:
            token: Bot token from BotFather.
            timeout_s: Per-request timeout. `getUpdates` long polling uses a
                shorter value, see :meth:`get_updates`.
            max_retries: Total attempts for retryable failures.
            client: Existing client to reuse (tests inject one with a mock
                transport); when omitted the caller must `await aclose()`.
        """
        if not token or ":" not in token:
            msg = "TELEGRAM_BOT_TOKEN looks malformed (expected '<id>:<secret>')."
            raise TelegramError(msg)
        self._token = token
        self._base_url = f"{TELEGRAM_API_BASE}/bot{token}"
        self._max_retries = max(1, max_retries)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=10.0))

    # -- lifecycle --------------------------------------------------------- #
    async def aclose(self) -> None:
        """Close the underlying HTTP client when this instance created it."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> TelegramClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -- core call --------------------------------------------------------- #
    async def call(self, method: str, payload: Mapping[str, Any] | None = None) -> Any:
        """Call a Bot API method and return its `result`.

        Raises:
            TelegramError: on a 4xx answer, on a body with `ok: false`, or
                after exhausting the retries for 429/5xx/network failures.
        """
        url = f"{self._base_url}/{method}"
        request_payload = dict(payload or {})
        attempt = 1
        while True:
            failure: TelegramError | None = None
            try:
                response = await self._client.post(url, json=request_payload)
            except httpx.HTTPError as exc:
                failure = TelegramError(f"[{method}] network error: {self._redact(exc)}")
            else:
                if response.status_code == 429:
                    delay = _retry_after_seconds(response)
                    if attempt < self._max_retries:
                        LOGGER.warning(
                            "[%s] rate limited, waiting %.1fs before retry %d/%d",
                            method,
                            delay,
                            attempt + 1,
                            self._max_retries,
                        )
                        await _sleep(delay)
                        attempt += 1
                        continue
                    failure = TelegramError(
                        f"[{method}] rate limited (HTTP 429)",
                        status_code=429,
                        retry_after_s=delay,
                    )
                elif response.status_code >= 500:
                    failure = TelegramError(
                        f"[{method}] server error (HTTP {response.status_code})",
                        status_code=response.status_code,
                    )
                elif response.status_code >= 400:
                    description = _describe_body(response)
                    msg = f"[{method}] HTTP {response.status_code}: {description}"
                    raise TelegramError(
                        msg, status_code=response.status_code, description=description
                    )
                else:
                    body = _decode_body(response, method)
                    if body.get("ok") is True:
                        return body.get("result")
                    description = str(body.get("description") or "unknown error")
                    msg = f"[{method}] Telegram rejected the request: {description}"
                    raise TelegramError(
                        msg, status_code=response.status_code, description=description
                    )

            # Every branch above returns, raises, or assigns `failure`.
            assert failure is not None
            if attempt >= self._max_retries:
                raise failure
            delay = RETRY_BACKOFF_S * (2 ** (attempt - 1))
            LOGGER.warning(
                "%s; retrying in %.1fs (attempt %d/%d)",
                failure,
                delay,
                attempt + 1,
                self._max_retries,
            )
            await _sleep(delay)
            attempt += 1

    # -- high level methods ------------------------------------------------ #
    async def get_me(self) -> dict[str, Any]:
        """Return the bot account description (used as a credentials smoke test)."""
        result = await self.call("getMe")
        return result if isinstance(result, dict) else {}

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = "MarkdownV2",
        disable_notification: bool = False,
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        """Send `text` to `chat_id` and return the created message object."""
        if len(text) > MAX_MESSAGE_LENGTH:
            LOGGER.warning("Message longer than %d chars, truncating", MAX_MESSAGE_LENGTH)
            text = text[: MAX_MESSAGE_LENGTH - 1] + "…"
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_notification": disable_notification,
            "link_preview_options": {"is_disabled": disable_web_page_preview},
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        result = await self.call("sendMessage", payload)
        return result if isinstance(result, dict) else {}

    async def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout_s: int = 25,
        limit: int = 50,
        allowed_updates: Sequence[str] = ("message",),
    ) -> list[dict[str, Any]]:
        """Fetch pending updates once (short long-poll) with `offset` confirmation."""
        payload: dict[str, Any] = {"limit": limit, "allowed_updates": list(allowed_updates)}
        if offset is not None:
            payload["offset"] = offset
        if timeout_s:
            payload["timeout"] = timeout_s
        result = await self.call("getUpdates", payload)
        if not isinstance(result, list):
            return []
        return [update for update in result if isinstance(update, dict)]

    # -- helpers ----------------------------------------------------------- #
    def _redact(self, exc: BaseException) -> str:
        """Return an error description with the bot token removed."""
        text = f"{type(exc).__name__}: {exc}"
        if self._token in text:
            text = text.replace(self._token, "***")
        return text


def _decode_body(response: httpx.Response, method: str) -> Mapping[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        msg = f"[{method}] response was not JSON (HTTP {response.status_code})"
        raise TelegramError(msg, status_code=response.status_code) from exc
    if not isinstance(body, Mapping):
        msg = f"[{method}] unexpected response shape: {type(body).__name__}"
        raise TelegramError(msg, status_code=response.status_code)
    return body


def _describe_body(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200] or "no body"
    if isinstance(body, Mapping):
        description = body.get("description")
        if isinstance(description, str):
            return description
    return "unknown error"


def _retry_after_seconds(response: httpx.Response) -> float:
    """Read the suggested wait from the `Retry-After` header or the body."""
    header = response.headers.get("Retry-After")
    if header:
        try:
            return min(max(float(header), 1.0), MAX_RETRY_AFTER_S)
        except ValueError:
            pass
    try:
        body = response.json()
    except ValueError:
        return 5.0
    if isinstance(body, Mapping):
        parameters = body.get("parameters")
        if isinstance(parameters, Mapping):
            value = parameters.get("retry_after")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return min(max(float(value), 1.0), MAX_RETRY_AFTER_S)
    return 5.0
