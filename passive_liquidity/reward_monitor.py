from __future__ import annotations

import logging
from typing import Any

from passive_liquidity.config_manager import PassiveConfig
from passive_liquidity.http_utils import http_json
from passive_liquidity.models import RewardRange, ScoringStatus

LOG = logging.getLogger(__name__)


class RewardMonitor:
    def __init__(self, config: PassiveConfig):
        self._config = config
        self._rewards_spread_cache: dict[str, float] = {}

    def get_reward_range(self, mid_price: float, rewards_max_spread: float) -> RewardRange:
        delta = max(0.0, float(rewards_max_spread)) * 0.01
        return RewardRange(mid=mid_price, delta=delta)

    def get_rewards_max_spread_for_market(self, condition_id: str) -> float:
        if condition_id in self._rewards_spread_cache:
            return self._rewards_spread_cache[condition_id]
        url = f"{self._config.clob_host}/rewards/markets/{condition_id}"
        try:
            page = http_json("GET", url)
            rows = page.get("data") or []
            v = float(rows[0].get("rewards_max_spread", 0) or 0) if rows else 0.0
        except Exception as e:
            LOG.warning("rewards market fetch failed %s: %s", condition_id[:12], e)
            v = 0.0
        self._rewards_spread_cache[condition_id] = v
        return v

    @staticmethod
    def _as_scoring_bool(v: Any) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, str) and v.lower() in ("true", "false"):
            return v.lower() == "true"
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, dict) and "scoring" in v:
            return bool(v["scoring"])
        return False

    def _parse_orders_scoring_payload(self, raw: Any, chunk: list[str]) -> dict[str, bool]:
        """Normalize CLOB /orders-scoring response to order_id -> bool."""
        if raw is None:
            return {str(oid): False for oid in chunk}

        if isinstance(raw, dict) and "data" in raw:
            raw = raw["data"]

        out: dict[str, bool] = {str(oid): False for oid in chunk}

        if isinstance(raw, dict):
            for oid in chunk:
                key = str(oid)
                out[key] = self._as_scoring_bool(raw.get(key))
            return out

        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                oid = str(
                    item.get("order_id")
                    or item.get("orderId")
                    or item.get("id")
                    or ""
                )
                if oid in out:
                    out[oid] = self._as_scoring_bool(item.get("scoring"))
            return out

        return out

    def batch_order_scoring(self, client: Any, order_ids: list[str]) -> dict[str, bool]:
        """Map order_id -> scoring bool; missing ids treated False."""
        from py_clob_client_v2.clob_types import OrdersScoringParams

        out: dict[str, bool] = {}
        if not order_ids:
            return out

        chunk_size = 80
        for i in range(0, len(order_ids), chunk_size):
            chunk = order_ids[i : i + chunk_size]
            try:
                raw = client.are_orders_scoring(OrdersScoringParams(orderIds=chunk))
            except Exception as e:
                LOG.warning("are_orders_scoring batch failed: %s", e)
                continue
            parsed = self._parse_orders_scoring_payload(raw, chunk)
            out.update(parsed)
        return out

    def get_scoring_status(self, client: Any, condition_id: str, token_id: str) -> ScoringStatus:
        from py_clob_client_v2.clob_types import OpenOrderParams, OrdersScoringParams

        try:
            params = OpenOrderParams(market=condition_id, asset_id=token_id)
            open_orders = client.get_open_orders(params)
        except Exception as e:
            LOG.warning("get_open_orders failed for %s: %s", token_id[:16], e)
            return ScoringStatus(False, False, 0.0, 0, {})

        ids: list[str] = []
        for o in open_orders:
            oid = o.get("id") if isinstance(o, dict) else getattr(o, "id", None)
            if oid:
                ids.append(str(oid))
        if not ids:
            return ScoringStatus(False, False, 0.0, 0, {})

        try:
            raw = client.are_orders_scoring(OrdersScoringParams(orderIds=ids))
        except Exception as e:
            LOG.warning("are_orders_scoring failed: %s", e)
            return ScoringStatus(False, False, 0.0, len(ids), {})

        parsed = self._parse_orders_scoring_payload(raw, ids)
        bools = [parsed.get(str(oid), False) for oid in ids]
        if not bools:
            return ScoringStatus(False, False, 0.0, len(ids), parsed)

        any_s = any(bools)
        all_s = all(bools)
        frac = sum(1 for b in bools if b) / len(bools)
        return ScoringStatus(any_s, all_s, frac, len(ids), parsed)
