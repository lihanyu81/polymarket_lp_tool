"""
Long-poll Telegram updates in a daemon thread; handle /status, /orders, /pnl.

Isolated from the trading main loop; failures here do not affect order logic.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from passive_liquidity.custom_pricing_rules_store import CustomPricingRulesStore
from passive_liquidity.order_manager import OrderManager
from passive_liquidity.orderbook_fetcher import OrderBookFetcher
from passive_liquidity.reward_monitor import RewardMonitor
from passive_liquidity.simple_price_policy import CustomPricingSettings
from passive_liquidity.telegram_live_queries import (
    get_live_account_status,
    get_live_order_summary,
    get_live_pnl,
)
from passive_liquidity.market_display import MarketDisplayResolver
from passive_liquidity.telegram_notifier import TelegramNotifier
from passive_liquidity.telegram_rule_setup import dispatch_command, handle_fsm_text

LOG = logging.getLogger(__name__)


def _commands_enabled_from_env() -> bool:
    v = os.environ.get("TELEGRAM_COMMANDS_ENABLED", "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    return True


def _chat_id_matches(msg_chat_id: Any, configured: str) -> bool:
    if msg_chat_id is None or not configured:
        return False
    return str(msg_chat_id).strip() == str(configured).strip()


def _get_updates(bot_token: str, offset: int, timeout_sec: int) -> list[dict]:
    params: dict[str, Any] = {"timeout": int(timeout_sec)}
    if offset > 0:
        params["offset"] = int(offset)
    q = urllib.parse.urlencode(params)
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates?{q}"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout_sec + 5) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw) if raw else {}
    if not data.get("ok"):
        LOG.warning("getUpdates not ok: %s", raw[:500])
        return []
    return list(data.get("result") or [])


def _poll_loop(
    *,
    stop: threading.Event,
    notifier: TelegramNotifier,
    client: Any,
    order_manager: OrderManager,
    funder: str,
    poll_timeout_sec: int,
    rules_store: CustomPricingRulesStore,
    book_fetcher: OrderBookFetcher,
    reward_monitor: RewardMonitor,
    default_custom_settings: CustomPricingSettings,
    market_display: Optional[MarketDisplayResolver],
) -> None:
    token = notifier.bot_token
    expect_chat = notifier.chat_id
    offset = 0
    while not stop.is_set():
        try:
            updates = _get_updates(token, offset, poll_timeout_sec)
        except urllib.error.HTTPError as e:
            LOG.warning("Telegram getUpdates HTTPError: %s", e)
            time.sleep(3.0)
            continue
        except Exception as e:
            LOG.warning("Telegram getUpdates failed: %s", e)
            time.sleep(3.0)
            continue

        max_uid = 0
        for u in updates:
            try:
                max_uid = max(max_uid, int(u.get("update_id") or 0))
            except (TypeError, ValueError):
                pass

        for u in updates:
            msg = u.get("message") or u.get("edited_message")
            if not isinstance(msg, dict):
                continue
            chat = msg.get("chat") or {}
            if not _chat_id_matches(chat.get("id"), expect_chat):
                continue

            text = msg.get("text")
            if not isinstance(text, str):
                continue

            chat_id = str(chat.get("id"))
            stripped = text.strip()

            def _label(msg_body: str) -> str:
                return f"[{notifier.account_label}]\n{msg_body}"

            rule_slash = (
                "/set_rule",
                "/get_rule",
                "/clear_rule",
                "/cancel_rule_setup",
            )

            if stripped.startswith("/"):
                first_tok = stripped.split(None, 1)[0]
                cmd_base = first_tok.split("@", 1)[0].lower()
                arg_rest = (
                    stripped.split(None, 1)[1].strip()
                    if len(stripped.split(None, 1)) > 1
                    else ""
                )

                if cmd_base in rule_slash:
                    LOG.info("Telegram rule command: %s", cmd_base)
                    try:
                        reply = dispatch_command(
                            chat_id,
                            cmd_base,
                            arg_rest,
                            client=client,
                            order_manager=order_manager,
                            book_fetcher=book_fetcher,
                            store=rules_store,
                            default_settings=default_custom_settings,
                        )
                    except Exception as e:
                        LOG.exception("Telegram rule command error")
                        reply = f"⚠️ 命令处理异常: {e}"
                    if reply:
                        notifier.send_command_reply(_label(reply))
                    continue

                # 群组隐私模式：纯数字/yes 等普通消息可能收不到；用 /input 走命令入口
                if cmd_base in ("/input", "/answer"):
                    if not arg_rest.strip():
                        notifier.send_command_reply(
                            _label(
                                "用法: /input <本步答案>\n"
                                "与直接发消息相同，例如：`/input 2`、`/input yes`、`/input 0.4`、`/input confirm`。\n"
                                "在群里配置时若单独发数字 Bot 无回复，请用本条命令。"
                            )
                        )
                        continue
                    fsm_reply = handle_fsm_text(
                        chat_id,
                        arg_rest,
                        store=rules_store,
                        default_settings=default_custom_settings,
                    )
                    if fsm_reply is not None:
                        notifier.send_command_reply(_label(fsm_reply))
                    else:
                        notifier.send_command_reply(
                            _label(
                                "当前没有进行中的规则配置，请先 /set_rule <order_id>。"
                            )
                        )
                    continue

                cmd = cmd_base
                LOG.info("Telegram command received: %s", cmd)

                try:
                    if cmd == "/status":
                        ok, body = get_live_account_status(
                            client=client,
                            order_manager=order_manager,
                            funder=funder,
                            account_label=notifier.account_label,
                        )
                    elif cmd == "/orders":
                        ok, body = get_live_order_summary(
                            client=client,
                            order_manager=order_manager,
                            market_display=market_display,
                            book_fetcher=book_fetcher,
                            reward_monitor=reward_monitor,
                        )
                    elif cmd == "/cancel":
                        arg = arg_rest.strip()
                        if not arg:
                            ok, body = False, "用法: /cancel <order_id|all>"
                        elif arg.lower() == "all":
                            try:
                                orders = order_manager.fetch_all_open_orders(client)
                            except Exception as e:
                                ok, body = False, f"拉取未成交单失败: {e}"
                            else:
                                total = 0
                                failed = 0
                                for o in orders:
                                    oid = str(o.get("id") or o.get("orderID") or "").strip()
                                    if not oid:
                                        continue
                                    total += 1
                                    try:
                                        client.cancel(oid)
                                    except Exception:
                                        failed += 1
                                if total == 0:
                                    ok, body = True, "当前无挂单，无需取消。"
                                elif failed == 0:
                                    ok, body = True, f"已提交取消全部挂单，共 {total} 笔。"
                                else:
                                    ok, body = False, f"取消完成：成功 {total - failed}/{total}，失败 {failed}。"
                        else:
                            oid = arg
                            try:
                                client.cancel(oid)
                                ok, body = True, f"已提交取消订单: {oid[:48]}…"
                            except Exception as e:
                                ok, body = False, f"取消失败: {e}"
                    elif cmd == "/pnl":
                        ok, body = get_live_pnl(
                            client=client,
                            order_manager=order_manager,
                            funder=funder,
                            account_label=notifier.account_label,
                        )
                    elif cmd in ("/start", "/help"):
                        body = (
                            "可用命令（实时查询，非半点摘要）：\n"
                            "/status — 账户与挂单概览\n"
                            "/orders — 未成交单（盘口名、order_id、side、price、size）\n"
                            "/cancel <order_id|all> — 取消指定订单或全部挂单\n"
                            "/pnl — 盈亏\n"
                            "\n自定义调价（规则按 token_id + 买卖方向 保存）：\n"
                            "/set_rule <order_id> — 交互式配置\n"
                            "/input <答案> — 群内提交某一步（等同发普通消息）\n"
                            "/get_rule <order_id> — 查看已保存规则\n"
                            "/clear_rule <order_id> — 删除规则恢复默认\n"
                            "/cancel_rule_setup — 取消进行中的配置\n"
                        )
                        ok = True
                    else:
                        continue

                    if not ok:
                        body = f"⚠️ {body}"
                    notifier.send_command_reply(_label(body))
                except Exception as e:
                    LOG.exception("Telegram command handler error: %s", e)
                    notifier.send_command_reply(
                        _label(f"⚠️ 命令处理异常: {e}")
                    )
                continue

            fsm_reply = handle_fsm_text(
                chat_id,
                text,
                store=rules_store,
                default_settings=default_custom_settings,
            )
            if fsm_reply is not None:
                notifier.send_command_reply(_label(fsm_reply))

        if max_uid > 0:
            offset = max_uid + 1


def start_telegram_command_poller(
    *,
    notifier: TelegramNotifier,
    client: Any,
    order_manager: OrderManager,
    funder: str,
    stop: threading.Event,
    rules_store: CustomPricingRulesStore,
    book_fetcher: OrderBookFetcher,
    reward_monitor: RewardMonitor,
    default_custom_settings: CustomPricingSettings,
    market_display: Optional[MarketDisplayResolver] = None,
) -> Optional[threading.Thread]:
    if not notifier.enabled:
        LOG.info("Telegram command poller skipped (notifications disabled)")
        return None
    if not _commands_enabled_from_env():
        LOG.info("Telegram command poller skipped (TELEGRAM_COMMANDS_ENABLED=off)")
        return None

    def _timeout() -> int:
        try:
            v = int(os.environ.get("TELEGRAM_COMMAND_POLL_TIMEOUT", "25"))
        except ValueError:
            v = 25
        return max(1, min(50, v))

    poll_timeout = _timeout()

    def _run() -> None:
        LOG.info(
            "Telegram command poller started (timeout=%ds, chat_id=%s)",
            poll_timeout,
            notifier.chat_id[:12] + "…" if len(notifier.chat_id) > 12 else notifier.chat_id,
        )
        _poll_loop(
            stop=stop,
            notifier=notifier,
            client=client,
            order_manager=order_manager,
            funder=funder,
            poll_timeout_sec=poll_timeout,
            rules_store=rules_store,
            book_fetcher=book_fetcher,
            reward_monitor=reward_monitor,
            default_custom_settings=default_custom_settings,
            market_display=market_display,
        )
        LOG.info("Telegram command poller stopped")

    t = threading.Thread(
        target=_run,
        name="telegram-commands",
        daemon=True,
    )
    t.start()
    return t
