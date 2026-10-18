"""Tests for the Turkish price parsing/formatting helpers."""

from __future__ import annotations

import pytest

from mont_indirim_bot.prices import currency_symbol, format_percent, format_price, parse_price


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.299,00 TL", 1299.0),
        ("1.299,00", 1299.0),
        ("₺1.299,00", 1299.0),
        ("1.299,00TL", 1299.0),
        ("1299 TL", 1299.0),
        ("1299", 1299.0),
        ("9,99 TL", 9.99),
        ("9.99", 9.99),
        ("1.299 TL", 1299.0),
        ("1,299", 1299.0),
        ("1.299.000,50 TL", 1299000.5),
        ("12,5", 12.5),
        ("  2.499,90\u00a0TL ", 2499.9),
        ("Fiyat yok", None),
        ("", None),
        (None, None),
        (0, None),
        (False, None),
        (1299, 1299.0),
        (1299.5, 1299.5),
        (["1.299,00 TL"], None),
    ],
)
def test_parse_price(raw, expected):
    assert parse_price(raw) == expected


def test_parse_price_picks_the_first_price_of_the_text():
    assert parse_price("Eski fiyat 1.799,00 TL yeni fiyat 1.299,00 TL") == 1799.0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1299.0, "1.299,00 TL"),
        (1299.5, "1.299,50 TL"),
        (5.0, "5,00 TL"),
        (1299000.5, "1.299.000,50 TL"),
        (None, "-"),
    ],
)
def test_format_price(value, expected):
    assert format_price(value) == expected


def test_format_price_honours_the_currency():
    assert format_price(1299.0, "USD") == "1.299,00 $"
    assert format_price(1299.0, "chf") == "1.299,00 CHF"


def test_format_percent_uses_turkish_decimal_separator():
    assert format_percent(27.83) == "%27,8"
    assert format_percent(0.0) == "%0,0"
    assert format_percent(None) == "-"


def test_currency_symbol_defaults_to_try():
    assert currency_symbol(None) == "TL"
    assert currency_symbol("try") == "TL"
    assert currency_symbol("eur") == "€"
