"""Trendyol scraper.

Field knowledge lives in the class constants below. The storefront sits behind
Cloudflare and answers a plain HTTP client with 403 (see
:mod:`mont_indirim_bot.scrapers.http`), so requests go through a browser
fingerprint. There is no reachable public JSON product endpoint any more, so the
pipeline reads the schema.org block (`application/ld+json`) and the
server-rendered state before falling back to regular expressions over the HTML.
"""

from __future__ import annotations

from typing import ClassVar

from mont_indirim_bot.scrapers.base import BaseScraper

__all__ = ["TrendyolScraper"]

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

    # The public JSON endpoint the frontend used to call no longer resolves, so
    # the HTML pipeline (JSON-LD, then regex) is the only reliable source.
    api_url_template: ClassVar[str | None] = None

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
