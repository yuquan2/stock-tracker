"""Pure screening rules for the three-trading-day pattern."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

PRICE_TICK = 0.01


def is_excluded_stock(
    ts_code: str,
    name: str,
    market: str = "",
    exchange: str = "",
) -> bool:
    """Return whether a security is outside this screener's A-share universe."""
    normalized_name = (name or "").upper()
    normalized_market = market or ""
    normalized_exchange = (exchange or "").upper()
    code_prefix = (ts_code or "").split(".", maxsplit=1)[0]

    return (
        "ST" in normalized_name
        or normalized_market in {"科创板", "北交所"}
        or normalized_exchange == "BSE"
        or code_prefix.startswith(("688", "689", "4", "8", "920"))
    )

def prices_equal(
    left: float,
    right: float,
    *,
    price_tick: float = PRICE_TICK,
) -> bool:
    """Compare quoted prices within half of their minimum price increment."""
    if price_tick <= 0:
        raise ValueError("price_tick must be positive")
    return math.isclose(
        float(left),
        float(right),
        rel_tol=0.0,
        abs_tol=price_tick / 2 + 1e-9,
    )


def matches_pattern(
    d0: Mapping[str, Any],
    d1: Mapping[str, Any],
    d2: Mapping[str, Any],
    *,
    price_tick: float = PRICE_TICK,
) -> bool:
    """Evaluate the requested D0/D1/D2 daily-bar pattern."""
    try:
        d0_volume = float(d0["vol"])
        d1_open = float(d1["open"])
        d1_close = float(d1["close"])
        d1_high = float(d1["high"])
        d1_volume = float(d1["vol"])
        d2_high = float(d2["high"])
        d2_low = float(d2["low"])
    except (KeyError, TypeError, ValueError):
        return False

    values = (
        d0_volume,
        d1_open,
        d1_close,
        d1_high,
        d1_volume,
        d2_high,
        d2_low,
    )
    if not all(math.isfinite(value) for value in values):
        return False

    return (
        d1_close > d1_open
        and d1_volume >= 1.5 * d0_volume
        and d1_high > d1_close
        and prices_equal(d2_high, d1_close, price_tick=price_tick)
        and prices_equal(d2_low, d1_open, price_tick=price_tick)
    )
