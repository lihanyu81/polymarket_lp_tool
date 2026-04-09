"""
Non-blocking Telegram notifications for Polymarket monitoring (optional, env-driven).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

LOG = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _fmt_amt(x: float) -> str:
    """Telegram amount display (money/price) keeps 2 decimals."""
    return f"{float(x):.2f}"


def _maybe_log_supergroup_migration(error_body: str) -> None:
    """
    Telegram returns 400 with migrate_to_chat_id when a group was upgraded to supergroup.
    Old TELEGRAM_CHAT_ID stops working; user must update .env to the new id.
    """
    try:
        data = json.loads(error_body)
    except json.JSONDecodeError:
        return
    params = data.get("parameters")
    if not isinstance(params, dict):
        return
    new_id = params.get("migrate_to_chat_id")
    if new_id is None:
        return
    LOG.error(
        "Telegram：当前 TELEGRAM_CHAT_ID 已失效（群已升级为超级群）。"
        "请把 .env 中 TELEGRAM_CHAT_ID 改为: %s  然后重启程序。"
        "命令轮询与推送都依赖该 ID。",
        new_id,
    )

# Status strings (Telegram copy)
SCORING_STATUS_ON = "获取积分中"
SCORING_STATUS_OFF = "未获取积分"
MANUAL_HANDLING_STATUS = "人工处理中"
BOT_RESUMED_STATUS = "恢复程序监控"
REPLACE_FAILED_STATUS = "改价失败，需要人工介入"


def scoring_status_text(scoring: bool) -> str:
    return SCORING_STATUS_ON if scoring else SCORING_STATUS_OFF


def scoring_transition_text(was_scoring: bool, now_scoring: bool) -> str:
    return f"{scoring_status_text(was_scoring)} -> {scoring_status_text(now_scoring)}"


# simple_price_policy / apply_decision reason codes → Telegram 中文说明
_PRICING_REASON_ZH: dict[str, str] = {
    "coarse_tick_abandon_due_to_too_few_levels": (
        "粗tick：奖励带内有效档位数≤2，判定风险过大，撤单且不再挂回"
    ),
    "coarse_tick_choose_middle_of_3": "粗tick：共3档，选离中间价距离居中的一档",
    "coarse_tick_choose_third_from_mid_of_4": (
        "粗tick：共4档，选离中间价第二远的一档（非最外档）"
    ),
    "coarse_tick_choose_second_farthest_default": (
        "粗tick：档位数>4，默认选离中间价第二远的一档"
    ),
    "coarse_tick_keep_already_at_target": "粗tick：已在目标价，最小变动不足，保持",
    "unsupported_tick_keep": "tick 非 0.01/0.001（或 1/0.1），不在策略内，保持",
    "fine_tick_keep_in_target_band": "细tick：|价−mid|/δ 已在 0.4～0.6 带内，保持",
    "fine_tick_move_outward_to_half_band": "细tick：过近 mid，外移至 0.5×δ",
    "fine_tick_move_inward_to_half_band": "细tick：过远 mid，内收至 0.5×δ",
    "fine_tick_move_outward_to_half_band_noop_small_delta": (
        "细tick：应外移半带，但与现价相差不足最小 tick，保持"
    ),
    "fine_tick_move_inward_to_half_band_noop_small_delta": (
        "细tick：应内收半带，但与现价相差不足最小 tick，保持"
    ),
    # custom pricing mode (PASSIVE_CUSTOM_ORDER_IDS)
    "custom_missing_settings_keep": "自定义调价：缺少参数，保持",
    "custom_coarse_keep_band_outside_market": (
        "自定义粗tick：激励带与有效价格区间无交集，保持"
    ),
    "custom_coarse_keep_insufficient_candidates": (
        "自定义粗tick：激励带内订单簿有深度的价位数少于 min_candidate_levels，保持"
    ),
    "custom_coarse_replace_exact_offset_from_mid": (
        "自定义粗tick：按激励带内订单簿价位改价至第 N 档（离 mid 最近为第 1 档）"
    ),
    "custom_coarse_keep_rank_outside_band_levels": (
        "自定义粗tick：配置 N 超出激励带内订单簿可选档位数，保持"
    ),
    "custom_coarse_keep_offset_outside_band": (
        "自定义粗tick：目标档被夹到带外或与配置步数不一致，保持"
    ),
    "custom_coarse_keep_target_is_top_of_book": (
        "自定义粗tick：目标价即最优买/卖档且未允许 top-of-book，保持"
    ),
    "custom_coarse_keep_offset_invalid_price": (
        "自定义粗tick：目标价无效（越出 0–1），保持"
    ),
    "custom_coarse_keep_already_at_target": (
        "自定义粗tick：已在目标价附近（最小变动不足），保持"
    ),
    "custom_fine_keep_in_safe_band": (
        "自定义细tick：|价−mid|/δ 已在安全比例带内，保持"
    ),
    "custom_fine_move_toward_target_ratio": (
        "自定义细tick：向目标 band 比例价位调整"
    ),
    "custom_fine_keep_small_delta": (
        "自定义细tick：目标价与现价相差不足最小 tick，保持"
    ),
}


def pricing_adjustment_reason_zh(reason: str) -> str:
    """Turn engine reason codes into Chinese for Telegram; keep tail after `|`."""
    if not (reason or "").strip():
        return ""
    if "|" in reason:
        head, tail = reason.split("|", 1)
        head = head.strip()
        tail = tail.strip()
        zh = _PRICING_REASON_ZH.get(head, head)
        return f"{zh} | {tail}" if tail else zh
    head = reason.strip()
    return _PRICING_REASON_ZH.get(head, head)


@dataclass
class OrderEventFormat:
    """Fields for format_order_event_message."""

    account_label: str
    market_title: str
    outcome: str
    token_id: str
    side: str
    old_price: Optional[float]
    new_price: Optional[float]
    size: Optional[float]
    scoring_status_text: str
    inventory: Optional[float] = None
    reason: str = ""


def stable_fingerprint(*parts: Any) -> str:
    s = "|".join(str(p) for p in parts)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:32]


def polymarket_api_error_zh_hint(error_text: str) -> str:
    """Short Chinese explanation for common CLOB / PolyApi error bodies."""
    e = (error_text or "").lower()
    if "not enough balance" in e or "balance is not enough" in e:
        return (
            "可能原因：Polymarket 交易余额中的 USDC 不足以覆盖本单所需金额/保证金，"
            "或链上 allowance 不足。可尝试：向账户充值、取消其它挂单释放资金、"
            "减小本单份额，或检查钱包授权。"
        )
    if "allowance" in e:
        return (
            "可能原因：链上 USDC 对合约的授权（allowance）不足。"
            "请在 Polymarket 前端完成存款/授权流程。"
        )
    if "post only" in e or "post_only" in e or "post-only" in e:
        return "可能原因：post-only 订单与当前盘口价格冲突，无法挂在会立即成交的价位。"
    if "invalid" in e and "price" in e:
        return "可能原因：挂单价格或最小 tick 不符合该市场规则。"
    if "nonce" in e or "expired" in e:
        return "可能原因：签名/nonce 或请求过期，可稍后重试或检查本地时钟。"
    return "请根据下方接口返回原文排查；若持续出现需人工查看账户与网络状态。"


class TelegramNotifier:
    """
    send_message is non-blocking (daemon thread).
    Failures are logged only; never raises to callers.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        bot_token: str,
        chat_id: str,
        account_label: str,
        cooldown_sec: float,
    ):
        self._enabled = bool(enabled and bot_token and chat_id)
        self._bot_token = bot_token.strip()
        self._chat_id = chat_id.strip()
        self._account_label = (account_label or "Polymarket").strip() or "Polymarket"
        self._cooldown = max(0.0, float(cooldown_sec))
        self._lock = threading.Lock()
        # event_key -> (last_fingerprint, last_sent_monotonic_ts)
        self._last: dict[str, tuple[str, float]] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def account_label(self) -> str:
        return self._account_label

    @property
    def bot_token(self) -> str:
        return self._bot_token

    @property
    def chat_id(self) -> str:
        return self._chat_id

    def send_command_reply(self, text: str) -> None:
        """
        Reply to /status, /orders, etc. Bypasses cooldown and dedupe so each command
        gets a fresh message. Runs synchronously (caller should be a background thread).
        """
        if not self._enabled:
            return
        token = self._bot_token
        chat = self._chat_id
        url = TELEGRAM_API.format(token=token)
        body = json.dumps(
            {"chat_id": chat, "text": text, "disable_web_page_preview": True},
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
            if not data.get("ok"):
                _maybe_log_supergroup_migration(raw)
                LOG.warning("Telegram command reply not ok: %s", raw[:400])
            else:
                LOG.info("Telegram command reply sent ok (%d chars)", len(text))
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = str(e)
            _maybe_log_supergroup_migration(err_body)
            LOG.warning(
                "Telegram command reply HTTPError: %s %s", e.code, err_body[:400]
            )
        except Exception as e:
            LOG.warning("Telegram command reply failed: %s", e)

    def should_notify(self, event_key: str, payload_hash: str) -> bool:
        """True if we may send (not duplicate fingerprint; cooldown ok)."""
        with self._lock:
            return self._should_notify_unlocked(event_key, payload_hash)

    def _should_notify_unlocked(self, event_key: str, payload_hash: str) -> bool:
        rec = self._last.get(event_key)
        now = time.monotonic()
        if rec:
            last_fp, last_ts = rec
            if last_fp == payload_hash:
                LOG.debug("Telegram skip duplicate fingerprint key=%s", event_key)
                return False
            if self._cooldown > 0 and (now - last_ts) < self._cooldown:
                LOG.debug("Telegram skip cooldown key=%s (%.1fs)", event_key, self._cooldown)
                return False
        return True

    def record_last_notification(self, event_key: str, payload_hash: str) -> None:
        with self._lock:
            self._last[event_key] = (payload_hash, time.monotonic())

    def send_message(self, text: str, *, event_key: str, payload_hash: str) -> None:
        if not self._enabled:
            return
        with self._lock:
            if not self._should_notify_unlocked(event_key, payload_hash):
                return

        LOG.info("Telegram enqueue send key=%s fp=%s…", event_key, payload_hash[:12])

        token = self._bot_token
        chat = self._chat_id
        url = TELEGRAM_API.format(token=token)
        body = json.dumps(
            {"chat_id": chat, "text": text, "disable_web_page_preview": True},
            ensure_ascii=False,
        ).encode("utf-8")

        def _worker() -> None:
            try:
                req = urllib.request.Request(
                    url,
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw) if raw else {}
                if not data.get("ok"):
                    LOG.warning(
                        "Telegram API not ok key=%s response=%s",
                        event_key,
                        raw[:500],
                    )
                    return
                with self._lock:
                    self._last[event_key] = (payload_hash, time.monotonic())
                LOG.info("Telegram sent OK key=%s", event_key)
            except urllib.error.HTTPError as e:
                try:
                    err_body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    err_body = str(e)
                _maybe_log_supergroup_migration(err_body)
                LOG.warning("Telegram HTTPError key=%s: %s %s", event_key, e.code, err_body[:300])
            except Exception as e:
                LOG.warning("Telegram send failed key=%s: %s", event_key, e)

        threading.Thread(target=_worker, name="telegram-send", daemon=True).start()

    def format_order_fill_message(
        self,
        *,
        account_label: str,
        market_title: str,
        outcome: str,
        side: str,
        order_price: float,
        filled_size: float,
        remaining_size: float,
        fill_type_zh: str,
        scoring_status_text_s: str,
        fill_price: Optional[float] = None,
        inventory: Optional[float] = None,
        fill_detection_source: Optional[str] = None,
    ) -> str:
        label = (account_label or "").strip() or self._account_label
        lines = [
            f"[{label}]",
            "事件: 订单成交",
            f'盘口: "{market_title}"',
            f"方向: {outcome or '—'}",
            f"订单方向: {str(side).upper()}",
            f"挂单价格: {_fmt_amt(order_price)}",
            f"成交数量: {filled_size:g}",
            f"剩余数量: {remaining_size:g}",
            f"成交类型: {fill_type_zh}",
        ]
        if fill_price is not None:
            lines.append(f"成交价格（约）: {_fmt_amt(fill_price)}")
        lines.append(f"计分状态: {scoring_status_text_s}")
        if inventory is not None:
            lines.append(f"当前持仓: {_fmt_amt(inventory)}")
        if fill_detection_source:
            lines.append(f"成交检测来源: {fill_detection_source}")
        return "\n".join(lines)

    def format_order_event_message(self, ev: OrderEventFormat) -> str:
        label = (ev.account_label or "").strip() or self._account_label
        header = f"[{label}]"
        lines = [
            header,
            f'盘口: "{ev.market_title}"',
            f"方向: {ev.outcome or ev.side}",
        ]
        if ev.outcome:
            lines.append(f"买卖: {ev.side}")
        if ev.token_id:
            lines.append(f"token: {ev.token_id}")
        if ev.old_price is not None and ev.new_price is not None and ev.old_price != ev.new_price:
            lines.append(f"价格: {_fmt_amt(ev.old_price)} -> {_fmt_amt(ev.new_price)}")
        elif ev.new_price is not None:
            lines.append(f"价格: {_fmt_amt(ev.new_price)}")
        elif ev.old_price is not None:
            lines.append(f"价格: {_fmt_amt(ev.old_price)}")
        if ev.size is not None:
            lines.append(f"份额: {ev.size:g}")
        if ev.inventory is not None:
            lines.append(f"库存: {_fmt_amt(ev.inventory)}")
        lines.append(f"状态: {ev.scoring_status_text}")
        if ev.reason:
            lines.append(f"原因: {pricing_adjustment_reason_zh(ev.reason)}")
        return "\n".join(lines)

    def notify_operational_warning_zh(
        self,
        *,
        title_zh: str,
        lines: list[str],
        event_key: str,
    ) -> None:
        """Generic Chinese alert for API / order-operation warnings (balance, retry, etc.)."""
        body = "\n".join([f"[{self._account_label}]", f"⚠️ {title_zh}", ""] + lines)
        fp = stable_fingerprint("op_warn", event_key, body[:3000])
        self.send_message(text=body, event_key=event_key, payload_hash=fp)

    def notify_ws_transport_zh(
        self,
        *,
        title_zh: str,
        lines: list[str],
        event_key: str,
    ) -> None:
        """WebSocket connect/disconnect / REST fallback (monitoring transport only)."""
        body = "\n".join([f"[{self._account_label}]", title_zh, ""] + lines)
        fp = stable_fingerprint(
            "ws_transport", event_key, body[:2000], time.time()
        )
        self.send_message(text=body, event_key=event_key, payload_hash=fp)

    def notify_whitelist_init(
        self,
        *,
        source: str,
        token_ids: list[str],
        open_order_count: Optional[int],
    ) -> None:
        parts = ["启动白名单", source, str(open_order_count), ",".join(sorted(token_ids))]
        fp = stable_fingerprint(*parts)
        lines = [
            f"[{self._account_label}]",
            "事件: 监控白名单已初始化",
            f"来源: {source}",
        ]
        if open_order_count is not None:
            lines.append(f"启动时未成交单数: {open_order_count}")
        lines.append(f"唯一 token 数: {len(token_ids)}")
        for tid in sorted(token_ids)[:40]:
            lines.append(f" · {tid}")
        if len(token_ids) > 40:
            lines.append(f" … 共 {len(token_ids)} 个，已截断")
        text = "\n".join(lines)
        self.send_message(text, event_key="startup:whitelist", payload_hash=fp)

    def notify_account_startup(
        self,
        *,
        deposited_reference_usdc: Optional[float],
        total_account_usdc: float,
        available_balance_usdc: float,
        locked_open_buy_usdc: float,
        pnl_usdc: Optional[float],
        extra_note_zh: str = "",
        clob_collateral_usdc: Optional[float] = None,
        positions_market_value_usdc: Optional[float] = None,
        positions_error_zh: str = "",
    ) -> None:
        """
        ``total_account_usdc`` = portfolio total (CLOB + positions ``currentValue``) when
        breakdown is passed; otherwise legacy CLOB-only total.
        """
        if deposited_reference_usdc is None:
            ref_line = "累计入账（参考）: 未配置"
            pnl_line = "盈亏（相对入账参考）: 暂不计算（需先配置入账参考）"
        else:
            ref_line = f"累计入账（参考）: {_fmt_amt(deposited_reference_usdc)} USDC"
            p = float(pnl_usdc or 0.0)
            sign = "+" if p >= 0 else ""
            pnl_line = f"盈亏（相对入账参考）: {sign}{_fmt_amt(p)} USDC"
        lines = [
            f"[{self._account_label}]",
            "事件: 程序启动 · 账户资金快照",
            ref_line,
            f"当前账户总额（组合≈）: {_fmt_amt(total_account_usdc)} USDC",
        ]
        if clob_collateral_usdc is not None:
            lines.append(f"CLOB 抵押 USDC: {_fmt_amt(float(clob_collateral_usdc))} USDC")
            if positions_market_value_usdc is not None:
                lines.append(
                    f"持仓市值（Data API）: {_fmt_amt(float(positions_market_value_usdc))} USDC"
                )
            elif (positions_error_zh or "").strip():
                lines.append(
                    f"持仓市值: （未计入：{(positions_error_zh or '').strip()[:120]}）"
                )
        lines.extend(
            [
                f"当前可用余额（CLOB 可开新单≈）: {_fmt_amt(available_balance_usdc)} USDC",
                f"未成交买单占用: {_fmt_amt(locked_open_buy_usdc)} USDC",
                pnl_line,
            ]
        )
        if (extra_note_zh or "").strip():
            lines.append((extra_note_zh or "").strip())
        text = "\n".join(lines)
        fp = stable_fingerprint("startup_balance", text)
        self.send_message(text, event_key="startup:account_balance", payload_hash=fp)

    def notify_periodic_account_summary(
        self,
        *,
        slot_key: str,
        time_label: str,
        total_account_usdc: float,
        available_balance_usdc: float,
        deposited_reference_usdc: Optional[float],
        pnl_usdc: Optional[float],
        clob_collateral_usdc: Optional[float] = None,
        positions_market_value_usdc: Optional[float] = None,
        positions_error_zh: str = "",
    ) -> None:
        if deposited_reference_usdc is None:
            ref_line = "入账参考: 未配置"
            pnl_line = "盈亏（相对入账参考）: 暂不计算（需先配置入账参考）"
        else:
            ref_line = f"入账参考: {_fmt_amt(deposited_reference_usdc)} USDC"
            p = float(pnl_usdc or 0.0)
            sign = "+" if p >= 0 else ""
            pnl_line = f"盈亏（相对入账参考）: {sign}{_fmt_amt(p)} USDC"
        lines = [
            f"[{self._account_label}]",
            f"定期摘要（半点/整点 · {time_label}）",
            f"账户总额（组合≈）: {_fmt_amt(total_account_usdc)} USDC",
        ]
        if clob_collateral_usdc is not None:
            lines.append(f"CLOB 抵押: {_fmt_amt(float(clob_collateral_usdc))} USDC")
            if positions_market_value_usdc is not None:
                lines.append(
                    f"持仓市值: {_fmt_amt(float(positions_market_value_usdc))} USDC"
                )
            elif (positions_error_zh or "").strip():
                lines.append(
                    f"持仓市值: （未计入：{(positions_error_zh or '').strip()[:100]}）"
                )
        lines.extend(
            [
                f"可用余额（CLOB≈）: {_fmt_amt(available_balance_usdc)} USDC",
                ref_line,
                pnl_line,
            ]
        )
        text = "\n".join(lines)
        fp = stable_fingerprint("periodic", slot_key, text)
        self.send_message(text, event_key=f"periodic:summary:{slot_key}", payload_hash=fp)

    def notify_order_cancelled_chinese(
        self,
        *,
        order_id_short: str,
        market_title: str,
        outcome: str,
        price: float,
        size: float,
        category_zh: str,
        detail_zh: str,
        raw_reason: str,
    ) -> None:
        lines = [
            f"[{self._account_label}]",
            "事件: 订单已撤销",
            f'盘口: "{market_title}"',
            f"方向: {outcome or '—'}",
            f"价格: {_fmt_amt(price)}",
            f"份额: {size:g}",
            f"撤单类别: {category_zh}",
            f"说明: {detail_zh}",
            f"策略原因码: {raw_reason}",
            f"订单: {order_id_short}",
        ]
        text = "\n".join(lines)
        fp = stable_fingerprint(text)
        oid_key = (order_id_short or "unknown").replace(":", "_")[:24]
        self.send_message(
            text,
            event_key=f"cancel:order:{oid_key}:{raw_reason[:40]}",
            payload_hash=fp,
        )

    def notify_order_band_summary(
        self,
        *,
        time_label: str,
        interval_sec: float,
        lines: list[str],
        time_bucket: int,
    ) -> None:
        """Periodic list of managed orders: distance from mid as fraction of reward δ."""
        n = len(lines)
        header = [
            f"[{self._account_label}]",
            f"挂单相对中间价（占激励半宽 δ）· 每 {interval_sec:g}s",
            f"时间: {time_label}",
            f"共 {n} 条",
        ]
        body = lines if lines else ["（无明细）"]
        text = "\n".join(header + [""] + body)
        fp = stable_fingerprint("band_summary", time_bucket, text)
        self.send_message(
            text,
            event_key=f"periodic:band_summary:{time_bucket}",
            payload_hash=fp,
        )

    def notify_coarse_tick_abandon(
        self,
        *,
        market_title: str,
        outcome: str,
        token_id: str,
        n_candidates: int,
        reason_code: str,
        candidate_prices: Optional[list[float]] = None,
        mid: Optional[float] = None,
        coarse_range_lo_hi: Optional[tuple[float, float]] = None,
        tick_size: Optional[float] = None,
        reward_band_delta: Optional[float] = None,
    ) -> None:
        """Case A: too few coarse-tick book levels — cancel without replace."""
        prices = candidate_prices or []
        prices_fmt = ", ".join(f"{p:.4f}" for p in prices) if prices else ""
        prices_line = (
            f"符合条件价位: {prices_fmt}" if prices_fmt else "符合条件价位: （无）"
        )
        range_line = ""
        if coarse_range_lo_hi is not None:
            lo, hi = coarse_range_lo_hi
            range_line = f"\n粗tick统计区间[lo,hi]: [{lo:.4f}, {hi:.4f}]"
        mid_line = f"\nmid: {mid:.4f}" if mid is not None else ""
        tick_line = f"\ntick_size: {tick_size}" if tick_size is not None else ""
        delta_line = (
            f"\n奖励半宽δ(用于统计区间): {reward_band_delta:.4f}"
            if reward_band_delta is not None
            else ""
        )
        body = (
            f"该盘口目前风险过大（条件内只有 {n_candidates} 个符合条件的挂单），放弃持仓\n"
            f"账户: {self._account_label}\n"
            f"市场: {market_title}\n"
            f"方向: {outcome or '—'}\n"
            f"token_id: {token_id}\n"
            f"{prices_line}\n"
            f"符合条件档位数: {n_candidates}"
            f"{mid_line}{range_line}{tick_line}{delta_line}\n"
            f"原因: {pricing_adjustment_reason_zh(reason_code)}"
        )
        fp = stable_fingerprint(
            "coarse_abandon",
            token_id,
            n_candidates,
            reason_code,
            prices_fmt,
            mid,
            coarse_range_lo_hi,
            tick_size,
            reward_band_delta,
        )
        self.send_message(
            body,
            event_key=f"coarse_abandon:{token_id}:{n_candidates}",
            payload_hash=fp,
        )

    def notify_passive_fill_risk_alert(
        self,
        *,
        market_title: str,
        outcome: str,
        token_id: str,
        side: str,
        fill_rate: float,
        short_trades: int,
        long_trades: int,
        fill_risk_score: float,
        direction_en: str,
        reasons: list[str],
    ) -> None:
        """Monitoring-only: high fill / tape activity (does not change orders)."""
        reason_s = ",".join(reasons) if reasons else "—"
        lines = [
            f"[{self._account_label}]",
            "[Fill Risk Alert]",
            f'Market: "{market_title}"',
            f"Token: {token_id}",
            f"Side: {outcome or side}",
            f"Order side: {str(side).upper()}",
            f"Fill rate: {fill_rate:.4f}",
            f"Short trades: {short_trades}",
            f"Long trades: {long_trades}",
            f"Fill risk score: {fill_risk_score:.4f}",
            f"Direction: {direction_en}",
            f"Triggers: {reason_s}",
            'Message: "成交活跃度上升，存在被吃风险"',
        ]
        text = "\n".join(lines)
        fp = stable_fingerprint("passive_fill_risk", text)
        tid = (token_id or "na")[:40].replace(":", "_")
        # Include fp prefix so TELEGRAM_NOTIFY_COOLDOWN_SEC does not block a new metric state
        # while still deduping identical payloads (same event_key + same fp).
        self.send_message(
            text,
            event_key=f"monitor:fill_risk:{tid}:{fp[:20]}",
            payload_hash=fp,
        )

    def notify_passive_depth_risk_alert(
        self,
        *,
        market_title: str,
        outcome: str,
        token_id: str,
        order_id_short: str,
        band_lo: float,
        band_hi: float,
        total_depth: float,
        closer_depth: float,
        depth_ratio: float,
    ) -> None:
        """Monitoring-only: crowded side of band vs our quote (does not change orders)."""
        pct = depth_ratio * 100.0
        lines = [
            f"[{self._account_label}]",
            "[Depth Risk Alert]",
            f'Market: "{market_title}"',
            f"Token: {token_id}",
            f"Order: {order_id_short}",
            f"Outcome: {outcome or '—'}",
            f"Band: [{band_lo:.4f}, {band_hi:.4f}]",
            f"Total depth: {total_depth:.4f}",
            f"Closer-to-mid depth: {closer_depth:.4f}",
            f"Ratio: {pct:.1f}%",
            'Message: "带内更靠近 mid 的挂单占比过高，存在竞争风险"',
        ]
        text = "\n".join(lines)
        fp = stable_fingerprint("passive_depth_risk", text)
        oid_key = (order_id_short or "na")[:40].replace(":", "_")
        self.send_message(
            text,
            event_key=f"monitor:depth_risk:{oid_key}:{fp[:20]}",
            payload_hash=fp,
        )


def build_telegram_notifier_from_env() -> TelegramNotifier:
    load = __import__("dotenv", fromlist=["load_dotenv"])
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    load.load_dotenv(root / ".env", override=False)

    def b(name: str, default: bool) -> bool:
        v = os.environ.get(name)
        if v is None or v == "":
            return default
        return v.strip().lower() in ("1", "true", "yes", "on")

    def f(name: str, default: float) -> float:
        v = os.environ.get(name)
        if v is None or v == "":
            return default
        return float(v)

    enabled = b("TELEGRAM_ENABLED", False)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    label = os.environ.get("TELEGRAM_ACCOUNT_LABEL", "Polymarket").strip()
    cooldown = f("TELEGRAM_NOTIFY_COOLDOWN_SEC", 30.0)

    return TelegramNotifier(
        enabled=enabled,
        bot_token=token,
        chat_id=chat,
        account_label=label,
        cooldown_sec=cooldown,
    )
