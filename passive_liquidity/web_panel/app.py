from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from py_clob_client_v2.clob_types import OrderPayload
from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from passive_liquidity.custom_pricing_rules_store import (
    CustomRuleRegime,
    StoredCustomRule,
    stable_rule_key,
)
from passive_liquidity.orderbook_fetcher import (
    pricing_tick_for_order_like_main_loop,
    resolve_effective_tick_size,
)
from passive_liquidity.simple_price_policy import classify_custom_tick_regime
from passive_liquidity.telegram_live_queries import (
    get_live_account_status,
    get_live_pnl,
)
from passive_liquidity.web_panel.context import WebPanelContext
from passive_liquidity.web_panel.orders_data import orders_as_rows

LOG = logging.getLogger(__name__)

_ctx: Optional[WebPanelContext] = None
_page_cache: dict[str, tuple[float, Any]] = {}
_page_cache_lock = threading.Lock()


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _web_panel_token() -> str:
    return (os.environ.get("WEB_PANEL_TOKEN") or "").strip()


def _secret_key() -> str:
    sk = (os.environ.get("WEB_PANEL_SECRET_KEY") or "").strip()
    if sk:
        return sk
    tok = _web_panel_token()
    if not tok:
        return secrets.token_hex(32)
    return hmac.new(b"web-panel", tok.encode("utf-8"), hashlib.sha256).hexdigest()


def get_ctx() -> WebPanelContext:
    global _ctx
    if _ctx is None:
        _ctx = WebPanelContext()
    return _ctx


def _rules_form_redirect_url() -> str:
    if (request.form.get("redirect") or "").strip() == "orders":
        return url_for("orders_page")
    return url_for("rules_page")


def _cache_get_or_compute(key: str, ttl_sec: float, producer: Any) -> Any:
    now = time.monotonic()
    with _page_cache_lock:
        cached = _page_cache.get(key)
        if cached is not None:
            ts, value = cached
            if now - ts <= max(0.0, float(ttl_sec)):
                return value
    value = producer()
    with _page_cache_lock:
        _page_cache[key] = (time.monotonic(), value)
    return value


def _cache_invalidate(prefix: str = "") -> None:
    with _page_cache_lock:
        if not prefix:
            _page_cache.clear()
            return
        for k in list(_page_cache.keys()):
            if k.startswith(prefix):
                _page_cache.pop(k, None)


def _orders_summary_text_from_rows(rows: list[dict[str, Any]]) -> str:
    lines = [
        "实时挂单",
        f"未成交单总数: {len(rows)}",
    ]
    if not rows:
        lines.append("（当前无挂单）")
        return "\n".join(lines)
    for idx, r in enumerate(rows, start=1):
        oid = str(r.get("order_id") or "")
        lines.append(f"{idx}) 盘口: {str(r.get('market_title') or '（未知盘口）')}")
        lines.append(f"   order_id={oid}")
        lines.append(
            "   side={side}  price={price}  size={size}".format(
                side=str(r.get("side") or "?"),
                price=r.get("price"),
                size=r.get("size"),
            )
        )
    return "\n".join(lines)


def _custom_rule_defaults_payload(ctx: WebPanelContext) -> dict[str, Any]:
    c = ctx.config
    return {
        "coarse_tick_offset_from_mid": int(c.custom_coarse_tick_offset_from_mid),
        "coarse_allow_top_of_book": bool(c.custom_coarse_allow_top_of_book),
        "coarse_min_candidate_levels": int(c.custom_coarse_min_candidate_levels),
        "fine_safe_band_min": float(c.custom_fine_safe_band_min),
        "fine_safe_band_max": float(c.custom_fine_safe_band_max),
        "fine_target_band_ratio": float(c.custom_fine_target_band_ratio),
    }


