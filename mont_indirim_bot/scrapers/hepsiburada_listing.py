"""Hepsiburada listing pages, read by the brand scan.

The watchlist pins one product per entry, which is precise but brittle: a shop
can delist a product at any time, and a delisted URL often redirects to a
listing page full of other prices. The brand scan takes the opposite approach
and reads the *search result page* of a brand, so every run sees the products
the shop sells right now - new arrivals included.

Hepsiburada renders those pages server side, so the product cards (name, link
and price) are in the HTML and no browser is needed. Prices there are the same
ones the product page shows, but they are only used to detect a *movement*; the
watchlist remains the source of truth for a tracked product.
"""

from __future__ import annotations

import logging
import random
import re
from collections.abc import Sequence
from dataclasses import dataclass
from html import unescape
from urllib.parse import quote_plus

from mont_indirim_bot.prices import parse_price
from mont_indirim_bot.scrapers.base import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_S,
    DEFAULT_USER_AGENTS,
    PRODUCT_ID_PATTERN,
    RequestPolicy,
    browser_headers,
    request_with_retries,
    strip_html,
)
from mont_indirim_bot.scrapers.http import BrowserClient, HttpClient

LOGGER = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_LISTING_QUERY_SUFFIX",
    "JACKET_KEYWORDS",
    "SEARCH_URL",
    "ListingClient",
    "ListingProduct",
    "build_listing_url",
    "fold_turkish",
    "is_jacket",
    "matches_brand",
    "parse_listing",
]

#: Search endpoint of the marketplace.
SEARCH_URL = "https://www.hepsiburada.com/ara"

#: Appended to a brand name to ask for that brand's men's jackets.
DEFAULT_LISTING_QUERY_SUFFIX = "erkek mont"

#: Words that mean "outerwear" in a Turkish product title (folded to ASCII).
JACKET_KEYWORDS: tuple[str, ...] = (
    "mont",
    "parka",
    "kaban",
    "palto",
    "trenckot",
    "softshell",
    "soft shell",
    "anorak",
    "sisme",
    "puffer",
    "ruzgarlik",
    "yagmurluk",
)

#: Turkish letters that have no ASCII equivalent, mapped for comparison.
_TRANSLATION = str.maketrans(
    {
        "ı": "i",
        "İ": "i",
        "ş": "s",
        "Ş": "s",
        "ğ": "g",
        "Ğ": "g",
        "ü": "u",
        "Ü": "u",
        "ö": "o",
        "Ö": "o",
        "ç": "c",
        "Ç": "c",
        "â": "a",
        "î": "i",
        "û": "u",
    }
)

_CARD_PATTERN = re.compile(r"<article\b[^>]*>(.*?)</article>", re.DOTALL | re.IGNORECASE)
_ANCHOR_PATTERN = re.compile(r"<a\b[^>]*>", re.IGNORECASE)
_HREF_PATTERN = re.compile(r'href="(/[^"]*?-p-HB[A-Za-z0-9]+)"', re.IGNORECASE)
_TITLE_PATTERN = re.compile(r'title="([^"]+)"', re.IGNORECASE)
_NAME_PATTERN = re.compile(r"<h2\b[^>]*>(.*?)</h2>", re.DOTALL | re.IGNORECASE)
_PRICE_PATTERN = re.compile(
    r'data-test-id="final-price-\d+"[^>]*>(.*?)</div>', re.DOTALL | re.IGNORECASE
)
_LABEL_PRICE_PATTERN = re.compile(r'aria-label="[^"]*?fiyat:\s*([^"]+)"', re.IGNORECASE)
_JACKET_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(keyword) for keyword in JACKET_KEYWORDS) + r")\b"
)


def fold_turkish(text: str) -> str:
    """Lowercase `text` and drop Turkish diacritics so names compare reliably."""
    return text.translate(_TRANSLATION).lower()


def is_jacket(name: str) -> bool:
    """`True` when a product title names a piece of outerwear."""
    return _JACKET_PATTERN.search(fold_turkish(name)) is not None


def matches_brand(name: str, tokens: Sequence[str]) -> bool:
    """`True` when the title carries one of the brand's (folded) tokens.

    An empty token list means "no constraint", so any title matches.
    """
    if not tokens:
        return True
    folded = fold_turkish(name)
    return any(token in folded for token in tokens)


