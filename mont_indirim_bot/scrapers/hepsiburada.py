"""Hepsiburada scraper.

Product URLs end with `-p-<product code>` (for example `HBCV0000ABCDE`). The
site has no documented public JSON API, so the pipeline uses the unofficial
frontend endpoint as the first attempt and then parses the JSON state embedded
in the HTML document (`window.__PRODUCT_DETAIL_APP_INITIAL_STATE__`) before
giving up on regular expressions over the markup.
"""

from __future__ import annotations

import re
from typing import ClassVar

from mont_indirim_bot.scrapers.base import BaseScraper

__all__ = ["HepsiburadaScraper"]

#: Hepsiburada product URLs end with `-p-<alphanumeric code>`.
PRODUCT_CODE_PATTERN = re.compile(r"-p-([A-Za-z0-9]+)")

JSON_PRICE_KEYS: tuple[str, ...] = (
    "price",
    "priceWithDiscount",
    "discountedPrice",
    "sellingPrice",
    "currentPrice",
)

JSON_LIST_PRICE_KEYS: tuple[str, ...] = (
    "originalPrice",
    "listPrice",
    "priceWithoutDiscount",
    "marketPrice",
    "regularPrice",
)

JSON_NAME_KEYS: tuple[str, ...] = ("name", "productName", "title", "productTitle")

JSON_STOCK_KEYS: tuple[str, ...] = (
    "inStock",
    "isInStock",
    "hasStock",
    "available",
    "stockCount",
    "availableStock",
)

HTML_PRICE_PATTERNS: tuple[str, ...] = (
    r'id="offering-price"[^>]*content="([^"]+)"',
    r'data-test-id="price-current-price"[^>]*>\s*([^<]+)<',
    r'"price"\s*:\s*"?([\d.,]+)"?',
    r'itemprop="price"\s+content="([^"]+)"',
)

HTML_LIST_PRICE_PATTERNS: tuple[str, ...] = (
    r'id="original-price"[^>]*content="([^"]+)"',
    r'data-test-id="price-original-price"[^>]*>\s*([^<]+)<',
    r'"originalPrice"\s*:\s*"?([\d.,]+)"?',
)

HTML_NAME_PATTERNS: tuple[str, ...] = (
    r'<meta\s+property="og:title"\s+content="([^"]+)"',
    r"<h1[^>]*>\s*(.*?)\s*</h1>",
    r'itemprop="name"\s+content="([^"]+)"',
)

HTML_OUT_OF_STOCK_MARKERS: tuple[str, ...] = (
    "Tükendi",
    "Stokta yok",
    "Stok kalmadı",
    "Satışa kapalı",
    "Temin edilemiyor",
)


class HepsiburadaScraper(BaseScraper):
    """Scraper for `hepsiburada.com` and its subdomains."""

    name: ClassVar[str] = "hepsiburada"
    display_name: ClassVar[str] = "Hepsiburada"
    host_patterns: ClassVar[tuple[str, ...]] = ("hepsiburada.com", "*.hepsiburada.com")

    # Unofficial endpoint used by the site's own frontend. It can disappear at
    # any time; when it does the pipeline logs at INFO level and falls back to
    # the embedded JSON / HTML parsing below, so nothing breaks.
    api_url_template: ClassVar[str | None] = (
        "https://www.hepsiburada.com/api/productdetail/getproductdetail/{code}"
    )

    json_product_paths: ClassVar[tuple[tuple[str, ...], ...]] = (
        ("product",),
        ("productDetail",),
        ("data", "product"),
        ("data",),
        ("result",),
    )
    json_price_keys: ClassVar[tuple[str, ...]] = JSON_PRICE_KEYS
    json_list_price_keys: ClassVar[tuple[str, ...]] = JSON_LIST_PRICE_KEYS
    json_name_keys: ClassVar[tuple[str, ...]] = JSON_NAME_KEYS
    json_stock_keys: ClassVar[tuple[str, ...]] = JSON_STOCK_KEYS

    embedded_json_vars: ClassVar[tuple[str, ...]] = (
        "__PRODUCT_DETAIL_APP_INITIAL_STATE__",
        "__PRODUCT_DETAIL_STATE__",
    )

    html_price_patterns: ClassVar[tuple[str, ...]] = HTML_PRICE_PATTERNS
    html_list_price_patterns: ClassVar[tuple[str, ...]] = HTML_LIST_PRICE_PATTERNS
    html_name_patterns: ClassVar[tuple[str, ...]] = HTML_NAME_PATTERNS
    html_out_of_stock_markers: ClassVar[tuple[str, ...]] = HTML_OUT_OF_STOCK_MARKERS

    def api_url(self, url: str) -> str | None:
        """Return the frontend JSON endpoint derived from the product code."""
        if self.api_url_template is None:
            return None
        match = PRODUCT_CODE_PATTERN.search(url)
        if match is None:
            return None
        return self.api_url_template.format(code=match.group(1))