def create_app() -> Flask:
    root = _project_root()
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    app.secret_key = _secret_key()

    @app.before_request
    def _require_login() -> Optional[Any]:
        if request.endpoint in ("login", "static", "logout", None):
            return None
        if not session.get("web_panel_auth"):
            return redirect(url_for("login", next=request.path))
        return None

    @app.route("/login", methods=["GET", "POST"])
    def login() -> Any:
        expect = _web_panel_token()
        if not expect:
            return (
                render_template(
                    "error.html",
                    title="未配置",
                    message="请在 .env 中设置 WEB_PANEL_TOKEN（登录密码）。",
                ),
                503,
            )
        if session.get("web_panel_auth"):
            nxt = request.args.get("next")
            if nxt and nxt.startswith("/"):
                return redirect(nxt)
            return redirect(url_for("index"))
        if request.method == "POST":
            pwd = (request.form.get("password") or "").strip()
            if hmac.compare_digest(pwd, expect):
                session["web_panel_auth"] = True
                session.permanent = True
                nxt = (request.form.get("next") or request.args.get("next") or "").strip()
                if nxt.startswith("/"):
                    return redirect(nxt)
                return redirect(url_for("index"))
            flash("密码错误", "error")
        return render_template("login.html")

    @app.route("/logout", methods=["GET", "POST"])
    def logout() -> Any:
        session.pop("web_panel_auth", None)
        return redirect(url_for("login"))

    @app.route("/")
    def index() -> str:
        ctx = get_ctx()
        ok, body = _cache_get_or_compute(
            "index:status",
            5.0,
            lambda: get_live_account_status(
                client=ctx.client,
                order_manager=ctx.order_manager,
                funder=ctx.funder,
                account_label=ctx.account_label,
            ),
        )
        lines = body.split("\n") if ok else []
        return render_template(
            "index.html",
            ok=ok,
            status_lines=lines,
            error=None if ok else body,
            account_label=ctx.account_label,
        )

    @app.route("/orders")
    def orders_page() -> str:
        ctx = get_ctx()
        rows, ok, text_body = _cache_get_or_compute(
            "orders:page",
            2.0,
            lambda: _build_orders_page_data(ctx),
        )
        return render_template(
            "orders.html",
            rows=rows,
            summary_ok=ok,
            summary_text=text_body if ok else "",
            summary_error=None if ok else text_body,
        )

    @app.route("/api/order_custom_rule")
    def api_order_custom_rule() -> Any:
        token_id = (request.args.get("token_id") or "").strip()
        side = (request.args.get("side") or "").strip().upper()
        if not token_id or side not in ("BUY", "SELL"):
            return jsonify({"error": "token_id 与 side(BUY/SELL) 必填"}), 400
        order_price: Optional[float] = None
        raw_order_price = (request.args.get("order_price") or "").strip()
        if raw_order_price:
            try:
                order_price = float(raw_order_price)
            except ValueError:
                order_price = None
        ctx = get_ctx()
        rule = ctx.rules_store.get_rule(token_id, side)
        suggested: Optional[str] = None
        effective_tick: Optional[float] = None
        try:
            book = ctx.book_fetcher.get_orderbook(token_id)
            t = max(float(book.tick_size or 0.01), 1e-12)
            t_eff = resolve_effective_tick_size(book.tick_size, book.bids, book.asks)
            if order_price is not None:
                t_used = pricing_tick_for_order_like_main_loop(
                    book_tick_size=book.tick_size,
                    bids=book.bids,
                    asks=book.asks,
                    order_price=float(order_price),
                )
            else:
                t_used = float(t_eff or t)
            effective_tick = max(float(t_used), 1e-12)
            suggested = classify_custom_tick_regime(effective_tick)
        except Exception:
            pass
        if rule is not None:
            form_regime: str = rule.tick_regime
        else:
            form_regime = suggested if suggested in ("coarse", "fine") else "coarse"
        defaults = _custom_rule_defaults_payload(ctx)
        return jsonify(
            {
                "rule": asdict(rule) if rule is not None else None,
                "suggested_regime": suggested,
                "form_regime": form_regime,
                "effective_tick": effective_tick,
                "defaults": defaults,
            }
        )

    @app.route("/pnl")
    def pnl_page() -> str:
        ctx = get_ctx()
        ok, body = _cache_get_or_compute(
            "pnl:live",
            5.0,
            lambda: get_live_pnl(
                client=ctx.client,
                order_manager=ctx.order_manager,
                funder=ctx.funder,
                account_label=ctx.account_label,
            ),
        )
        lines = body.split("\n") if ok else []
        return render_template(
            "pnl.html",
            ok=ok,
            lines=lines,
            error=None if ok else body,
        )

    @app.route("/cancel", methods=["POST"])
    def cancel_order() -> Any:
        oid = (request.form.get("order_id") or "").strip()
        if not oid:
            flash("缺少 order_id", "error")
            return redirect(url_for("orders_page"))
        ctx = get_ctx()
        try:
            ctx.client.cancel_order(OrderPayload(orderID=oid))
            _cache_invalidate()
            flash(f"已提交取消: {oid[:24]}…", "ok")
        except Exception as e:
            LOG.warning("web cancel failed: %s", e)
            flash(f"取消失败: {e}", "error")
        return redirect(url_for("orders_page"))

    @app.route("/cancel_all", methods=["POST"])
    def cancel_all() -> Any:
        ctx = get_ctx()
        try:
            orders = ctx.order_manager.fetch_all_open_orders(ctx.client)
        except Exception as e:
            flash(f"拉取挂单失败: {e}", "error")
            return redirect(url_for("orders_page"))
        total = 0
        failed = 0
        for o in orders:
            oi = str(o.get("id") or o.get("orderID") or "").strip()
            if not oi:
                continue
            total += 1
            try:
                ctx.client.cancel_order(OrderPayload(orderID=oi))
            except Exception:
                failed += 1
        if total == 0:
            flash("当前无挂单。", "ok")
        elif failed == 0:
            _cache_invalidate()
            flash(f"已提交取消全部 {total} 笔。", "ok")
        else:
            flash(f"部分失败: 成功 {total - failed}/{total}。", "error")
        return redirect(url_for("orders_page"))

    @app.route("/rules")
    def rules_page() -> str:
        ctx = get_ctx()
        store = ctx.rules_store
        items: list[dict[str, Any]] = []
        for key in store.list_keys():
            parts = key.rsplit(":", 1)
            token_id = parts[0] if len(parts) == 2 else key
            side = parts[1] if len(parts) == 2 else ""
            rule = store.get_rule(token_id, side)
            if rule is None:
                continue
            d = asdict(rule)
            d["key"] = key
            d["token_id"] = token_id
            d["side"] = side
            items.append(d)
        return render_template(
            "rules.html",
            items=items,
            rules_path=str(store.path),
        )

    @app.route("/rules/add", methods=["POST"])
    def rules_add() -> Any:
        ctx = get_ctx()
        back = _rules_form_redirect_url()
        token_id = (request.form.get("token_id") or "").strip()
        side = (request.form.get("side") or "").strip().upper()
        regime = (request.form.get("tick_regime") or "coarse").strip().lower()
        if not token_id or side not in ("BUY", "SELL"):
            flash("token_id 与 side(BUY/SELL) 必填", "error")
            return redirect(back)
        tr: CustomRuleRegime = "fine" if regime == "fine" else "coarse"
        try:
            rule = StoredCustomRule(
                tick_regime=tr,
                coarse_tick_offset_from_mid=int(
                    request.form.get("coarse_tick_offset_from_mid") or 1
                ),
                coarse_allow_top_of_book=request.form.get("coarse_allow_top_of_book")
                == "on",
                coarse_min_candidate_levels=int(
                    request.form.get("coarse_min_candidate_levels") or 1
                ),
                fine_safe_band_min=float(request.form.get("fine_safe_band_min") or 0.4),
                fine_safe_band_max=float(request.form.get("fine_safe_band_max") or 0.6),
                fine_target_band_ratio=float(
                    request.form.get("fine_target_band_ratio") or 0.5
                ),
            )
        except (TypeError, ValueError) as e:
            flash(f"参数无效: {e}", "error")
            return redirect(back)
        ctx.rules_store.set_rule(token_id, side, rule)
        _cache_invalidate()
        flash(f"已保存规则 {stable_rule_key(token_id, side)}", "ok")
        return redirect(back)

    @app.route("/rules/delete", methods=["POST"])
    def rules_delete() -> Any:
        ctx = get_ctx()
        back = _rules_form_redirect_url()
        token_id = (request.form.get("token_id") or "").strip()
        side = (request.form.get("side") or "").strip().upper()
        if not token_id or side not in ("BUY", "SELL"):
            flash("token_id 与 side 必填", "error")
            return redirect(back)
        if ctx.rules_store.clear_rule(token_id, side):
            _cache_invalidate()
            flash("已删除规则", "ok")
        else:
            flash("无此规则", "error")
        return redirect(back)

    return app


def _build_orders_page_data(ctx: WebPanelContext) -> tuple[list[dict[str, Any]], bool, str]:
    orders = ctx.order_manager.fetch_all_open_orders(ctx.client)
    rows = orders_as_rows(
        client=ctx.client,
        order_manager=ctx.order_manager,
        market_display=ctx.market_display,
        book_fetcher=ctx.book_fetcher,
        reward_monitor=ctx.reward_monitor,
        orders=orders,
    )
    ok = True
    text_body = _orders_summary_text_from_rows(rows)
    return rows, ok, text_body


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(_project_root() / ".env", override=False)
    host = (os.environ.get("WEB_PANEL_HOST") or "127.0.0.1").strip()
    port = int(os.environ.get("WEB_PANEL_PORT") or "8765")
    logging.basicConfig(level=logging.INFO)
    app = create_app()
    if not _web_panel_token():
        LOG.error("WEB_PANEL_TOKEN is not set; refusing to start.")
        raise SystemExit(1)
    LOG.info("Web panel listening on http://%s:%s", host, port)
    app.run(host=host, port=port, threaded=True, use_reloader=False)
