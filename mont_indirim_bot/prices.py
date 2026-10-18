"""Turkish price string parsing and formatting.

The storefronts render prices in the Turkish convention (`1.299,00 TL`), while
every JSON payload uses the plain `1299.0` form. `parse_price` is the single
place that knows how to read the former, so a format change on a site only has
to be handled here.
"""

from __future__ import annotations

import re

__all__ = [
    "DEFAULT_CURRENCY",
    "currency_symbol",
    "format_percent",
    "format_price",
    "parse_price",
]

#: Currency used when a payload does not state one.
DEFAULT_CURRENCY = "TRY"

_CURRENCY_SYMBOLS: dict[str, str] = {
    "TRY": "TL",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
}

#: Characters that may appear around a price: currency markers and separators.
_NOISE_PATTERN = re.compile(r"[^\d.,\s]", flags=re.UNICODE)
_DIGIT_PATTERN = re.compile(r"\d")
_TOKEN_PATTERN = re.compile(r"\d[\d.,]*")


def _normalize_number(raw: str) -> str | None:
    """Convert one numeric token to a plain `1234.56` string.

    Rules (Turkish convention first, US convention accepted as a fallback):

    * Both separators present -> the last one is the decimal separator.
    * Only `,`: one comma followed by exactly three digits, with no other
      signal, is a thousands separator (`1,299` -> `1299`); otherwise it is a
      decimal separator (`9,99` -> `9.99`).
    * Only `.`: the same rule applies in reverse (`1.299` -> `1299`,
      `9.99` -> `9.99`). Multiple identical separators are always thousands.
    """
    token = raw.strip()
    if not token or not _DIGIT_PATTERN.search(token):
        return None

    last_dot = token.rfind(".")
    last_comma = token.rfind(",")

    if last_dot != -1 and last_comma != -1:
        decimal_sep = "." if last_dot > last_comma else ","
        thousands_sep = "," if decimal_sep == "." else "."
        integer_part, decimal_part = token.rsplit(decimal_sep, 1)
        integer_part = integer_part.replace(thousands_sep, "").replace(decimal_sep, "")
        return _assemble(integer_part, decimal_part)

    separator = "." if last_dot != -1 else ("," if last_comma != -1 else "")
    if not separator:
        return token

    chunks = token.split(separator)
    if len(chunks) > 2:
        # 1.299.000 -> every separator is a thousands separator.
        return "".join(chunks)
    head, tail = chunks
    if len(tail) == 3 and head:
        return head + tail
    return _assemble(head, tail)


def _assemble(integer_part: str, decimal_part: str) -> str:
    integer_digits = "".join(ch for ch in integer_part if ch.isdigit()) or "0"
    decimal_digits = "".join(ch for ch in decimal_part if ch.isdigit())
    if not decimal_digits:
        return integer_digits
    return f"{integer_digits}.{decimal_digits}"


def parse_price(text: object) -> float | None:
    """Parse a Turkish (or plain) price string into a float.

    Args:
        text: Raw price text, e.g. `"1.299,00 TL"`, `"₺1.199"`, `"1299.9"`.
            Numbers are accepted too, because JSON payloads often already
            contain `1299.0`.

    Returns:
        The price as a float, or `None` when no usable number is present.
        `None` (instead of an exception) lets scrapers treat an unparsable
        price exactly like a missing field.

    Examples:
        >>> parse_price("1.299,00 TL")
        1299.0
        >>> parse_price("9,99 TL")
        9.99
        >>> parse_price("Stokta yok")
    """
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        value = float(text)
        return value if value > 0 else None
    if not isinstance(text, str):
        return None

    cleaned = _NOISE_PATTERN.sub(" ", text)
    for match in _TOKEN_PATTERN.finditer(cleaned):
        normalized = _normalize_number(match.group())
        if normalized is None:
            continue
        try:
            value = float(normalized)
        except ValueError:  # pragma: no cover - defensive, regex keeps this unreachable
            continue
        if value > 0:
            return value
    return None


def currency_symbol(currency: str | None) -> str:
    """Return the display symbol for an ISO currency code (`TRY` -> `TL`)."""
    if not currency:
        return _CURRENCY_SYMBOLS[DEFAULT_CURRENCY]
    return _CURRENCY_SYMBOLS.get(currency.upper(), currency.upper())


def format_price(value: float | None, currency: str = DEFAULT_CURRENCY) -> str:
    """Format a price the Turkish way, e.g. `1299.0` -> `"1.299,00 TL"`."""
    if value is None:
        return "-"
    formatted = f"{value:,.2f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    return f"{formatted} {currency_symbol(currency)}"


def format_percent(value: float | None, *, digits: int = 1) -> str:
    """Format a percentage for messages, e.g. `27.83` -> `"%27,8"`."""
    if value is None:
        return "-"
    text = f"{value:.{digits}f}".replace(".", ",")
    return f"%{text}"
