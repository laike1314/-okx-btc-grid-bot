# OKX 永续合约自动交易机器人（模拟盘）

**版本：V2.9.2**（优化版）

基于 EMA + RSI + MACD + ATR 的多周期趋势策略，运行在 OKX **模拟盘**，支持自动扫描 CoinGecko 市值 Top30 币种，动态杠杆，OCO 止盈止损，连续亏损保护。

> ⚠️ **仅用于模拟盘测试学习，实盘请自行充分回测并承担风险。**

---

## 功能特点

- 自动扫描 CoinGecko 市值 Top30 + OKX 24h 成交量过滤
- 多周期确认：5m 信号 + 15m / 1H 趋势
- 动态杠杆 5x ~ 20x（根据信号强度 + 合约最大杠杆限制）
- 基于 ATR 与目标 ROI 的智能止损止盈
- OCO 订单保护 + 验证 + 失败紧急平仓
- 连续亏损冷却保护
- 启动时自动接管已有仓位并补全 TP/SL
- 同时最多持有 1 个仓位
- UTC+8 日志

---

## 快速开始

### 1. 环境要求

- Python 3.10+
- OKX 模拟盘 API Key（权限：交易 + 读取）

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置 API Key

推荐使用环境变量（推荐）：

```bash
export OKX_API_KEY="你的APIKey"
export OKX_SECRET_KEY="你的SecretKey"
export OKX_PASSPHRASE="你的Passphrase"
```

或直接在系统环境中设置后运行。

### 4. 运行

```bash
python bot.py
```

---

## 主要参数说明（可在 bot.py 顶部修改）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `TARGET_INST_ID` | `""` | 留空=自动扫描Top30；填写如 `"BTC-USDT-SWAP"` 则只交易该品种 |
| `LOOP_SECONDS` | 20 | 主循环间隔（秒） |
| `MIN_SIGNAL_SCORE` | 4 | 最低信号得分才开仓 |
| `MAX_MARGIN_PCT` | 0.08 | 单笔最大保证金占比 |
| `RISK_PER_TRADE_PCT` | 0.005 | 单笔风险（账户权益的 0.5%） |
| `MIN_LEVERAGE` / `MAX_LEVERAGE` | 5 / 20 | 杠杆范围 |
| `MAX_CONSECUTIVE_LOSSES` | 3 | 连续亏损次数后进入冷却 |
| `LOSS_COOLDOWN_SECONDS` | 1800 | 冷却时间（30分钟） |
| `TARGET_SL_ROI_PCT` | 1.5 | 目标止损 ROI% |
| `TARGET_TP_ROI_PCT` | 3.5 | 目标止盈 ROI% |

---

## 策略逻辑简述

1. 从 CoinGecko 获取市值 Top30，过滤 OKX 有足够流动性的 USDT 永续
2. 计算 5m EMA(9/21) + RSI(14) + MACD + ATR
3. 结合 15m 和 1H 趋势方向打分
4. 得分 ≥ 4 且多空方向明确时产生候选
5. 选择最高分候选，计算仓位与动态杠杆后市价开仓
6. 立即挂 OCO（Mark Price 触发）止盈止损
7. 主循环优先管理已有仓位，无仓位时才扫描新信号

---

## 文件结构

```
.
├── bot.py                 # 主程序
├── requirements.txt       # 依赖
├── README.md              # 说明文档
└── .github/workflows/
    └── demo.yml           # GitHub Actions 测试工作流
```

---

## 风险提示

- 本机器人仅在 **模拟盘** 运行（`x-simulated-trading: 1`）
- 加密货币交易具有高风险，历史表现不代表未来收益
- 请勿直接用于实盘，除非你完全理解并修改了所有风险参数
- API Key 请使用最小权限，并定期轮换

---

## 更新日志

### V2.9.2
- 优化代码可读性与结构注释
- 增强 API 请求重试机制
- 完善 README 文档
- 改进日志与错误处理

### V2.9.1
- 修复 consecutive_losses 全局变量问题
- 修复连续亏损保护
- 优化 OCO 验证逻辑
- 增加实时 Mark Price / 浮盈 / ROI 显示
- 动态杠杆与合约最大杠杆适配
- 平仓后读取 realizedPnl
- UTC+8 日志
- 清理重复函数

---

## License

仅供个人学习研究使用。
