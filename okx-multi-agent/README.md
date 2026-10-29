# OKX 多代理模拟盘交易系统（MA-1.2.2）

参考推文里「六个代理分工」的思路，交易对象默认是 **市值靠前的 OKX USDT 永续**。股票永续可以打开，但默认关闭。策略是快进快出：止损加宽到能盖住手续费，止盈靠近，浮盈锁定，拿太久就市价走。**只跑模拟盘**：每个请求都带 `x-simulated-trading: 1`，代码里写死 `SIMULATED_TRADING = "1"`，改不成实盘。API 密钥只从环境变量读取。

> ⚠️ 冒烟测试（`--smoke`）用的是本地随机行情，盈亏数字**没有任何参考价值**，只用来确认程序能跑通。冒烟不访问 CoinGecko，也不访问 OKX。

## 交易池怎么来

启动时组建一次，之后按 `UNIVERSE_REFRESH_SEC`（默认 3600 秒）刷新。设为 `0` 则只在启动时建，运行中不刷新。明细写在 `data/universe.json`。

### 加密货币：市值前 N（默认 30）

1. 读 CoinGecko 公开接口 `GET /api/v3/coins/markets?vs_currency=usd&order=market_cap_desc`（最多 3 页）。
2. 读 OKX `GET /api/v5/public/instruments?instType=SWAP`。
3. 按市值从高到低，把币种符号映射成 `{SYMBOL}-USDT-SWAP`，并且这条合约必须同时满足：`state=live`、`settleCcy=USDT`、`ctType=linear`、`instCategory=1`（官方文档里 1 = Crypto）。映射不上的跳过，继续往后找，直到凑满 `CRYPTO_TOP_N`。
4. 稳定币（USDT、USDC、DAI、USDE 等，或名字里带 stablecoin）直接跳过，不占名额。
5. 个别更名只在「主合约不在 live 列表、别名合约在」时才用，不会编造代码：`MATIC→POL`、`RNDR→RENDER`、`TON→GRAM`。
6. 市值接口失败，或映射不足 N 个：用代码里一份 2026-10-28 的离线回退列表（约 30 个当时真实存在的 USDT 永续），再和当前 live 集合取交集。

### 股票 / 美股相关永续

OKX **没有**单独的美股 `instType`，instruments 响应里也 **没有** 国家或市场字段。股票合约仍然是 `SWAP`，`instId` 形如 `AAPL-USDT-SWAP`。

**默认 `ENABLE_US_STOCKS=0`，不交易股票永续。** 模拟盘上这类合约点差宽，报价也容易滞后（例如 AMD、EWJ）。设成 `1` 才按下面的规则全部纳入：

| 字段 | 要求 |
|---|---|
| `instType` | `SWAP`（请求参数） |
| `instCategory` | `"3"`（官方文档：Stocks；1 是加密，4 是商品，例如 XAU） |
| `state` | `live` |
| `settleCcy` | `USDT` |
| `ctType` | `linear`（空值也接受，币本位会被 settleCcy 挡掉） |
| `instId` | 以 `-USDT-SWAP` 结尾 |

官方帮助中心（股票永续）写明这一产品线同时包含 **美股、韩股、日股、港股**，以及少量 `ruleType=pre_market` 的名字（例如接口里出现过的 OPENAI）。因为没有地区字段，程序纳入该类别下 **全部** live 的 USDT 线性永续，而不是手写一份美股代码。商品、外汇、债券不在里面。

打开之后，点差、资金费、ATR、滚动亏损这些闸门对股票和加密一样生效。休市时段点差往往更宽，过不了 `MAX_SPREAD_BPS` 就不会开仓。

### 调试强制列表

设置 `SYMBOLS=BTC-USDT-SWAP,ETH-USDT-SWAP` 后，跳过市值排名和股票发现，只交易这几个。留空或不设才走自动交易池。

## 从 Dawn 一类机器人里借什么

只借能在 OKX 永续上站得住的纪律，不借 meme 新币那套。

借来的：

- 每笔活仓都挂着固定止损和止盈。监控里会定期核对，缺了就补；止损放不进标记价正确一侧就平掉。
- 进场前看流动性：24h 成交额、点差、标记价和指数的偏离。双边成本和计划止损比太高（成本吃掉太多 R）就跳过。
- 不追尖峰。信号收盘后价格又朝下单方向跑超过 `CHASE_LIMIT_PCT`（默认 0.15%）就不开。成交价比计划差超过 0.1% 就平掉、不记账。
- 平仓之后，同一合约要等一根新的趋势周期 K 线收盘才能再开，避免在同一段噪音里反复进。

明确不做：跟单钱包、Fomo、pump.fun、Telegram 交互、新币狙击、随机代币的 rug 检查、拉新返佣、把止盈放到 +100%～+500%。股票永续仍然默认关闭。快进快出（1R、跟踪止损、最长持仓）保持不变。

