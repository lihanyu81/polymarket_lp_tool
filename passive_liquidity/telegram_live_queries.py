"""
Live account / orders / PnL for Telegram commands (no periodic-summary cache).

Each call performs fresh CLOB + optional on-chain/Bridge fetches.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional, Tuple

from passive_liquidity.account_portfolio import (
    combine_clob_and_positions_market_value_usdc,
    fetch_collateral_snapshot,
    read_optional_deposit_env,
    resolve_deposit_reference,
)
from passive_liquidity.bridge_deposits import fetch_bridge_polygon_usdc_deposits
from passive_liquidity.market_display import MarketDisplayResolver
from passive_liquidity.orderbook_fetcher import (
    OrderBookFetcher,
    pricing_tick_for_order_like_main_loop,
    resolve_effective_tick_size,
)
from passive_liquidity.order_manager import (
    OrderManager,
    _oid,
    _price,
    _remaining_size,
    _side,
    _token_id,
)
from passive_liquidity.polygon_deposits import fetch_polygon_usdc_deposit_summary
from passive_liquidity.reward_monitor import RewardMonitor
from passive_liquidity.simple_price_policy import (
    classify_tick_regime,
    fine_reward_display_lo_hi,
    fine_tick_display_decimals,
    list_coarse_reward_book_candidates,
)

LOG = logging.getLogger(__name__)


def _fmt_usdc(x: float) -> str:
    return f"{x:.4f}"


def _data_api_host() -> str:
    return os.environ.get(
        "POLYMARKET_DATA_API", "https://data-api.polymarket.com"
    ).rstrip("/")


def _order_display_meta(order: dict) -> tuple[str, str]:
    title = str(
        order.get("question")
        or order.get("market_question")
        or order.get("title")
        or ""
    ).strip()
    if not title:
        slug = order.get("market_slug") or order.get("slug") or ""
        title = str(slug).strip() if slug else ""
    if not title:
        mid = str(order.get("market") or order.get("condition_id") or "").strip()
        title = (mid[:48] + "…") if len(mid) > 48 else mid if mid else ""
    outcome = str(order.get("outcome") or order.get("outcome_name") or "").strip()
    return title, outcome


def _order_has_human_market_copy(order: dict) -> bool:
    if str(
        order.get("question")
        or order.get("market_question")
        or order.get("title")
        or ""
    ).strip():
        return True
    if str(order.get("market_slug") or order.get("slug") or "").strip():
        return True
    return False


def _orders_line_market_title(
    order: dict,
    condition_id: str,
    token_id: str,
    resolver: Optional[MarketDisplayResolver],
) -> str:
    title, outcome = _order_display_meta(order)
    if resolver is not None and condition_id and token_id:
        if not _order_has_human_market_copy(order):
            gq, go = resolver.lookup(condition_id, token_id)
            if gq:
                title = gq
            if go:
                outcome = go
    if title and outcome:
        return f"{title}（{outcome}）"
    if title:
        return title
    if outcome:
        return outcome
    return "（未知盘口）"


def get_live_account_status(
    *,
    client: Any,
    order_manager: OrderManager,
    funder: str,
    account_label: str,
) -> Tuple[bool, str]:
    """
    Returns (ok, formatted_zh_message_or_error).
    """
    try:
        orders = order_manager.fetch_all_open_orders(client)
    except Exception as e:
        LOG.exception("live /status: fetch orders failed: %s", e)
        return False, f"查询失败（未成交单）: {e}"

    try:
        snap = fetch_collateral_snapshot(client, orders)
    except Exception as e:
        LOG.exception("live /status: collateral failed: %s", e)
        return False, f"查询失败（账户余额）: {e}"

    if snap is None:
        return False, "查询失败：无法取得 CLOB 抵押品快照。"

    clob_usdc = float(snap.total_balance_usdc)
    portfolio_usdc, pos_sum, pos_err = combine_clob_and_positions_market_value_usdc(
        clob_usdc, funder, _data_api_host()
    )
    if pos_sum is None:
        pos_note = f"持仓市值: （未计入，Data API: {pos_err}）"
    else:
        pos_note = f"持仓市值合计（Data API）: {_fmt_usdc(float(pos_sum))} USDC"

    env_dep = read_optional_deposit_env()
    polygon_summary = None
    bridge_summary = None
    try:
        polygon_summary = fetch_polygon_usdc_deposit_summary(funder)
    except Exception as e:
        LOG.debug("live /status: polygon deposit fetch: %s", e)
    try:
        bridge_summary = fetch_bridge_polygon_usdc_deposits(funder)
    except Exception as e:
        LOG.debug("live /status: bridge deposit fetch: %s", e)

    dep, dep_src, _approx = resolve_deposit_reference(
        polygon_summary=polygon_summary,
        env_override=env_dep,
        bridge_summary=bridge_summary,
        startup_total_balance=float(portfolio_usdc),
    )
    pnl: Optional[float]
    if dep is not None:
        pnl = float(portfolio_usdc) - float(dep)
    else:
        pnl = None

    label = (account_label or "Polymarket").strip() or "Polymarket"
    lines = [
        f"[{label}]",
        "实时状态",
        f"账户总额（组合≈）: {_fmt_usdc(portfolio_usdc)} USDC",
        f"CLOB 抵押 USDC: {_fmt_usdc(clob_usdc)} USDC",
        pos_note,
        f"可用余额（CLOB 可开新单≈）: {_fmt_usdc(snap.available_balance_usdc)} USDC",
    ]
    if dep is not None:
        lines.append(f"入账参考: {_fmt_usdc(dep)} USDC")
    else:
        lines.append("入账参考: （未配置）")
    if pnl is not None:
        sign = "+" if pnl >= 0 else ""
        lines.append(f"盈亏: {sign}{_fmt_usdc(pnl)} USDC")
    else:
        lines.append(f"盈亏: （未配置入账参考；{dep_src[:80]}）")
    lines.append(f"未成交买单占用: {_fmt_usdc(snap.locked_open_buy_usdc)} USDC")
    lines.append(f"当前挂单数: {len(orders)}")
    return True, "\n".join(lines)


def get_live_order_summary(
    *,
    client: Any,
    order_manager: OrderManager,
    market_display: Optional[MarketDisplayResolver] = None,
    book_fetcher: Optional[OrderBookFetcher] = None,
    reward_monitor: Optional[RewardMonitor] = None,
) -> Tuple[bool, str]:
    try:
        orders = order_manager.fetch_all_open_orders(client)
    except Exception as e:
        LOG.exception("live /orders: fetch failed: %s", e)
        return False, f"查询失败: {e}"

    n = len(orders)
    # 账号前缀由 Telegram send_command_reply 统一加，此处不再重复 [label]
    lines = [
        "实时挂单",
        f"未成交单总数: {n}",
    ]
    if n == 0:
        lines.append("（当前无挂单）")
        return True, "\n".join(lines)

    shown = 0
    for o in orders:
        if not isinstance(o, dict):
            continue
        oid = str(_oid(o) or "").strip()
        if not oid:
            continue
        shown += 1
        cid = str(o.get("market") or o.get("condition_id") or "").strip()
        tid = str(_token_id(o) or "").strip()
        market_title = _orders_line_market_title(o, cid, tid, market_display)
        su = _side(o) or "?"
        try:
            px = float(_price(o))
        except (TypeError, ValueError):
            px = 0.0
        sz = _remaining_size(o)
        lines.append(f"{shown}) 盘口: {market_title}")
        lines.append(f"   order_id={oid}")
        lines.append(f"   side={su}  price={px}  size={sz}")
        if reward_monitor is not None and book_fetcher is not None and cid and tid:
            try:
                book = book_fetcher.get_orderbook(tid)
                mid = book.mid
                if mid is None:
                    mid = book_fetcher.mid_price(tid)
                if mid is not None:
                    max_spread = reward_monitor.get_rewards_max_spread_for_market(cid)
                    rr = reward_monitor.get_reward_range(float(mid), float(max_spread))
                    t_book_eff = max(
                        float(
                            resolve_effective_tick_size(
                                book.tick_size, book.bids, book.asks
                            )
                        ),
                        1e-12,
                    )
                    t_reward = pricing_tick_for_order_like_main_loop(
                        book_tick_size=book.tick_size,
                        bids=book.bids,
                        asks=book.asks,
                        order_price=float(px),
                    )
                    reg = classify_tick_regime(t_book_eff)
                    if reg == "coarse":
                        # Coarse: only resting book prices in the reward half-band (near-mid→far),
                        # aligned with custom coarse N and default coarse candidates.
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
                            lines.append(
                                f"   可得奖励档位({str(su).upper()})簿上≈[{levels_s}]"
                                f"（扫描[{lo:.4f},{hi:.4f}] mid={rr.mid:.4f}, δ={rr.delta:.4f}, tick={t_reward:.4f}）"
                            )
                        else:
                            lines.append(
                                f"   奖励扫描≈[{lo:.4f}, {hi:.4f}] 簿上无同侧正深度档位"
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
                        lines.append(
                            f"   奖励区间≈[{lo_d:.{dec}f}, {hi_d:.{dec}f}]"
                            f"（mid={rr.mid:.4f}, δ={rr.delta:.4f}）"
                        )
            except Exception as e:
                LOG.debug("live /orders reward range failed oid=%s: %s", oid[:16], e)
    if shown == 0:
        lines.append("（未能解析订单 id）")
    return True, "\n".join(lines)


def get_live_pnl(
    *,
    client: Any,
    order_manager: OrderManager,
    funder: str,
    account_label: str,
) -> Tuple[bool, str]:
    """Same deposit resolution as /status; message focused on PnL."""
    try:
        orders = order_manager.fetch_all_open_orders(client)
    except Exception as e:
        LOG.exception("live /pnl: orders failed: %s", e)
        return False, f"查询失败（未成交单）: {e}"

    try:
        snap = fetch_collateral_snapshot(client, orders)
    except Exception as e:
        LOG.exception("live /pnl: collateral failed: %s", e)
        return False, f"查询失败（账户）: {e}"

    if snap is None:
        return False, "查询失败：无法取得账户总额。"

    clob_usdc = float(snap.total_balance_usdc)
    portfolio_usdc, pos_sum, pos_err = combine_clob_and_positions_market_value_usdc(
        clob_usdc, funder, _data_api_host()
    )

    env_dep = read_optional_deposit_env()
    polygon_summary = None
    bridge_summary = None
    try:
        polygon_summary = fetch_polygon_usdc_deposit_summary(funder)
    except Exception as e:
        LOG.debug("live /pnl: polygon: %s", e)
    try:
        bridge_summary = fetch_bridge_polygon_usdc_deposits(funder)
    except Exception as e:
        LOG.debug("live /pnl: bridge: %s", e)

    dep, dep_src, _ = resolve_deposit_reference(
        polygon_summary=polygon_summary,
        env_override=env_dep,
        bridge_summary=bridge_summary,
        startup_total_balance=float(portfolio_usdc),
    )
    label = (account_label or "Polymarket").strip() or "Polymarket"
    lines = [
        f"[{label}]",
        "实时盈亏",
        f"组合总额（≈）: {_fmt_usdc(portfolio_usdc)} USDC",
        f"CLOB 抵押: {_fmt_usdc(clob_usdc)} USDC",
    ]
    if pos_sum is None:
        lines.append(f"持仓市值: （未计入：{pos_err}）")
    else:
        lines.append(f"持仓市值: {_fmt_usdc(float(pos_sum))} USDC")
    if dep is not None:
        pnl = float(portfolio_usdc) - float(dep)
        sign = "+" if pnl >= 0 else ""
        lines.append(f"入账参考: {_fmt_usdc(dep)} USDC")
        lines.append(f"参考来源: {dep_src}")
        lines.append(f"盈亏: {sign}{_fmt_usdc(pnl)} USDC")
    else:
        lines.append("入账参考: （未配置）")
        lines.append(f"说明: {dep_src}")
        lines.append("盈亏: 无法计算（请先配置入账参考）")
    return True, "\n".join(lines)
