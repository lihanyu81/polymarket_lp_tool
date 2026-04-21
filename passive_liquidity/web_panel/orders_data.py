from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
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
    orders: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Structured rows for HTML (same reward-band logic as Telegram /orders)."""
    src_orders = (
        orders if orders is not None else order_manager.fetch_all_open_orders(client)
    )
    rows: list[dict[str, Any]] = []
    parsed_orders: list[dict[str, Any]] = []
    token_ids: set[str] = set()
    condition_ids: set[str] = set()
    for o in src_orders:
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
        parsed_orders.append(
            {
                "o": o,
                "oid": oid,
                "cid": cid,
                "tid": tid,
                "side": su,
                "price": px,
                "size": sz,
            }
        )
        if tid:
            token_ids.add(tid)
        if cid:
            condition_ids.add(cid)

    book_cache: dict[str, Any] = {}
    spread_cache: dict[str, float] = {}
    title_cache: dict[tuple[str, str], str] = {}
    if (
        parsed_orders
        and reward_monitor is not None
        and book_fetcher is not None
        and (token_ids or condition_ids)
    ):
        max_workers = min(
            16,
            max(4, len(token_ids) + len(condition_ids)),
        )
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="web-orders") as pool:
            fut_to_tid = {
                pool.submit(book_fetcher.get_orderbook, tid): tid for tid in sorted(token_ids)
            }
            fut_to_cid = {
                pool.submit(reward_monitor.get_rewards_max_spread_for_market, cid): cid
                for cid in sorted(condition_ids)
            }
            for fut in as_completed(fut_to_tid):
                tid = fut_to_tid[fut]
                try:
                    book_cache[tid] = fut.result()
                except Exception:
                    continue
            for fut in as_completed(fut_to_cid):
                cid = fut_to_cid[fut]
                try:
                    spread_cache[cid] = float(fut.result())
                except Exception:
                    spread_cache[cid] = 0.0
    for item in parsed_orders:
        o = item["o"]
        oid = item["oid"]
        cid = item["cid"]
        tid = item["tid"]
        su = item["side"]
        px = item["price"]
        sz = item["size"]
        title_key = (cid, tid)
        if title_key not in title_cache:
            title_cache[title_key] = _orders_line_market_title(o, cid, tid, market_display)
        market_title = title_cache[title_key]
        reward_note = ""
        effective_tick: Optional[float] = None
        custom_tick_regime = ""
        if reward_monitor is not None and book_fetcher is not None and cid and tid:
            try:
                book = book_cache.get(tid)
                if book is None:
                    book = book_fetcher.get_orderbook(tid)
                    book_cache[tid] = book
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
                    max_spread = spread_cache.get(cid)
                    if max_spread is None:
                        max_spread = reward_monitor.get_rewards_max_spread_for_market(cid)
                        spread_cache[cid] = float(max_spread)
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
