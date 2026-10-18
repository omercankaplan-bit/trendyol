"""Scraper interface plus the HTTP/parsing behaviour shared by all sites.

Site specific knowledge is concentrated in class constants: JSON key names,
embedded-JS variable names and HTML regex fallbacks. When a storefront changes
its markup, only those constants have to be updated - the logic in
:class:`BaseScraper` stays untouched.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar
from urllib.parse import urlsplit, urlunsplit

import httpx

from mont_indirim_bot.prices import DEFAULT_CURRENCY, parse_price

LOGGER = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_USER_AGENTS",
    "BaseScraper",
    "PriceResult",
    "Scraper",
    "ScraperError",
    "ScraperFetchError",
    "ScraperParseError",
    "extract_json_assignment",
    "extract_json_ld",
    "first_match",
    "normalize_url",
    "strip_html",
]

DEFAULT_MAX_RETRIES = 3
DEFAULT_TIMEOUT_S = 20.0

#: Realistic desktop user agents, rotated per request to avoid a single
#: recognisable fingerprint.
DEFAULT_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
)

#: HTTP statuses that are worth retrying with exponential backoff.
RETRYABLE_STATUS_CODES = frozenset({403, 408, 425, 429, 500, 502, 503, 504})

_LD_JSON_PATTERN = re.compile(
    r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)
_TAG_PATTERN = re.compile(r"<[^>]+>")
_WS_PATTERN = re.compile(r"\s+")
_HTML_ENTITIES = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&quot;": '"',
    "&#39;": "'",
    "&apos;": "'",
    "&lt;": "<",
    "&gt;": ">",
    "&uuml;": "ü",
    "&#252;": "ü",
    "&ouml;": "ö",
    "&#246;": "ö",
    "&ccedil;": "ç",
    "&#231;": "ç",
    "&scedil;": "ş",
    "&#351;": "ş",
    "&gbreve;": "ğ",
    "&#287;": "ğ",
    "&Idot;": "İ",
    "&#304;": "İ",
}


class ScraperError(RuntimeError):
    """Base class for scraper failures (all of them are per-product)."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ScraperFetchError(ScraperError):
    """The page could not be downloaded (network error or bad status)."""


class ScraperParseError(ScraperError):
    """The page was downloaded but no usable price could be extracted."""


@dataclass(frozen=True, slots=True)
class PriceResult:
    """One successful price measurement."""

    product_name: str
    price: float
    list_price: float | None = None
    currency: str = DEFAULT_CURRENCY
    in_stock: bool = True
    url: str = ""
    source: str = "json"

    @property
    def list_discount_pct(self) -> float | None:
        """Discount against the strikethrough ("liste") price, when known."""
        if self.list_price is None or self.list_price <= self.price:
            return None
        return (self.list_price - self.price) / self.list_price * 100.0


