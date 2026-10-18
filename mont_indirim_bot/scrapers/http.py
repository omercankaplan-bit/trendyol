"""HTTP transports used by the scrapers.

Both storefronts sit behind Cloudflare, which fingerprints the TLS handshake
rather than only the headers: a plain `httpx` request is answered with HTTP 403
even when it carries a perfect Chrome user agent, while a client that
impersonates Chrome's TLS/HTTP2 fingerprint is served normally. `curl_cffi`
provides that impersonation, so :class:`BrowserClient` is the default transport.

The scrapers only need a tiny slice of a client (`get` + `aclose` + a response
with `status_code`, `text`, `headers` and `json()`), so that surface is captured
by :class:`HttpClient`. Tests keep injecting an `httpx.AsyncClient` built on
`httpx.MockTransport`, which satisfies the same protocol without touching the
network.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

import httpx
from curl_cffi.requests import AsyncSession, Response
from curl_cffi.requests.errors import RequestsError

__all__ = [
    "DEFAULT_IMPERSONATE",
    "NETWORK_ERRORS",
    "BrowserClient",
    "HttpClient",
    "HttpResponse",
]

#: Exceptions that mean "the request never completed" (as opposed to a bad status).
NETWORK_ERRORS: tuple[type[Exception], ...] = (httpx.HTTPError, RequestsError)

#: Which browser build curl_cffi imitates for the TLS/HTTP2 fingerprint.
DEFAULT_IMPERSONATE = "chrome"


class HttpResponse(Protocol):
    """The response surface the parsers rely on."""

    @property
    def status_code(self) -> int:
        """HTTP status code of the response."""
        ...

    @property
    def text(self) -> str:
        """Decoded body."""
        ...

    @property
    def headers(self) -> Mapping[str, Any]:
        """Response headers (used for `Retry-After`)."""
        ...

    def json(self) -> Any:
        """Parse the body as JSON."""
        ...


class HttpClient(Protocol):
    """The client surface the scrapers rely on."""

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        follow_redirects: bool = True,
    ) -> HttpResponse:
        """Perform a GET request."""
        ...

    async def aclose(self) -> None:
        """Release the underlying connections."""
        ...


class BrowserClient:
    """`HttpClient` backed by curl_cffi with a real browser fingerprint."""

    def __init__(
        self,
        *,
        timeout_s: float,
        impersonate: str = DEFAULT_IMPERSONATE,
        proxy: str | None = None,
    ) -> None:
        self._session: AsyncSession[Response] = AsyncSession(
            impersonate=impersonate,
            timeout=timeout_s,
            proxies={"http": proxy, "https": proxy} if proxy else None,
        )

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        follow_redirects: bool = True,
    ) -> HttpResponse:
        """Perform a GET request with a Chrome-like fingerprint."""
        return await self._session.get(
            url,
            headers=dict(headers) if headers else None,
            allow_redirects=follow_redirects,
        )

    async def aclose(self) -> None:
        """Close the underlying curl session."""
        await self._session.close()
