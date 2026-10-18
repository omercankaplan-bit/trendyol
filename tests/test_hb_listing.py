"""Tests for the Hepsiburada listing reader used by the brand scan."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from mont_indirim_bot.scrapers.base import ScraperFetchError
from mont_indirim_bot.scrapers.hepsiburada_listing import (
    ListingClient,
    ListingProduct,
    build_listing_url,
    fold_turkish,
    is_jacket,
    matches_brand,
    parse_listing,
)
from tests.helpers import read_fixture

CARD_WITHOUT_PRICE = """
<article class="productCard-module_article__x">
  <a title="AVVA Erkek Mont" href="/avva-erkek-mont-p-HBCV0000000001"><h2>AVVA</h2></a>
  <div class="price-module_priceInfo__y"></div>
</article>
"""

CARD_WITHOUT_LINK = """
<article class="productCard-module_article__x">
  <h2 class="title-module_titleRoot__z">AVVA Erkek Mont</h2>
  <div class="price-module_finalPrice__y" data-test-id="final-price-1">4.499
    <span>,99 TL</span>
  </div>
</article>
"""

DUPLICATE_CARD = """
<article class="productCard-module_article__x">
  <a title="AVVA Erkek İndigo Mont" href="/avva-erkek-indigo-mont-p-HBCV000077DPUD"><h2>x</h2></a>
  <div class="price-module_finalPrice__y" data-test-id="final-price-9">
    1.111<span>,00 TL</span>
  </div>
</article>
"""


class FakeResponse:
    """Minimal `HttpResponse` stand-in."""

    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text
        self.headers: Mapping[str, Any] = {}

    def json(self) -> Any:
        raise ValueError("no json in a listing")


class FakeHttp:
    """`HttpClient` stand-in that replays one response per request."""

    def __init__(self, statuses: list[int], text: str = "") -> None:
        self.statuses = list(statuses)
        self.text = text
        self.calls: list[str] = []
        self.closed = False

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        follow_redirects: bool = True,
    ) -> FakeResponse:
        del headers, follow_redirects
        self.calls.append(url)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return FakeResponse(status, self.text)

    async def aclose(self) -> None:
        self.closed = True


def test_parse_listing_reads_name_price_and_link():
    products = parse_listing(read_fixture("hepsiburada_listing.html"))

    assert [product.code for product in products] == [
        "HBCV000077DPUD",
        "HBCV0000ATK9HN",
        "HBCV00009VBMZ3",
    ]
    first = products[0]
    assert first.price == 4499.99
    assert first.name.startswith("AVVA Erkek İndigo")
    assert first.url.endswith("-p-HBCV000077DPUD")
    assert first.absolute_url == f"https://www.hepsiburada.com{first.url}"


def test_parse_listing_skips_cards_without_a_price_or_a_link():
    html = f"<html><body>{CARD_WITHOUT_PRICE}{CARD_WITHOUT_LINK}</body></html>"

    assert parse_listing(html) == []


def test_parse_listing_collapses_repeated_products():
    fixture = read_fixture("hepsiburada_listing.html")

    products = parse_listing(f"{fixture}{DUPLICATE_CARD}")

    codes = [product.code for product in products]
    assert codes.count("HBCV000077DPUD") == 1
    assert products[0].price == 4499.99


def test_parse_listing_accepts_an_empty_document():
    assert parse_listing("<html><body>Hiç ürün yok</body></html>") == []


def test_parse_listing_falls_back_to_the_heading_name():
    card = """
    <article class="productCard-module_article__x">
      <a href="/avva-erkek-mont-p-HBCV0000000042" title=""><h2>
        AVVA Erkek Şişme Mont
      </h2></a>
      <div class="price-module_finalPrice__y" data-test-id="final-price-1">
        2.199<span>,90 TL</span>
      </div>
    </article>
    """

    products = parse_listing(f"<html>{card}</html>")

    assert [product.name for product in products] == ["AVVA Erkek Şişme Mont"]
    assert products[0].price == 2199.9


@pytest.mark.parametrize(
    ("query", "page", "expected"),
    [
        ("avva erkek mont", 1, "https://www.hepsiburada.com/ara?q=avva+erkek+mont"),
        ("avva erkek mont", 2, "https://www.hepsiburada.com/ara?q=avva+erkek+mont&sayfa=2"),
        (
            "kiğılı erkek mont",
            1,
            "https://www.hepsiburada.com/ara?q=ki%C4%9F%C4%B1l%C4%B1+erkek+mont",
        ),
    ],
)
def test_build_listing_url(query: str, page: int, expected: str):
    assert build_listing_url(query, page) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("AVVA Erkek Şişme Mont", True),
        ("Lufian Kaz Tüyü Erkek Kaban", True),
        ("Columbia Softshell Ceket", True),
        ("Demonte Gardırop", False),
        ("AVVA Erkek Basic Sweatshirt", False),
        ("Columbia Erkek Ayakkabı", False),
    ],
)
def test_is_jacket(name: str, expected: bool):
    assert is_jacket(name) is expected


def test_matches_brand_ignores_case_and_accents():
    assert matches_brand("Kiğılı Dik Yaka Mont", ("kigili",)) is True
    assert matches_brand("AVVA Erkek Mont", ("lufian",)) is False
    assert matches_brand("Herhangi Bir Mont", ()) is True
    assert fold_turkish("İSTANBUL ŞİŞME") == "istanbul sisme"


def test_listing_product_is_hashable_for_set_use():
    product = ListingProduct(code="HBCV1", name="AVVA Mont", url="/x-p-HBCV1", price=10.0)

    assert {product} == {product}


async def test_listing_client_retries_retryable_status_codes():
    http = FakeHttp([403, 200], text="<html>ok</html>")
    client = ListingClient(client=http, min_delay_s=0.0, max_delay_s=0.0, max_retries=2)

    assert await client.fetch("https://www.hepsiburada.com/ara?q=avva") == "<html>ok</html>"
    assert len(http.calls) == 2
    assert http.closed is False


async def test_listing_client_raises_after_exhausting_retries():
    http = FakeHttp([403], text="")
    client = ListingClient(client=http, min_delay_s=0.0, max_delay_s=0.0, max_retries=2)

    with pytest.raises(ScraperFetchError):
        await client.fetch("https://www.hepsiburada.com/ara?q=avva")


async def test_listing_client_does_not_close_an_injected_client():
    http = FakeHttp([200], text="<html></html>")
    client = ListingClient(client=http, min_delay_s=0.0, max_delay_s=0.0)

    await client.aclose()

    assert http.closed is False