## 成本和记账（MA-1.2.2）

Demo 里多数单子是拿满 15 分钟才平，来回成本大约吃掉计划止损的一大截；滑点撤单写了「不记账」，账上的盈亏比权益少了一截；还有一笔记成 +0.00。

- 计划止损至少要盖住来回成本的 `MIN_EDGE_FEE_MULT` 倍（默认 **3.5**）。成本按 maker 进、taker 出，再加上点差和滑点。不够就加宽止损和 1R 止盈，加到 `MAX_SL_PCT` 仍不够就跳过。仓位仍按每笔风险预算算，止损变宽张数变少，不会因此加仓。
- 新开仓用 **post-only** 挂在己方盘口。`MAKER_ENTRY_TIMEOUT_SEC`（默认 25 秒）还没成交就撤单，不改市价去追。
- 滑点超阈而平掉的真实成交要写入账本、计入滚动 24 小时亏损，并走和正常平仓一样的冷却、等新 15m K 线。
- 交易所回报的盈亏如果是 0，但开平价明显不同，按成交价估算，不再记一笔莫名的 +0.00。

## 快进快出

默认不再干等 2R。止损用 `ATR_SL_MULT=2.5`、最短 `MIN_SL_PCT=0.004`，让双边手续费不要占掉三分之一个 R。交易所止盈是 `RISK_REWARD=1`（1R）。净利在扣掉费用后达到 `max(0.3U, 名义×0.15%)` 并连续确认 2 次就落袋。浮盈走到止损距离的 0.4 倍之后，止损上移，锁定大约 60% 的浮盈；新止损如果会落到标记价错误的一侧（OKX 51302/51304），这次就不改。持仓超过 `MAX_HOLD_SEC`（默认 900 秒）还没走，市价平。

信号用 **5m**，方向用 **15m**。ADX 低于 `MIN_ADX`（28）直接放弃，分数默认要到 7。

## 再入场和同向拥挤

- `SYMBOL_COOLDOWN_SEC=900`：这个合约亏损平仓后 15 分钟不再开。
- `LOSS_COOLDOWN_SEC=1800`、`MAX_CONSECUTIVE_LOSSES=3`：连亏 3 笔，全局停 30 分钟。
- 秒数设为 0 才是关闭。0 也不会去读 `state.json` 里还没到期的旧冷却。
- 同一合约、同一方向，要等一根新的 `REENTRY_BAR` K 线收盘（至少 5m）才能再开。任何平仓之后，还要等一根新的趋势周期 K 线收盘。
- 同一方向的加密持仓最多 `MAX_SAME_DIR_CRYPTO=2`。股票不计入这个上限。

`MAX_POSITIONS` 仍默认 0，表示不另设总个数上限。同一 `instId` 还是只有一个方向。每一笔止损亏损不超过权益的 `RISK_PER_TRADE_PCT`（0.25%），保证金不超过 `MAX_MARGIN_PCT`（5%）。

亏损刹车按 **滚动 24 小时已实现盈亏**（`ROLLING_LOSS_WINDOW_SEC`），不在北京时间 0 点清零。点差、资金费、波动、止盈覆盖 3 倍双边成本这些闸门都还在。

## 每个周期监控全部持仓

Equity 和 Net-TP 遍历交易所 `positions` 返回的 **每一个** 未平仓 SWAP，而不是只看最后开的那一笔。某一仓询价或记账失败会写下 `monitor_error` / `adopt_error`，本轮继续处理剩下的仓，下一轮再试。

交易池可能有几十个加密加一两百个股票永续。扫描按 `SCAN_BATCH`（默认 8）分批，批与批之间再监控一遍全部持仓。OKX 的 GET（行情和账户）之间默认间隔 `PUBLIC_REQ_GAP_SEC`（0.12 秒），避免限速。所以一整轮扫描会比以前只扫 3 个币更长；持仓检查不会为了赶扫描而跳过。

每个持仓至少在出现时和之后大约每 60 秒有一条 `net_pnl` 日志；持仓集合变化时另有 `monitor_all` / `positions`。

## 六个代理各管什么