def normalize_url(url: str) -> str:
    """Return `url` without query string or fragment, lowercased host."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower() or "https", host, path, "", ""))


def _host_of(url: str) -> str:
    return urlsplit(url.strip()).hostname or ""


def strip_html(text: str) -> str:
    """Remove tags and decode the handful of entities Turkish pages use."""
    without_tags = _TAG_PATTERN.sub(" ", text)
    for entity, replacement in _HTML_ENTITIES.items():
        without_tags = without_tags.replace(entity, replacement)
    return _WS_PATTERN.sub(" ", without_tags).strip()


def first_match(patterns: Iterable[str], text: str, *, flags: int = re.DOTALL) -> str | None:
    """Return capture group 1 of the first matching pattern, or `None`."""
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            return match.group(1) if match.groups() else match.group(0)
    return None


def extract_json_ld(html: str) -> list[Any]:
    """Parse every `application/ld+json` block of an HTML document."""
    documents: list[Any] = []
    for match in _LD_JSON_PATTERN.finditer(html):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            documents.append(json.loads(raw))
        except json.JSONDecodeError:
            LOGGER.debug("Skipping malformed JSON-LD block (%d chars)", len(raw))
    return documents


def _scan_balanced(text: str, start: int) -> tuple[int, int] | None:
    """Return the (start, end) slice of the JSON value beginning at `start`."""
    if start >= len(text) or text[start] not in "{[":
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                return start, index + 1
    return None


def extract_json_assignment(html: str, variable: str) -> Any | None:
    """Extract `variable = {...}` (or `[...]`) embedded in a `<script>` block.

    Storefronts ship their initial state as a JS assignment instead of a JSON
    endpoint; this pulls that object out without a JS engine by brace matching
    that respects string literals.
    """
    cursor = html.find(variable)
    while cursor != -1:
        opening = -1
        for index in range(cursor + len(variable), min(len(html), cursor + len(variable) + 200)):
            if html[index] in "{[":
                opening = index
                break
        if opening != -1:
            span = _scan_balanced(html, opening)
            if span is not None:
                try:
                    return json.loads(html[span[0] : span[1]])
                except json.JSONDecodeError:
                    LOGGER.debug("Embedded JSON of %s is not valid JSON", variable)
        cursor = html.find(variable, cursor + 1)
    return None


def _dig(node: Any, path: Sequence[str]) -> Any:
    current = node
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _price_from_value(value: Any, *, depth: int = 0) -> float | None:
    """Read a price out of a number, a string, or a nested price object."""
    if isinstance(value, Mapping):
        if depth >= 2:
            return None
        for key in ("value", "amount", "price", "discountedPrice", "sellingPrice", "rawPrice"):
            nested = _price_from_value(value.get(key), depth=depth + 1)
            if nested is not None:
                return nested
        return None
    return parse_price(value)


def _has_price(node: Mapping[str, Any], keys: Sequence[str]) -> bool:
    return any(_price_from_value(node.get(key)) is not None for key in keys)


def _search_for_product(
    node: Any, price_keys: Sequence[str], *, max_depth: int = 6
) -> Mapping[str, Any] | None:
    """Breadth-first search for the mapping that holds the price fields."""
    queue: list[tuple[Any, int]] = [(node, 0)]
    while queue:
        current, depth = queue.pop(0)
        if isinstance(current, Mapping):
            if _has_price(current, price_keys):
                return current
            if depth < max_depth:
                queue.extend((value, depth + 1) for value in current.values())
        elif isinstance(current, list) and depth < max_depth:
            queue.extend((entry, depth + 1) for entry in current)
    return None


def _string_from_keys(node: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return strip_html(value)
    return None


def _currency_from(node: Mapping[str, Any], price_keys: Sequence[str]) -> str | None:
    for key in price_keys:
        value = node.get(key)
        if isinstance(value, Mapping):
            currency = value.get("currency") or value.get("currencyCode")
            if isinstance(currency, str) and currency.strip():
                return currency.strip().upper()
        if isinstance(value, str):
            for code in ("TL", "TRY", "₺"):
                if code in value:
                    return "TRY"
    return None


class Scraper(ABC):
    """Common interface every storefront scraper implements."""

    #: Short identifier used in logs and in `watchlist.json`.
    name: ClassVar[str] = "base"
    #: Human readable name used in Telegram messages.
    display_name: ClassVar[str] = "Site"
    #: `fnmatch` patterns matched against the URL host.
    host_patterns: ClassVar[tuple[str, ...]] = ()

    def supports(self, url: str) -> bool:
        """Return `True` when this scraper can handle `url`.

        Patterns are compared against the hostname only, and a leading `*.` is
        treated as "this domain or a subdomain" - so `www.evil-trendyol.com`
        does not sneak past the Trendyol scraper.
        """
        host = _host_of(url)
        if not host:
            return False
        for pattern in self.host_patterns:
            base = pattern.lower().removeprefix("*.")
            if host == base or host.endswith("." + base):
                return True
        return False

    @abstractmethod
    async def fetch(self, url: str, *, client: httpx.AsyncClient | None = None) -> PriceResult:
        """Return the current price of `url`.

        Raises:
            ScraperFetchError: download failed after the retries.
            ScraperParseError: page downloaded but no price found.
        """


class BaseScraper(Scraper):
    """Download + parse pipeline shared by the concrete scrapers.

    Subclasses only declare constants (and optionally :meth:`api_url`). The
    pipeline is: JSON endpoint -> embedded JSON -> JSON-LD -> HTML regex.
    """

    #: `format` template of a JSON product endpoint; `None` skips that step.
    api_url_template: ClassVar[str | None] = None

    #: Candidate paths to the product node inside a JSON payload.
    json_product_paths: ClassVar[tuple[tuple[str, ...], ...]] = ()
    #: JSON keys holding the current selling price.
    json_price_keys: ClassVar[tuple[str, ...]] = ("price", "discountedPrice", "sellingPrice")
    #: JSON keys holding the strikethrough (pre-discount) price.
    json_list_price_keys: ClassVar[tuple[str, ...]] = ("listPrice", "originalPrice", "marketPrice")
    #: JSON keys holding the product title.
    json_name_keys: ClassVar[tuple[str, ...]] = ("name", "productName", "title")
    #: JSON keys holding stock information (booleans or counts).
    json_stock_keys: ClassVar[tuple[str, ...]] = ("inStock", "hasStock", "available", "stockCount")
    #: `window.__X__ = {...}` variables carrying the server rendered state.
    embedded_json_vars: ClassVar[tuple[str, ...]] = ()

    #: HTML regex fallbacks, capture group 1 is the price text.
    html_price_patterns: ClassVar[tuple[str, ...]] = ()
    #: HTML regex fallbacks for the strikethrough price, capture group 1.
    html_list_price_patterns: ClassVar[tuple[str, ...]] = ()
    #: HTML regex fallbacks for the title, capture group 1.
    html_name_patterns: ClassVar[tuple[str, ...]] = ()
    #: Markers that mean "out of stock" in the rendered HTML.
    html_out_of_stock_markers: ClassVar[tuple[str, ...]] = ()

    def __init__(
        self,
        *,
        min_delay_s: float = 2.0,
        max_delay_s: float = 6.0,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        user_agents: Sequence[str] = DEFAULT_USER_AGENTS,
        rng: random.Random | None = None,
    ) -> None:
        """
        Args:
            min_delay_s / max_delay_s: Random politeness delay before every
                request. Set both to 0 in tests.
            max_retries: Attempts per URL before giving up.
            timeout_s: HTTP timeout of the internally created client.
            user_agents: Rotating UA pool.
            rng: Injectable RNG so tests are deterministic.
        """
        self._min_delay_s = max(0.0, min_delay_s)
        self._max_delay_s = max(self._min_delay_s, max_delay_s)
        self._max_retries = max(1, max_retries)
        self._timeout_s = timeout_s
        self._user_agents = tuple(user_agents) or DEFAULT_USER_AGENTS
        self._rng = rng or random.Random()  # jitter and UA rotation, not security

    # -- public API -------------------------------------------------------- #
    def api_url(self, url: str) -> str | None:
        """Return the JSON endpoint for `url`, or `None` when unsupported."""
        return None

    async def fetch(self, url: str, *, client: httpx.AsyncClient | None = None) -> PriceResult:
        """Measure the price of `url`, preferring JSON over HTML."""
        owns_client = client is None
        http = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_s, connect=10.0), follow_redirects=True
        )
        try:
            return await self._fetch_pipeline(http, url)
        finally:
            if owns_client:
                await http.aclose()

    # -- pipeline ---------------------------------------------------------- #
    async def _fetch_pipeline(self, http: httpx.AsyncClient, url: str) -> PriceResult:
        api_url = self.api_url(url)
        if api_url:
            try:
                payload = await self._get_json(http, api_url)
            except ScraperError as exc:
                LOGGER.info("[%s] JSON endpoint unavailable (%s); using HTML", self.name, exc)
            else:
                try:
                    return self.parse_json(payload, url)
                except ScraperError as exc:
                    LOGGER.info("[%s] JSON payload unusable (%s); using HTML", self.name, exc)

        html = await self._get_text(http, url)

        for variable in self.embedded_json_vars:
            payload = extract_json_assignment(html, variable)
            if payload is None:
                continue
            try:
                result = self.parse_json(payload, url)
            except ScraperError as exc:
                LOGGER.debug("[%s] embedded %s unusable: %s", self.name, variable, exc)
                continue
            return result

        json_ld = self.parse_json_ld(html, url)
        if json_ld is not None:
            return json_ld

        return self.parse_html_regex(html, url)

    async def _get_json(self, http: httpx.AsyncClient, url: str) -> Any:
        response = await self._request(http, url)
        try:
            return response.json()
        except ValueError as exc:
            msg = f"HTTP {response.status_code} but the body was not JSON"
            raise ScraperParseError(msg) from exc

    async def _get_text(self, http: httpx.AsyncClient, url: str) -> str:
        response = await self._request(http, url)
        return response.text

    async def _request(self, http: httpx.AsyncClient, url: str) -> httpx.Response:
        """GET with politeness delay and exponential backoff on 403/429/5xx."""
        attempt = 1
        headers = self._headers()
        while True:
            await self._politeness_delay()
            failure: ScraperFetchError | None = None
            retry_after: float | None = None
            try:
                response = await http.get(url, headers=headers, follow_redirects=True)
            except httpx.HTTPError as exc:
                failure = ScraperFetchError(f"network error: {type(exc).__name__}")
            else:
                if response.status_code in RETRYABLE_STATUS_CODES:
                    retry_after = _retry_after_seconds(response)
                    failure = ScraperFetchError(
                        f"HTTP {response.status_code}", status_code=response.status_code
                    )
                elif response.status_code >= 400:
                    msg = f"HTTP {response.status_code}"
                    raise ScraperFetchError(msg, status_code=response.status_code)
                else:
                    return response

            # Every branch above returns, raises, or assigns `failure`.
            assert failure is not None
            if attempt >= self._max_retries:
                raise failure
            delay = retry_after or self._backoff_delay(attempt)
            LOGGER.warning(
                "[%s] %s for %s; retrying in %.1fs (%d/%d)",
                self.name,
                failure,
                url,
                delay,
                attempt + 1,
                self._max_retries,
            )
            await asyncio.sleep(delay)
            attempt += 1

    async def _politeness_delay(self) -> None:
        if self._max_delay_s > 0:
            await asyncio.sleep(self._rng.uniform(self._min_delay_s, self._max_delay_s))

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with jitter: ~1s, 2s, 4s (+/- 30%)."""
        base = 2.0 ** (attempt - 1)
        return base * self._rng.uniform(0.7, 1.3)

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self._rng.choice(self._user_agents),
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "DNT": "1",
        }

    # -- parsing ----------------------------------------------------------- #
    def parse_json(self, payload: Any, url: str) -> PriceResult:
        """Build a :class:`PriceResult` from a JSON payload (API or embedded)."""
        node = self._find_product_node(payload)
        if node is None:
            msg = "no product node found in the JSON payload"
            raise ScraperParseError(msg)

        price = self._first_price(node, self.json_price_keys)
        if price is None:
            msg = f"product node has no price ({', '.join(self.json_price_keys)})"
            raise ScraperParseError(msg)

        list_price = self._first_price(node, self.json_list_price_keys)
        if list_price is not None and list_price <= price:
            list_price = None

        name = _string_from_keys(node, self.json_name_keys) or ""
        return PriceResult(
            product_name=name,
            price=price,
            list_price=list_price,
            currency=_currency_from(node, self.json_price_keys) or DEFAULT_CURRENCY,
            in_stock=self._stock_from_json(node),
            url=url,
            source="json",
        )

    def parse_json_ld(self, html: str, url: str) -> PriceResult | None:
        """Try the schema.org product markup, returning `None` when absent."""
        for document in extract_json_ld(html):
            for candidate in _iter_mappings(document):
                if not _looks_like_product(candidate):
                    continue
                offers = candidate.get("offers")
                price = _offer_price(offers)
                if price is None:
                    continue
                list_price = _offer_price(offers, key="highPrice")
                if list_price is not None and list_price <= price:
                    list_price = None
                name = candidate.get("name")
                return PriceResult(
                    product_name=strip_html(name) if isinstance(name, str) else "",
                    price=price,
                    list_price=list_price,
                    currency=_offer_currency(offers) or DEFAULT_CURRENCY,
                    in_stock=_offer_in_stock(offers),
                    url=url,
                    source="json-ld",
                )
        return None

    def parse_html_regex(self, html: str, url: str) -> PriceResult:
        """Last resort: pull the price out of the rendered HTML."""
        raw_price = first_match(self.html_price_patterns, html)
        price = parse_price(strip_html(raw_price) if raw_price else None)
        if price is None:
            msg = "no price found in the HTML (patterns may be outdated)"
            raise ScraperParseError(msg)

        raw_list = first_match(self.html_list_price_patterns, html)
        list_price = parse_price(strip_html(raw_list) if raw_list else None)
        if list_price is not None and list_price <= price:
            list_price = None

        raw_name = first_match(self.html_name_patterns, html)
        name = strip_html(raw_name) if raw_name else ""
        text = strip_html(html)
        out_of_stock = any(marker in text for marker in self.html_out_of_stock_markers)
        return PriceResult(
            product_name=name,
            price=price,
            list_price=list_price,
            currency=DEFAULT_CURRENCY,
            in_stock=not out_of_stock,
            url=url,
            source="html",
        )

    # -- parsing helpers --------------------------------------------------- #
    def _find_product_node(self, payload: Any) -> Mapping[str, Any] | None:
        for path in self.json_product_paths:
            candidate = _dig(payload, path)
            if isinstance(candidate, Mapping):
                return candidate
        return _search_for_product(payload, self.json_price_keys)

    def _first_price(self, node: Mapping[str, Any], keys: Sequence[str]) -> float | None:
        for key in keys:
            price = _price_from_value(node.get(key))
            if price is not None:
                return price
        return None

    def _stock_from_json(self, node: Mapping[str, Any]) -> bool:
        for key in self.json_stock_keys:
            value = node.get(key)
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value > 0
            if isinstance(value, Mapping):
                nested = value.get("inStock") or value.get("available")
                if isinstance(nested, bool):
                    return nested
        return True


