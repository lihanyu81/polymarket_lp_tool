"""
Telegram interactive /set_rule flow (finite-state machine, one session per chat).
"""

from __future__ import annotations

import enum
import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Literal, Optional

from passive_liquidity.custom_pricing_rules_store import (
    CustomPricingRulesStore,
    StoredCustomRule,
    stable_rule_key,
)
from passive_liquidity.order_manager import (
    _oid,
    _price,
    _side,
    _token_id,
)
from passive_liquidity.orderbook_fetcher import OrderBookFetcher
from passive_liquidity.simple_price_policy import (
    CustomPricingSettings,
    classify_custom_tick_regime,
)

LOG = logging.getLogger(__name__)

# 群组默认隐私模式：非命令、非 @、非回复 Bot 的纯文字不会进入 getUpdates。
_GROUP_INPUT_HINT = (
    "\n\n———\n"
    "若 Bot 对你发的数字/文字**没有反应**（常见于**群组**）：Telegram 隐私模式下 Bot 收不到普通消息。\n"
    "请任选：① 与 Bot **私聊**继续；② 在群内 **回复** Bot 上一条消息再输入；③ **@提及 Bot** 后输入；"
    "④ 用命令提交本步答案，例如：`/input 2` 或 `/input yes` 或 `/input confirm`。"
)

_fsm_lock = threading.RLock()
# chat_id str -> session
_sessions: dict[str, "RuleSetupSession"] = {}


class _State(enum.Enum):
    IDLE = 0
    COARSE_OFFSET = 1
    COARSE_TOP = 2
    COARSE_MIN = 3
    COARSE_CONFIRM = 4
    FINE_SAFE_MIN = 5
    FINE_SAFE_MAX = 6
    FINE_TARGET = 7
    FINE_CONFIRM = 8


@dataclass
class _OrderSnap:
    order_id: str
    token_id: str
    side: str
    tick_size: float
    market_title: str
    outcome: str
    price: float


@dataclass
class RuleSetupSession:
    state: _State = _State.IDLE
    snap: Optional[_OrderSnap] = None
    tick_type: Literal["coarse", "fine"] = "fine"
    draft_offset: int = 0
    draft_allow_top: bool = True
    draft_min_cand: int = 1
    draft_safe_min: float = 0.4
    draft_safe_max: float = 0.6
    draft_target_ratio: float = 0.5


def _cancel_session(chat_id: str) -> None:
    with _fsm_lock:
        _sessions.pop(str(chat_id), None)


def _get_session(chat_id: str) -> Optional[RuleSetupSession]:
    with _fsm_lock:
        return _sessions.get(str(chat_id))


def _set_session(chat_id: str, sess: RuleSetupSession) -> None:
    with _fsm_lock:
        _sessions[str(chat_id)] = sess


def cancel_rule_setup_chat(chat_id: str) -> bool:
    """Return True if a session was active."""
    with _fsm_lock:
        if str(chat_id) not in _sessions:
            return False
        _sessions.pop(str(chat_id), None)
        return True


def _normalize_step_text(text: str) -> str:
    """Strip; map fullwidth digits and comma/dot for numeric steps."""
    s = str(text).strip()
    s = s.translate(
        str.maketrans(
            "０１２３４５６７８９，．",
            "0123456789,.",
        )
    )
    return s


def _order_meta_title_outcome(order: dict) -> tuple[str, str]:
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
        title = (mid[:48] + "…") if len(mid) > 48 else mid if mid else "(未知盘口)"
    outcome = str(order.get("outcome") or order.get("outcome_name") or "").strip()
    return title, outcome


def _find_open_order(
    orders: list[dict], order_id: str
) -> Optional[dict]:
    want = str(order_id).strip()
    if not want:
        return None
    for o in orders:
        if not isinstance(o, dict):
            continue
        oid = str(_oid(o) or "").strip()
        if oid == want:
            return o
    return None


def _parse_yes_no(text: str) -> Optional[bool]:
    t = text.strip().lower()
    if t in ("yes", "y", "true", "1", "是"):
        return True
    if t in ("no", "n", "false", "0", "否"):
        return False
    return None


def _fmt_snap(s: _OrderSnap) -> str:
    return (
        f"订单 id: {s.order_id[:40]}{'…' if len(s.order_id) > 40 else ''}\n"
        f"token_id: {s.token_id}\n"
        f"方向: {s.side}\n"
        f"现价: {s.price}\n"
        f"tick_size: {s.tick_size}\n"
        f"市场: {s.market_title}\n"
        f"结果: {s.outcome or '—'}\n"
        f"保存键: {stable_rule_key(s.token_id, s.side)}"
    )


