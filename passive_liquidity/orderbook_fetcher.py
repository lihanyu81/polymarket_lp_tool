from __future__ import annotations

import logging
import math
from typing import Any, Optional

from passive_liquidity.models import OrderBookSnapshot

LOG = logging.getLogger(__name__)


def _level_price(level: Any) -> Optional[float]:
    if level is None:
        return None
    p = getattr(level, "price", None)
    if p is None and isinstance(level, dict):
        p = level.get("price")
    if p is None or p == "":
        return None
    return float(p)


def _best_bid_from_levels(bids: list[Any]) -> Optional[float]:
    """Highest buy price; API does not guarantee bids[0] is the best bid."""
    prices = [_level_price(b) for b in bids]
    prices = [x for x in prices if x is not None]
    return max(prices) if prices else None


def _best_ask_from_levels(asks: list[Any]) -> Optional[float]:
    """Lowest sell price; API does not guarantee asks[0] is the best ask."""
    prices = [_level_price(a) for a in asks]
    prices = [x for x in prices if x is not None]
    return min(prices) if prices else None


def second_best_bid_from_levels(bids: list[Any]) -> Optional[float]:
    """Second-highest distinct bid (买二); None if fewer than two price levels."""
    prices = sorted({_level_price(b) for b in bids if _level_price(b) is not None}, reverse=True)
    return prices[1] if len(prices) >= 2 else None


def second_best_ask_from_levels(asks: list[Any]) -> Optional[float]:
    """Second-lowest distinct ask (卖二); None if fewer than two price levels."""
    prices = sorted({_level_price(a) for a in asks if _level_price(a) is not None})
    return prices[1] if len(prices) >= 2 else None


def _infer_tick_from_prices(bids: list[Any], asks: list[Any]) -> Optional[float]:
    """Infer tick size from actual orderbook price granularity.

    If *any* price has a non-zero third decimal place (e.g. 0.038, 0.125),
    the market must be fine-tick (0.001).  Otherwise assume coarse (0.01).

    Returns 0.001 or 0.01, or None if no prices to inspect.
    """
    for levels in (bids, asks):
        for lv in levels or []:
            p = _level_price(lv)
            if p is None:
                continue
            # Check if the price has sub-cent precision:
            # p * 100 should be integer for coarse tick;
            # if p * 1000 has non-zero fractional remainder at the thousandths place,
            # this is fine tick.
            cents = p * 100.0
            if abs(cents - round(cents)) > 1e-7:
                return 0.001
    return None


def _infer_tick_from_level_gaps(bids: list[Any], asks: list[Any]) -> Optional[float]:
    """Infer tick from minimum gap between distinct L2 prices (handles API tick_size wrong).

    When the book shows steps of ~0.001 but every level happens to land on
    cent boundaries (0.94, 0.95), sub-cent digit detection alone misses; gaps
    still reveal a finer grid when multiple levels exist.
    """
    prices: list[float] = []
    for lv in (bids or []) + (asks or []):
        p = _level_price(lv)
        if p is None:
            continue
        xf = float(p)
        if 1e-12 < xf < 1.0 - 1e-12:
            prices.append(round(xf, 6))
    uniq = sorted(set(prices))
    if len(uniq) < 2:
        return None
    min_gap = min(uniq[i + 1] - uniq[i] for i in range(len(uniq) - 1))
    if min_gap <= 1e-9:
        return None
    # ~0.001 ladder
    if 0.00025 <= min_gap <= 0.0025:
        return 0.001
    # ~0.01 ladder
    if 0.008 <= min_gap <= 0.012:
        return 0.01
    return None


def resolve_effective_tick_size(
    api_tick: Any,
    bids: list[Any],
    asks: list[Any],
) -> float:
    """Public wrapper: same rules as internal _resolve_tick_size."""
    return _resolve_tick_size(api_tick, bids, asks)


def _resolve_tick_size(
    api_tick: Any,
    bids: list[Any],
    asks: list[Any],
) -> float:
    """Determine the effective tick size.

    Priority:
    1. If the API returned a known valid tick (0.01/1.0 for coarse, 0.001/0.1
       for fine), trust it — but cross-check against orderbook prices.  If the
       API says coarse (0.01) yet the book contains sub-cent prices, override
       to fine (0.001) because the API value is stale / wrong.
    2. If the API value is missing / zero / unparseable, infer from book prices.
    3. Final fallback: 0.01 (coarse).
    """
    # --- parse raw API value ---
    parsed: Optional[float] = None
    if api_tick is not None and str(api_tick).strip() not in ("", "0", "0.0"):
        try:
            parsed = float(api_tick)
        except (TypeError, ValueError):
            parsed = None

    inferred = _infer_tick_from_prices(bids, asks)
    gap_inferred = _infer_tick_from_level_gaps(bids, asks)

    if parsed is not None and parsed > 0:
        is_api_coarse = (
            math.isclose(parsed, 0.01, abs_tol=1e-9)
            or math.isclose(parsed, 1.0, abs_tol=1e-6)
        )
        # API says coarse but book proves fine → override
        if is_api_coarse and inferred == 0.001:
            LOG.warning(
                "tick_size override: API returned %.6f (coarse) but book prices "
                "show sub-cent granularity → using 0.001 (fine)",
                parsed,
            )
            return 0.001
        if is_api_coarse and gap_inferred == 0.001:
            LOG.warning(
                "tick_size override: API returned %.6f (coarse) but L2 min gap "
                "≈0.001 → using 0.001 (fine)",
                parsed,
            )
            return 0.001
        return parsed

    # API missing → use inferred or default
    if inferred is not None:
        LOG.debug("tick_size inferred from book prices: %s", inferred)
        return inferred
    if gap_inferred is not None:
        LOG.debug("tick_size inferred from L2 gaps: %s", gap_inferred)
        return gap_inferred

    return 0.01


class OrderBookFetcher:
    def __init__(self, clob_ro: Any):
        """
        clob_ro: ClobClient with at least get_order_book (can be read-only / L2).
        """
        self._client = clob_ro

    def get_orderbook(self, token_id: str) -> OrderBookSnapshot:
        book = self._client.get_order_book(token_id)
        bids = getattr(book, "bids", None) or []
        asks = getattr(book, "asks", None) or []
        bb = _best_bid_from_levels(bids)
        ba = _best_ask_from_levels(asks)
        raw_tick = getattr(book, "tick_size", None)
        tick = _resolve_tick_size(raw_tick, bids, asks)
        nr = bool(getattr(book, "neg_risk", False))
        snap = OrderBookSnapshot(
            best_bid=bb,
            best_ask=ba,
            tick_size=tick,
            neg_risk=nr,
            bids=bids,
            asks=asks,
            raw=book,
        )
        return snap

    def mid_price(self, token_id: str) -> Optional[float]:
        book = self.get_orderbook(token_id)
        if book.mid is not None:
            return book.mid
        mp = self._client.get_midpoint(token_id)
        if isinstance(mp, dict):
            raw = mp.get("mid")
            if raw is None or raw == "":
                LOG.warning("get_midpoint missing mid for token_id=%s…", token_id[:24])
                return None
            return float(raw)
        if mp is None:
            return None
        return float(mp)
