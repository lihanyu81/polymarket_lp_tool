# Polymarket Order Monitoring (Liquidity Rewards)

Python **监控与调价**程序：您在 [Polymarket](https://docs.polymarket.com/api-reference/introduction) 前端**手动挂单**，本程序**不会新建订单**，只轮询该 API 密钥下的**未成交订单**，按**订单簿 + 激励半宽 δ** 的**简化规则**做 **保持 / 撤单 / 同量改价重挂**。

这不是自动做市机器人。

@臭臭Panda 推特/X ： https://x.com/Chosmos110

如果不介意，欢迎使用作者的PolyMarket注册链接（反佣30%，全反） ： https://polymarket.com/?r=xiaochouchou 
## 当前策略概要（主循环）

1. **白名单**：若设置 `PASSIVE_TOKEN_WHITELIST`，则仅以环境变量为准（运行中不随挂单变化）。若未设置，则从当前未成交单提取 `token_id`，并默认每 **120 秒**（`PASSIVE_WHITELIST_REFRESH_SEC`）用未成交单刷新，以便启动后新挂的单可被纳入；设为 `0` 则仅在启动时种子一次。
2. **过滤**：仅管理白名单内订单；若该 `token_id` **已有持仓**（`abs(inventory) > 1e-8`），则**整 token 不处理**（不撤、不改、不进 fill 推断与周期摘要明细）。
3. **调价**：仅 `passive_liquidity/simple_price_policy.py` 中的 **`decide_simple_price`**（粗 tick / 细 tick；见下文）。若该 `token_id`+方向在 **Telegram 保存了自定义规则**，或订单 id 列入 **`PASSIVE_CUSTOM_ORDER_IDS`**，或开启 **`PASSIVE_DEFAULT_CUSTOM_PRICING`**，则进入 **custom** 分支（用 `.env` 的 **`PASSIVE_CUSTOM_*`**；持久化规则优先）。**不再**使用 `AdjustmentEngine`、结构性风控、fill risk、按积分微调、按库存调价等旧逻辑（相关文件仍留在仓库，主循环不调用）。
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

## 自定义调价（Telegram / 环境变量）

在默认粗/细 tick 规则之外，可对**指定订单或整 token+方向**使用固定逻辑（仍由主循环撤单改价，程序不新建首单）。

**生效优先级（同一订单）**

1. 已为该 **`token_id` + `BUY`/`SELL`** 在 Telegram **保存规则** → 使用持久化规则（`custom_pricing_rules.json`，路径可由 **`PASSIVE_CUSTOM_RULES_PATH`** 覆盖；文件已在 `.gitignore` 中忽略）。
2. 否则，若 **`PASSIVE_DEFAULT_CUSTOM_PRICING=true`** → 使用 **`.env` 里 `PASSIVE_CUSTOM_*`** 作为**全局默认**自定义参数（不必再列 `PASSIVE_CUSTOM_ORDER_IDS`）。
3. 否则，若订单 id 在 **`PASSIVE_CUSTOM_ORDER_IDS`**（逗号分隔，与 CLOB 返回 id 完全一致）→ 使用同一套 **`PASSIVE_CUSTOM_*`**。
4. 否则 → 上节**内置**粗/细 tick 策略（非 custom）。

**Telegram 交互**（需 **`TELEGRAM_ENABLED=true`** 且 **`TELEGRAM_COMMANDS_ENABLED`** 未设为关闭；命令由 `telegram_command_poller` 轮询处理）

| 命令 | 作用 |
| --- | --- |
| **`/set_rule <order_id>`** | 对**当前仍挂单**的订单启动多步配置；粗 tick：输入 N、是否允许最优档、`min_candidate_levels` 等；细 tick：safe 区间与 `target_ratio`。 |
| **`/get_rule <order_id>`** | 查看该单对应键下已保存规则摘要。 |
| **`/clear_rule <order_id>`** | 删除该键规则，恢复默认调价。 |
| **`/cancel_rule_setup`** | 取消进行中的配置会话。 |
| **`/input <答案>`** | 与逐步回复等价；在**群组隐私模式**下 Bot 收不到纯文字时，用此命令提交**当前步骤**的答案（**一步一条**，勿把多步写在同一条消息里）。 |

**粗 tick 自定义（`pricing_mode=custom` 且 tick 归为粗）**

- 配置正整数 **N**：只在**当前订单簿同侧、激励带扫描范围内有正深度的价位**中排序（离 mid 最近为第 1 档）；**没有挂单的 tick 价位不计入**。BUY 示例：扫描带可能覆盖到 0.28，但簿上只有 `[0.26,0.27]` 时只在这两档上数 N（SELL 对称）。
- 可选 **不允许挂在最优买/卖价**（与 **`PASSIVE_CUSTOM_COARSE_ALLOW_TOP_OF_BOOK`** 同理）；**`min_candidate_levels`**：上述「簿上价位」个数至少达到该值才允许按 N 调价，否则本轮回合保持。

**细 tick 自定义**

- 在 **`PASSIVE_CUSTOM_FINE_SAFE_MIN`～`PASSIVE_CUSTOM_FINE_SAFE_MAX`** 比例带内保持；否则按 **`PASSIVE_CUSTOM_FINE_TARGET_RATIO`** 在带内收放（与 `simple_price_policy._decide_custom_fine` 一致）。

**环境变量一览（`PASSIVE_CUSTOM_*` 与默认开关）**

当 **`PASSIVE_DEFAULT_CUSTOM_PRICING=true`**，或订单在 **`PASSIVE_CUSTOM_ORDER_IDS`** 中，且**没有** Telegram 持久化规则时，下列参数作为自定义调价；**`/set_rule`** 保存的规则仍优先，且规则内自带一份快照（不再读 env 粗/细项）。

| 变量 | 含义 | 默认（未设置 env 时） |
| --- | --- | --- |
| **`PASSIVE_DEFAULT_CUSTOM_PRICING`** | `true`/`yes`/`1`/`on`：凡无 Telegram 规则的挂单均走 **`PASSIVE_CUSTOM_*`**（内置默认策略关闭）。 | `false` |
| **`PASSIVE_CUSTOM_ORDER_IDS`** | 逗号分隔的 **order id**（与 CLOB 一致）；仅当上一项为 `false` 时，列在此的订单才用自定义调价（无持久化规则时）。 | （空，不启用） |
| **`PASSIVE_CUSTOM_RULES_PATH`** | 规则 JSON 文件路径；空则使用项目目录下 **`custom_pricing_rules.json`**。 | `custom_pricing_rules.json` |
| **`PASSIVE_CUSTOM_COARSE_TICK_OFFSET`** | 粗 tick 档位 **N**（正整数）：在激励带内**订单簿有深度的价位**中从离 mid 最近往外数第 N 个。 | `1` |
| **`PASSIVE_CUSTOM_COARSE_ALLOW_TOP_OF_BOOK`** | 粗 tick 是否允许目标价落在**最优买/卖档**（`true`/`yes`/`1`/`on` 为允许）。 | `true` |
| **`PASSIVE_CUSTOM_COARSE_MIN_CANDIDATES`** | 激励带内**簿上价位**个数下限；不足则本轮回合保持。 | `1` |
| **`PASSIVE_CUSTOM_FINE_SAFE_MIN`** | 细 tick：`distance_ratio` 安全带下界（与 mid、δ 比例相关）。 | `0.4` |
| **`PASSIVE_CUSTOM_FINE_SAFE_MAX`** | 细 tick：安全带上界。 | `0.6` |
| **`PASSIVE_CUSTOM_FINE_TARGET_RATIO`** | 细 tick：带外时趋向的 **目标比例**（0～1）。 | `0.5` |

更细的注释与可选写法见 **`.env.example`**；粗 tick 行为回归见 **`test_simple_price_custom_coarse.py`**。

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
| **CustomPricingRulesStore** | `passive_liquidity/custom_pricing_rules_store.py` | Telegram 规则 JSON 读写（按 `token_id`+方向键） |
| **TelegramRuleSetup** | `passive_liquidity/telegram_rule_setup.py` | `/set_rule` 等多步 FSM 与 `/get_rule`、`/clear_rule` |
| **TelegramCommandPoller** | `passive_liquidity/telegram_command_poller.py` | 命令与 `/input`；`telegram_live_queries` 提供 `/status`、`/orders`、`/pnl` |
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

2. 在 `.env` 中至少填写 **`PRIVATE_KEY`（或 `POLYMARKET_PRIVATE_KEY`）** 与 **`POLYMARKET_FUNDER`**。其余变量以仓库根目录 **`.env.example`** 为准：示例已按功能分块注释（**被动监控告警**、**订单成交通知**、**自定义调价**等），可按需删减行，不必与示例逐行一致。

`.env` 已在 `.gitignore` 中忽略。

### 与主循环强相关（`PASSIVE_*`）

完整列表与默认值见 `passive_liquidity/config_manager.py` → `PassiveConfig.from_env()`。常用项：

| 变量 | 含义 |
| --- | --- |
| **`PASSIVE_LOOP_INTERVAL`** | 主循环休眠间隔（秒） |
| **`PASSIVE_TOKEN_WHITELIST`** | 逗号分隔 `token_id`；留空则按未成交单维护白名单（见下行） |
| **`PASSIVE_WHITELIST_REFRESH_SEC`** | 未设环境白名单时，每隔多少秒用未成交单重算白名单（默认 `120`）；`0` = 仅启动时种子 |
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
| **`TELEGRAM_COMMANDS_ENABLED`** | 未设为 `off`/`0`/`false` 时轮询处理 `/set_rule`、`/input`、`/status` 等（见 `telegram_command_poller.py`） |

**两类「成交」相关推送（不要混用开关）**

| 变量 | 含义 |
| --- | --- |
| **`PASSIVE_ALERT_MONITORING`** | `false` 时关闭的是盘口 **成交活跃度 / 被吃风险** 与 **深度占比** 等**被动监控**告警，**不是**「你自己的订单成交了」。 |
| **`PASSIVE_TELEGRAM_NOTIFY_FILL`** | 是否在 CLOB 上推断到**订单部分/全部成交**时发 Telegram（`PASSIVE_TELEGRAM_NOTIFY_PARTIAL_FILL` / **`PASSIVE_TELEGRAM_NOTIFY_FULL_FILL`** 可再细分）。 |

若只关监控却仍收到「订单成交」类消息，请将 **`PASSIVE_TELEGRAM_NOTIFY_FILL=false`**（或按需关 partial/full）。启动时若监控已关而成交通知仍开，日志会给出 **WARNING** 提示。

**群组 → 超级群**：Telegram 会更换 `chat_id`。若发消息报 `group chat was upgraded to a supergroup`，请按接口返回的 **`migrate_to_chat_id`** 更新 **`TELEGRAM_CHAT_ID`** 并重启（否则推送与 `/status` 等命令都不会进新群）。

**资金快照**：周期摘要里总额与可用均为 **CLOB API collateral**。**Telegram `/status`、`/pnl`** 中的「组合总额」为 **CLOB 抵押 USDC + Data API 持仓 `currentValue` 合计**（与前端「组合」口径更接近；若 positions 拉取失败则仅显示 CLOB）。未成交买单占用单独**估算展示**。改价挂单失败、撤单失败等会发**中文运营警告**（含余额不足等常见错误的简要说明）。

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

### 使用 tmux（SSH 断开后仍运行）

在远程服务器上若直接在前台运行，**SSH 断开或终端关闭**时，Shell 会向子进程发信号，进程通常会退出。用 **tmux**（或 screen）把程序跑在**持久会话**里，断线后程序继续跑，下次 SSH 再连上去即可查看日志或停止。

**安装**（若系统没有 `tmux`）：

```bash
sudo apt update && sudo apt install -y tmux
```

**典型用法**：

1. 登录 SSH 后进入项目目录并激活虚拟环境（与上文一致）。
2. 新建一个命名会话（名称可自定，例如 `poly`）：

```bash
tmux new -s poly
```

3. 在 tmux 窗口里启动程序，例如：

```bash
cd polymarket_lp_tool
source .venv/bin/activate
python run_passive_bot.py
```

4. **断开会话但保持程序运行**：按 **`Ctrl+b`**，松开后按 **`d`**（detach）。此时可安全关闭 SSH；程序在服务器上继续执行。
5. **再次 SSH 登录后接回会话**：

```bash
tmux attach -t poly
```

若忘记会话名，可先列出：

```bash
tmux ls
```

再接回：`tmux attach -t <会话名>`。在 tmux 内用 **`Ctrl+c`** 可停止程序；退出 tmux 窗口可输入 `exit` 或按 **`Ctrl+d`**。

## 免责声明

版本属于 `@臭臭Panda`。非官方产品；不保证激励计分或盈亏。请自行遵守服务条款与所在地法规（含 [地理限制](https://docs.polymarket.com/api-reference/geoblock)）。实盘前请小额测试。
