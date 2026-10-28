# OKX 多代理模拟盘交易系统（MA-1.0.0）

参考推文里「六个代理分工」的思路，但交易对象换成 **OKX 永续合约（BTC / ETH / SOL）**，**只跑模拟盘**：
每个请求都带 `x-simulated-trading: 1`，代码里写死 `SIMULATED_TRADING = "1"`，改不成实盘。

> ⚠️ 冒烟测试（`--smoke`）用的是本地随机行情，盈亏数字**没有任何参考价值**，只用来确认程序能跑通。

## 六个代理各管什么

| 代理 | 文件 | 职责 |
|---|---|---|
| 1. 扫描 Scanner | `agents/scanner.py` | 拉 5m + 1H K 线，大小周期 EMA 同向是硬条件，再加 ADX 趋势强度、DI 方向、放量异动、突破前高/前低打分（满分 8，默认 ≥6 才算候选） |
| 2. 风控 Risk（对应推文的 Rug 检测） | `agents/risk.py` | 一票否决：当日亏损上限 2%、连亏 2 次全局冷却 30 分钟、单币止损后冷却 4 小时、最多持仓数、点差过宽、资金费率极端、波动过大/过小、止盈距离不够覆盖**双边**手续费+滑点的 3 倍 |
| 3. 仓位 Position | `agents/position.py` | 按权益百分比定仓位：止损时的亏损（含双边手续费+滑点）≤ 权益 0.25%；保证金 ≤ 权益 5%；杠杆默认 5x，上限 10x |
| 4. 执行 Executor | `agents/executor.py` | 市价开仓并附带交易所止损（标记价格触发）+ 止盈；开仓后核验止损在不在，不在就补挂，补挂失败立刻平仓；平仓先 IOC 吃对手价，没成交完再市价兜底；处理 51023 / 51304 / 51088 和限流 |
| 5. 日志 Logger | `agents/journal.py` | 每一个决策和原因都写进 `data/decisions.jsonl`（机器可读）和 `data/bot.log`（人看的） |
| 6. 净值 Equity | `agents/equity.py` | 发现仓位平掉后，从交易所取**实际**已实现盈亏（已含手续费和资金费），写 `trades.csv`、`equity.csv`、`equity.json`（胜率、盈亏比 PF、平均盈亏、最大回撤），并通知风控更新连亏/冷却 |
| + 净利止盈 Net-TP | `agents/net_tp.py` | 每 2 秒算：净利 = 浮盈 − 开仓手续费(实际成交) − 平仓手续费(按 taker) − 已付资金费 − (滑点 + 半个点差)；连续 3 次超过 `max(0.5U, 名义价值×0.2%)` 就平仓落袋。浮盈走到止盈距离一半时，交易所止损移到「开仓价 + 双边成本」 |

调度器是 `main.py`：每 2 秒 Equity 同步持仓 → Net-TP 检查；每 20 秒跑一轮 Scanner → Risk → Position → Executor。

## 文件结构

```
okx-multi-agent/
├── main.py            入口 / 调度器
├── config.py          全部参数（保守默认，可用环境变量覆盖）
├── okx_client.py      OKX v5 精简封装 + 离线 Mock
├── agents/
│   ├── scanner.py  risk.py  position.py  executor.py  journal.py  equity.py
│   ├── net_tp.py      净利止盈 + 保本
│   ├── fees.py        账户手续费率
│   ├── indicators.py  EMA / ATR / ADX
│   └── state.py       连亏、冷却、当日盈亏，落盘 data/state.json（重启不丢）
├── requirements.txt
├── .env.example
└── README.md
```

## 怎么跑

需要 Python 3.9+。

```bash
cd okx-multi-agent
pip install -r requirements.txt

# 1) 先离线试跑，不需要密钥
python main.py --smoke

# 2) 填模拟盘密钥
cp .env.example .env      # 编辑 .env 填三项；或者直接 export 环境变量
#   OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE
#   必须是 OKX「模拟交易」里创建的 API，实盘密钥会报 50101/50111

# 3) 单轮检查（连上账户、看一轮信号，有合格信号会真的在模拟盘下单）
python main.py --once

# 4) 正式跑
python main.py                              # 一直跑，Ctrl+C 停止
python main.py --hours 6 --stop-after-flat  # 跑 6 小时，到时后等最后一笔平仓再退出
```

运行结果都在 `data/`：`bot.log`、`decisions.jsonl`、`trades.csv`、`equity.csv`、`equity.json`、`state.json`。

缺密钥时 `python main.py` 会直接提示缺哪几项并退出；密钥错误会在启动时报 `auth_fail` 并退出，不会空转。

## 常用参数（环境变量或 .env）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SYMBOLS` | BTC,ETH,SOL 永续 | 逗号分隔 |
| `LEVERAGE` | 5 | 上限 10 |
| `MAX_POSITIONS` | 1 | 同时最多持仓数 |
| `RISK_PER_TRADE_PCT` | 0.0025 | 每笔止损最多亏权益的比例 |
| `DAILY_LOSS_CAP_PCT` | 0.02 | 当日亏损上限 |
| `MIN_SIGNAL_SCORE` | 6 | 越高越严格、单越少 |
| `NET_TP_MIN_USDT` / `NET_TP_MIN_PCT` | 0.5 / 0.002 | 净利止盈门槛 |
| `ENABLE_NET_TP` | true | 关掉则只靠交易所止盈止损 |
| `MAX_SPREAD_BPS` | 6 | 模拟盘 ETH 点差常见约 4bps |

## GitHub Actions

仓库里另有工作流 **OKX Multi-Agent Demo**（`.github/workflows/multi-agent.yml`），和旧的 `demo.yml`（根目录 `bot.py`）分开，互不取消。

1. 密钥放在仓库 Settings → Secrets and variables → Actions，名称与旧机器人相同：`OKX_API_KEY`、`OKX_SECRET_KEY`、`OKX_PASSPHRASE`。不要写进仓库。
2. Actions → **OKX Multi-Agent Demo** → **Run workflow**。
3. 默认 `hours=5.8`、`once=false`，执行 `python -u main.py --hours 5.8 --stop-after-flat`。单个 job 大约 6 小时会被平台取消，5.8 小时是为了平仓后正常退出，并上传 `data/` 产物（保留约 7 天）。
4. 把 `once` 设为 true 则只跑 `python -u main.py --once`（一轮扫描）。

这是模拟盘演示。私有仓库免费 Actions 分钟有限，长期跑更适合自己的服务器。

## 设计取舍

- **只做趋势单**：大小周期方向不一致直接不做，震荡市基本空仓。
- **手续费按双边算**：风控要求止盈距离 ≥ 双边成本 3 倍；仓位计算把双边手续费算进止损亏损；Net-TP 净利同样扣双边。
- **交易所止损永远在**：开仓附带止损 → 核验 → 补挂 → 失败则平仓，不留裸仓。
- **下单不在网络错误时重试**（避免重复下单），只在限流/系统繁忙时退避重试；每笔订单带 `clOrdId`。
- **重启安全**：发现已有仓位会接管监控；冷却、连亏、当日盈亏存在 `state.json`。
- 默认单向持仓（net_mode）；如果账户是双向持仓会自动识别并带 `posSide`。