def _summary_coarse(sess: RuleSetupSession) -> str:
    assert sess.snap
    top = "是" if sess.draft_allow_top else "否"
    return (
        f"{_fmt_snap(sess.snap)}\n"
        f"规则类型: 粗 tick（与主程序一致：tick≈0.01 或 ≈1.0）\n"
        f"粗 tick 档位 N（按奖励区间离散档位，从离 mid 最近开始计数）: N={sess.draft_offset}\n"
        f"允许最优档: {top}\n"
        f"min_candidate_levels: {sess.draft_min_cand}\n"
        f"\n回复 confirm 保存，或 cancel 放弃。"
    )


def _summary_fine(sess: RuleSetupSession) -> str:
    assert sess.snap
    return (
        f"{_fmt_snap(sess.snap)}\n"
        f"规则类型: 细 tick（与主程序一致：tick≈0.1 或 ≈0.001）\n"
        f"safe_band_min: {sess.draft_safe_min}\n"
        f"safe_band_max: {sess.draft_safe_max}\n"
        f"target_band_ratio: {sess.draft_target_ratio}\n"
        f"\n回复 confirm 保存，或 cancel 放弃。"
    )


def cmd_set_rule(
    chat_id: str,
    order_id_arg: str,
    *,
    client: Any,
    order_manager: Any,
    book_fetcher: OrderBookFetcher,
    default_settings: CustomPricingSettings,
) -> str:
    oid = order_id_arg.strip()
    if not oid:
        return "用法: /set_rule <order_id>"

    try:
        orders = order_manager.fetch_all_open_orders(client)
    except Exception as e:
        LOG.exception("set_rule fetch orders")
        return f"拉取未成交单失败: {e}"

    o = _find_open_order(orders, oid)
    if o is None:
        return f"未找到未成交订单 id: {oid[:48]}…（请确认 id 完整且该单仍挂单）"

    tid = str(_token_id(o) or "").strip()
    side = str(_side(o) or "").strip().upper()
    if not tid or side not in ("BUY", "SELL"):
        return "订单缺少 token_id 或 side，无法配置。"

    try:
        book = book_fetcher.get_orderbook(tid)
        tick = float(book.tick_size or 0.01)
    except Exception as e:
        LOG.warning("set_rule orderbook %s: %s", tid[:24], e)
        tick = 0.01

    title, outcome = _order_meta_title_outcome(o)
    try:
        price = float(_price(o))
    except (TypeError, ValueError):
        price = 0.0

    tt: Literal["coarse", "fine"] = classify_custom_tick_regime(tick)

    sess = RuleSetupSession()
    sess.snap = _OrderSnap(
        order_id=str(_oid(o)),
        token_id=tid,
        side=side,
        tick_size=tick,
        market_title=title,
        outcome=outcome,
        price=price,
    )
    sess.tick_type = tt
    sess.draft_offset = max(1, int(default_settings.coarse_tick_offset_from_mid))
    sess.draft_allow_top = bool(default_settings.coarse_allow_top_of_book)
    sess.draft_min_cand = max(1, int(default_settings.coarse_min_candidate_levels))
    sess.draft_safe_min = float(default_settings.fine_safe_band_min)
    sess.draft_safe_max = float(default_settings.fine_safe_band_max)
    sess.draft_target_ratio = float(default_settings.fine_target_band_ratio)

    if tt == "coarse":
        sess.state = _State.COARSE_OFFSET
        _set_session(chat_id, sess)
        return (
            f"开始配置自定义调价（粗 tick）。\n{_fmt_snap(sess.snap)}\n\n"
            "第 1/4 步：请输入正整数 N（≥1）。"
            "N 表示奖励区间离散档位序号（从离 mid 最近开始）。"
            "例 BUY 且可得档位为 [0.28,0.27,0.26]：N=1→0.28，N=2→0.27，N=3→0.26。"
            f"{_GROUP_INPUT_HINT}"
        )

    sess.state = _State.FINE_SAFE_MIN
    _set_session(chat_id, sess)
    return (
        f"开始配置自定义调价（细 tick）。\n{_fmt_snap(sess.snap)}\n\n"
        "第 1/4 步：请输入 safe_band_min（0～1 的数字，例如 0.4）"
        f"{_GROUP_INPUT_HINT}"
    )


