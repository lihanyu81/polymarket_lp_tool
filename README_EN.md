# Polymarket Order Monitoring (Liquidity Rewards)

[中文](README.md) | [English](README_EN.md)

Python monitoring and repricing tool for existing Polymarket orders.
You manually place limit orders on the Polymarket frontend; this program does **not** create initial orders.
It only polls **open orders** under your API key and applies simplified actions (**keep / cancel / cancel+replace with same size**) based on **order book + reward half-width delta**.

This is **not** a fully automated market-making bot.

Author X/Twitter: [@臭臭Panda](https://x.com/Chosmos110)  
Referral (optional): <https://polymarket.com/?r=xiaochouchou>

## Current Strategy (Main Loop)

1. **Whitelist**
   - If `PASSIVE_TOKEN_WHITELIST` is set, it is treated as fixed during runtime.
   - If not set, token IDs are seeded from current open orders and refreshed every `PASSIVE_WHITELIST_REFRESH_SEC` (default 120s). Set `0` to seed once at startup.
2. **Filter**
   - Only orders in the whitelist are managed.
   - If a token already has inventory (`abs(inventory) > 1e-8`), that entire token is skipped (no cancel/replace, no fill inference details).
3. **Pricing**
   - Only `decide_simple_price` in `passive_liquidity/simple_price_policy.py` is used.
   - If there is a saved Telegram/Web custom rule for `token_id + side`, or order ID is in `PASSIVE_CUSTOM_ORDER_IDS`, or `PASSIVE_DEFAULT_CUSTOM_PRICING=true`, the order goes through custom pricing (`PASSIVE_CUSTOM_*`, with persisted rules taking priority).
   - Legacy logic (AdjustmentEngine, structural risk, inventory-based tuning, etc.) is no longer used by the main loop.
4. **Execution**
   - `OrderManager.apply_decision` handles cancel / delayed replace / retry policy.
5. **Optional**
   - Telegram fill inference alerts
   - Half-hour account summary
   - Periodic band + depth summary

## Pricing Rules (`simple_price_policy`)

### Tick Regime

- **Coarse tick**: `tick ~= 0.01` or `~= 1.0`
- **Fine tick**: `tick ~= 0.001` or `~= 0.1`
- **Other**: keep, no price adjustment

### Coarse Tick

- For **BUY use bids / SELL use asks**, collect price levels with positive depth inside the reward half-band.
- Band: `band = floor(delta/tick) * tick`
  - BUY: `[mid-band, mid]`
  - SELL: `[mid, mid+band]`
- If candidate level count `<= 2`: cancel and do not repost.
- If `3`: choose middle distance-from-mid level.
- If `4`: choose second farthest level.
- If `> 4`: default to second farthest.
- If movement is smaller than minimum replace ticks: keep.

### Fine Tick

- `distance_ratio = |price-mid| / delta`
- In `[0.4, 0.6]`: keep
- `< 0.4`: move outward toward `0.5 * delta`
- `> 0.6`: move inward toward `0.5 * delta`
- If movement is below minimum tick threshold: keep (`_noop_small_delta`)

## Custom Pricing (Telegram / Web / Env)

Beyond built-in coarse/fine logic, you can apply fixed custom behavior per `token_id + side`.
Telegram `/set_rule` and Web custom-rule UI write to the same `custom_pricing_rules.json` (path controlled by `PASSIVE_CUSTOM_RULES_PATH`).

### Priority (per order)

1. Saved JSON rule for `token_id + BUY/SELL`
2. Else if `PASSIVE_DEFAULT_CUSTOM_PRICING=true`, use env `PASSIVE_CUSTOM_*`
3. Else if order ID is listed in `PASSIVE_CUSTOM_ORDER_IDS`, use env `PASSIVE_CUSTOM_*`
4. Else use default built-in coarse/fine strategy

### Telegram Commands

Requires `TELEGRAM_ENABLED=true` and `TELEGRAM_COMMANDS_ENABLED` not disabled.

| Command | Action |
| --- | --- |
| `/set_rule <order_id>` | Start interactive custom-rule setup for this live order |
| `/get_rule <order_id>` | Show saved rule summary |
| `/clear_rule <order_id>` | Remove saved rule |
| `/cancel_rule_setup` | Cancel active setup session |
| `/input <answer>` | Submit current step answer (useful in group privacy mode) |

### Custom Coarse Rule

- Configure positive integer `N`: choose the N-th level from near-mid to far, among **actual resting book prices with positive depth** in the reward scan range.
- Empty tick rungs do not count.
- Optional: forbid top-of-book placement (`PASSIVE_CUSTOM_COARSE_ALLOW_TOP_OF_BOOK`).
- Optional minimum candidate count (`PASSIVE_CUSTOM_COARSE_MIN_CANDIDATES`).

### Custom Fine Rule

- Keep inside `[PASSIVE_CUSTOM_FINE_SAFE_MIN, PASSIVE_CUSTOM_FINE_SAFE_MAX]`.
- Outside safe range, move toward `PASSIVE_CUSTOM_FINE_TARGET_RATIO`.

### Key Env Vars for Custom Mode

| Variable | Meaning | Default |
| --- | --- | --- |
| `PASSIVE_DEFAULT_CUSTOM_PRICING` | Apply env custom pricing globally when no saved rule exists | `false` |
| `PASSIVE_CUSTOM_ORDER_IDS` | Comma-separated order IDs for custom mode when global custom is off | empty |
| `PASSIVE_CUSTOM_RULES_PATH` | Rule JSON file path | `custom_pricing_rules.json` |
| `PASSIVE_CUSTOM_COARSE_TICK_OFFSET` | Coarse N-th level from near-mid | `1` |
| `PASSIVE_CUSTOM_COARSE_ALLOW_TOP_OF_BOOK` | Allow top-of-book target | `true` |
| `PASSIVE_CUSTOM_COARSE_MIN_CANDIDATES` | Minimum coarse candidates | `1` |
| `PASSIVE_CUSTOM_FINE_SAFE_MIN` | Fine safe lower bound | `0.4` |
| `PASSIVE_CUSTOM_FINE_SAFE_MAX` | Fine safe upper bound | `0.6` |
| `PASSIVE_CUSTOM_FINE_TARGET_RATIO` | Fine target ratio outside safe band | `0.5` |

See `.env.example` and `test_simple_price_custom_coarse.py` for more details.

## Architecture

| Module | File | Responsibility |
| --- | --- | --- |
| MainLoop | `passive_liquidity/main_loop.py` | Main control loop, whitelist/filter/pricing/actions |
| SimplePricePolicy | `passive_liquidity/simple_price_policy.py` | Sole pricing decision engine |
| OrderManager | `passive_liquidity/order_manager.py` | Open-order fetch, cancel/replace execution |
| RewardMonitor | `passive_liquidity/reward_monitor.py` | Reward spread delta and scoring status |
| OrderBookFetcher | `passive_liquidity/orderbook_fetcher.py` | Order book + midpoint fetch |
| RiskManager | `passive_liquidity/risk_manager.py` | Inventory and trade history fetch |
| FillNotificationTracker | `passive_liquidity/fill_detection.py` | Fill inference and notifications |
| TelegramNotifier | `passive_liquidity/telegram_notifier.py` | Telegram alerts and text formatting |
| AccountPortfolio | `passive_liquidity/account_portfolio.py` | Collateral snapshot and balances |
| ConfigManager | `passive_liquidity/config_manager.py` | Environment-based config |
| CustomPricingRulesStore | `passive_liquidity/custom_pricing_rules_store.py` | JSON rule persistence |
| Web Panel | `passive_liquidity/web_panel/`, `run_web_panel.py` | Optional Flask UI |
| TelegramRuleSetup | `passive_liquidity/telegram_rule_setup.py` | Multi-step rule setup FSM |
| TelegramCommandPoller | `passive_liquidity/telegram_command_poller.py` | `/status`, `/orders`, `/pnl`, rule commands |

Entry points: `run_passive_bot.py` or `python -m passive_liquidity.main_loop`  
Web panel: `run_web_panel.py`

## Installation

For Ubuntu/Debian with PEP 668 enabled, avoid installing into system Python directly.

```bash
cd polymarket_lp_tool
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run bot:

```bash
./.venv/bin/python run_passive_bot.py
```

If you see `ensurepip is not available`:

```bash
sudo apt install python3.12-venv
```

## Environment Variables

1. Copy template:

```bash
cp .env.example .env
```

2. At minimum set:
- `PRIVATE_KEY` (or `POLYMARKET_PRIVATE_KEY`)
- `POLYMARKET_FUNDER`

`.env` is ignored by Git.

### Main Loop Related (`PASSIVE_*`)

See `PassiveConfig.from_env()` in `passive_liquidity/config_manager.py` for full list.

Common vars:
- `PASSIVE_LOOP_INTERVAL`
- `PASSIVE_TOKEN_WHITELIST`
- `PASSIVE_WHITELIST_REFRESH_SEC`
- `PASSIVE_ADJ_MIN_REPLACE_TICKS`
- `PASSIVE_MONITORING_POST_ONLY`
- `PASSIVE_REPLACE_DELAY_AFTER_CANCEL_SEC`
- `PASSIVE_REPLACE_POST_RETRY_INTERVAL_SEC`
- `PASSIVE_REPLACE_POST_MAX_RETRIES` (`0` = unlimited)
- `PASSIVE_MAX_API_ERRORS` (`0` = never auto-cancel-all)

### Telegram (`.env`)

Common vars:
- `TELEGRAM_ENABLED`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `TELEGRAM_ACCOUNT_LABEL`
- `TELEGRAM_NOTIFY_COOLDOWN_SEC`
- `TELEGRAM_TOTAL_DEPOSITED_USDC`
- `PASSIVE_TELEGRAM_BAND_SUMMARY`
- `PASSIVE_TELEGRAM_BAND_SUMMARY_SEC`
- `TELEGRAM_COMMANDS_ENABLED`

Note: `PASSIVE_ALERT_MONITORING=false` disables market-condition monitoring alerts, not your own fill alerts.  
To disable fill alerts, set `PASSIVE_TELEGRAM_NOTIFY_FILL=false` (or partial/full flags).

## Run

1. Place manual limit orders on Polymarket using the same API key.
2. Start the bot. If there are no open orders, it stays idle and does not place initial orders.

```bash
cd polymarket_lp_tool
python run_passive_bot.py
```

or

```bash
python -m passive_liquidity.main_loop
```

## Web Panel (Optional)

Read-only/management UI. It does not replace the bot main loop.

1. Set `WEB_PANEL_TOKEN` in `.env`
2. Install deps (`flask` is already in `requirements.txt`)
3. Start:

```bash
cd polymarket_lp_tool
source .venv/bin/activate
python run_web_panel.py
```

Default host/port:
- `WEB_PANEL_HOST=127.0.0.1`
- `WEB_PANEL_PORT=8765`

Open <http://127.0.0.1:8765> and log in with `WEB_PANEL_TOKEN`.

Optional vars:
- `WEB_PANEL_SECRET_KEY`

To stop a detached/stuck web process:

```bash
ss -tlnp 2>/dev/null | grep ':8765' || true
kill -9 <pid>
```

## Run with tmux (recommended on SSH)

Install:

```bash
sudo apt update && sudo apt install -y tmux
```

Usage:

```bash
tmux new -s poly
cd polymarket_lp_tool
source .venv/bin/activate
python run_passive_bot.py
```

Detach: `Ctrl+b`, then `d`  
Reattach:

```bash
tmux attach -t poly
```

List sessions:

```bash
tmux ls
```

## Disclaimer

Version maintained by `@臭臭Panda`.  
Not an official Polymarket product. No guarantee on reward scoring or PnL.
Use at your own risk, comply with local laws and Polymarket terms, including [geo restrictions](https://docs.polymarket.com/api-reference/geoblock).  
Always test with small size first.
