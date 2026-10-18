"""Serverless Telegram price tracker for Turkish e-commerce storefronts.

The package is designed for GitHub Actions: every invocation downloads the
persisted state from ``data/``, does one unit of work (scan the watchlist,
consume pending Telegram commands, or both), writes the state back and exits.
There is no long running process and no database.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
