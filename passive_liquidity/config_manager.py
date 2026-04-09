from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

_PROJECT_DIR = Path(__file__).resolve().parent.parent


def _parse_token_whitelist(raw: Optional[str]) -> frozenset[str]:
    if raw is None or not str(raw).strip():
        return frozenset()
    parts = [p.strip() for p in str(raw).split(",")]
    return frozenset(p for p in parts if p)


def _parse_custom_order_ids(raw: Optional[str]) -> frozenset[str]:
    """Comma-separated CLOB order ids using pricing_mode=custom when matched."""
    if raw is None or not str(raw).strip():
        return frozenset()
    parts = [p.strip() for p in str(raw).split(",")]
    return frozenset(p for p in parts if p)


@dataclass
class PassiveConfig:
    """Tunable parameters for passive reward quoting."""

    base_size: float = 10.0
    min_offset: float = 0.01
    spread_offset_mult: float = 0.3
    inventory_limit: float = 50.0
    max_position: float = 100.0
    loop_interval: float = 2.0
    price_step: float = 0.005
    max_fill_rate: float = 0.1
    fill_rate_denominator: float = 25.0
    # Short-window fill activity (seconds); used with size + direction weighting.
    fill_short_lookback_sec: int = 900
    fill_short_count_denom: float = 8.0
    fill_long_size_norm_usdc: float = 400.0
    fill_short_size_norm_usdc: float = 120.0
    fill_activity_count_weight: float = 0.35
    fill_activity_size_weight: float = 0.65
    fill_dir_aligned_weight: float = 1.0
    fill_dir_misaligned_weight: float = 0.35
    fill_dir_same_side_weight: float = 0.45
    fill_dir_unknown_weight: float = 0.55
    fill_book_ticks_scale: float = 2.5
    fill_risk_blend_short: float = 0.55
    fill_short_spike_boost: float = 1.12
    fill_short_spike_mix: float = 0.32
    fill_activity_book_floor: float = 0.18
    fill_activity_book_mult: float = 0.82
    fill_risk_level_1_max: float = 0.24
    fill_risk_level_2_max: float = 0.42
    fill_risk_level_3_max: float = 0.64
    fill_risk_moderate_widen_ticks: int = 1
    safety_ticks_from_top: int = 2
    max_markets: int = 25
    max_quote_tasks: int = 30
    quote_all_outcome_tokens: bool = True
    fill_lookback_sec: int = 3600
    volatility_abs_change_threshold: float = 0.08
    widen_fill_rate_factor: float = 2.0
    tighten_not_scoring_steps: int = 3
    # 0 = never call cancel_all on repeated API errors (recommended for monitoring mode).
    max_api_errors_before_cancel_all: int = 0
    clob_host: str = "https://clob.polymarket.com"
    data_api_host: str = "https://data-api.polymarket.com"
    gamma_api_host: str = "https://gamma-api.polymarket.com"
    chain_id: int = 137
    # 0 = 列出全部；>0 时仅打印前 N 条（仍按 rate 排序后的顺序），序号 1..N 与屏幕一致
    catalog_max_rows: int = 0
    # 交互目录每页行数（每条 outcome 占 2 行文本）
    catalog_page_size: int = 25
    # Monitoring bot: ticks to widen when fill pressure is high
    adjustment_widen_ticks: int = 2
    # Monitoring bot: ticks to nudge toward mid when not scoring
    adjustment_nudge_ticks: int = 1
    # Only replace if price moves by at least this many ticks (minimal churn)
    adjustment_min_replace_ticks: int = 1
    # Not scoring but inside reward band: nudge only if within this many ticks of outer band edge
    # (bid_floor for BUY, ask_ceiling for SELL) for this many consecutive non-scoring cycles.
    adjustment_non_scoring_boundary_ticks: int = 3
    adjustment_non_scoring_streak_nudge: int = 5
    # Inside reward band but API says not scoring for this many consecutive cycles -> low-quality placement.
    inside_band_low_quality_streak: int = 30
    # In low-quality mode: allow 1-tick explore only if fill_risk_score <= this.
    low_quality_explore_max_risk_score: float = 0.08
    # Deprecated alias (env PASSIVE_LOW_Q_EXPLORE_MAX_FILL); used if _MAX_RISK unset.
    low_quality_explore_max_fill_rate: float = 0.05
    # In low-quality mode: explore only every N cycles (after crossing streak threshold). 0 = never.
    low_quality_explore_interval_cycles: int = 10
    # In low-quality mode: exploration requires |inventory| < max_position * this fraction.
    low_quality_explore_inv_frac: float = 0.85
    monitoring_post_only: bool = True
    # Seconds to wait after cancel before posting replacement (0 = immediate).
    replace_delay_after_cancel_sec: float = 0.0
    # After cancel, if post fails: wait this long before retrying create+post.
    replace_post_retry_interval_sec: float = 5.0
    # 0 = retry until post succeeds (blocks main loop until then). >0 = max attempts then give up.
    replace_post_max_retries: int = 0
    # Comma-separated CLOB outcome token_ids (asset_id). Empty = no automatic management.
    token_whitelist: frozenset[str] = field(default_factory=frozenset)
    # When token_whitelist is empty: recompute allowed token_ids from open orders this often (seconds).
    # <= 0 disables periodic refresh (startup seed only). Default 120 = 2 minutes.
    whitelist_refresh_interval_sec: float = 120.0
    # Manage whitelisted tokens only while abs(position) <= this; above = manual mode (no touch).
    inventory_manual_threshold: float = 0.01
    # Second-level structural risk (only risky orders when token danger exposure is high).
    struct_enabled: bool = True
    struct_min_danger_exposure: float = 25.0
    struct_exposure_cut_frac: float = 0.5
    struct_book_proximity_min: float = 0.52
    struct_short_activity_min: float = 0.22
    struct_max_queue_ticks: float = 2.5
    struct_safety_ticks: int = 2
    struct_cooldown_sec: float = 90.0
    struct_min_post_size: float = 1.0
    # OR gate with struct_min_danger_exposure: trigger if either threshold is exceeded (when > 0).
    struct_min_danger_notional_usdc: float = 150.0
    # Short window after a successful structural replace: no mid-chasing / explore (Level 1).
    struct_observation_sec: float = 20.0
    # Microtrend: share of tape notional in lookback that trades against our resting side [0,1].
    struct_dir_lookback_sec: float = 120.0
    struct_directional_min: float = 0.55
    struct_directional_allow_no_tape: bool = True
    # Exposure cut fraction by max fill-risk level among structurally risky orders on the token.
    struct_cut_frac_low: float = 0.22
    struct_cut_frac_moderate: float = 0.35
    struct_cut_frac_elevated: float = 0.50
    struct_cut_frac_high: float = 0.62
    # Adaptive re-center toward mid when fill risk is LOW (AdjustmentEngine; after widen).
    recenter_enabled: bool = True
    recenter_min_ticks_from_top: int = 2
    recenter_step_ticks: int = 1
    recenter_target_ratio: float = 0.35
    recenter_max_step_ticks: int = 3
    # Require mid to move by at least this fraction of delta vs last cycle (anti-churn).
    # <= 0 disables this filter (always allow recenter when other gates pass).
    recenter_mid_move_frac: float = 0.1
    # In 0.2<=distance_ratio<0.5 zone: nudge 1 tick toward mid after this many non-scoring cycles.
    recenter_nudge_streak: int = 5
    # Telegram: order fill alerts (requires TELEGRAM_ENABLED and working bot).
    telegram_notify_fill: bool = True
    telegram_notify_partial_fill: bool = True
    telegram_notify_full_fill: bool = True
    # If True, also emit fill alerts for tokens in manual inventory mode.
    telegram_fill_manual_tokens: bool = False
    # Trades lookback (seconds) when inferring fills for vanished orders (order id on trade).
    fill_infer_trade_lookback_sec: float = 300.0
    # Telegram: periodic summary of each managed order's |price−mid| / δ (reward half-spread).
    telegram_band_summary_enabled: bool = True
    telegram_band_summary_interval_sec: float = 600.0
    # Passive market-condition alerts: tape fill-risk / depth ratio (Telegram + logs only).
    # Does not control per-order fill notifications; see telegram_notify_fill.
    alert_monitoring_enabled: bool = True
    alert_cooldown_sec: float = 120.0
    monitor_short_trade_lookback_sec: float = 180.0
    alert_fill_rate_threshold: float = 0.35
    alert_short_trades_threshold: int = 8
    alert_fill_risk_score_threshold: float = 0.45
    alert_direction_imbalance_min: float = 0.62
    alert_depth_ratio_threshold: float = 0.15
    alert_significant_fill_rate_delta: float = 0.08
    alert_significant_short_trades_delta: int = 4
    alert_significant_fill_risk_delta: float = 0.08
    alert_significant_adverse_share_delta: float = 0.07
    alert_significant_depth_ratio_delta: float = 0.05
    # WebSocket monitoring (optional; REST remains source of truth for execution).
    ws_enabled: bool = True
    ws_user_enabled: bool = True
    ws_market_enabled: bool = True
    ws_stale_sec: float = 25.0
    ws_reconcile_every_loops: int = 15
    ws_telegram_transport_alerts: bool = True
    # When True, orders without a Telegram-stored rule use PASSIVE_CUSTOM_* as the pricing profile
    # (same as listing every order in PASSIVE_CUSTOM_ORDER_IDS). Stored rules still win per token+side.
    default_custom_pricing_from_env: bool = False
    # Custom pricing (simple_price_policy): only orders listed in custom_pricing_order_ids.
    custom_pricing_order_ids: frozenset[str] = field(default_factory=frozenset)
    custom_coarse_tick_offset_from_mid: int = 1
    custom_coarse_allow_top_of_book: bool = True
    custom_coarse_min_candidate_levels: int = 1
    custom_fine_safe_band_min: float = 0.4
    custom_fine_safe_band_max: float = 0.6
    custom_fine_target_band_ratio: float = 0.5
    # JSON path for Telegram /set_rule persisted rules (token_id+side keys).
    custom_rules_store_path: str = ""

    @classmethod
    def from_env(cls) -> PassiveConfig:
        load_dotenv(_PROJECT_DIR / ".env", override=False)

        def f(name: str, default: float) -> float:
            v = os.environ.get(name)
            if v is None or v == "":
                return default
            return float(v)

        def i(name: str, default: int) -> int:
            v = os.environ.get(name)
            if v is None or v == "":
                return default
            return int(v)

        def b(name: str, default: bool) -> bool:
            v = os.environ.get(name)
            if v is None or v == "":
                return default
            return v.strip().lower() in ("1", "true", "yes", "on")

        if os.environ.get("PASSIVE_LOW_Q_EXPLORE_MAX_RISK") not in (None, ""):
            low_q_explore_max_risk = f(
                "PASSIVE_LOW_Q_EXPLORE_MAX_RISK", cls.low_quality_explore_max_risk_score
            )
        elif os.environ.get("PASSIVE_LOW_Q_EXPLORE_MAX_FILL") not in (None, ""):
            low_q_explore_max_risk = f(
                "PASSIVE_LOW_Q_EXPLORE_MAX_FILL", cls.low_quality_explore_max_fill_rate
            )
        else:
            low_q_explore_max_risk = cls.low_quality_explore_max_risk_score

        if os.environ.get("PASSIVE_STRUCT_EXPOSURE_CUT") not in (None, ""):
            _struct_legacy_cut = f("PASSIVE_STRUCT_EXPOSURE_CUT", cls.struct_exposure_cut_frac)
            _cut_low = _cut_mod = _cut_el = _cut_hi = _struct_legacy_cut
            _struct_exposure_cut_frac = _struct_legacy_cut
        else:
            _struct_exposure_cut_frac = cls.struct_exposure_cut_frac
            _cut_low = f("PASSIVE_STRUCT_CUT_LOW", cls.struct_cut_frac_low)
            _cut_mod = f("PASSIVE_STRUCT_CUT_MODERATE", cls.struct_cut_frac_moderate)
            _cut_el = f("PASSIVE_STRUCT_CUT_ELEVATED", cls.struct_cut_frac_elevated)
            _cut_hi = f("PASSIVE_STRUCT_CUT_HIGH", cls.struct_cut_frac_high)

        return cls(
            base_size=f("PASSIVE_BASE_SIZE", cls.base_size),
            min_offset=f("PASSIVE_MIN_OFFSET", cls.min_offset),
            spread_offset_mult=f("PASSIVE_SPREAD_OFFSET_MULT", cls.spread_offset_mult),
            inventory_limit=f("PASSIVE_INVENTORY_LIMIT", cls.inventory_limit),
            max_position=f("PASSIVE_MAX_POSITION", cls.max_position),
            loop_interval=f("PASSIVE_LOOP_INTERVAL", cls.loop_interval),
            price_step=f("PASSIVE_PRICE_STEP", cls.price_step),
            max_fill_rate=f("PASSIVE_MAX_FILL_RATE", cls.max_fill_rate),
            fill_rate_denominator=f("PASSIVE_FILL_RATE_DENOM", cls.fill_rate_denominator),
            fill_short_lookback_sec=i("PASSIVE_FILL_SHORT_LOOKBACK_SEC", cls.fill_short_lookback_sec),
            fill_short_count_denom=f("PASSIVE_FILL_SHORT_COUNT_DENOM", cls.fill_short_count_denom),
            fill_long_size_norm_usdc=f("PASSIVE_FILL_LONG_SIZE_NORM_USDC", cls.fill_long_size_norm_usdc),
            fill_short_size_norm_usdc=f("PASSIVE_FILL_SHORT_SIZE_NORM_USDC", cls.fill_short_size_norm_usdc),
            fill_activity_count_weight=f(
                "PASSIVE_FILL_ACTIVITY_COUNT_W", cls.fill_activity_count_weight
            ),
            fill_activity_size_weight=f(
                "PASSIVE_FILL_ACTIVITY_SIZE_W", cls.fill_activity_size_weight
            ),
            fill_dir_aligned_weight=f("PASSIVE_FILL_DIR_ALIGNED", cls.fill_dir_aligned_weight),
            fill_dir_misaligned_weight=f(
                "PASSIVE_FILL_DIR_MISALIGNED", cls.fill_dir_misaligned_weight
            ),
            fill_dir_same_side_weight=f(
                "PASSIVE_FILL_DIR_SAME_SIDE", cls.fill_dir_same_side_weight
            ),
            fill_dir_unknown_weight=f("PASSIVE_FILL_DIR_UNKNOWN", cls.fill_dir_unknown_weight),
            fill_book_ticks_scale=f("PASSIVE_FILL_BOOK_TICKS_SCALE", cls.fill_book_ticks_scale),
            fill_risk_blend_short=f("PASSIVE_FILL_RISK_BLEND_SHORT", cls.fill_risk_blend_short),
            fill_short_spike_boost=f("PASSIVE_FILL_SHORT_SPIKE_BOOST", cls.fill_short_spike_boost),
            fill_short_spike_mix=f("PASSIVE_FILL_SHORT_SPIKE_MIX", cls.fill_short_spike_mix),
            fill_activity_book_floor=f(
                "PASSIVE_FILL_ACTIVITY_BOOK_FLOOR", cls.fill_activity_book_floor
            ),
            fill_activity_book_mult=f(
                "PASSIVE_FILL_ACTIVITY_BOOK_MULT", cls.fill_activity_book_mult
            ),
            fill_risk_level_1_max=f("PASSIVE_FILL_RISK_LVL1", cls.fill_risk_level_1_max),
            fill_risk_level_2_max=f("PASSIVE_FILL_RISK_LVL2", cls.fill_risk_level_2_max),
            fill_risk_level_3_max=f("PASSIVE_FILL_RISK_LVL3", cls.fill_risk_level_3_max),
            fill_risk_moderate_widen_ticks=i(
                "PASSIVE_FILL_RISK_MOD_WIDEN_TICKS", cls.fill_risk_moderate_widen_ticks
            ),
            safety_ticks_from_top=i("PASSIVE_SAFETY_TICKS", cls.safety_ticks_from_top),
            max_markets=i("PASSIVE_MAX_MARKETS", cls.max_markets),
            max_quote_tasks=i("PASSIVE_MAX_QUOTE_TASKS", cls.max_quote_tasks),
            quote_all_outcome_tokens=b("PASSIVE_QUOTE_ALL_TOKENS", cls.quote_all_outcome_tokens),
            fill_lookback_sec=i("PASSIVE_FILL_LOOKBACK_SEC", cls.fill_lookback_sec),
            volatility_abs_change_threshold=f(
                "PASSIVE_VOLATILITY_THRESHOLD", cls.volatility_abs_change_threshold
            ),
            widen_fill_rate_factor=f("PASSIVE_WIDEN_FILL_FACTOR", cls.widen_fill_rate_factor),
            tighten_not_scoring_steps=i(
                "PASSIVE_TIGHTEN_NOT_SCORING_STEPS", cls.tighten_not_scoring_steps
            ),
            max_api_errors_before_cancel_all=i(
                "PASSIVE_MAX_API_ERRORS", cls.max_api_errors_before_cancel_all
            ),
            clob_host=os.environ.get("POLYMARKET_HOST", cls.clob_host).rstrip("/"),
            data_api_host=os.environ.get("POLYMARKET_DATA_API", cls.data_api_host).rstrip("/"),
            gamma_api_host=os.environ.get("POLYMARKET_GAMMA_API", cls.gamma_api_host).rstrip(
                "/"
            ),
            chain_id=i("POLYMARKET_CHAIN_ID", cls.chain_id),
            catalog_max_rows=i("PASSIVE_CATALOG_MAX_ROWS", cls.catalog_max_rows),
            catalog_page_size=i("PASSIVE_CATALOG_PAGE_SIZE", cls.catalog_page_size),
            adjustment_widen_ticks=i("PASSIVE_ADJ_WIDEN_TICKS", cls.adjustment_widen_ticks),
            adjustment_nudge_ticks=i("PASSIVE_ADJ_NUDGE_TICKS", cls.adjustment_nudge_ticks),
            adjustment_min_replace_ticks=i(
                "PASSIVE_ADJ_MIN_REPLACE_TICKS", cls.adjustment_min_replace_ticks
            ),
            adjustment_non_scoring_boundary_ticks=i(
                "PASSIVE_ADJ_NS_BOUNDARY_TICKS", cls.adjustment_non_scoring_boundary_ticks
            ),
            adjustment_non_scoring_streak_nudge=i(
                "PASSIVE_ADJ_NS_STREAK_NUDGE", cls.adjustment_non_scoring_streak_nudge
            ),
            inside_band_low_quality_streak=i(
                "PASSIVE_INSIDE_BAND_LOW_Q_STREAK", cls.inside_band_low_quality_streak
            ),
            low_quality_explore_max_risk_score=low_q_explore_max_risk,
            low_quality_explore_max_fill_rate=f(
                "PASSIVE_LOW_Q_EXPLORE_MAX_FILL", cls.low_quality_explore_max_fill_rate
            ),
            low_quality_explore_interval_cycles=i(
                "PASSIVE_LOW_Q_EXPLORE_INTERVAL", cls.low_quality_explore_interval_cycles
            ),
            low_quality_explore_inv_frac=f(
                "PASSIVE_LOW_Q_EXPLORE_INV_FRAC", cls.low_quality_explore_inv_frac
            ),
            monitoring_post_only=b("PASSIVE_MONITORING_POST_ONLY", cls.monitoring_post_only),
            replace_delay_after_cancel_sec=f(
                "PASSIVE_REPLACE_DELAY_SEC", cls.replace_delay_after_cancel_sec
            ),
            replace_post_retry_interval_sec=f(
                "PASSIVE_REPLACE_POST_RETRY_SEC", cls.replace_post_retry_interval_sec
            ),
            replace_post_max_retries=i(
                "PASSIVE_REPLACE_POST_MAX_RETRIES", cls.replace_post_max_retries
            ),
            token_whitelist=_parse_token_whitelist(os.environ.get("PASSIVE_TOKEN_WHITELIST")),
            whitelist_refresh_interval_sec=f(
                "PASSIVE_WHITELIST_REFRESH_SEC", cls.whitelist_refresh_interval_sec
            ),
            inventory_manual_threshold=f(
                "PASSIVE_INV_MANUAL_THRESHOLD", cls.inventory_manual_threshold
            ),
            struct_enabled=b("PASSIVE_STRUCT_ENABLED", cls.struct_enabled),
            struct_min_danger_exposure=f(
                "PASSIVE_STRUCT_MIN_DANGER", cls.struct_min_danger_exposure
            ),
            struct_exposure_cut_frac=_struct_exposure_cut_frac,
            struct_book_proximity_min=f(
                "PASSIVE_STRUCT_BOOK_PROX_MIN", cls.struct_book_proximity_min
            ),
            struct_short_activity_min=f(
                "PASSIVE_STRUCT_SHORT_ACT_MIN", cls.struct_short_activity_min
            ),
            struct_max_queue_ticks=f(
                "PASSIVE_STRUCT_MAX_QUEUE_TICKS", cls.struct_max_queue_ticks
            ),
            struct_safety_ticks=i("PASSIVE_STRUCT_SAFETY_TICKS", cls.struct_safety_ticks),
            struct_cooldown_sec=f("PASSIVE_STRUCT_COOLDOWN_SEC", cls.struct_cooldown_sec),
            struct_min_post_size=f(
                "PASSIVE_STRUCT_MIN_POST_SIZE", cls.struct_min_post_size
            ),
            struct_min_danger_notional_usdc=f(
                "PASSIVE_STRUCT_MIN_DANGER_NOTIONAL", cls.struct_min_danger_notional_usdc
            ),
            struct_observation_sec=f(
                "PASSIVE_STRUCT_OBSERVATION_SEC", cls.struct_observation_sec
            ),
            struct_dir_lookback_sec=f(
                "PASSIVE_STRUCT_DIR_LOOKBACK_SEC", cls.struct_dir_lookback_sec
            ),
            struct_directional_min=f(
                "PASSIVE_STRUCT_DIR_MIN", cls.struct_directional_min
            ),
            struct_directional_allow_no_tape=b(
                "PASSIVE_STRUCT_DIR_ALLOW_NO_TAPE", cls.struct_directional_allow_no_tape
            ),
            struct_cut_frac_low=_cut_low,
            struct_cut_frac_moderate=_cut_mod,
            struct_cut_frac_elevated=_cut_el,
            struct_cut_frac_high=_cut_hi,
            recenter_enabled=b("PASSIVE_RECENTER_ENABLED", cls.recenter_enabled),
            recenter_min_ticks_from_top=i(
                "PASSIVE_RECENTER_MIN_TICKS", cls.recenter_min_ticks_from_top
            ),
            recenter_step_ticks=i(
                "PASSIVE_RECENTER_STEP_TICKS", cls.recenter_step_ticks
            ),
            recenter_target_ratio=f(
                "PASSIVE_RECENTER_TARGET_RATIO", cls.recenter_target_ratio
            ),
            recenter_max_step_ticks=i(
                "PASSIVE_RECENTER_MAX_STEP", cls.recenter_max_step_ticks
            ),
            recenter_mid_move_frac=f(
                "PASSIVE_RECENTER_TRIGGER_THRESHOLD",
                cls.recenter_mid_move_frac,
            ),
            recenter_nudge_streak=i(
                "PASSIVE_RECENTER_NUDGE_STREAK", cls.recenter_nudge_streak
            ),
            telegram_notify_fill=b(
                "PASSIVE_TELEGRAM_NOTIFY_FILL", cls.telegram_notify_fill
            ),
            telegram_notify_partial_fill=b(
                "PASSIVE_TELEGRAM_NOTIFY_PARTIAL_FILL",
                cls.telegram_notify_partial_fill,
            ),
            telegram_notify_full_fill=b(
                "PASSIVE_TELEGRAM_NOTIFY_FULL_FILL",
                cls.telegram_notify_full_fill,
            ),
            telegram_fill_manual_tokens=b(
                "PASSIVE_TELEGRAM_FILL_MANUAL_TOKENS",
                cls.telegram_fill_manual_tokens,
            ),
            fill_infer_trade_lookback_sec=f(
                "PASSIVE_FILL_INFER_TRADE_LOOKBACK_SEC",
                cls.fill_infer_trade_lookback_sec,
            ),
            telegram_band_summary_enabled=b(
                "PASSIVE_TELEGRAM_BAND_SUMMARY",
                cls.telegram_band_summary_enabled,
            ),
            telegram_band_summary_interval_sec=f(
                "PASSIVE_TELEGRAM_BAND_SUMMARY_SEC",
                cls.telegram_band_summary_interval_sec,
            ),
            alert_monitoring_enabled=b(
                "PASSIVE_ALERT_MONITORING", cls.alert_monitoring_enabled
            ),
            alert_cooldown_sec=f(
                "PASSIVE_ALERT_COOLDOWN_SEC", cls.alert_cooldown_sec
            ),
            monitor_short_trade_lookback_sec=f(
                "PASSIVE_MONITOR_SHORT_TRADE_LOOKBACK_SEC",
                cls.monitor_short_trade_lookback_sec,
            ),
            alert_fill_rate_threshold=f(
                "PASSIVE_ALERT_FILL_RATE_THRESHOLD", cls.alert_fill_rate_threshold
            ),
            alert_short_trades_threshold=i(
                "PASSIVE_ALERT_SHORT_TRADES_THRESHOLD",
                cls.alert_short_trades_threshold,
            ),
            alert_fill_risk_score_threshold=f(
                "PASSIVE_ALERT_FILL_RISK_SCORE_THRESHOLD",
                cls.alert_fill_risk_score_threshold,
            ),
            alert_direction_imbalance_min=f(
                "PASSIVE_ALERT_DIRECTION_IMBALANCE_MIN",
                cls.alert_direction_imbalance_min,
            ),
            alert_depth_ratio_threshold=f(
                "PASSIVE_ALERT_DEPTH_RATIO_THRESHOLD",
                cls.alert_depth_ratio_threshold,
            ),
            alert_significant_fill_rate_delta=f(
                "PASSIVE_ALERT_SIG_FILL_RATE_DELTA",
                cls.alert_significant_fill_rate_delta,
            ),
            alert_significant_short_trades_delta=i(
                "PASSIVE_ALERT_SIG_SHORT_TRADES_DELTA",
                cls.alert_significant_short_trades_delta,
            ),
            alert_significant_fill_risk_delta=f(
                "PASSIVE_ALERT_SIG_FILL_RISK_DELTA",
                cls.alert_significant_fill_risk_delta,
            ),
            alert_significant_adverse_share_delta=f(
                "PASSIVE_ALERT_SIG_ADVERSE_SHARE_DELTA",
                cls.alert_significant_adverse_share_delta,
            ),
            alert_significant_depth_ratio_delta=f(
                "PASSIVE_ALERT_SIG_DEPTH_RATIO_DELTA",
                cls.alert_significant_depth_ratio_delta,
            ),
            ws_enabled=b("PASSIVE_WS_ENABLED", cls.ws_enabled),
            ws_user_enabled=b("PASSIVE_WS_USER_ENABLED", cls.ws_user_enabled),
            ws_market_enabled=b("PASSIVE_WS_MARKET_ENABLED", cls.ws_market_enabled),
            ws_stale_sec=f("PASSIVE_WS_STALE_SEC", cls.ws_stale_sec),
            ws_reconcile_every_loops=i(
                "PASSIVE_WS_RECONCILE_LOOPS", cls.ws_reconcile_every_loops
            ),
            ws_telegram_transport_alerts=b(
                "PASSIVE_WS_TELEGRAM_TRANSPORT",
                cls.ws_telegram_transport_alerts,
            ),
            default_custom_pricing_from_env=b(
                "PASSIVE_DEFAULT_CUSTOM_PRICING",
                cls.default_custom_pricing_from_env,
            ),
            custom_pricing_order_ids=_parse_custom_order_ids(
                os.environ.get("PASSIVE_CUSTOM_ORDER_IDS")
            ),
            custom_coarse_tick_offset_from_mid=i(
                "PASSIVE_CUSTOM_COARSE_TICK_OFFSET",
                cls.custom_coarse_tick_offset_from_mid,
            ),
            custom_coarse_allow_top_of_book=b(
                "PASSIVE_CUSTOM_COARSE_ALLOW_TOP_OF_BOOK",
                cls.custom_coarse_allow_top_of_book,
            ),
            custom_coarse_min_candidate_levels=i(
                "PASSIVE_CUSTOM_COARSE_MIN_CANDIDATES",
                cls.custom_coarse_min_candidate_levels,
            ),
            custom_fine_safe_band_min=f(
                "PASSIVE_CUSTOM_FINE_SAFE_MIN",
                cls.custom_fine_safe_band_min,
            ),
            custom_fine_safe_band_max=f(
                "PASSIVE_CUSTOM_FINE_SAFE_MAX",
                cls.custom_fine_safe_band_max,
            ),
            custom_fine_target_band_ratio=f(
                "PASSIVE_CUSTOM_FINE_TARGET_RATIO",
                cls.custom_fine_target_band_ratio,
            ),
            custom_rules_store_path=(
                os.environ.get("PASSIVE_CUSTOM_RULES_PATH", "").strip()
                or str(_PROJECT_DIR / "custom_pricing_rules.json")
            ),
        )