def _iter_mappings(node: Any) -> Iterable[Mapping[str, Any]]:
    """Yield every mapping inside a JSON-LD document, including `@graph`."""
    if isinstance(node, Mapping):
        yield node
        for value in node.values():
            yield from _iter_mappings(value)
    elif isinstance(node, list):
        for entry in node:
            yield from _iter_mappings(entry)


def _looks_like_product(node: Mapping[str, Any]) -> bool:
    node_type = node.get("@type")
    types = [node_type] if isinstance(node_type, str) else list(node_type or [])
    return any(str(entry).lower() == "product" for entry in types)


def _offer_price(offers: Any, *, key: str = "price") -> float | None:
    if isinstance(offers, list):
        for entry in offers:
            price = _offer_price(entry, key=key)
            if price is not None:
                return price
        return None
    if isinstance(offers, Mapping):
        direct = _price_from_value(offers.get(key))
        if direct is not None:
            return direct
        specification = offers.get("priceSpecification")
        return _price_from_value(_dig(specification, (key,)))
    return None


def _offer_currency(offers: Any) -> str | None:
    if isinstance(offers, list):
        for entry in offers:
            currency = _offer_currency(entry)
            if currency:
                return currency
        return None
    if isinstance(offers, Mapping):
        currency = offers.get("priceCurrency")
        return currency.upper() if isinstance(currency, str) else None
    return None


def _offer_in_stock(offers: Any) -> bool:
    if isinstance(offers, list):
        return all(_offer_in_stock(entry) for entry in offers if isinstance(entry, Mapping))
    if isinstance(offers, Mapping):
        availability = offers.get("availability")
        if isinstance(availability, str):
            return "outofstock" not in availability.lower().replace(" ", "")
    return True


def _retry_after_seconds(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After")
    if not header:
        return None
    try:
        return min(max(float(header), 1.0), 60.0)
    except ValueError:
        return None
