from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from passive_liquidity.models import AdjustmentDecision, QuotePlan

LOG = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class ApplyDecisionResult:
    """Outcome of apply_decision for telemetry / Telegram."""

    outcome: str
    old_price: Optional[float] = None
    new_price: Optional[float] = None
    size: Optional[float] = None
    decision_reason: str = ""
    error_detail: Optional[str] = None


def _side(o: dict) -> str:
    return str(o.get("side") or "").upper()


def _price(o: dict) -> float:
    return float(o.get("price") or 0)


def _oid(o: dict) -> str:
    return str(o.get("id") or o.get("orderID") or "")


def _token_id(o: dict) -> str:
    return str(o.get("asset_id") or o.get("token_id") or "")


def _market(o: dict) -> str:
    return str(o.get("market") or o.get("condition_id") or "")


def _remaining_size(o: dict) -> float:
    if o.get("size") is not None and str(o.get("size")).strip() != "":
        return max(0.0, float(o["size"]))
    orig = float(o.get("original_size") or 0)
    matched = float(o.get("size_matched") or 0)
    return max(0.0, orig - matched)


class OrderManager:
    def __init__(self, tick_tolerance_mult: float = 1.0):
        self._tol = tick_tolerance_mult

    def fetch_all_open_orders(self, client: Any) -> list[dict]:
        from py_clob_client.clob_types import OpenOrderParams

        try:
            raw_list = client.get_orders(OpenOrderParams())
        except Exception as e:
            LOG.error("fetch_all_open_orders get_orders failed: %s", e)
            raise
        return [o for o in raw_list if isinstance(o, dict)]

    def apply_decision(
        self,
        client: Any,
        order: dict,
        decision: AdjustmentDecision,
        *,
        post_only: bool,
        delay_after_cancel_sec: float = 0.0,
        replace_post_retry_interval_sec: float = 5.0,
        replace_post_max_retries: int = 0,
        on_replace_post_retry: Optional[Callable[[int, str], None]] = None,
        replace_size: Optional[float] = None,
    ) -> ApplyDecisionResult:
        from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client.order_builder.constants import BUY, SELL

        oid = _oid(order)
        if not oid:
            LOG.warning("apply_decision: order missing id, skip")
            return ApplyDecisionResult("noop_missing_id")

        old_p = _price(order)
        sz0 = _remaining_size(order)

        if decision.action == "keep":
            return ApplyDecisionResult(
                "keep",
                old_price=old_p,
                size=sz0,
                decision_reason=decision.reason,
            )

        if decision.action == "cancel":
            try:
                LOG.info(
                    "ORDER_CANCEL_MS event=cancel_by_rule ts_ms=%d order_id=%s reason=%s",
                    _now_ms(),
                    oid[:20],
                    decision.reason,
                )
                client.cancel(oid)
                return ApplyDecisionResult(
                    "canceled_ok",
                    old_price=old_p,
                    size=sz0,
                    decision_reason=decision.reason,
                )
            except Exception as e:
                LOG.warning("cancel failed %s: %s", oid[:20], e)
                return ApplyDecisionResult(
                    "canceled_fail",
                    old_price=old_p,
                    size=sz0,
                    decision_reason=decision.reason,
                    error_detail=str(e),
                )

        if decision.action != "replace" or decision.new_price is None:
            return ApplyDecisionResult(
                "noop_unknown_action",
                old_price=old_p,
                decision_reason=decision.reason,
            )

        token_id = _token_id(order)
        side_u = _side(order)
        sz = _remaining_size(order)
        if replace_size is not None:
            sz = min(sz, max(0.0, float(replace_size)))
        if not token_id or not side_u:
            LOG.warning("apply_decision replace: missing token_id/side for %s", oid[:20])
            return ApplyDecisionResult(
                "replace_skip_bad_order",
                old_price=old_p,
                new_price=float(decision.new_price),
                decision_reason=decision.reason,
            )
        if sz <= 0:
            LOG.info("Skip replace %s: zero remaining size", oid[:20])
            return ApplyDecisionResult(
                "replace_skip_size",
                old_price=old_p,
                new_price=float(decision.new_price),
                size=0.0,
                decision_reason=decision.reason,
            )

        try:
            LOG.info(
                "Replace order %s %s %.4f -> %.4f size=%.4f (%s)",
                oid[:20],
                side_u,
                _price(order),
                decision.new_price,
                sz,
                decision.reason,
            )
            LOG.info(
                "ORDER_CANCEL_MS event=replace_cancel ts_ms=%d order_id=%s reason=%s",
                _now_ms(),
                oid[:20],
                decision.reason,
            )
            client.cancel(oid)
        except Exception as e:
            LOG.warning("replace cancel failed %s: %s", oid[:20], e)
            return ApplyDecisionResult(
                "replace_cancel_failed",
                old_price=old_p,
                new_price=float(decision.new_price),
                size=sz,
                decision_reason=decision.reason,
                error_detail=str(e),
            )

        if delay_after_cancel_sec > 0:
            LOG.info(
                "Waiting %.1fs after cancel before posting replacement for %s…",
                delay_after_cancel_sec,
                oid[:20],
            )
            time.sleep(delay_after_cancel_sec)

        attempt = 0
        unlimited = replace_post_max_retries <= 0
        last_err: Optional[str] = None
        while True:
            attempt += 1
            try:
                LOG.info(
                    "ORDER_REPOST_MS event=replace_post_attempt ts_ms=%d order_id=%s attempt=%d "
                    "price=%.4f size=%.4f post_only=%s",
                    _now_ms(),
                    oid[:20],
                    attempt,
                    float(decision.new_price),
                    float(sz),
                    bool(post_only),
                )
                order_signed = client.create_order(
                    OrderArgs(
                        token_id=token_id,
                        price=float(decision.new_price),
                        size=float(sz),
                        side=BUY if side_u == "BUY" else SELL,
                    ),
                    PartialCreateOrderOptions(),
                )
                client.post_order(order_signed, orderType=OrderType.GTC, post_only=post_only)
                LOG.info(
                    "ORDER_REPOST_MS event=replace_post_success ts_ms=%d order_id=%s attempt=%d",
                    _now_ms(),
                    oid[:20],
                    attempt,
                )
                return ApplyDecisionResult(
                    "replaced_ok",
                    old_price=old_p,
                    new_price=float(decision.new_price),
                    size=sz,
                    decision_reason=decision.reason,
                )
            except Exception as e:
                last_err = str(e)
                LOG.warning(
                    "ORDER_REPOST_MS event=replace_post_failed ts_ms=%d attempt=%d%s order_id=%s: %s",
                    _now_ms(),
                    attempt,
                    "" if unlimited else f"/{replace_post_max_retries}",
                    oid[:20],
                    e,
                )
                if on_replace_post_retry is not None:
                    try:
                        on_replace_post_retry(attempt, last_err or "unknown")
                    except Exception:
                        LOG.exception("on_replace_post_retry failed")
                if not unlimited and attempt >= replace_post_max_retries:
                    LOG.error(
                        "Giving up replace post for %s after %d attempts",
                        oid[:20],
                        replace_post_max_retries,
                    )
                    return ApplyDecisionResult(
                        "replace_failed",
                        old_price=old_p,
                        new_price=float(decision.new_price),
                        size=sz,
                        decision_reason=decision.reason,
                        error_detail=last_err,
                    )
                interval = (
                    replace_post_retry_interval_sec
                    if replace_post_retry_interval_sec > 0
                    else 1.0
                )
                time.sleep(interval)

    def sync_orders(
        self,
        client: Any,
        condition_id: str,
        token_id: str,
        plan: QuotePlan,
        tick_size: float,
    ) -> None:
        from py_clob_client.clob_types import OpenOrderParams, OrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client.order_builder.constants import BUY, SELL

        params = OpenOrderParams(market=condition_id, asset_id=token_id)
        try:
            raw_list = client.get_orders(params)
        except Exception as e:
            LOG.error("sync_orders get_orders failed: %s", e)
            raise

        open_orders: list[dict] = [o for o in raw_list if isinstance(o, dict)]

        if plan.skip_reason:
            if open_orders:
                LOG.warning("Canceling all on %s (%s): %s", token_id[:20], condition_id[:12], plan.skip_reason)
                client.cancel_market_orders(market=condition_id, asset_id=token_id)
            return

        thr = max(tick_size * self._tol, 1e-9)

        def keep_or_replace(side: str, desired: Optional[float]) -> None:
            same = [o for o in open_orders if _side(o) == side]
            if desired is None:
                for o in same:
                    oid = _oid(o)
                    if oid:
                        LOG.info("Cancel %s %s", side, oid[:16])
                        client.cancel(oid)
                return
            keep_id: Optional[str] = None
            for o in same:
                if abs(_price(o) - desired) <= thr:
                    keep_id = _oid(o)
                    break
            if keep_id:
                for o in same:
                    oid = _oid(o)
                    if oid and oid != keep_id:
                        LOG.info("Cancel duplicate %s %s", side, oid[:16])
                        client.cancel(oid)
                LOG.debug("Keep %s order %s @ %.4f", side, keep_id[:16], desired)
                return
            for o in same:
                oid = _oid(o)
                if oid:
                    LOG.info("Cancel stale %s %s price=%.4f (want %.4f)", side, oid[:16], _price(o), desired)
                    client.cancel(oid)
            LOG.info("Post %s %.4f size=%.4f post_only=%s", side, desired, plan.size, plan.post_only)
            order = client.create_order(
                OrderArgs(
                    token_id=token_id,
                    price=float(desired),
                    size=float(plan.size),
                    side=BUY if side == "BUY" else SELL,
                ),
                PartialCreateOrderOptions(),
            )
            client.post_order(order, orderType=OrderType.GTC, post_only=plan.post_only)

        keep_or_replace("BUY", plan.bid_price)
        keep_or_replace("SELL", plan.ask_price)
