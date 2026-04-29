"""
Simplified placement policy: coarse tick (book-based level pick) or fine tick (band ratio).

All legacy risk / scoring / structural paths are disabled; this module is the sole price logic.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal, Optional

from passive_liquidity.models import AdjustmentDecision
from passive_liquidity.orderbook_fetcher import _level_price

LOG = logging.getLogger(__name__)

TickRegime = Literal["coarse", "fine", "unsupported"]
CustomTickRegime = Literal["coarse", "fine"]
PricingMode = Literal["default", "custom"]


@dataclass(frozen=True)
class CustomPricingSettings:
    """Env-driven knobs for pricing_mode=custom (see PASSIVE_CUSTOM_*).

    ``coarse_tick_offset_from_mid`` (N): rank within **order-book prices** that fall
    in the coarse reward half-band (1 = nearest to mid among those levels only;
    empty tick rungs with no resting size do not count); see ``_decide_custom_coarse``.
    """

    coarse_tick_offset_from_mid: int
    coarse_allow_top_of_book: bool
    coarse_min_candidate_levels: int
    fine_safe_band_min: float
    fine_safe_band_max: float
    fine_target_band_ratio: float


def order_uses_custom_pricing(order: dict, custom_order_ids: frozenset[str]) -> bool:
    """True when order id is listed in PASSIVE_CUSTOM_ORDER_IDS."""
    if not custom_order_ids:
        return False
    oid = str(order.get("id") or order.get("orderID") or "").strip()
    return bool(oid) and oid in custom_order_ids


def classify_tick_regime(tick: float) -> TickRegime:
    """Classify regime from effective tick size (may be reconciled from the L2 book).

    Call sites should pass ``tick_size`` after ``resolve_effective_tick_size`` so
    WS/API mismatches (e.g. reported 0.01 but book is 0.001) map to ``fine``.
    """
    t = float(tick)
    if math.isclose(t, 1.0, rel_tol=0.0, abs_tol=1e-6) or math.isclose(
        t, 0.01, rel_tol=0.0, abs_tol=1e-9
    ):
        return "coarse"
    if math.isclose(t, 0.1, rel_tol=0.0, abs_tol=1e-6) or math.isclose(
        t, 0.001, rel_tol=0.0, abs_tol=1e-12
    ):
        return "fine"
    return "unsupported"


def classify_custom_tick_regime(tick: float) -> CustomTickRegime:
    """
    Custom / Telegram rules: same coarse vs fine split as default ``decide_simple_price``.

    Coarse (book-style grid): tick ≈ 0.01 or ≈ 1.0. Fine (band ratio): ≈ 0.1 or ≈ 0.001.
    Unknown tick sizes fall back with a simple size heuristic so setup always picks a branch.
    """
    r = classify_tick_regime(tick)
    if r == "coarse":
        return "coarse"
    if r == "fine":
        return "fine"
    t = float(tick)
    if t < 0.05:
        return "fine"
    return "coarse"


def _round_tick(price: float, tick: float) -> float:
    t = max(float(tick), 1e-12)
    steps = round(price / t)
    p = steps * t
    return max(t, min(1.0 - t, p))


def _level_size(level: Any) -> float:
    if level is None:
        return 0.0
    s = getattr(level, "size", None)
    if s is None and isinstance(level, dict):
        s = (
            level.get("size")
            or level.get("amount")
            or level.get("quantity")
            or level.get("shares")
        )
    try:
        return max(0.0, float(s or 0))
    except (TypeError, ValueError):
        return 0.0


def _coarse_reward_scan_range(
    side_u: str, mid: float, delta: float, tick: float
) -> tuple[float, float, float, int]:
    """
    Scan the reward half-band aligned to whole ticks (CLOB δ may be fractional, e.g. 0.045):

    - band = floor(δ / tick) * tick  → e.g. δ=0.045, tick=0.01 → 4 ticks → 0.04 → BUY [0.245, mid]
    - BUY: [mid − band, mid], SELL: [mid, mid + band]

    If δ is missing vs tick, fall back to 5 ticks.
    Returns (lo, hi, band_used, n_ticks).
    """
    t = max(float(tick), 1e-12)
    d = max(float(delta), 0.0)
    if d >= t - 1e-15:
        n_ticks = max(1, int(math.floor(d / t + 1e-9)))
    else:
        n_ticks = 5
    band = n_ticks * t
    band = max(band, 1e-12)
    if side_u == "BUY":
        lo, hi = mid - band, mid
    else:
        lo, hi = mid, mid + band
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi, band, n_ticks


def _book_prices_in_range(
    side_u: str,
    levels: list[Any],
    lo: float,
    hi: float,
    tick: float,
) -> list[float]:
    """Distinct prices with positive size on the given side, snapped to tick, inside [lo, hi]."""
    out: set[float] = set()
    for lv in levels or []:
        raw = _level_price(lv)
        if raw is None:
            continue
        p = _round_tick(float(raw), tick)
        if p < lo - 1e-12 or p > hi + 1e-12:
            continue
        if _level_size(lv) <= 0:
            continue
        out.add(p)
    return sorted(out)


def _order_coarse_candidates_near_to_far(
    side_u: str, mid: float, prices: list[float]
) -> list[float]:
    """
    Custom coarse N: order distinct band prices from nearest mid to farthest.

    Tie-break (same |p-mid|): BUY prefers higher p, SELL prefers lower p.
    """
    mid_f = float(mid)

    def _key(p: float) -> tuple[float, float]:
        p = float(p)
        d = abs(mid_f - p)
        if side_u == "BUY":
            return (d, -p)
        return (d, p)

    return sorted(prices, key=_key)


def list_coarse_reward_book_candidates(
    side: str,
    mid: float,
    delta: float,
    tick: float,
    bids: list[Any],
    asks: list[Any],
) -> tuple[float, float, list[float]]:
    """
    Coarse regime: (scan_lo, scan_hi_clipped, book prices in band, near-mid→far).

    Used by Telegram/Web reward lines and matches custom coarse N semantics.
    """
    side_u = str(side).strip().upper()
    lo, hi, _, _ = _coarse_reward_scan_range(side_u, float(mid), float(delta), tick)
    lo_c = max(float(lo), 1e-12)
    hi_c = min(float(hi), 1.0 - 1e-12)
    if lo_c > hi_c + 1e-15:
        return lo_c, hi_c, []
    levels = bids if side_u == "BUY" else asks
    cand = _book_prices_in_range(side_u, levels, lo_c, hi_c, tick)
    ordered = _order_coarse_candidates_near_to_far(side_u, mid, cand)
    return lo_c, hi_c, ordered


def list_coarse_reward_tick_levels(
    side: str,
    mid: float,
    delta: float,
    tick: float,
) -> tuple[float, float, list[float]]:
    """
    Coarse regime theoretical tick ladder inside reward half-band.

    Returns (scan_lo, scan_hi_clipped, tick-aligned price levels ascending).
    Example: mid=0.6650, delta=0.0350, tick=0.01 -> [0.64, 0.65, 0.66].
    """
    side_u = str(side).strip().upper()
    lo, hi, _, _ = _coarse_reward_scan_range(side_u, float(mid), float(delta), tick)
    t = max(float(tick), 1e-12)
    lo_c = max(float(lo), 1e-12)
    hi_c = min(float(hi), 1.0 - 1e-12)
    if lo_c > hi_c + 1e-15:
        return lo_c, hi_c, []

    k_lo = int(math.ceil(lo_c / t - 1e-9))
    k_hi = int(math.floor(hi_c / t + 1e-9))
    if k_hi < k_lo:
        return lo_c, hi_c, []

    levels = [max(t, min(1.0 - t, k * t)) for k in range(k_lo, k_hi + 1)]
    # De-duplicate after clipping near boundaries (0/1).
    uniq = sorted({round(float(p), 12) for p in levels})
    return lo_c, hi_c, uniq


def fine_tick_display_decimals(tick: float) -> int:
    """Print width for outcome prices: 0.01 grid -> 2 decimals, 0.001 -> 3."""
    return 2 if float(tick) >= 0.009 else 3


def fine_reward_display_lo_hi(
    mid: float,
    delta: float,
    tick: float,
    bids: list[Any],
    asks: list[Any],
    side: Optional[str] = None,
) -> tuple[float, float, bool]:
    """
    Fine-tick reward *display* interval.

    When ``side`` is ``BUY`` or ``SELL``, uses the same **half-band** as pricing
    (``_eligible_band_lo_hi`` fine branch): BUY scans ``[mid−δ, mid]`` on **bids**
    only; SELL scans ``[mid, mid+δ]`` on **asks** only. This avoids showing an
    upper bound from the opposite side (e.g. asks near ``mid+δ``) that is not
    in the reward zone for that order.

    When ``side`` is omitted, keeps legacy behavior: symmetric ``[mid−δ, mid+δ]``
    merged across bids and asks (min/max resting in band).

    - If any relevant level lies in the clipped band: return (min, max) among
      those prices (tick-rounded), and True.
    - Else: return band edges snapped **inward** to the tick grid; and False.
    """
    m = float(mid)
    d = max(0.0, float(delta))
    t = max(float(tick), 1e-12)
    side_u = str(side).strip().upper() if side is not None else ""

    if side_u in ("BUY", "SELL"):
        band = max(d, 1e-12)
        if side_u == "BUY":
            raw_lo, raw_hi = m - band, m
        else:
            raw_lo, raw_hi = m, m + band
        if raw_lo > raw_hi:
            raw_lo, raw_hi = raw_hi, raw_lo
        clip_lo = max(t, min(1.0 - t, raw_lo))
        clip_hi = max(t, min(1.0 - t, raw_hi))
        if clip_lo > clip_hi + 1e-15:
            return clip_lo, clip_hi, False
        levels = bids if side_u == "BUY" else asks
        prices = _book_prices_in_range(side_u, levels, clip_lo, clip_hi, t)
        if prices:
            return float(prices[0]), float(prices[-1]), True
        k_lo = int(math.ceil(clip_lo / t - 1e-9))
        k_hi = int(math.floor(clip_hi / t + 1e-9))
        lo_d = max(t, min(1.0 - t, k_lo * t))
        hi_d = max(t, min(1.0 - t, k_hi * t))
        if lo_d > hi_d + 1e-12:
            lo_d = _round_tick(clip_lo, t)
            hi_d = _round_tick(clip_hi, t)
        return lo_d, hi_d, False

    raw_lo = m - d
    raw_hi = m + d
    clip_lo = max(t, min(1.0 - t, raw_lo))
    clip_hi = max(t, min(1.0 - t, raw_hi))
    if clip_lo > clip_hi + 1e-15:
        return clip_lo, clip_hi, False

    bp = _book_prices_in_range("BUY", bids, clip_lo, clip_hi, t)
    ap = _book_prices_in_range("SELL", asks, clip_lo, clip_hi, t)
    merged = sorted(set(bp + ap))
    if merged:
        return float(merged[0]), float(merged[-1]), True

    k_lo = int(math.ceil(clip_lo / t - 1e-9))
    k_hi = int(math.floor(clip_hi / t + 1e-9))
    lo_d = max(t, min(1.0 - t, k_lo * t))
    hi_d = max(t, min(1.0 - t, k_hi * t))
    if lo_d > hi_d + 1e-12:
        lo_d = _round_tick(clip_lo, t)
        hi_d = _round_tick(clip_hi, t)
    return lo_d, hi_d, False


@dataclass(frozen=True)
class EligibleBandDepthStats:
    """Order-book depth inside the same reward half-band used for pricing (coarse vs fine)."""

    scan_lo: float
    scan_hi: float
    tick_regime: str
    price_sizes: tuple[tuple[float, float], ...]
    total_in_band: float
    closer_to_mid_than_order: float
    pct_closer_of_band: Optional[float]


def _eligible_band_lo_hi(
    side_u: str, mid: float, delta: float, tick: float, regime: TickRegime
) -> tuple[float, float]:
    if regime == "coarse":
        lo, hi, _, _ = _coarse_reward_scan_range(side_u, mid, delta, tick)
        return lo, hi
    band = max(float(delta), 1e-12)
    if side_u == "BUY":
        lo, hi = mid - band, mid
    else:
        lo, hi = mid, mid + band
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def aggregate_depth_in_band(
    *,
    side: str,
    mid: float,
    delta: float,
    tick: float,
    bids: list[Any],
    asks: list[Any],
) -> tuple[float, float, str, list[tuple[float, float]], float]:
    """
    Returns (lo, hi, regime, sorted (price, size) list, total_size).
    BUY sums bids in band; SELL sums asks in band. Sizes merged per tick-rounded price.
    """
    side_u = side.upper()
    regime = classify_tick_regime(tick)
    lo, hi = _eligible_band_lo_hi(side_u, mid, delta, tick, regime)
    levels = bids if side_u == "BUY" else asks
    t = max(float(tick), 1e-12)
    agg: dict[float, float] = defaultdict(float)
    for lv in levels or []:
        raw = _level_price(lv)
        if raw is None:
            continue
        p = _round_tick(float(raw), t)
        if p < lo - 1e-12 or p > hi + 1e-12:
            continue
        sz = _level_size(lv)
        if sz <= 0:
            continue
        agg[p] += sz
    ordered = sorted(agg.items(), key=lambda x: x[0])
    total = sum(s for _, s in ordered)
    return lo, hi, regime, ordered, total


def compute_eligible_band_depth_stats(
    *,
    side: str,
    order_price: float,
    mid: float,
    delta: float,
    tick: float,
    bids: list[Any],
    asks: list[Any],
) -> EligibleBandDepthStats:
    lo, hi, regime, price_sizes, total = aggregate_depth_in_band(
        side=side,
        mid=mid,
        delta=delta,
        tick=tick,
        bids=bids,
        asks=asks,
    )
    t = max(float(tick), 1e-12)
    op = _round_tick(float(order_price), t)
    closer = 0.0
    for p, s in price_sizes:
        if abs(mid - p) + 1e-12 < abs(mid - op):
            closer += s
    pct = (closer / total * 100.0) if total > 1e-12 else None
    return EligibleBandDepthStats(
        scan_lo=lo,
        scan_hi=hi,
        tick_regime=regime,
        price_sizes=tuple(price_sizes),
        total_in_band=total,
        closer_to_mid_than_order=closer,
        pct_closer_of_band=pct,
    )


def format_eligible_band_depth_summary_zh(
    stats: EligibleBandDepthStats,
    *,
    max_levels: int = 10,
) -> str:
    """Extra lines for periodic Telegram band summary (Chinese)."""
    lines = [
        f"  深度统计区间[{stats.scan_lo:.4f},{stats.scan_hi:.4f}] regime={stats.tick_regime}",
    ]
    if not stats.price_sizes:
        lines.append("  带内各价深度: 无（该侧无正深度）")
    else:
        shown = stats.price_sizes[:max_levels]
        seg = " ".join(f"{p:.4f}:{s:g}" for p, s in shown)
        if len(stats.price_sizes) > max_levels:
            seg += f" …共{len(stats.price_sizes)}档"
        lines.append(
            f"  带内各价深度: {seg} | 带内合计 {stats.total_in_band:g}"
        )
    if stats.total_in_band <= 1e-12:
        lines.append("  较本单更靠 mid 侧深度: —（带内合计为 0）")
    elif stats.pct_closer_of_band is not None:
        lines.append(
            f"  较本单更靠 mid 侧深度: {stats.closer_to_mid_than_order:g} "
            f"（占带内合计 {stats.pct_closer_of_band:.1f}%）"
        )
    else:
        lines.append(
            f"  较本单更靠 mid 侧深度: {stats.closer_to_mid_than_order:g}"
        )
    return "\n".join(lines)


def _pick_coarse_target(
    side_u: str, mid: float, candidates: list[float]
) -> tuple[Optional[float], str]:
    """
    candidates sorted ascending by price.
    Rank by distance from mid: BUY below mid -> lower price = farther.
    """
    n = len(candidates)
    if n <= 0:
        return None, "coarse_tick_abandon_due_to_too_few_levels"
    if n <= 2:
        return None, "coarse_tick_abandon_due_to_too_few_levels"

    by_dist_asc = sorted(candidates, key=lambda p: abs(p - mid))
    if n == 3:
        chosen = by_dist_asc[1]
        return chosen, "coarse_tick_choose_middle_of_3"

    by_dist_desc = sorted(candidates, key=lambda p: abs(p - mid), reverse=True)
    # second farthest: index 1 when n >= 2
    idx = 1 if n >= 2 else 0
    chosen = by_dist_desc[idx]
    if n == 4:
        return chosen, "coarse_tick_choose_third_from_mid_of_4"
    return chosen, "coarse_tick_choose_second_farthest_default"


def _min_replace_delta(tick: float, min_replace_ticks: int) -> float:
    return max(1, int(min_replace_ticks)) * float(tick)


def _valid_clob_probability_price(p: float) -> bool:
    """Polymarket outcome prices must lie strictly inside (0, 1)."""
    x = float(p)
    return 1e-12 < x < 1.0 - 1e-12


def _ticks_from_mid_into_band(side_u: str, mid: float, price: float, tick: float) -> int:
    """Whole ticks from mid toward the reward band (BUY: below mid, SELL: above mid).

    Uses tick-index subtraction after snapping to the grid so float noise in
    ``(mid - price) / tick`` cannot undercount (e.g. spurious
    ``custom_coarse_keep_offset_outside_band`` when target is one tick off mid).
    """
    t = max(float(tick), 1e-12)
    mid_t = _round_tick(float(mid), t)
    pr_t = _round_tick(float(price), t)
    im = int(round(mid_t / t))
    ip = int(round(pr_t / t))
    if side_u == "BUY":
        return max(0, im - ip)
    return max(0, ip - im)


def _distance_ratio_in_band(side_u: str, price: float, mid: float, delta: float) -> float:
    band = max(float(delta), 1e-12)
    if side_u == "BUY":
        return max(0.0, float(mid) - float(price)) / band
    return max(0.0, float(price) - float(mid)) / band


def _decide_custom_coarse(
    *,
    side_u: str,
    price: float,
    mid: float,
    tick: float,
    delta: float,
    bids: list[Any],
    asks: list[Any],
    min_replace_ticks: int,
    settings: CustomPricingSettings,
    best_bid: Optional[float],
    best_ask: Optional[float],
    meta: dict[str, Any],
) -> tuple[AdjustmentDecision, dict[str, Any]]:
    lo, hi, band_used, band_ticks = _coarse_reward_scan_range(
        side_u, mid, delta, tick
    )
    meta["coarse_reward_band_delta"] = band_used
    meta["coarse_band_ticks"] = band_ticks

    lo_c = max(float(lo), 1e-12)
    hi_c = min(float(hi), 1.0 - 1e-12)
    meta["coarse_range_lo_hi"] = (lo_c, hi_c)

    if lo_c > hi_c + 1e-15:
        meta["candidate_prices"] = []
        meta["candidate_count"] = 0
        meta["chosen_target_price"] = None
        meta["reason_code"] = "custom_coarse_keep_band_outside_market"
        LOG.info(
            "custom_price coarse reward band does not intersect (0,1) after clip -> keep"
        )
        return (
            AdjustmentDecision("keep", reason="custom_coarse_keep_band_outside_market"),
            meta,
        )

    levels = bids if side_u == "BUY" else asks
    cand = _book_prices_in_range(side_u, levels, lo_c, hi_c, tick)
    ordered = _order_coarse_candidates_near_to_far(side_u, mid, cand)
    min_need = int(settings.coarse_min_candidate_levels)
    if min_need < 1:
        min_need = 1
    if len(ordered) < min_need:
        meta["candidate_prices"] = list(ordered)
        meta["candidate_count"] = len(ordered)
        meta["custom_coarse_tick_offset"] = int(settings.coarse_tick_offset_from_mid)
        rcode = "custom_coarse_keep_insufficient_candidates"
        meta["reason_code"] = rcode
        meta["chosen_target_price"] = None
        LOG.info(
            "custom_price coarse tick=%s book_levels_in_band=%d need>=%d -> keep",
            tick,
            len(ordered),
            min_need,
        )
        return AdjustmentDecision("keep", reason=rcode), meta

    user_n = max(1, int(settings.coarse_tick_offset_from_mid))
    # N is rank among **resting** prices in the band (near-mid → far), not empty ticks.
    rank_idx = user_n - 1
    if rank_idx >= len(ordered):
        meta["candidate_prices"] = list(ordered)
        meta["candidate_count"] = len(ordered)
        meta["custom_coarse_tick_offset"] = user_n
        meta["custom_coarse_tick_offset_effective"] = None
        meta["chosen_target_price"] = None
        meta["reason_code"] = "custom_coarse_keep_rank_outside_band_levels"
        return (
            AdjustmentDecision("keep", reason="custom_coarse_keep_rank_outside_band_levels"),
            meta,
        )
    chosen = float(ordered[rank_idx])
    effective_rank = rank_idx + 1

    if not _valid_clob_probability_price(chosen):
        meta["candidate_prices"] = []
        meta["candidate_count"] = 0
        meta["custom_coarse_tick_offset"] = user_n
        meta["custom_coarse_tick_offset_effective"] = effective_rank
        meta["chosen_target_price"] = None
        meta["reason_code"] = "custom_coarse_keep_offset_invalid_price"
        return (
            AdjustmentDecision("keep", reason="custom_coarse_keep_offset_invalid_price"),
            meta,
        )

    if not settings.coarse_allow_top_of_book:
        if side_u == "BUY" and best_bid is not None:
            if abs(chosen - float(best_bid)) <= 1e-9:
                meta["candidate_prices"] = [chosen]
                meta["candidate_count"] = 1
                meta["custom_coarse_tick_offset"] = user_n
                meta["custom_coarse_tick_offset_effective"] = effective_rank
                meta["chosen_target_price"] = chosen
                meta["reason_code"] = "custom_coarse_keep_target_is_top_of_book"
                return (
                    AdjustmentDecision(
                        "keep", reason="custom_coarse_keep_target_is_top_of_book"
                    ),
                    meta,
                )
        if side_u == "SELL" and best_ask is not None:
            if abs(chosen - float(best_ask)) <= 1e-9:
                meta["candidate_prices"] = [chosen]
                meta["candidate_count"] = 1
                meta["custom_coarse_tick_offset"] = user_n
                meta["custom_coarse_tick_offset_effective"] = effective_rank
                meta["chosen_target_price"] = chosen
                meta["reason_code"] = "custom_coarse_keep_target_is_top_of_book"
                return (
                    AdjustmentDecision(
                        "keep", reason="custom_coarse_keep_target_is_top_of_book"
                    ),
                    meta,
                )

    meta["candidate_prices"] = list(ordered)
    meta["candidate_count"] = len(ordered)
    meta["custom_coarse_tick_offset"] = user_n
    meta["custom_coarse_tick_offset_effective"] = effective_rank
    meta["chosen_target_price"] = chosen
    meta["reason_code"] = "custom_coarse_replace_exact_offset_from_mid"

    cur = _round_tick(float(price), tick)
    min_d = _min_replace_delta(tick, min_replace_ticks)
    if abs(chosen - cur) < min_d - 1e-12:
        meta["reason_code"] = "custom_coarse_keep_already_at_target"
        return (
            AdjustmentDecision("keep", reason="custom_coarse_keep_already_at_target"),
            meta,
        )
    return (
        AdjustmentDecision(
            "replace",
            new_price=chosen,
            reason="custom_coarse_replace_exact_offset_from_mid",
        ),
        meta,
    )


def _decide_custom_fine(
    *,
    side_u: str,
    price: float,
    mid: float,
    tick: float,
    delta: float,
    min_replace_ticks: int,
    settings: CustomPricingSettings,
    meta: dict[str, Any],
) -> tuple[AdjustmentDecision, dict[str, Any]]:
    band = max(float(delta), 1e-12)
    dr = _distance_ratio_in_band(side_u, price, mid, delta)
    meta["distance_ratio"] = dr
    meta["candidate_prices"] = []
    meta["candidate_count"] = 0

    lo = mid - band if side_u == "BUY" else mid
    hi = mid if side_u == "BUY" else mid + band
    if lo > hi:
        lo, hi = hi, lo

    smin = float(settings.fine_safe_band_min)
    smax = float(settings.fine_safe_band_max)
    if smin > smax:
        smin, smax = smax, smin

    tr = float(settings.fine_target_band_ratio)
    tr = max(0.0, min(1.0, tr))

    if smin - 1e-12 <= dr <= smax + 1e-12:
        meta["reason_code"] = "custom_fine_keep_in_safe_band"
        ideal = (
            mid - tr * band if side_u == "BUY" else mid + tr * band
        )
        meta["chosen_target_price"] = _round_tick(ideal, tick)
        LOG.info(
            "custom_price fine tick=%s dr=%.4f in [%.3f,%.3f] -> keep",
            tick,
            dr,
            smin,
            smax,
        )
        return (
            AdjustmentDecision("keep", reason="custom_fine_keep_in_safe_band"),
            meta,
        )

    ideal = mid - tr * band if side_u == "BUY" else mid + tr * band
    ideal = _round_tick(ideal, tick)
    ideal = max(lo, min(hi, ideal))
    ideal = _round_tick(ideal, tick)
    meta["chosen_target_price"] = ideal
    meta["reason_code"] = "custom_fine_move_toward_target_ratio"

    t = float(tick)
    min_d = _min_replace_delta(t, min_replace_ticks)
    cur = _round_tick(float(price), t)
    if abs(ideal - cur) < min_d - 1e-12:
        meta["reason_code"] = "custom_fine_keep_small_delta"
        LOG.info(
            "custom_price fine tick=%s dr=%.4f target=%.4f cur=%.4f -> keep small delta",
            tick,
            dr,
            ideal,
            cur,
        )
        return AdjustmentDecision("keep", reason="custom_fine_keep_small_delta"), meta

    LOG.info(
        "custom_price fine tick=%s dr=%.4f -> replace to %.4f (target_ratio=%.4f)",
        tick,
        dr,
        ideal,
        tr,
    )
    return (
        AdjustmentDecision(
            "replace",
            new_price=ideal,
            reason="custom_fine_move_toward_target_ratio",
        ),
        meta,
    )


def decide_simple_price(
    *,
    side: str,
    price: float,
    mid: float,
    tick: float,
    delta: float,
    bids: list[Any],
    asks: list[Any],
    min_replace_ticks: int = 1,
    pricing_mode: PricingMode = "default",
    custom_settings: Optional[CustomPricingSettings] = None,
    best_bid: Optional[float] = None,
    best_ask: Optional[float] = None,
    custom_tick_regime_override: Optional[CustomTickRegime] = None,
) -> tuple[AdjustmentDecision, dict[str, Any]]:
    """
    Single pricing rule: coarse (tick ~0.01) or fine (~0.001); unsupported -> keep.

    When pricing_mode is ``custom``, coarse/fine follows ``classify_custom_tick_regime``
    (aligned with default ``classify_tick_regime``: 0.01|1.0 coarse, 0.1|0.001 fine),
    unless ``custom_tick_regime_override`` is set (e.g. persisted Telegram rule).
    """
    side_u = side.upper()
    meta: dict[str, Any] = {
        "tick_size": tick,
        "mid": mid,
        "side": side_u,
        "pricing_mode": pricing_mode,
    }

    if pricing_mode == "custom":
        if custom_settings is None:
            meta["tick_regime"] = None
            meta["reason_code"] = "custom_missing_settings_keep"
            LOG.warning("pricing_mode=custom but custom_settings is None -> keep")
            return (
                AdjustmentDecision("keep", reason="custom_missing_settings_keep"),
                meta,
            )
        creg: CustomTickRegime
        if custom_tick_regime_override is not None:
            creg = custom_tick_regime_override
            meta["custom_tick_regime_source"] = "stored_rule"
        else:
            creg = classify_custom_tick_regime(tick)
            meta["custom_tick_regime_source"] = "live_tick"
        meta["tick_regime"] = f"custom_{creg}"
        if creg == "coarse":
            return _decide_custom_coarse(
                side_u=side_u,
                price=price,
                mid=mid,
                tick=tick,
                delta=delta,
                bids=bids,
                asks=asks,
                min_replace_ticks=min_replace_ticks,
                settings=custom_settings,
                best_bid=best_bid,
                best_ask=best_ask,
                meta=meta,
            )
        return _decide_custom_fine(
            side_u=side_u,
            price=price,
            mid=mid,
            tick=tick,
            delta=delta,
            min_replace_ticks=min_replace_ticks,
            settings=custom_settings,
            meta=meta,
        )

    regime = classify_tick_regime(tick)
    meta["tick_regime"] = regime

    if regime == "unsupported":
        meta["candidate_prices"] = []
        meta["candidate_count"] = 0
        meta["chosen_target_price"] = None
        meta["reason_code"] = "unsupported_tick_keep"
        LOG.info(
            "simple_price tick=%s regime=unsupported -> keep (no coarse/fine rule)",
            tick,
        )
        return (
            AdjustmentDecision("keep", reason="unsupported_tick_keep"),
            meta,
        )

    if regime == "coarse":
        lo, hi, band_used, band_ticks = _coarse_reward_scan_range(
            side_u, mid, delta, tick
        )
        levels = bids if side_u == "BUY" else asks
        cand = _book_prices_in_range(side_u, levels, lo, hi, tick)
        meta["candidate_prices"] = list(cand)
        meta["candidate_count"] = len(cand)
        meta["coarse_range_lo_hi"] = (lo, hi)
        meta["coarse_reward_band_delta"] = band_used
        meta["coarse_band_ticks"] = band_ticks

        target, rcode = _pick_coarse_target(side_u, mid, cand)
        meta["chosen_target_price"] = target
        meta["reason_code"] = rcode

        LOG.info(
            "simple_price coarse tick=%s mid=%.4f side=%s api_delta=%.4f "
            "band_ticks=%d band_used=%.4f scan=[%.4f,%.4f] book_levels=%d "
            "candidates=%s n=%d chosen=%s reason=%s",
            tick,
            mid,
            side_u,
            float(delta),
            band_ticks,
            band_used,
            lo,
            hi,
            len(levels or []),
            cand,
            len(cand),
            target,
            rcode,
        )

        if target is None:
            return (
                AdjustmentDecision("cancel", reason=rcode),
                meta,
            )

        tp = _round_tick(float(target), tick)
        min_d = _min_replace_delta(tick, min_replace_ticks)
        if abs(tp - price) < min_d - 1e-12:
            meta["reason_code"] = "coarse_tick_keep_already_at_target"
            return (
                AdjustmentDecision(
                    "keep",
                    reason="coarse_tick_keep_already_at_target",
                ),
                meta,
            )
        return (
            AdjustmentDecision("replace", new_price=tp, reason=rcode),
            meta,
        )

    # fine regime
    band = max(float(delta), 1e-12)
    dr = abs(float(price) - float(mid)) / band
    meta["distance_ratio"] = dr
    meta["candidate_prices"] = []
    meta["candidate_count"] = 0
    t = float(tick)

    ideal = (
        mid - 0.5 * band
        if side_u == "BUY"
        else mid + 0.5 * band
    )
    ideal = _round_tick(ideal, t)
    meta["chosen_target_price"] = ideal

    if 0.4 - 1e-12 <= dr <= 0.6 + 1e-12:
        meta["reason_code"] = "fine_tick_keep_in_target_band"
        LOG.info(
            "simple_price fine tick=%s mid=%.4f price=%.4f dr=%.4f -> keep in [0.4,0.6] band",
            tick,
            mid,
            price,
            dr,
        )
        return (
            AdjustmentDecision(
                "keep",
                reason="fine_tick_keep_in_target_band",
            ),
            meta,
        )

    if dr < 0.4 - 1e-12:
        rcode = "fine_tick_move_outward_to_half_band"
    else:
        rcode = "fine_tick_move_inward_to_half_band"

    meta["reason_code"] = rcode
    min_d = _min_replace_delta(t, min_replace_ticks)
    if abs(ideal - price) < min_d - 1e-12:
        meta["reason_code"] = f"{rcode}_noop_small_delta"
        LOG.info(
            "simple_price fine tick=%s dr=%.4f target=%.4f current=%.4f -> keep small delta",
            tick,
            dr,
            ideal,
            price,
        )
        return (
            AdjustmentDecision("keep", reason=meta["reason_code"]),
            meta,
        )

    LOG.info(
        "simple_price fine tick=%s mid=%.4f price=%.4f dr=%.4f -> replace to %.4f (%s)",
        tick,
        mid,
        price,
        dr,
        ideal,
        rcode,
    )
    return (
        AdjustmentDecision("replace", new_price=ideal, reason=rcode),
        meta,
    )