def cmd_get_rule(
    order_id_arg: str,
    *,
    client: Any,
    order_manager: Any,
    store: CustomPricingRulesStore,
) -> str:
    oid = order_id_arg.strip()
    if not oid:
        return "用法: /get_rule <order_id>"

    try:
        orders = order_manager.fetch_all_open_orders(client)
    except Exception as e:
        return f"拉取未成交单失败: {e}"

    o = _find_open_order(orders, oid)
    if o is None:
        return f"未找到未成交订单 id: {oid[:48]}…"

    tid = str(_token_id(o) or "").strip()
    side = str(_side(o) or "").strip().upper()
    key = stable_rule_key(tid, side)
    rule = store.get_rule(tid, side)
    if rule is None:
        return f"键 {key} 暂无保存的自定义规则（使用默认调价）。"
    top = "是" if rule.coarse_allow_top_of_book else "否"
    return (
        f"键: {key}\n"
        f"保存的 tick 分支: {rule.tick_regime}\n"
        f"粗: N(配置)={rule.coarse_tick_offset_from_mid} "
        f"（奖励区间第N档） allow_top={top} min_band_ticks={rule.coarse_min_candidate_levels}\n"
        f"细: safe=[{rule.fine_safe_band_min}, {rule.fine_safe_band_max}] "
        f"target_ratio={rule.fine_target_band_ratio}"
    )


def cmd_clear_rule(
    order_id_arg: str,
    *,
    client: Any,
    order_manager: Any,
    store: CustomPricingRulesStore,
) -> str:
    oid = order_id_arg.strip()
    if not oid:
        return "用法: /clear_rule <order_id>"

    try:
        orders = order_manager.fetch_all_open_orders(client)
    except Exception as e:
        return f"拉取未成交单失败: {e}"

    o = _find_open_order(orders, oid)
    if o is None:
        return f"未找到未成交订单 id: {oid[:48]}…"

    tid = str(_token_id(o) or "").strip()
    side = str(_side(o) or "").strip().upper()
    key = stable_rule_key(tid, side)
    if store.clear_rule(tid, side):
        return f"已删除自定义规则: {key}（该 token+方向恢复默认调价）。"
    return f"键 {key} 本来就没有自定义规则。"


def _confirm_save(
    chat_id: str,
    sess: RuleSetupSession,
    store: CustomPricingRulesStore,
    defaults: CustomPricingSettings,
) -> str:
    assert sess.snap
    if sess.tick_type == "coarse":
        rule = StoredCustomRule(
            tick_regime="coarse",
            coarse_tick_offset_from_mid=sess.draft_offset,
            coarse_allow_top_of_book=sess.draft_allow_top,
            coarse_min_candidate_levels=sess.draft_min_cand,
            fine_safe_band_min=defaults.fine_safe_band_min,
            fine_safe_band_max=defaults.fine_safe_band_max,
            fine_target_band_ratio=defaults.fine_target_band_ratio,
        )
    else:
        rule = StoredCustomRule(
            tick_regime="fine",
            coarse_tick_offset_from_mid=defaults.coarse_tick_offset_from_mid,
            coarse_allow_top_of_book=defaults.coarse_allow_top_of_book,
            coarse_min_candidate_levels=defaults.coarse_min_candidate_levels,
            fine_safe_band_min=sess.draft_safe_min,
            fine_safe_band_max=sess.draft_safe_max,
            fine_target_band_ratio=sess.draft_target_ratio,
        )
    store.set_rule(sess.snap.token_id, sess.snap.side, rule)
    key = stable_rule_key(sess.snap.token_id, sess.snap.side)
    _cancel_session(chat_id)
    return f"已保存自定义规则（键 {key}）。新挂单同一 token+方向会自动套用。"