| 代理 | 文件 | 职责 |
|---|---|---|
| 1. 扫描 Scanner | `agents/scanner.py` | 拉 5m 信号 + 15m 方向。大小周期 EMA 同向是硬条件，ADX 不够直接放弃，再加 DI、放量、突破打分（满分 8，默认 ≥7） |
| 2. 风控 Risk | `agents/risk.py` | 滚动 24 小时亏损上限 2%、连亏 3 次冷却 30 分钟、单币亏损冷却 15 分钟、同向加密最多 2 个、同向再入场要等新的 5m K 线、同币已有仓、点差、资金费、波动、止盈距离覆盖 3 倍双边成本 |
| 3. 仓位 Position | `agents/position.py` | 按权益百分比定仓：止损亏损（含双边成本）≤ 权益 0.25%；保证金 ≤ 权益 5%；杠杆默认 5x，上限 10x |
| 4. 执行 Executor | `agents/executor.py` | 市价开仓。`accFillSz` 要真正大于 0 才记账（字符串 `"0"` 不算成交）。成交价偏离计划超过 0.1% 就平掉。止损止盈按真实成交价重算。接管已有仓时补挂止损和止盈，挂不出有效止损就平仓 |
| 5. 日志 Logger | `agents/journal.py` | 每个决策和原因写入 `data/decisions.jsonl` 和 `data/bot.log` |
| 6. 净值 Equity | `agents/equity.py` | 发现某个仓位从交易所消失后，取实际已实现盈亏，写 `trades.csv`、`equity.csv`、`equity.json`，并通知风控更新连亏/冷却。重启后会接管已有仓位 |
| + 净利止盈 Net-TP | `agents/net_tp.py` | 每个持仓都算净利。连续 2 次超过 `max(0.3U, 名义×0.15%)` 就平。浮盈够一段就把止损抬到锁定大部分利润。超过 `MAX_HOLD_SEC` 市价平 |
| 交易池 | `universe.py` | 启动和定时刷新时组建 `config.SYMBOLS` |

调度器是 `main.py`：持仓监控大约每 `MONITOR_INTERVAL_SEC`（默认 2 秒）一轮；扫描大约每 `SCAN_INTERVAL_SEC`（默认 20 秒）开始一轮，一轮内部按批穿插监控。

## 文件结构

```
okx-multi-agent/
├── main.py            入口 / 调度器
├── config.py          全部参数（保守默认，可用环境变量覆盖）
├── universe.py        市值前 N + 股票类永续；离线回退
├── okx_client.py      OKX v5 精简封装 + 离线 Mock
├── agents/
│   ├── scanner.py  risk.py  position.py  executor.py  journal.py  equity.py
│   ├── net_tp.py      净利止盈 + 保本（逐仓）
│   ├── fees.py        账户手续费率
│   ├── indicators.py  EMA / ATR / ADX
│   └── state.py       连亏、冷却、当日盈亏，落盘 data/state.json
├── requirements.txt
├── .env.example
└── README.md
```

## 怎么跑

需要 Python 3.9+。

```bash
cd okx-multi-agent
pip install -r requirements.txt

# 1) 先离线试跑，不需要密钥，也不联网
python main.py --smoke

# 2) 填模拟盘密钥
cp .env.example .env      # 编辑 .env 填三项；或者直接 export 环境变量
#   OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE
#   必须是 OKX「模拟交易」里创建的 API，实盘密钥会报 50101/50111

# 3) 单轮检查（连上账户、看一轮信号，有合格信号会在模拟盘下单）
python main.py --once

# 4) 正式跑
python main.py                              # 一直跑，Ctrl+C 停止
python main.py --hours 6 --stop-after-flat  # 跑 6 小时，到时后等最后一笔平仓再退出
```

运行结果都在 `data/`：`bot.log`、`decisions.jsonl`、`universe.json`、`trades.csv`、`equity.csv`、`equity.json`、`state.json`。

缺密钥时 `python main.py` 会直接提示缺哪几项并退出；密钥错误会在启动时报 `auth_fail` 并退出，不会空转。