@dataclass(frozen=True, slots=True)
class ListingProduct:
    """One product card of a listing page."""

    code: str
    name: str
    url: str
    price: float

    @property
    def absolute_url(self) -> str:
        """The product URL as a link the owner can open."""
        return f"https://www.hepsiburada.com{self.url}"


def build_listing_url(query: str, page: int = 1) -> str:
    """URL of the `page`-th search result page for `query`."""
    url = f"{SEARCH_URL}?q={quote_plus(query)}"
    if page > 1:
        url = f"{url}&sayfa={page}"
    return url


def parse_listing(html: str) -> list[ListingProduct]:
    """Extract every readable product card of a listing page.

    Cards without a usable price are skipped: a listing is a snapshot, so one
    broken card must not cost the whole scan. Duplicates (the page repeats some
    products in carousels) are collapsed by product code, keeping the first.
    """
    products: dict[str, ListingProduct] = {}
    for card in _CARD_PATTERN.findall(html):
        product = _parse_card(card)
        if product is not None:
            products.setdefault(product.code, product)
    return list(products.values())


def _parse_card(card: str) -> ListingProduct | None:
    href: str | None = None
    name = ""
    for anchor in _ANCHOR_PATTERN.findall(card):
        match = _HREF_PATTERN.search(anchor)
        if match is None:
            continue
        href = match.group(1)
        title = _TITLE_PATTERN.search(anchor)
        if title is not None:
            name = unescape(title.group(1))
        break
    if href is None:
        return None

    code_match = PRODUCT_ID_PATTERN.search(href)
    if code_match is None:
        return None

    price = _card_price(card)
    if price is None:
        LOGGER.debug("Listing card without a price: %s", href)
        return None

    return ListingProduct(
        code=code_match.group(1),
        name=name or _card_name(card),
        url=href,
        price=price,
    )


def _card_name(card: str) -> str:
    """Fallback product name: the card's own heading."""
    heading = _NAME_PATTERN.search(card)
    if heading is None:
        return ""
    return unescape(strip_html(heading.group(1))).strip()


def _card_price(card: str) -> float | None:
    """The current price shown on a card, from the price element or the label."""
    block = _PRICE_PATTERN.search(card)
    if block is not None:
        # Tags (such as the fraction span) must disappear without leaving a
        # space, otherwise "4.499<span>,99 TL</span>" becomes "4.499 ,99 TL"
        # and the decimal part is lost.
        text = re.sub(r"<[^>]+>", "", block.group(1))
        text = re.sub(r"\s+", " ", text).strip()
        price = parse_price(text)
        if price is not None:
            return price

    label = _LABEL_PRICE_PATTERN.search(card)
    if label is not None:
        return parse_price(unescape(label.group(1)))
    return None


class ListingClient:
    """Fetches listing pages with the delays and retries of the scrapers."""

    def __init__(
        self,
        *,
        client: HttpClient | None = None,
        min_delay_s: float = 2.0,
        max_delay_s: float = 6.0,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        user_agents: Sequence[str] = DEFAULT_USER_AGENTS,
        rng: random.Random | None = None,
        proxy: str | None = None,
    ) -> None:
        """
        Args:
            client: HTTP client to use; one is created (and owned) when omitted.
            min_delay_s / max_delay_s: Random politeness delay before a request.
            max_retries: Attempts per URL before giving up.
            timeout_s: Timeout of the internally created client.
            user_agents: Rotating UA pool.
            rng: Injectable RNG so tests are deterministic.
            proxy: Optional proxy URL (`None` = direct).
        """
        self._owns_client = client is None
        self._http: HttpClient = client or BrowserClient(timeout_s=timeout_s, proxy=proxy)
        self._user_agents = tuple(user_agents) or DEFAULT_USER_AGENTS
        self._rng = rng or random.Random()  # jitter and UA rotation, not security
        self._policy = RequestPolicy(
            name="hb-listing",
            min_delay_s=max(0.0, min_delay_s),
            max_delay_s=max(0.0, max_delay_s),
            max_retries=max(1, max_retries),
            rng=self._rng,
        )

    async def fetch(self, url: str) -> str:
        """Download one listing page.

        Raises:
            ScraperError: the page could not be downloaded.
        """
        response = await request_with_retries(
            self._http,
            url,
            headers=browser_headers(self._user_agents, self._rng),
            policy=self._policy,
        )
        return response.text

    async def aclose(self) -> None:
        """Close the client when this instance created it."""
        if self._owns_client:
            await self._http.aclose()
