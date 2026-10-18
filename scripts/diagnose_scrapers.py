#!/usr/bin/env python3
"""Probe what the storefronts really return, from wherever this script runs.

Useful when a scrape suddenly stops working: the answer differs between a home
connection and GitHub's datacenter IPs, and this prints the status, the length
and the structural markers of each response so the cause is visible at a glance.

    python scripts/diagnose_scrapers.py

It also runs from the "Diagnose storefronts" workflow (manual trigger only),
which is the way to see the runner's view of the sites.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence

from curl_cffi.requests import AsyncSession

#: Product pages plus one search page per storefront, and the Akakce listing.
URLS: tuple[str, ...] = (
    "https://www.trendyol.com/avva/erkek-siyah-bomber-yaka-suet-dokulu-mevsimlik-"
    "comfort-fit-mont-b006045-p-835456121",
    "https://www.trendyol.com/sr?q=erkek%20mont",
    "https://www.hepsiburada.com/kigili-dik-yaka-fermuarli-cepli-ruzgara-dayanikli-"
    "sicak-tutan-kapitone-spor-mont-p-HBCV0000C5J4DB",
    "https://www.akakce.com/mont.html",
    "https://www.akakce.com/mont/en-ucuz-suvari-mn2008700316-erkek-kapitone-mont-"
    "fiyati,280542423.html",
)

#: Browser builds to try; a mismatch here is often why a site answers oddly.
IMPERSONATIONS: tuple[str, ...] = ("chrome", "chrome124", "chrome120", "firefox133")

#: Structural markers that say whether a document really carries product data.
MARKERS: tuple[str, ...] = (
    "application/ld+json",
    "__PRODUCT_DETAIL_APP_INITIAL_STATE__",
    "PuzzleJs.emit",
    "prc-dsc",
    "add-to-basket",
    "Tükendi",
    "Just a moment",  # Cloudflare's interstitial instead of the page
    "akakce.com",  # Akakce's own markup (the listing pages)
)

PRICE_PATTERN = re.compile(r"\d[\d.]*,\d{2}\s*TL")


async def probe(impersonate: str, url: str) -> None:
    """Fetch `url` once and print a one-line summary of the response."""
    label = f"{impersonate:<11}"
    try:
        async with AsyncSession(impersonate=impersonate, timeout=25) as session:
            response = await session.get(url)
    except Exception as exc:  # a probe reports whatever happened
        print(f"{label} EXC       {type(exc).__name__}: {str(exc)[:70]}")
        return

    html = response.text or ""
    found = [marker for marker in MARKERS if marker in html]
    prices = PRICE_PATTERN.findall(html)[:3]
    print(
        f"{label} {response.status_code} len={len(html):<7} "
        f"markers={','.join(found) or '-'} prices={prices or '-'}"
    )
    if not found:
        head = " ".join(html.split())[:150]
        print(f"{label} head: {head}")


async def main(urls: Sequence[str] = URLS) -> None:
    """Probe every combination of impersonation and URL."""
    for url in urls:
        print(f"\n=== {url}")
        for impersonate in IMPERSONATIONS:
            await probe(impersonate, url)

    print("\n=== session warm-up (homepage first, cookies kept)")
    await probe_session(URLS[0])


async def probe_session(url: str) -> None:
    """Visit the homepage first so the session cookie is present, then the page.

    Some bot managers only render server-side data once the client looks like a
    browser that has already been through the site.
    """
    async with AsyncSession(impersonate="chrome", timeout=25) as session:
        for step, target in (("home", "https://www.trendyol.com/"), ("page", url)):
            try:
                response = await session.get(target)
            except Exception as exc:  # a probe reports whatever happened
                print(f"warm-{step:<5} EXC       {type(exc).__name__}: {str(exc)[:60]}")
                continue
            html = response.text or ""
            found = [marker for marker in MARKERS if marker in html]
            prices = PRICE_PATTERN.findall(html)[:2]
            print(
                f"warm-{step:<5} {response.status_code} len={len(html):<7} "
                f"markers={','.join(found) or '-'} prices={prices or '-'} "
                f"cookies={len(session.cookies)}"
            )


if __name__ == "__main__":
    asyncio.run(main())
