# Polymarket Order Monitoring (Liquidity Rewards)

Python **监控与调价**程序：您在 [Polymarket](https://docs.polymarket.com/api-reference/introduction) 前端**手动挂单**，本程序**不会新建订单**，只轮询该 API 密钥下的**未成交订单**，按**订单簿 + 激励半宽 δ** 的**简化规则**做 **保持 / 撤单 / 同量改价重挂**。

这不是自动做市机器人。
@臭臭Panda 推特/X ： https://x.com/Chosmos110
## 当前策略概要（主循环）

1. **冻结白名单**（进程启动时确定，运行中不增 token）：环境变量 `PASSIVE_TOKEN_WHITELIST`，或启动当刻未成交单里的唯一 `token_id`。
2. **过滤**：仅管理白名单内订单；若该 `token_id` **已有持仓**（`abs(inventory) > 1e-8`），则**整 token 不处理**（不撤、不改、不进 fill 推断与周期摘要明细）。
3. **调价**：仅 `passive_liquidity/simple_price_policy.py` 中的 **`decide_simple_price`**（粗 tick / 细 tick；见下文）。**不再**使用 `AdjustmentEngine`、结构性风控、fill risk、按积分微调、按库存调价等旧逻辑（相关文件仍留在仓库，主循环不调用）。
4. **执行**：`OrderManager.apply_decision`（撤单、撤单后延迟、挂单失败可无限重试或限次，由配置决定）。
5. **可选**：成交推断 Telegram、半点资金摘要、周期性 **band + 盘口深度** 摘要。

## 调价规则（`simple_price_policy`）

**Tick 分类**

- **粗 tick**：`tick ≈ 0.01` 或 `≈ 1.0`（API 写法不同）。
- **细 tick**：`tick ≈ 0.001` 或 `≈ 0.1`。
- **其它**：**保持**，不调价。

**粗 tick**

- 在 **BUY 看 bids / SELL 看 asks** 上，统计激励半带内有**正深度**的价位（按 tick 对齐合并）。
- **区间**：`band = floor(δ/tick)×tick`；BUY **`[mid−band, mid]`**，SELL **`[mid, mid+band]`**（δ 来自 CLOB rewards，与 `|价−mid|/δ` 同源）。
- **档位数 ≤ 2**：撤单且不挂回（Telegram 发「风险过大放弃持仓」，可带各档价格列表）。
- **3 档**：选离 mid **距离居中**的一档。
- **4 档**：选离 mid **第二远**的一档。
- **>4 档**：默认 **第二远**。
- 与目标价差小于 **最小替换 tick**：保持。

**细 tick**

- `distance_ratio = |价−mid|/δ`。
- **\[0.4, 0.6\]**：保持。
- **< 0.4**：外移至 **0.5×δ**。
- **> 0.6**：内收至 **0.5×δ**。
- 变动不足最小 tick：保持（带 `_noop_small_delta` 原因码）。

订单事件与部分 Telegram 文案中，原因码会显示为**中文说明**（`pricing_adjustment_reason_zh`）。

## 架构（模块）

| 模块 | 文件 | 职责 |
| --- | --- | --- |
| **MainLoop** | `passive_liquidity/main_loop.py` | 主循环；白名单、持仓过滤、盘口、调价、Telegram 触发 |
| **SimplePricePolicy** | `passive_liquidity/simple_price_policy.py` | 唯一调价决策；周期摘要用的**带内深度**统计 |
| **OrderManager** | `passive_liquidity/order_manager.py` | 拉单、`apply_decision`；改价重试回调 |
| **RewardMonitor** | `passive_liquidity/reward_monitor.py` | δ（激励半宽）、`are_orders_scoring`（展示/成交，**不参与调价**） |
| **OrderBookFetcher** | `passive_liquidity/orderbook_fetcher.py` | 订单簿与中间价 |
| **RiskManager** | `passive_liquidity/risk_manager.py` | 持仓、成交拉取（fill 与持仓展示） |
| **FillNotificationTracker** | `passive_liquidity/fill_detection.py` | 成交/撤单侧推断与 Telegram |
| **TelegramNotifier** | `passive_liquidity/telegram_notifier.py` | 各类中文通知、运营警告、原因码映射 |
| **AccountPortfolio** | `passive_liquidity/account_portfolio.py` | CLOB collateral 快照；**总额=API collateral**，不将未成交买单占用加回总额 |
| **ConfigManager** | `passive_liquidity/config_manager.py` | `PassiveConfig` + 环境变量 |
| **AdjustmentEngine** / **structural_risk** 等 | 遗留代码 | **主循环未使用** |

入口：`run_passive_bot.py`，或 `python -m passive_liquidity.main_loop`。

## 安装

Ubuntu / Debian 等系统自带的 Python 往往启用 **PEP 668**，**不要**对系统 Python 直接 `pip install`。

```bash
cd polymarket_lp_tool
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

```bash
./.venv/bin/python run_passive_bot.py
```

若提示 `ensurepip is not available`：

```bash
sudo apt install python3.12-venv
```

## 环境变量

1. 复制示例文件并编辑（**不要**把真实 `.env` 提交到 Git）：

```bash
cp .env.example .env
```

2. 在 `.env` 中至少填写 **`PRIVATE_KEY`（或 `POLYMARKET_PRIVATE_KEY`）** 与 **`POLYMARKET_FUNDER`**。其余变量见仓库根目录 **`.env.example`** 内注释。

`.env` 已在 `.gitignore` 中忽略。

### 与主循环强相关（`PASSIVE_*`）

完整列表与默认值见 `passive_liquidity/config_manager.py` → `PassiveConfig.from_env()`。常用项：

| 变量 | 含义 |
| --- | --- |
| **`PASSIVE_LOOP_INTERVAL`** | 主循环休眠间隔（秒） |
| **`PASSIVE_TOKEN_WHITELIST`** | 逗号分隔 `token_id`；留空则启动时用当时未成交单种子白名单 |
| **`PASSIVE_ADJ_MIN_REPLACE_TICKS`** | 价差小于 N 个 tick 则视为不必 replace |
| **`PASSIVE_MONITORING_POST_ONLY`** | 重挂是否 post-only |
| **`PASSIVE_REPLACE_DELAY_AFTER_CANCEL_SEC`** | 撤单后等待再挂单 |
| **`PASSIVE_REPLACE_POST_RETRY_INTERVAL_SEC`** / **`PASSIVE_REPLACE_POST_MAX_RETRIES`** | 挂单失败重试；**`MAX_RETRIES=0` 表示无限重试**（会阻塞该轮直到成功） |
| **`PASSIVE_MAX_API_ERRORS`** | 连续 API 失败多少次后 `cancel_all`；**`0` = 永不因此全撤** |

`PASSIVE_INV_MANUAL_THRESHOLD` 等仍存在于配置中（启动日志会打印），**当前主循环用「任意非零持仓即跳过整 token」**，与该阈值的旧「手动模式」语义不同。

### Telegram（`.env`）

| 变量 | 含义 |
| --- | --- |
| **`TELEGRAM_ENABLED`** | `true` / `false` |
| **`TELEGRAM_BOT_TOKEN`** / **`TELEGRAM_CHAT_ID`** | Bot 与会话 |
| **`TELEGRAM_ACCOUNT_LABEL`** | 消息前缀账号名 |
| **`TELEGRAM_NOTIFY_COOLDOWN_SEC`** | 同事件键冷却与指纹去重 |
| **`TELEGRAM_TOTAL_DEPOSITED_USDC`** | 可选；盈亏参考入账；不设则尝试活动 API 或启动时读数 |
| **`PASSIVE_TELEGRAM_BAND_SUMMARY`** | 是否发送周期性 **`|价−mid|/δ` + 带内深度** 摘要（默认开） |
| **`PASSIVE_TELEGRAM_BAND_SUMMARY_SEC`** | 周期间隔（秒），默认 `600`；`≤0` 关闭 |

另有成交通知相关 `PASSIVE_TELEGRAM_NOTIFY_*`（见 `config_manager.py`）。

**资金快照**：总额与可用均为 **CLOB API collateral**；未成交买单占用单独**估算展示**，**不加回**总额。改价挂单失败、撤单失败等会发**中文运营警告**（含余额不足等常见错误的简要说明）。

## 运行

1. 在 Polymarket 用**同一 API 密钥**手动挂好限价单。  
2. 启动程序；若无未成交单，会 idle，**不会下单**。

```bash
cd polymarket_lp_tool
python run_passive_bot.py
```

或：

```bash
python -m passive_liquidity.main_loop
```

## 免责声明

版本属于 `@臭臭Panda`。非官方产品；不保证激励计分或盈亏。请自行遵守服务条款与所在地法规（含 [地理限制](https://docs.polymarket.com/api-reference/geoblock)）。实盘前请小额测试。
