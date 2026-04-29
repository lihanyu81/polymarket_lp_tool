"""
CLOB collateral snapshot, open-order USDC lock, and optional deposit baseline for PnL.
"""

from __future__ import annotations

import datetime
import logging
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from passive_liquidity.http_utils import http_json

if TYPE_CHECKING:
    from passive_liquidity.bridge_deposits import BridgeDepositSummary
    from passive_liquidity.polygon_deposits import PolygonDepositSummary
from passive_liquidity.order_manager import _price, _remaining_size, _side

LOG = logging.getLogger(__name__)

# Activity rows that increase net USDC into the proxy (extend as API evolves).
# Matching is case-insensitive on `type`.
_DEPOSIT_ACTIVITY_TYPES = frozenset(
    {
        "DEPOSIT",
        "DEPOSIT_USDC",
        "BRIDGE_DEPOSIT",
        "CONVERT",
        "DEPOSIT_RECEIVED",
        "COLLATERAL_DEPOSIT",
    }
)


@dataclass
class CollateralSnapshot:
    """USDC collateral view for Telegram / PnL."""

    # Raw collateral from CLOB balance API (1e-6 scaled in response).
    api_collateral_usdc: float
    locked_open_buy_usdc: float
    # Account total — same as API collateral for this integration (see debug logs).
    total_balance_usdc: float
    # Funds still free to place new orders after reserving open BUY notionals.
    available_balance_usdc: float

    @property
    def raw_api_balance_usdc(self) -> float:
        """Alias for older call sites."""
        return self.api_collateral_usdc


def usdc_locked_in_open_buys(orders: list[dict]) -> float:
    """USDC notionally reserved by open BUY limit orders (price × remaining size)."""
    locked = 0.0
    for o in orders:
        if not isinstance(o, dict):
            continue
        if _side(o) != "BUY":
            continue
        locked += max(0.0, _price(o)) * max(0.0, _remaining_size(o))
    return locked


def _parse_balance_allowance_response(raw: Any) -> float:
    if raw is None:
        return 0.0
    if isinstance(raw, dict):
        b = raw.get("balance")
        if b is None:
            return 0.0
        return float(b) / 1_000_000.0
    return 0.0


def fetch_collateral_snapshot(client: Any, open_orders: list[dict]) -> Optional[CollateralSnapshot]:
    """
    Refresh allowance cache, read COLLATERAL balance.

    Polymarket returns balance in 1e-6 USDC units.
    **total_balance_usdc** = API account total (collateral).
    **available_balance_usdc** = API collateral minus estimated USDC locked in open BUYs
    (price × remaining size), floored at zero — usable for new orders.
    """
    try:
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=-1)
        try:
            client.update_balance_allowance(params)
        except Exception as e:
            LOG.debug("update_balance_allowance (optional): %s", e)
        raw = client.get_balance_allowance(params)
    except Exception as e:
        LOG.warning("get_balance_allowance failed: %s", e)
        return None

    api_usdc = _parse_balance_allowance_response(raw)
    locked = usdc_locked_in_open_buys(open_orders)
    account_total = max(0.0, api_usdc)
    computed_avail = max(0.0, api_usdc - locked)
    LOG.debug(
        "collateral snapshot: api_account_total=%.6f api_collateral=%.6f "
        "locked_amount_in_open_orders=%.6f computed_available_balance=%.6f",
        account_total,
        api_usdc,
        locked,
        computed_avail,
    )
    return CollateralSnapshot(
        api_collateral_usdc=api_usdc,
        locked_open_buy_usdc=locked,
        total_balance_usdc=account_total,
        available_balance_usdc=computed_avail,
    )


