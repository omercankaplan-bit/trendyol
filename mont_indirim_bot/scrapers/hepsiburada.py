"""Hepsiburada scraper.

Product URLs end with `-p-<product code>` (for example `HBCV0000ABCDE`). The
site sits behind Cloudflare and answers a plain HTTP client with 403 (see
:mod:`mont_indirim_bot.scrapers.http`), so requests go through a browser
fingerprint. The unofficial frontend JSON endpoint now returns 404, so the
pipeline reads the schema.org block (`application/ld+json`) and the
server-rendered state before falling back to regular expressions over the HTML.
"""

from __future__ import annotations

from typing import ClassVar

from mont_indirim_bot.scrapers.base import BaseScraper

__all__ = ["HepsiburadaScraper"]

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

    # The unofficial frontend endpoint now answers 404 for every product code,
    # so the HTML pipeline (JSON-LD, then regex) is the only reliable source.
    api_url_template: ClassVar[str | None] = None

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