## 常用参数（环境变量或 .env）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SIGNAL_BAR` | 5m | 信号 K 线周期 |
| `TREND_BAR` | 15m | 大周期方向过滤 |
| `MIN_SIGNAL_SCORE` | 7 | 满分 8 |
| `MIN_ADX` | 28 | 低于此不做 |
| `ATR_SL_MULT` | 2.5 | 止损 = ATR × 倍数 |
| `MIN_SL_PCT` | 0.004 | 止损距离下限 |
| `RISK_REWARD` | 1 | 交易所止盈倍数，不再默认 2R |
| `MAX_HOLD_SEC` | 900 | 最长持仓秒数，超时市价平；0 = 不限 |
| `MAX_FILL_SLIP_PCT` | 0.001 | 成交价偏离计划超过这个比例就撤掉 |
| `CHASE_LIMIT_PCT` | 0.0015 | 相对信号收盘追价超过这个比例就不开 |
| `MIN_VOL_USDT_24H` | 10000000 | 24h 成交额下限（USDT） |
| `MAX_MARK_INDEX_BPS` | 50 | 标记价偏离指数的上限 |
| `MAX_COST_TO_R` | 0.30 | 双边成本 / 止损距离上限；0 = 不检查 |
| `MIN_EDGE_FEE_MULT` | 3.5 | 止损距离至少是来回成本的这么多倍，不够就加宽 |
| `MAKER_ENTRY_TIMEOUT_SEC` | 25 | post-only 未成交则撤单的秒数 |
| `PROTECT_AUDIT_SEC` | 30 | 核对持仓是否仍有止损和止盈的间隔 |
| `REENTRY_BAR` | 5m | 同向再入场至少等这一根 K 线收盘 |
| `MAX_SAME_DIR_CRYPTO` | 2 | 同一方向加密持仓上限；0 = 不限制 |
| `LOSS_COOLDOWN_SEC` | 1800 | 连亏后的全局冷却；0 = 关闭 |
| `SYMBOL_COOLDOWN_SEC` | 900 | 单币亏损后的冷却；0 = 关闭 |
| `MAX_CONSECUTIVE_LOSSES` | 3 | 触发全局冷却的连亏次数 |
| `ROLLING_LOSS_WINDOW_SEC` | 86400 | 亏损上限的回看秒数，不按自然日清零 |
| `ENABLE_US_STOCKS` | 0 | 1 才纳入 OKX 股票类永续 |
| `SYMBOLS` | 空 | 非空则强制只交易这些 instId，跳过自动交易池 |
| `CRYPTO_TOP_N` | 30 | 按市值映射的 USDT 永续个数 |
| `UNIVERSE_REFRESH_SEC` | 3600 | 交易池刷新间隔；0 表示只在启动时组建 |
| `SCAN_BATCH` | 8 | 每批扫描的合约数，批间监控全部持仓 |
| `PUBLIC_REQ_GAP_SEC` | 0.12 | 两次 GET 的最小间隔 |
| `LEVERAGE` | 5 | 上限 10 |
| `MAX_POSITIONS` | 0 | 同时持仓个数；0 或不大于 0 = 不限制 |
| `RISK_PER_TRADE_PCT` | 0.0025 | 每笔止损最多亏权益的比例 |
| `MAX_MARGIN_PCT` | 0.05 | 单笔保证金不超过权益的比例 |
| `DAILY_LOSS_CAP_PCT` | 0.02 | 当日亏损上限 |
| `NET_TP_MIN_USDT` / `NET_TP_MIN_PCT` | 0.5 / 0.002 | 净利止盈门槛 |
| `ENABLE_NET_TP` | true | 关掉则只靠交易所止盈止损 |
| `MAX_SPREAD_BPS` | 6 | 开仓点差上限 |

## GitHub Actions

仓库里另有工作流 **OKX Multi-Agent Demo**（`.github/workflows/multi-agent.yml`），和旧的 `demo.yml`（根目录 `bot.py`）分开，互不取消。

1. 密钥放在仓库 Settings → Secrets and variables → Actions，名称与旧机器人相同：`OKX_API_KEY`、`OKX_SECRET_KEY`、`OKX_PASSPHRASE`。不要写进仓库。
2. Actions → **OKX Multi-Agent Demo** → **Run workflow**。
3. 默认 `hours=5.8`、`once=false`，执行 `python -u main.py --hours 5.8 --stop-after-flat`。单个 job 大约 6 小时会被平台取消，5.8 小时是为了平仓后正常退出，并上传 `data/` 产物（保留约 7 天）。
4. 把 `once` 设为 true 则只跑 `python -u main.py --once`（一轮扫描）。工作流里的离线冒烟不联网。

这是模拟盘演示。私有仓库免费 Actions 分钟有限，长期跑更适合自己的服务器。交易池变大之后，一轮扫描会更久，5.8 小时里真正下单的次数取决于信号，而不是把每个股票合约都扫到就一定开仓。

## 设计取舍

- **只做趋势单**：大小周期方向不一致直接不做，震荡市基本空仓。
- **手续费按双边算**：风控要求止盈距离 ≥ 双边成本 3 倍；仓位计算把双边手续费算进止损亏损；Net-TP 净利同样扣双边。
- **交易所止损永远在**：开仓附带止损 → 核验 → 补挂 → 失败则平仓，不留裸仓。
- **下单不在网络错误时重试**（避免重复下单），只在限流/系统繁忙时退避重试；每笔订单带 `clOrdId`。GET 另有固定间隔，避免交易池和持仓变多时打满限速。
- **重启安全**：发现已有仓位会接管，并补挂止损止盈。连亏、滚动盈亏、冷却和上一笔入场 K 线时间存在 `state.json`。冷却秒数为 0 时，旧的到期时间不再生效。
- **不成交就不记账**：`accFillSz` 必须大于 0。0 张不会写入 `trade_closed`。
- 默认单向持仓（net_mode）；如果账户是双向持仓会自动识别并带 `posSide`。同一 instId 即使账户允许双向，本策略也不开对锁。
- **股票地区**：OKX 把美股和韩/日/港股放在同一个 `instCategory=3`。要排除非美股，用 `SYMBOLS` 自己指定，不要指望程序猜代码。