def handle_fsm_text(
    chat_id: str,
    text: str,
    *,
    store: CustomPricingRulesStore,
    default_settings: CustomPricingSettings,
) -> Optional[str]:
    """
    If this chat has an active setup session, consume ``text`` and return a reply.
    Return None if no session.
    """
    sess = _get_session(chat_id)
    if sess is None or sess.state == _State.IDLE:
        return None

    raw = _normalize_step_text(text)
    low = raw.lower()

    if low == "cancel":
        _cancel_session(chat_id)
        return "已取消规则配置（未保存）。"

    # confirm step
    if sess.state == _State.COARSE_CONFIRM:
        if low == "confirm":
            return _confirm_save(chat_id, sess, store, default_settings)
        return "请输入 confirm 保存，或 cancel 取消。"

    if sess.state == _State.FINE_CONFIRM:
        if low == "confirm":
            return _confirm_save(chat_id, sess, store, default_settings)
        return "请输入 confirm 保存，或 cancel 取消。"

    if sess.state == _State.COARSE_OFFSET:
        if not re.fullmatch(r"[0-9]+", raw):
            return "请输入正整数 N（≥1）。例：若 BUY 可得档位是[0.28,0.27,0.26]，要 0.27 就填 2。"
        v = int(raw)
        if v < 1:
            return "N 须 >= 1。"
        sess.draft_offset = v
        sess.state = _State.COARSE_TOP
        _set_session(chat_id, sess)
        return "第 2/4 步：是否允许挂在最优买/卖价？回复 yes 或 no。"

    if sess.state == _State.COARSE_TOP:
        yn = _parse_yes_no(raw)
        if yn is None:
            return "请回复 yes 或 no。"
        sess.draft_allow_top = yn
        sess.state = _State.COARSE_MIN
        _set_session(chat_id, sess)
        return (
            "第 3/4 步：请输入 min_candidate_levels（正整数）。"
            "含义：激励带与 (0,1) 交集内至少要有这么多个 tick 价位，"
            "才允许按上面固定 N 档调价；不足则本轮回合保持。"
        )

    if sess.state == _State.COARSE_MIN:
        if not re.fullmatch(r"[0-9]+", raw):
            return "请输入正整数。"
        v = int(raw)
        if v < 1:
            return "min_candidate_levels 须 >= 1。"
        sess.draft_min_cand = v
        sess.state = _State.COARSE_CONFIRM
        _set_session(chat_id, sess)
        return "第 4/4 步：请确认：\n" + _summary_coarse(sess)

    if sess.state == _State.FINE_SAFE_MIN:
        try:
            v = float(raw.replace(",", "."))
        except ValueError:
            return "请输入数字（例如 0.4）。"
        if v < 0 or v > 1:
            return "safe_band_min 须在 [0, 1] 内。"
        sess.draft_safe_min = v
        sess.state = _State.FINE_SAFE_MAX
        _set_session(chat_id, sess)
        return "第 2/4 步：请输入 safe_band_max（0～1，且须大于 min）。"

    if sess.state == _State.FINE_SAFE_MAX:
        try:
            v = float(raw.replace(",", "."))
        except ValueError:
            return "请输入数字（例如 0.6）。"
        if v < 0 or v > 1:
            return "safe_band_max 须在 [0, 1] 内。"
        if not (sess.draft_safe_min < v):
            return "须满足 safe_band_min < safe_band_max。"
        if not (v <= 1):
            return "safe_band_max 须 <= 1。"
        sess.draft_safe_max = v
        sess.state = _State.FINE_TARGET
        _set_session(chat_id, sess)
        return "第 3/4 步：请输入 target_band_ratio（0～1，例如 0.5）。"

    if sess.state == _State.FINE_TARGET:
        try:
            v = float(raw.replace(",", "."))
        except ValueError:
            return "请输入数字（例如 0.5）。"
        if v < 0 or v > 1:
            return "target_band_ratio 须在 [0, 1] 内。"
        sess.draft_target_ratio = v
        sess.state = _State.FINE_CONFIRM
        _set_session(chat_id, sess)
        return "第 4/4 步：请确认：\n" + _summary_fine(sess)

    return "当前步骤无法识别该输入，请按提示回复，或发送 cancel 取消。"


def dispatch_command(
    chat_id: str,
    command: str,
    arg_line: str,
    *,
    client: Any,
    order_manager: Any,
    book_fetcher: OrderBookFetcher,
    store: CustomPricingRulesStore,
    default_settings: CustomPricingSettings,
) -> Optional[str]:
    """Handle rule-related slash commands; return reply or None if not matched."""
    cmd = command.lower().strip()
    if cmd == "/set_rule":
        return cmd_set_rule(
            chat_id,
            arg_line,
            client=client,
            order_manager=order_manager,
            book_fetcher=book_fetcher,
            default_settings=default_settings,
        )
    if cmd == "/get_rule":
        return cmd_get_rule(
            arg_line,
            client=client,
            order_manager=order_manager,
            store=store,
        )
    if cmd == "/clear_rule":
        return cmd_clear_rule(
            arg_line,
            client=client,
            order_manager=order_manager,
            store=store,
        )
    if cmd == "/cancel_rule_setup":
        if cancel_rule_setup_chat(chat_id):
            return "已取消当前规则配置会话。"
        return "当前没有进行中的规则配置。"
    return None
