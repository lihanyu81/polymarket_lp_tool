from __future__ import annotations

from typing import Any, Optional

from passive_liquidity.market_display import MarketDisplayResolver
from passive_liquidity.order_manager import (
    OrderManager,
    _oid,
    _price,
    _remaining_size,
    _side,
    _token_id,
)
from passive_liquidity.orderbook_fetcher import (
    OrderBookFetcher,
    pricing_tick_for_order_like_main_loop,
    resolve_effective_tick_size,
)
from passive_liquidity.reward_monitor import RewardMonitor
from passive_liquidity.simple_price_policy import (
    classify_custom_tick_regime,
    classify_tick_regime,
    fine_reward_display_lo_hi,
    fine_tick_display_decimals,
    list_coarse_reward_book_candidates,
)
from passive_liquidity.telegram_live_queries import _orders_line_market_title


def orders_as_rows(
    *,
    client: Any,
    order_manager: OrderManager,
    market_display: Optional[MarketDisplayResolver],
    book_fetcher: Optional[OrderBookFetcher],
    reward_monitor: Optional[RewardMonitor],
) -> list[dict[str, Any]]:
    """Structured rows for HTML (same reward-band logic as Telegram /orders)."""
    orders = order_manager.fetch_all_open_orders(client)
    rows: list[dict[str, Any]] = []
    for o in orders:
        if not isinstance(o, dict):
            continue
        oid = str(_oid(o) or "").strip()
        if not oid:
            continue
        cid = str(o.get("market") or o.get("condition_id") or "").strip()
        tid = str(_token_id(o) or "").strip()
        su = _side(o) or "?"
        try:
            px = float(_price(o))
        except (TypeError, ValueError):
            px = 0.0
        sz = _remaining_size(o)
        market_title = _orders_line_market_title(o, cid, tid, market_display)
        reward_note = ""
        effective_tick: Optional[float] = None
        custom_tick_regime = ""
        if reward_monitor is not None and book_fetcher is not None and cid and tid:
            try:
                book = book_fetcher.get_orderbook(tid)
                t_eff = resolve_effective_tick_size(book.tick_size, book.bids, book.asks)
                effective_tick = max(float(t_eff), 1e-12)
                custom_tick_regime = classify_custom_tick_regime(effective_tick)
                # 粗/细展示看盘口；候选档与主循环调价用「本单 tick」（含订单价子分位 → 0.001）
                t_reward = pricing_tick_for_order_like_main_loop(
                    book_tick_size=book.tick_size,
                    bids=book.bids,
                    asks=book.asks,
                    order_price=float(px),
                )
                mid = book.mid
                if mid is None:
                    mid = book_fetcher.mid_price(tid)
                if mid is not None:
                    max_spread = reward_monitor.get_rewards_max_spread_for_market(cid)
                    rr = reward_monitor.get_reward_range(float(mid), float(max_spread))
                    reg = classify_tick_regime(effective_tick)
                    if reg == "coarse":
                        lo, hi, book_lv = list_coarse_reward_book_candidates(
                            str(su).upper(),
                            float(rr.mid),
                            float(rr.delta),
                            t_reward,
                            book.bids,
                            book.asks,
                        )
                        if book_lv:
                            dec = fine_tick_display_decimals(t_reward)
                            levels_s = ",".join(f"{p:.{dec}f}" for p in book_lv)
                            reward_note = (
                                f"可得奖励档位({str(su).upper()})簿上≈[{levels_s}] "
                                f"（扫描[{lo:.4f},{hi:.4f}] mid={rr.mid:.4f}, δ={rr.delta:.4f}）"
                            )
                        else:
                            reward_note = (
                                f"奖励扫描≈[{lo:.4f},{hi:.4f}] 簿上无同侧档位 "
                                f"（mid={rr.mid:.4f}, δ={rr.delta:.4f}）"
                            )
                    else:
                        side_fine = (
                            str(su).upper()
                            if str(su).upper() in ("BUY", "SELL")
                            else None
                        )
                        lo_d, hi_d, _ = fine_reward_display_lo_hi(
                            float(rr.mid),
                            float(rr.delta),
                            t_reward,
                            book.bids,
                            book.asks,
                            side=side_fine,
                        )
                        dec = fine_tick_display_decimals(t_reward)
                        reward_note = (
                            f"奖励区间≈[{lo_d:.{dec}f}, {hi_d:.{dec}f}] "
                            f"（mid={rr.mid:.4f}, δ={rr.delta:.4f}）"
                        )
            except Exception:
                reward_note = ""
                effective_tick = None
                custom_tick_regime = ""
        rows.append(
            {
                "order_id": oid,
                "market_title": market_title,
                "condition_id": cid,
                "token_id": tid,
                "side": su,
                "price": px,
                "size": sz,
                "reward_note": reward_note,
                "effective_tick": effective_tick,
                "custom_tick_regime": custom_tick_regime,
            }
        )
    return rows
