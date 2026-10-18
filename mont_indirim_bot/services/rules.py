"""The notification rules shared by the watchlist check and the brand scan.

Both scans answer the same question with the same bookkeeping: has this price
fallen far enough to bother the owner, and did the last notification happen long
enough ago? Keeping the rules here means a change to the cooldown or the re-arm
behaviour applies to both, and that each rule can be unit tested on its own.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from mont_indirim_bot.storage import ItemState, to_iso

__all__ = [
    "cooldown_elapsed",
    "drop_percent",
    "mark_notified",
    "rearm_if_recovered",
    "record_measurement",
    "trigger_reasons",
]


def drop_percent(old_price: float | None, new_price: float) -> float | None:
    """Return the percentage drop from `old_price` to `new_price`.

    Returns `None` when there is no usable previous price or when the price did
    not actually fall.
    """
    if old_price is None or old_price <= 0 or new_price >= old_price:
        return None
    return (old_price - new_price) / old_price * 100.0


def record_measurement(
    state: ItemState,
    *,
    price: float,
    list_price: float | None,
    currency: str,
    in_stock: bool,
    title: str | None,
    now: datetime,
) -> None:
    """Fold one successful measurement into `state`.

    The lowest and highest price ever seen are kept here, because they drive the
    "zirveden düşüş" figure the owner sees in `/liste` and in every alert.
    """
    state.last_price = price
    state.last_list_price = list_price
    state.last_currency = currency
    state.in_stock = in_stock
    state.last_checked_at = to_iso(now)
    state.last_success_at = to_iso(now)
    state.consecutive_errors = 0
    state.error_alerted = False
    state.last_error = None
    if title:
        state.title = title
    state.lowest_price = price if state.lowest_price is None else min(state.lowest_price, price)
    state.highest_price = price if state.highest_price is None else max(state.highest_price, price)


def rearm_if_recovered(state: ItemState, price: float, *, rearm_pct: float) -> bool:
    """Re-arm a trigger once the price climbed back above the notified level.

    Returns `True` when this call re-armed at least one trigger.
    """
    reference = state.last_notified_price
    if reference is None:
        return False
    threshold = reference * (1.0 + max(0.0, rearm_pct) / 100.0)
    if price < threshold:
        return False
    rearmed = not state.armed_target or not state.armed_drop
    state.armed_target = True
    state.armed_drop = True
    return rearmed


def trigger_reasons(
    state: ItemState,
    *,
    price: float,
    previous_price: float | None,
    threshold_pct: float,
    target_price: float | None,
) -> list[str]:
    """Return the triggers this measurement satisfies ("target" and/or "drop")."""
    reasons: list[str] = []
    if target_price is not None and state.armed_target and price <= target_price:
        reasons.append("target")
    drop = drop_percent(previous_price, price)
    if state.armed_drop and drop is not None and drop >= threshold_pct:
        reasons.append("drop")
    return reasons


def cooldown_elapsed(state: ItemState, now: datetime, *, cooldown_seconds: float) -> bool:
    """`True` when the notification cooldown has passed (or never started)."""
    last_notified = state.last_notified_datetime
    if last_notified is None:
        return True
    return (now - last_notified).total_seconds() >= cooldown_seconds


def mark_notified(state: ItemState, *, price: float, reasons: Sequence[str], now: datetime) -> None:
    """Record that the owner was told about this price."""
    state.last_notified_at = to_iso(now)
    state.last_notified_price = price
    state.last_notified_reasons = list(reasons)
    if "target" in reasons:
        state.armed_target = False
    if "drop" in reasons:
        state.armed_drop = False
