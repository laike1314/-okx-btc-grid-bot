import os
import json
import time
import hmac
import base64
import hashlib
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone

import requests


# ============================================================
# OKX BTC 永续自动交易模拟盘
# ============================================================
#
# 保留原来的交易策略：
# 1. BTC-USDT-SWAP
# 2. OKX 模拟盘
# 3. EMA + RSI + MACD + ATR
# 4. 5x~20x 动态杠杆
# 5. 自动计算仓位
# 6. 市价开仓
# 7. 自动 TP / SL
# 8. 同方向不重复开仓
# 9. 信号反转 -> 平仓
# 10. 信号反转后等待下一根确认K线
#
# 新增：
# 11. 自动读取 OKX 实际成交记录
# 12. 显示每笔实际盈亏
# 13. 显示手续费
# 14. 显示净利润
# 15. 显示累计交易次数 / 胜率 / 累计净利润
# ============================================================


API_KEY = os.getenv("OKX_API_KEY")
SECRET_KEY = os.getenv("OKX_SECRET_KEY")
PASSPHRASE = os.getenv("OKX_PASSPHRASE")

BASE_URL = "https://openapi.okx.com"

INST_ID = "BTC-USDT-SWAP"
INST_TYPE = "SWAP"

# 模拟盘
SIMULATED_TRADING = "1"

# 全仓
TD_MODE = "cross"

# K线周期
BAR = "5m"

# 每次循环间隔
LOOP_SECONDS = 20


# ============================================================
# 策略参数
# ============================================================

EMA_FAST = 9
EMA_SLOW = 21

RSI_PERIOD = 14

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

ATR_PERIOD = 14

LONG_RSI_MIN = 52
LONG_RSI_MAX = 72

SHORT_RSI_MIN = 28
SHORT_RSI_MAX = 48

ATR_SL_MULTIPLIER = 1.5

RISK_REWARD = 1.8

MAX_MARGIN_PCT = 0.08

MIN_LEVERAGE = 5
MAX_LEVERAGE = 20

MIN_SIGNAL_SCORE = 4

MAX_CONSECUTIVE_LOSSES = 3

LOSS_COOLDOWN_SECONDS = 30 * 60


# ============================================================
# 全局状态
# ============================================================

instrument = None

last_processed_candle = None

consecutive_losses = 0
cooldown_until = 0

last_position_side = None


# ============================================================
# 交易统计
# ============================================================

# 已经处理过的成交ID
seen_close_trade_ids = set()

# 统计
trade_total = 0
trade_wins = 0
trade_losses = 0

trade_gross_pnl = Decimal("0")
trade_fees = Decimal("0")
trade_net_pnl = Decimal("0")

# Bot主动平仓原因
close_order_reasons = {}

# 当前持仓信息
active_direction = None
active_take_profit = None
active_stop_loss = None


# ============================================================
# 基础
# ============================================================

def check_config():
    print("=" * 60)
    print("检查 OKX API 配置")
    print("=" * 60)

    if not API_KEY:
        raise Exception("缺少环境变量 OKX_API_KEY")

    if not SECRET_KEY:
        raise Exception("缺少环境变量 OKX_SECRET_KEY")

    if not PASSPHRASE:
        raise Exception("缺少环境变量 OKX_PASSPHRASE")

    print("OKX_API_KEY    : 已读取")
    print("OKX_SECRET_KEY : 已读取")
    print("OKX_PASSPHRASE : 已读取")
    print("