def fetch_positions_current_value_sum_usdc(
    user_address: str,
    data_api_host: str,
    *,
    limit: int = 500,
    max_pages: int = 25,
) -> tuple[Optional[float], str]:
    """
    Sum ``currentValue`` from Data API ``GET /positions`` (portfolio mark, USDC).

    This is **not** included in CLOB ``balance-allowance`` collateral; Polymarket UI
    "portfolio" total is typically CLOB USDC + position current values.

    Returns ``(None, reason)`` on failure, ``(0.0, \"\")`` if no positions, else ``(sum, \"\")``.
    """
    host = data_api_host.rstrip("/")
    user = str(user_address).strip()
    if not user:
        return None, "empty user address"
    total = 0.0
    offset = 0
    for _ in range(max(1, max_pages)):
        url = f"{host}/positions?user={user}&limit={int(limit)}&offset={int(offset)}"
        try:
            rows = http_json("GET", url)
        except Exception as e:
            LOG.warning("positions currentValue sum failed: %s", e)
            return None, str(e)[:200]
        if not isinstance(rows, list):
            return None, "positions API returned non-list"
        for p in rows:
            if not isinstance(p, dict):
                continue
            try:
                cv = p.get("currentValue")
                if cv is None or str(cv).strip() == "":
                    continue
                total += max(0.0, float(cv))
            except (TypeError, ValueError):
                continue
        if len(rows) < limit:
            break
        offset += limit
    return float(total), ""


def combine_clob_and_positions_market_value_usdc(
    clob_total_usdc: float,
    funder_address: str,
    data_api_host: str,
) -> tuple[float, Optional[float], str]:
    """
    Portfolio total ≈ CLOB collateral + sum(Data API position ``currentValue``).

    Returns ``(portfolio_total, positions_sum_or_None, error_if_positions_failed)``.
    When positions fetch fails, ``portfolio_total == clob_total_usdc`` and
    ``positions_sum_or_None is None``.
    """
    c = max(0.0, float(clob_total_usdc))
    ps, err = fetch_positions_current_value_sum_usdc(
        str(funder_address).strip(), data_api_host
    )
    if ps is None:
        return c, None, err
    return c + max(0.0, float(ps)), float(ps), ""


def fetch_total_deposited_from_activity(
    user_address: str,
    data_api_host: str,
    *,
    max_pages: int = 40,
    page_size: int = 500,
) -> Optional[float]:
    """
    Sum usdcSize for known deposit-like activity types. Returns None if nothing matched.
    """
    host = data_api_host.rstrip("/")
    total = 0.0
    found = False
    offset = 0
    for _ in range(max_pages):
        url = f"{host}/activity?user={user_address}&limit={page_size}&offset={offset}"
        try:
            rows = http_json("GET", url)
        except Exception as e:
            LOG.warning("activity API failed (deposits): %s", e)
            return None
        if not isinstance(rows, list) or not rows:
            break
        for r in rows:
            if not isinstance(r, dict):
                continue
            row_type = str(r.get("type") or "").strip().upper()
            if row_type not in _DEPOSIT_ACTIVITY_TYPES:
                continue
            try:
                total += float(r.get("usdcSize") or r.get("amount") or 0)
            except (TypeError, ValueError):
                continue
            found = True
        if len(rows) < page_size:
            break
        offset += page_size
    return total if found else None


def allow_startup_total_as_deposit_reference() -> bool:
    """Opt-in only: using current balance as 累计入账 is usually misleading for PnL."""
    v = os.environ.get("PASSIVE_USE_STARTUP_TOTAL_AS_DEPOSIT_REF", "").strip().lower()
    return v in ("1", "true", "yes", "on")


