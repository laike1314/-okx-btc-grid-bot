# OKX 多代理模拟盘交易系统（MA-1.1.0）

参考推文里「六个代理分工」的思路，交易对象是 **OKX USDT 保证金永续**：市值靠前的加密货币，再加上 OKX 列出的股票类永续（美股相关合约在这一类里）。**只跑模拟盘**：每个请求都带 `x-simulated-trading: 1`，代码里写死 `SIMULATED_TRADING = "1"`，改不成实盘。API 密钥只从环境变量读取。

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

判定规则（`ENABLE_US_STOCKS=1` 时全部纳入）：

| 字段 | 要求 |
|---|---|
| `instType` | `SWAP`（请求参数） |
| `instCategory` | `"3"`（官方文档：Stocks；1 是加密，4 是商品，例如 XAU） |
| `state` | `live` |
| `settleCcy` | `USDT` |
| `ctType` | `linear`（空值也接受，币本位会被 settleCcy 挡掉） |
| `instId` | 以 `-USDT-SWAP` 结尾 |

官方帮助中心（股票永续）写明这一产品线同时包含 **美股、韩股、日股、港股**，以及少量 `ruleType=pre_market` 的名字（例如接口里出现过的 OPENAI）。因为没有地区字段，程序纳入该类别下 **全部** live 的 USDT 线性永续，而不是手写一份美股代码。商品、外汇、债券不在里面。

点差、资金费、ATR、当日亏损这些闸门对股票和加密一样生效。休市时段点差往往更宽，过不了 `MAX_SPREAD_BPS` 就不会开仓。

### 调试强制列表

设置 `SYMBOLS=BTC-USDT-SWAP,ETH-USDT-SWAP` 后，跳过市值排名和股票发现，只交易这几个。留空或不设才走自动交易池。

## 不限制同时持仓个数

`MAX_POSITIONS` 默认 **0 = 不限制**。别的币已经有仓，不再成为拒绝理由。通过 Scanner 分数和 Risk 闸门的合约都可以开。

仍保留的约束：

- **同一 `instId` 只有一个方向**。已有多或空都不再开，不加仓，也不反向对锁。
- **每一笔单独算仓**。止损时的亏损（含双边手续费和滑点）不超过权益的 `RISK_PER_TRADE_PCT`（默认 0.25%），保证金不超过权益的 `MAX_MARGIN_PCT`（默认 5%）。
- 当日亏损上限、连亏冷却、单币冷却、点差、资金费、波动上下限、止盈距离覆盖双边成本，这些一票否决都还在。

笔数不设上限时，多笔保证金会相加。账户级刹车是当日亏损上限，不是「已经有一个仓了」。

## 每个周期监控全部持仓

Equity 和 Net-TP 遍历交易所 `positions` 返回的 **每一个** 未平仓 SWAP，而不是只看最后开的那一笔。某一仓询价或记账失败会写下 `monitor_error` / `adopt_error`，本轮继续处理剩下的仓，下一轮再试。

交易池可能有几十个加密加一两百个股票永续。扫描按 `SCAN_BATCH`（默认 8）分批，批与批之间再监控一遍全部持仓。OKX 的 GET（行情和账户）之间默认间隔 `PUBLIC_REQ_GAP_SEC`（0.12 秒），避免限速。所以一整轮扫描会比以前只扫 3 个币更长；持仓检查不会为了赶扫描而跳过。

每个持仓至少在出现时和之后大约每 60 秒有一条 `net_pnl` 日志；持仓集合变化时另有 `monitor_all` / `positions`。

## 六个代理各管什么

| 代理 | 文件 | 职责 |
|---|---|---|
| 1. 扫描 Scanner | `agents/scanner.py` | 对交易池里每个还没有持仓的合约拉 5m + 1H K 线。大小周期 EMA 同向是硬条件，再加 ADX、DI、放量、突破前高/前低打分（满分 8，默认 ≥6 才算候选） |
| 2. 风控 Risk | `agents/risk.py` | 一票否决：当日亏损上限 2%、连亏 2 次全局冷却 30 分钟、单币止损后冷却 4 小时、同币已有仓、`MAX_POSITIONS>0` 时的个数上限、点差、资金费率、波动过大/过小、止盈距离不够覆盖双边手续费+滑点的 3 倍 |
| 3. 仓位 Position | `agents/position.py` | 按权益百分比定仓：止损亏损（含双边成本）≤ 权益 0.25%；保证金 ≤ 权益 5%；杠杆默认 5x，上限 10x |
| 4. 执行 Executor | `agents/executor.py` | 市价开仓并附带交易所止损（标记价格触发）+ 止盈；开仓后核验止损，不在就补挂，补挂失败立刻平仓；平仓先 IOC，再市价兜底 |
| 5. 日志 Logger | `agents/journal.py` | 每个决策和原因写入 `data/decisions.jsonl` 和 `data/bot.log` |
| 6. 净值 Equity | `agents/equity.py` | 发现某个仓位从交易所消失后，取实际已实现盈亏，写 `trades.csv`、`equity.csv`、`equity.json`，并通知风控更新连亏/冷却。重启后会接管已有仓位 |
| + 净利止盈 Net-TP | `agents/net_tp.py` | 对每个持仓算净利 = 浮盈 − 开仓费 − 预估平仓费 − 资金费 − 滑点。连续 3 次超过 `max(0.5U, 名义×0.2%)` 就平仓。浮盈走到止盈距离一半时，止损移到开仓价+双边成本 |
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
| `SYMBOLS` | 空 | 非空则强制只交易这些 instId，跳过自动交易池 |
| `CRYPTO_TOP_N` | 30 | 按市值映射的 USDT 永续个数 |
| `ENABLE_US_STOCKS` | 1 | 是否纳入 instCategory=3 的全部 live USDT 线性永续 |
| `UNIVERSE_REFRESH_SEC` | 3600 | 交易池刷新间隔；0 表示只在启动时组建 |
| `SCAN_BATCH` | 8 | 每批扫描的合约数，批间监控全部持仓 |
| `PUBLIC_REQ_GAP_SEC` | 0.12 | 两次 GET 的最小间隔 |
| `LEVERAGE` | 5 | 上限 10 |
| `MAX_POSITIONS` | 0 | 同时持仓个数；0 或不大于 0 = 不限制 |
| `RISK_PER_TRADE_PCT` | 0.0025 | 每笔止损最多亏权益的比例 |
| `MAX_MARGIN_PCT` | 0.05 | 单笔保证金不超过权益的比例 |
| `DAILY_LOSS_CAP_PCT` | 0.02 | 当日亏损上限 |
| `MIN_SIGNAL_SCORE` | 6 | 越高越严格、单越少 |
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
- **重启安全**：发现已有仓位会接管监控；冷却、连亏、当日盈亏存在 `state.json`。
- 默认单向持仓（net_mode）；如果账户是双向持仓会自动识别并带 `posSide`。同一 instId 即使账户允许双向，本策略也不开对锁。
- **股票地区**：OKX 把美股和韩/日/港股放在同一个 `instCategory=3`。要排除非美股，用 `SYMBOLS` 自己指定，不要指望程序猜代码。
