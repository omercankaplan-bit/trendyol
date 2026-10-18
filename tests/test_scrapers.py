"""Tests for the storefront scrapers, driven by saved HTML/JSON fixtures."""

from __future__ import annotations

import httpx
import pytest

from mont_indirim_bot.scrapers import (
    HepsiburadaScraper,
    ScraperFetchError,
    ScraperParseError,
    TrendyolScraper,
    default_scrapers,
    find_scraper,
    is_supported_url,
    normalize_url,
    product_id,
    supported_site_names,
)
from tests.helpers import (
    HEPSIBURADA_URL,
    TRENDYOL_URL,
    make_client,
    read_fixture,
    read_json_fixture,
)


@pytest.fixture
def trendyol() -> TrendyolScraper:
    return TrendyolScraper(min_delay_s=0.0, max_delay_s=0.0)


@pytest.fixture
def hepsiburada() -> HepsiburadaScraper:
    return HepsiburadaScraper(min_delay_s=0.0, max_delay_s=0.0)


@pytest.fixture
def trendyol_fast_retry(monkeypatch: pytest.MonkeyPatch) -> TrendyolScraper:
    """Trendyol scraper with the backoff sleep neutralised (fast retry tests)."""
    scraper = TrendyolScraper(min_delay_s=0.0, max_delay_s=0.0, max_retries=3)
    monkeypatch.setattr(scraper, "_backoff_delay", lambda _attempt: 0.0)
    return scraper


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.trendyol.com/a/b-p-1", True),
        ("https://trendyol.com/a/b-p-1", True),
        ("https://m.trendyol.com/a/b-p-1", True),
        ("https://www.hepsiburada.com/a-p-HBCV1", False),
        ("https://www.evil-trendyol.com/a/b-p-1", False),
        ("https://not-trendyol.com.tr/a/b-p-1", False),
        ("", False),
        ("not a url", False),
    ],
)
def test_scraper_supports_matches_the_host_only(trendyol: TrendyolScraper, url, expected):
    assert trendyol.supports(url) is expected


def test_scraper_supports_accepts_subdomains_of_hepsiburada(hepsiburada: HepsiburadaScraper):
    assert hepsiburada.supports("https://www.hepsiburada.com/a-p-HBCV1") is True
    assert hepsiburada.supports("https://hepsiburada.com/a-p-HBCV1") is True
    assert hepsiburada.supports("https://www.trendyol.com/a/b-p-1") is False


def test_registry_picks_a_scraper_per_url():
    trendyol = find_scraper(TRENDYOL_URL)
    hepsiburada = find_scraper(HEPSIBURADA_URL)
    assert trendyol is not None
    assert hepsiburada is not None
    assert trendyol.name == "trendyol"
    assert hepsiburada.name == "hepsiburada"
    assert find_scraper("https://www.amazon.com.tr/x-p-1") is None
    assert is_supported_url(TRENDYOL_URL) is True
    assert is_supported_url("https://www.amazon.com.tr/x-p-1") is False
    assert supported_site_names() == ("Trendyol", "Hepsiburada")


def test_default_scrapers_forwards_the_constructor_arguments():
    scrapers = default_scrapers(min_delay_s=0.0, max_delay_s=0.0)

    assert [scraper.name for scraper in scrapers] == ["trendyol", "hepsiburada"]


def test_normalize_url_strips_query_and_trailing_slash():
    assert normalize_url(TRENDYOL_URL + "?boutiqueId=61") == normalize_url(TRENDYOL_URL + "/")


def test_scrapers_rely_on_the_html_pipeline(trendyol: TrendyolScraper, hepsiburada):
    """Both JSON endpoints are gone, so `api_url` must be inert."""
    assert trendyol.api_url(TRENDYOL_URL) is None
    assert hepsiburada.api_url(HEPSIBURADA_URL) is None
    assert find_scraper(TRENDYOL_URL) is not None
    assert find_scraper(HEPSIBURADA_URL) is not None


def test_parse_json_reads_the_saved_trendyol_payload(trendyol: TrendyolScraper):
    result = trendyol.parse_json(read_json_fixture("trendyol_product.json"), TRENDYOL_URL)

    assert result.product_name == "Columbia Erkek Pike Lake Mont"
    assert result.price == 1299.0
    assert result.list_price == 1799.0
    assert result.currency == "TRY"
    assert result.in_stock is True
    assert result.source == "json"
    assert result.list_discount_pct == pytest.approx(27.8, abs=0.05)


def test_parse_json_reads_the_saved_hepsiburada_payload(hepsiburada: HepsiburadaScraper):
    result = hepsiburada.parse_json(read_json_fixture("hepsiburada_product.json"), HEPSIBURADA_URL)

    assert result.product_name == "The North Face Erkek Resolve Mont"
    assert result.price == 2349.0
    assert result.list_price == 2999.0