def resolve_deposit_reference(
    *,
    polygon_summary: Optional["PolygonDepositSummary"],
    env_override: Optional[float],
    bridge_summary: Optional["BridgeDepositSummary"],
    startup_total_balance: float,
) -> tuple[Optional[float], str, bool]:
    """
    Funding baseline for PnL. Priority:

    1. On-chain Polygon USDC (Polygonscan tokentx), when ``polygon_summary`` is set.
    2. ``TELEGRAM_TOTAL_DEPOSITED_USDC`` in environment.
    3. Polymarket Bridge API — completed transfers targeting Polygon USDC.
    4. Startup account total **only** if ``PASSIVE_USE_STARTUP_TOTAL_AS_DEPOSIT_REF`` is enabled.
    5. Otherwise **no** reference (``None``) — do not equate 累计入账 with current balance.

    Returns (deposited_reference_usdc_or_None, deposit_reference_source_zh, approximate_tracking).
    """
    if polygon_summary is not None:
        ref = max(0.0, float(polygon_summary.total_usdc))
        src = "链上 Polygon USDC 转入累计（Polygonscan tokentx）"
        approx = bool(polygon_summary.approximate)
        if (
            ref <= 1e-9
            and bridge_summary is not None
            and bridge_summary.total_usdc > 1e-9
        ):
            return (
                max(0.0, float(bridge_summary.total_usdc)),
                "Polymarket Bridge API（链上 tokentx 合计为 0，改用 Bridge 已完成入账）",
                True,
            )
        return (ref, src, approx)
    if env_override is not None:
        return (
            max(0.0, float(env_override)),
            "环境变量 TELEGRAM_TOTAL_DEPOSITED_USDC",
            False,
        )
    if bridge_summary is not None and bridge_summary.total_usdc > 0:
        return (
            max(0.0, float(bridge_summary.total_usdc)),
            "Polymarket Bridge API（已完成且目标为 Polygon USDC 的入账）",
            True,
        )
    if allow_startup_total_as_deposit_reference() and startup_total_balance > 0:
        return (
            startup_total_balance,
            "临时参考：启动时账户总额（已开启 PASSIVE_USE_STARTUP_TOTAL_AS_DEPOSIT_REF）",
            True,
        )
    return (
        None,
        "未配置入账参考（请设置 POLYGONSCAN_API_KEY、TELEGRAM_TOTAL_DEPOSITED_USDC，"
        "或确认 Bridge API 能返回您的入账记录；勿将当前余额误作累计充值）",
        True,
    )


def resolve_total_deposited_usdc(
    *,
    env_override: Optional[float],
    activity_sum: Optional[float],
    startup_total_balance: float,
) -> tuple[float, str]:
    """
    Deprecated: use resolve_deposit_reference with polygon_summary.
    Kept for callers that still use Data API activity only.
    """
    if env_override is not None:
        return (max(0.0, float(env_override)), "环境变量 TELEGRAM_TOTAL_DEPOSITED_USDC")
    if activity_sum is not None and activity_sum > 0:
        return (activity_sum, "Data API 活动记录汇总（入账类）")
    if startup_total_balance > 0:
        return (
            startup_total_balance,
            "未配置入账总额：以本次启动时账户总额为参考（后续盈亏相对该值）",
        )
    return (0.0, "无法确定入账参考（余额为 0）")


def seconds_until_next_half_hour_boundary(now: Optional[float] = None) -> float:
    """Wall-clock seconds until next :00 or :30 (local time)."""
    now = now or time.time()
    dt = datetime.datetime.fromtimestamp(now)
    if dt.minute < 30:
        target = dt.replace(minute=30, second=0, microsecond=0)
    else:
        target = dt.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
    delta = (target - dt).total_seconds()
    return max(0.5, delta)


def half_hour_slot_key(ts: Optional[float] = None) -> str:
    """Stable id for the current local half-hour window (for one-shot summary per slot)."""
    ts = ts or time.time()
    t = time.localtime(ts)
    block = 0 if t.tm_min < 30 else 30
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}T{t.tm_hour:02d}:{block:02d}"


def read_optional_deposit_env() -> Optional[float]:
    v = os.environ.get("TELEGRAM_TOTAL_DEPOSITED_USDC", "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        LOG.warning("Invalid TELEGRAM_TOTAL_DEPOSITED_USDC=%r", v)
        return None
