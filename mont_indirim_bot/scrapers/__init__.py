"""Storefront scrapers and the registry that picks one for a URL."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from mont_indirim_bot.scrapers.base import (
    BaseScraper,
    PriceResult,
    Scraper,
    ScraperError,
    ScraperFetchError,
    ScraperParseError,
    normalize_url,
)
from mont_indirim_bot.scrapers.hepsiburada import HepsiburadaScraper
from mont_indirim_bot.scrapers.trendyol import TrendyolScraper

__all__ = [
    "BaseScraper",
    "HepsiburadaScraper",
    "PriceResult",
    "Scraper",
    "ScraperError",
    "ScraperFetchError",
    "ScraperParseError",
    "TrendyolScraper",
    "default_scrapers",
    "find_scraper",
    "is_supported_url",
    "normalize_url",
    "supported_site_names",
]

#: Scraper classes in priority order.
SCRAPER_CLASSES: tuple[type[BaseScraper], ...] = (TrendyolScraper, HepsiburadaScraper)


def default_scrapers(**kwargs: Any) -> list[Scraper]:
    """Instantiate every known scraper.

    Keyword arguments (delays, retries, timeout) are forwarded to the scraper
    constructors, which makes tests able to disable the politeness delay.
    """
    return [scraper_class(**kwargs) for scraper_class in SCRAPER_CLASSES]


def find_scraper(url: str, scrapers: Sequence[Scraper] | None = None) -> Scraper | None:
    """Return the first scraper able to handle `url`, or `None`."""
    pool = list(scrapers) if scrapers is not None else default_scrapers()
    return next((scraper for scraper in pool if scraper.supports(url)), None)


def is_supported_url(url: str, scrapers: Sequence[Scraper] | None = None) -> bool:
    """Return `True` when some scraper can handle `url`."""
    return find_scraper(url, scrapers) is not None


def supported_site_names(scrapers: Sequence[Scraper] | None = None) -> tuple[str, ...]:
    """Human readable names of the supported sites, for help messages."""
    pool = list(scrapers) if scrapers is not None else default_scrapers()
    return tuple(scraper.display_name for scraper in pool)
