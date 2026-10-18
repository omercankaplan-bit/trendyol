"""Entry point.

    python -m mont_indirim_bot.main --mode check      # scan the watchlist
    python -m mont_indirim_bot.main --mode commands   # consume Telegram updates
    python -m mont_indirim_bot.main --mode both       # what the cron job does

The process is meant to be short lived: it loads `data/*.json`, does one unit of
work, writes the state back and exits. A non-zero exit code marks the GitHub
Actions run as failed, which is exactly what should happen for a missing
secret - a green run that does nothing would hide the misconfiguration.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from mont_indirim_bot.config import Config, ConfigError, load_config
from mont_indirim_bot.scrapers import default_scrapers
from mont_indirim_bot.services.commands import CommandProcessor
from mont_indirim_bot.services.notifier import Notifier
from mont_indirim_bot.services.price_checker import PriceChecker
from mont_indirim_bot.storage import StorageError, Store
from mont_indirim_bot.telegram_client import TelegramClient, TelegramError

LOGGER = logging.getLogger("mont_indirim_bot")

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_INTERRUPTED = 130

MODES = ("check", "commands", "both")
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser."""
    parser = argparse.ArgumentParser(
        prog="mont-indirim-bot",
        description="Trendyol/Hepsiburada fiyat takip botu (GitHub Actions icin).",
    )
    parser.add_argument(
        "--mode",
        choices=MODES,
        default="both",
        help="check: fiyatlari tara, commands: Telegram komutlarini isle, both: ikisi.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="DEBUG seviyesinde log yaz (LOG_LEVEL degiskenini gecersiz kilar).",
    )
    return parser


def configure_logging(level: str) -> None:
    """Configure root logging once."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=LOG_FORMAT,
        stream=sys.stdout,
        force=True,
    )


async def run(mode: str, config: Config) -> int:
    """Execute one bot run and return the process exit code."""
    LOGGER.info(
        "Starting in %s mode (chat_id=%s, min_discount=%.1f%%, cooldown=%.1fh, data=%s)",
        mode,
        config.telegram_chat_id,
        config.min_discount_pct,
        config.cooldown_hours,
        config.data_dir,
    )

    store = await Store(config.data_dir).load()
    scrapers = default_scrapers(
        min_delay_s=config.request_min_delay_s,
        max_delay_s=config.request_max_delay_s,
        max_retries=config.max_retries,
        timeout_s=config.request_timeout_s,
    )
    telegram = TelegramClient(
        config.telegram_bot_token,
        timeout_s=max(config.request_timeout_s, 30.0),
        max_retries=config.max_retries,
    )
    notifier = Notifier(telegram, config.telegram_chat_id)
    checker = PriceChecker(store=store, config=config, scrapers=scrapers, notifier=notifier)
    processor = CommandProcessor(
        store=store, config=config, checker=checker, notifier=notifier, scrapers=scrapers
    )

    try:
        me = await telegram.get_me()
        LOGGER.info("Connected to Telegram as @%s", me.get("username") or "unknown")

        if mode in {"commands", "both"}:
            await processor.poll_once(telegram)

        if mode in {"check", "both"} and not processor.did_run_check:
            outcomes = await checker.run()
            if outcomes:
                alerts = sum(len(outcome.alerts) for outcome in outcomes)
                failures = sum(1 for outcome in outcomes if outcome.failed)
                LOGGER.info(
                    "Scan finished: %d product(s), %d alert(s), %d failure(s)",
                    len(outcomes),
                    alerts,
                    failures,
                )
            else:
                LOGGER.info("Watchlist is empty; nothing to scan")
    finally:
        await telegram.aclose()
    return EXIT_OK


def cli(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, configure logging and run the bot."""
    args = build_parser().parse_args(argv)

    try:
        config = load_config()
    except ConfigError as exc:
        configure_logging("INFO")
        LOGGER.error("Configuration error: %s", exc)
        LOGGER.error(
            "Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID as repository secrets "
            "(Settings > Secrets and variables > Actions); see README.md."
        )
        return EXIT_FAILURE

    configure_logging("DEBUG" if args.verbose else config.log_level)

    try:
        return asyncio.run(run(args.mode, config))
    except KeyboardInterrupt:
        LOGGER.warning("Interrupted")
        return EXIT_INTERRUPTED
    except StorageError as exc:
        LOGGER.error("State files are unusable: %s", exc)
        return EXIT_FAILURE
    except TelegramError as exc:
        LOGGER.error("Telegram rejected the request (check the token): %s", exc)
        return EXIT_FAILURE
    except Exception:
        LOGGER.exception("Unhandled error")
        return EXIT_FAILURE


def main() -> None:
    """Console entry point (`python -m mont_indirim_bot.main`)."""
    raise SystemExit(cli())


if __name__ == "__main__":
    main()