def test_parse_json_accepts_flat_and_turkish_formatted_prices(trendyol: TrendyolScraper):
    flat = trendyol.parse_json(
        {"product": {"name": "Mont", "price": 899.9, "originalPrice": "1.299,00 TL"}},
        TRENDYOL_URL,
    )
    assert flat.price == 899.9
    assert flat.list_price == 1299.0

    formatted = trendyol.parse_json(
        {"product": {"name": "Mont", "price": "1.299,00 TL"}}, TRENDYOL_URL
    )
    assert formatted.price == 1299.0
    assert formatted.list_price is None


def test_parse_json_uppercases_the_currency(trendyol: TrendyolScraper):
    result = trendyol.parse_json(
        {"product": {"name": "Mont", "price": {"value": 10.5, "currency": "usd"}}}, TRENDYOL_URL
    )
    assert result.currency == "USD"


@pytest.mark.parametrize(
    "payload",
    [
        {"product": {"name": "Fiyatsız"}},
        {"totally": "unrelated"},
        [],
        "html",
    ],
)
def test_parse_json_raises_when_there_is_no_price(trendyol: TrendyolScraper, payload):
    with pytest.raises(ScraperParseError):
        trendyol.parse_json(payload, TRENDYOL_URL)


@pytest.mark.parametrize(
    "node",
    [
        {"name": "Mont", "price": 10.0, "inStock": False},
        {"name": "Mont", "price": 10.0, "stockCount": 0},
    ],
)
def test_parse_json_detects_out_of_stock(trendyol: TrendyolScraper, node):
    result = trendyol.parse_json({"product": node}, TRENDYOL_URL)
    assert result.in_stock is False


def test_parse_json_ld_reads_schema_org_offers(trendyol: TrendyolScraper):
    result = trendyol.parse_json_ld(read_fixture("trendyol_product.html"), TRENDYOL_URL)

    assert result is not None
    assert result.price == 1499.0
    assert result.currency == "TRY"
    assert result.source == "json-ld"
    assert result.product_name == "Columbia Erkek Pike Lake Mont"


def test_parse_json_ld_returns_none_without_markup(trendyol: TrendyolScraper):
    assert trendyol.parse_json_ld("<html><body>yok</body></html>", TRENDYOL_URL) is None


def test_product_id_reads_the_marketplace_id():
    assert product_id(TRENDYOL_URL) == "123456789"
    assert product_id(HEPSIBURADA_URL) == "HBCV0000123456"
    assert product_id("https://www.trendyol.com/sr?q=erkek%20mont") is None


def test_parse_json_ld_ignores_the_prices_of_sibling_variants(trendyol: TrendyolScraper):
    # Trendyol groups every colourway into one ProductGroup; the variants carry
    # their own prices, and the first one is another colour.
    url = "https://www.trendyol.com/lufian/tommy-kaz-tuyu-erkek-mont-lacivert-p-860938793"

    result = trendyol.parse_json_ld(read_fixture("trendyol_product_group.html"), url)

    assert result is not None
    assert result.price == 9999.99
    assert result.product_name == "LUFIAN Tommy Kaz Tüyü Erkek Mont Lacivert"
    assert result.in_stock is True


def test_parse_json_ld_still_finds_a_nested_product(trendyol: TrendyolScraper):
    # The top level holds no product here, so the walk has to descend into it.
    html = (
        '<script type="application/ld+json">{"@type":"ItemList","itemListElement":['
        '{"@type":"Product","name":"Nested Mont","offers":{"price":"1.299,00 TL"}}]}</script>'
    )

    result = trendyol.parse_json_ld(html, TRENDYOL_URL)

    assert result is not None
    assert result.price == 1299.0
    assert result.product_name == "Nested Mont"


def test_parse_html_regex_is_the_last_resort(trendyol: TrendyolScraper):
    result = trendyol.parse_html_regex(
        read_fixture("trendyol_product_html_only.html"), TRENDYOL_URL
    )

    assert result.price == 1999.5
    assert result.list_price == 2499.0
    assert result.product_name == "The North Face Kadın Mont"
    assert result.source == "html"


def test_parse_html_regex_marks_the_product_out_of_stock(trendyol: TrendyolScraper):
    html = (
        '<h1 class="prc-detail-title">Mont</h1><span class="prc-dsc">999,00 TL</span><p>Tükendi</p>'
    )
    result = trendyol.parse_html_regex(html, TRENDYOL_URL)

    assert result.in_stock is False


