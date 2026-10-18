#!/usr/bin/env python3
"""Seed `data/watchlist.json` from the command line, without Telegram.

Examples:
    python scripts/seed_watchlist.py --url "https://www.trendyol.com/...-p-1234" --price 1500
    python scripts/seed_watchlist.py --file urls.txt --resolve

`urls.txt` is a plain list of links (one per line, `#` comments allowed).
Without `--resolve` the product titles stay empty and are filled in during the
first scheduled scan; with `--resolve` the script fetches each page once, which
needs network access and takes a few seconds per product.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from mont_indirim_bot.config import REPO_ROOT
from mont_indirim_bot.prices import format_price, parse_price
from mont_indirim_bot.scrapers import ScraperError, default_scrapers, find_scraper
from mont_indirim_bot.storage import Store, WatchItem


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser."""
    parser = argparse.ArgumentParser(
        prog="seed_watchlist.py",
        description="data/watchlist.json dosyasina urun ekler (Telegram gerektirmez).",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", action="append", default=[], help="Eklenecek urun linki.")
    source.add_argument("--file", type=Path, help="Link listesi iceren metin dosyasi.")
    parser.add_argument(
        "--price",
        default=None,
        help="Tum urunler icin hedef fiyat, or. 1500 ya da 1.500,00 (varsayilan: yok).",
    )
    parser.add_argument(
        "--resolve",
        action="store_true",
        help="Urun adini ve guncel fiyati simdi cek (ag erisimi gerekir).",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=REPO_ROOT / "data",
        help="Durum dosyalarinin klasoru (varsayilan: data/).",
    )
    return parser


def collect_urls(args: argparse.Namespace) -> list[str]:
    """Return the URLs to add, reading the optional list file."""
    urls = [str(url).strip() for url in args.url]
    file_path: Path | None = args.file
    if file_path is not None:
        if not file_path.exists():
            msg = f"{file_path} bulunamadi."
            raise SystemExit(msg)
        for raw_line in file_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return [url for url in urls if url]


async def seed(args: argparse.Namespace) -> int:
    """Add every URL to the watchlist and save it."""
    urls = collect_urls(args)
    if not urls:
        print("Eklenecek link bulunamadi.")
        return 1

    data_dir: Path = args.data_dir
    store = await Store(data_dir).load()
    scrapers = default_scrapers()
    target_price = parse_price(args.price) if args.price else None

    added = 0
    skipped = 0
    for url in urls:
        scraper = find_scraper(url, scrapers)
        if scraper is None:
            print(f"! desteklenmeyen site, atlandi: {url}")
            skipped += 1
            continue
        if store.watchlist.find_by_url(url) is not None:
            print(f"= zaten takipte, atlandi: {url}")
            skipped += 1
            continue

        title = ""
        if args.resolve:
            try:
                result = await scraper.fetch(url)
            except ScraperError as exc:
                print(f"! urun okunamadi ({exc}), ad bos kalacak: {url}")
            else:
                title = result.product_name
                print(f"  guncel fiyat: {format_price(result.price, result.currency)}")

        item = store.watchlist.add(
            WatchItem(id="", url=url, site=scraper.name, title=title, target_price=target_price)
        )
        print(f"+ {item.id}  {scraper.display_name}  {url}")
        added += 1

    await store.save_watchlist()
    print(f"\n{added} urun eklendi, {skipped} atlandi -> {store.watchlist_path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args = build_parser().parse_args(argv)
    if args.price is not None and parse_price(args.price) is None:
        print(f"Gecersiz hedef fiyat: {args.price}")
        return 1
    return asyncio.run(seed(args))


if __name__ == "__main__":
    sys.exit(main())
