"""Trendyol scraper.

Field knowledge lives in the class constants below. Trendyol ships a public
(undocumented) JSON product endpoint, and the HTML document additionally
contains `window.__PRODUCT_DETAIL_APP_INITIAL_STATE__`. Both are tried before
falling back to regular expressions over the rendered markup.
"""

from __future__ import annotations

import re
from typing import ClassVar

from mont_indirim_bot.scrapers.base import BaseScraper

__all__ = ["TrendyolScraper"]

#: Trendyol product URLs end with `-p-<numeric id>`.
PRODUCT_ID_PATTERN = re.compile(r"-p-(\d+)")

#: Keys holding the selling price in Trendyol payloads.
JSON_PRICE_KEYS: tuple[str, ...] = (
    "price",
    "discountedPrice",
    "sellingPrice",
    "currentPrice",
    "salePrice",
)

#: Keys holding the strikethrough (pre-discount) price.
JSON_LIST_PRICE_KEYS: tuple[str, ...] = (
    "listPrice",
    "originalPrice",
    "regularPrice",
    "marketPrice",
    "strikethroughPrice",
)

JSON_NAME_KEYS: tuple[str, ...] = ("name", "productName", "title", "displayName")

JSON_STOCK_KEYS: tuple[str, ...] = ("inStock", "hasStock", "available", "stockCount", "quantity")

#: HTML price fallbacks (capture group 1 is the price text).
HTML_PRICE_PATTERNS: tuple[str, ...] = (
    r'"discountedPrice"\s*:\s*\{\s*"value"\s*:\s*([\d.,]+)',
    r'"price"\s*:\s*\{\s*"value"\s*:\s*([\d.,]+)',
    r'class="prc-dsc[^"]*"[^>]*>\s*([^<]+)<',
    r'itemprop="price"\s+content="([^"]+)"',
    r'data-testid="price-current-price"[^>]*>\s*([^<]+)<',
)

HTML_LIST_PRICE_PATTERNS: tuple[str, ...] = (
    r'"listPrice"\s*:\s*\{\s*"value"\s*:\s*([\d.,]+)',
    r'class="prc-org[^"]*"[^>]*>\s*([^<]+)<',
    r'itemprop="highPrice"\s+content="([^"]+)"',
)

HTML_NAME_PATTERNS: tuple[str, ...] = (
    r'<meta\s+property="og:title"\s+content="([^"]+)"',
    r'<h1[^>]*class="[^"]*prc-detail[^"]*"[^>]*>(.*?)</h1>',
    r"<h1[^>]*>(.*?)</h1>",
)

#: Text that marks a product as unavailable on the rendered page.
HTML_OUT_OF_STOCK_MARKERS: tuple[str, ...] = (
    "Tükendi",
    "Stokta yok",
    "Stok kalmadı",
    "Satışa kapalı",
    "Bu ürün şu an satışa uygun değildir",
)


class TrendyolScraper(BaseScraper):
    """Scraper for `trendyol.com` and its subdomains."""

    name: ClassVar[str] = "trendyol"
    display_name: ClassVar[str] = "Trendyol"
    host_patterns: ClassVar[tuple[str, ...]] = ("trendyol.com", "*.trendyol.com")

    api_url_template: ClassVar[str | None] = (
        "https://public.trendyol.com/discovery-web-productgw-service/api/productDetail/{product_id}"
    )

    json_product_paths: ClassVar[tuple[tuple[str, ...], ...]] = (
        ("result", "product"),
        ("product",),
        ("result",),
        ("data", "product"),
        ("data",),
    )
    json_price_keys: ClassVar[tuple[str, ...]] = JSON_PRICE_KEYS
    json_list_price_keys: ClassVar[tuple[str, ...]] = JSON_LIST_PRICE_KEYS
    json_name_keys: ClassVar[tuple[str, ...]] = JSON_NAME_KEYS
    json_stock_keys: ClassVar[tuple[str, ...]] = JSON_STOCK_KEYS

    embedded_json_vars: ClassVar[tuple[str, ...]] = ("__PRODUCT_DETAIL_APP_INITIAL_STATE__",)

    html_price_patterns: ClassVar[tuple[str, ...]] = HTML_PRICE_PATTERNS
    html_list_price_patterns: ClassVar[tuple[str, ...]] = HTML_LIST_PRICE_PATTERNS
    html_name_patterns: ClassVar[tuple[str, ...]] = HTML_NAME_PATTERNS
    html_out_of_stock_markers: ClassVar[tuple[str, ...]] = HTML_OUT_OF_STOCK_MARKERS

    def api_url(self, url: str) -> str | None:
        """Return the JSON product endpoint derived from the product id."""
        if self.api_url_template is None:
            return None
        match = PRODUCT_ID_PATTERN.search(url)
        if match is None:
            return None
        return self.api_url_template.format(product_id=match.group(1))