def test_parse_html_regex_raises_when_no_price_matches(trendyol: TrendyolScraper):
    with pytest.raises(ScraperParseError):
        trendyol.parse_html_regex("<html><body>fiyat yok</body></html>", TRENDYOL_URL)


async def test_fetch_reads_the_embedded_state(trendyol: TrendyolScraper):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text=read_fixture("trendyol_product.html"))

    async with make_client(handler) as http:
        result = await trendyol.fetch(TRENDYOL_URL, client=http)

        assert result.price == 1299.0
        assert result.product_name == "Columbia Erkek Pike Lake Mont"
        assert result.source == "json"  # the window state embedded in the HTML
        assert calls == [TRENDYOL_URL]
        assert http.is_closed is False  # injected clients stay open


async def test_fetch_falls_back_to_json_ld(trendyol: TrendyolScraper):
    html = (
        "<html><head>"
        '<link rel="canonical" href="https://www.trendyol.com/x/mont-p-123456789" />'
        '<script type="application/ld+json">'
        '{"@type":"Product","name":"Mont","offers":{"price":"1.234,00","priceCurrency":"TRY"}}'
        "</script></head><body></body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    async with make_client(handler) as http:
        result = await trendyol.fetch(TRENDYOL_URL, client=http)

    assert result.price == 1234.0
    assert result.currency == "TRY"
    assert result.source == "json-ld"


async def test_fetch_falls_back_to_the_html_regex(trendyol: TrendyolScraper):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=read_fixture("trendyol_product_html_only.html"))

    async with make_client(handler) as http:
        result = await trendyol.fetch(TRENDYOL_URL, client=http)

    assert result.price == 1999.5
    assert result.source == "html"


async def test_fetch_retries_retryable_status_codes(
    trendyol_fast_retry: TrendyolScraper, monkeypatch: pytest.MonkeyPatch
):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="service unavailable")

    async with make_client(handler) as http:
        with pytest.raises(ScraperFetchError):
            await trendyol_fast_retry.fetch(TRENDYOL_URL, client=http)

    # One URL, so only the three attempts of a single fetch.
    assert calls["n"] == 3


async def test_fetch_does_not_retry_client_errors(
    trendyol_fast_retry: TrendyolScraper,
):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text="gone")

    async with make_client(handler) as http:
        with pytest.raises(ScraperFetchError):
            await trendyol_fast_retry.fetch(TRENDYOL_URL, client=http)

    assert calls["n"] == 1  # a 404 is final, not retried


async def test_fetch_reports_a_parse_error_when_everything_fails(trendyol_fast_retry):
    html = (
        '<html><head><link rel="canonical" href="https://www.trendyol.com/x/m-p-123456789" />'
        "</head><body>fiyat yok</body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    async with make_client(handler) as http:
        with pytest.raises(ScraperParseError):
            await trendyol_fast_retry.fetch(TRENDYOL_URL, client=http)


async def test_fetch_rejects_a_page_that_is_not_the_product(trendyol: TrendyolScraper):
    # A discontinued product redirects to a listing page, which carries plenty of
    # prices of its own; none of them belong to the watched item.
    html = "<html><body><span>1.299,00 TL</span><span>2.499,00 TL</span></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    async with make_client(handler) as http:
        with pytest.raises(ScraperParseError, match="removed"):
            await trendyol.fetch(TRENDYOL_URL, client=http)


async def test_fetch_passes_the_proxy_to_the_default_client(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, object] = {}

    class StubClient:
        def __init__(self, *, timeout_s: float, proxy: str | None = None) -> None:
            captured["timeout_s"] = timeout_s
            captured["proxy"] = proxy

        async def get(self, url, *, headers=None, follow_redirects=True):
            return httpx.Response(200, text=read_fixture("trendyol_product.html"))

        async def aclose(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr("mont_indirim_bot.scrapers.base.BrowserClient", StubClient)
    scraper = TrendyolScraper(min_delay_s=0.0, max_delay_s=0.0, proxy="http://user:pw@proxy.test")

    result = await scraper.fetch(TRENDYOL_URL)

    assert result.price == 1299.0
    assert captured["proxy"] == "http://user:pw@proxy.test"
    assert captured["closed"] is True  # an internally owned client is released


async def test_fetch_pipeline_for_hepsiburada(hepsiburada: HepsiburadaScraper):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text=read_fixture("hepsiburada_product.html"))

    async with make_client(handler) as http:
        result = await hepsiburada.fetch(HEPSIBURADA_URL, client=http)

    assert result.price == 2349.0
    assert result.list_price == 2999.0
    assert result.product_name == "The North Face Erkek Resolve Mont"
    assert calls == [HEPSIBURADA_URL]
