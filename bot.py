# -*- coding: utf-8 -*-

"""
===========================================================
OKX 全市场永续强行情自动交易机器人 V2
===========================================================

核心思想：

1. 自动扫描 OKX 全部 USDT 永续合约
2. 先用全市场 Ticker 筛选流动性
3. 再扫描 5m K线寻找短线强波动
4. 对候选币进一步检查：
   - 5m 趋势
   - 15m 趋势
   - 1H 趋势
   - MACD
   - RSI
   - ATR
   - 成交量
   - 突破
   - K线连续性
5. 只交易高分强行情
6. 自动判断 LONG / SHORT
7. 自动选择 5x / 7x / 10x / 12x / 15x
8. ATR 自动计算止损
9. 自动计算止盈
10. 单笔风险固定为账户权益的一定比例
11. 单笔保证金不超过账户权益 5%
12. 同时最多持有 1 个机器人仓位
13. 每日达到目标收益后停止开新仓
14. 每日最大亏损达到阈值后停止
15. 连续亏损达到阈值后暂停
16. 下单后验证订单是否真正成交
17. 使用 OKX 附带 TP/SL
18. 保存每日状态，避免机器人重启后完全丢失当天状态

===========================================================
重要：
本程序默认：
- SWAP
- USDT 永续
- cross 全仓
- net 单向持仓
- 模拟盘
===========================================================
"""

import os
import json
import time
import math
import hmac
import base64
import hashlib
import requests
import traceback

from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed


# =========================================================
# 1. 基础配置
# =========================================================

API_KEY = os.getenv("OKX_API_KEY", "")
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

BASE_URL = "https://openapi.okx.com"

# 1 = OKX 模拟盘
SIMULATED_TRADING = "1"

INST_TYPE = "SWAP"
TD_MODE = "cross"

# =========================================================
# 扫描配置
# =========================================================

# 每轮扫描间隔
SCAN_INTERVAL_SECONDS = 60

# 并发获取K线
SCAN_WORKERS = 12

# 每次扫描全部 USDT 永续
SCAN_ALL_USDT_SWAPS = True

# 5m / 15m / 1H
FAST_BAR = "5m"
MID_BAR = "15m"
SLOW_BAR = "1H"

# K线数量
FAST_LIMIT = 100
MID_LIMIT = 100
SLOW_LIMIT = 100

# =========================================================
# 市场过滤
# =========================================================

# 24小时成交额最低要求
# 太低的币直接过滤，避免垃圾币、流动性太差币种
MIN_24H_VOLUME_USDT = 5_000_000

# 5分钟最小 ATR 百分比
MIN_ATR_PCT = 0.0025

# 5分钟最大 ATR 百分比
MAX_ATR_PCT = 0.15

# 强行情最低分
MIN_SIGNAL_SCORE = 8

# 最低成交量倍数
MIN_VOLUME_RATIO = 1.20

# 突破要求
BREAKOUT_LOOKBACK = 20

# =========================================================
# 风险配置
# =========================================================

# 单笔理论风险
# 例如账户 300 USDT：
# 300 × 0.5% = 1.5 USDT
RISK_PER_TRADE_PCT = 0.005

# 单笔最大保证金
# 例如 300 USDT：
# 最大保证金 = 15 USDT
MAX_TRADE_MARGIN_PCT = 0.05

# 最低风险收益比
MIN_RISK_REWARD = 1.8

# 默认目标 RR
TARGET_RISK_REWARD = 2.0

# =========================================================
# 杠杆
# =========================================================

MIN_LEVERAGE = 5
MAX_LEVERAGE = 15

# =========================================================
# 每日收益控制
# =========================================================

# 建议 0.02 / 0.03 / 0.04
#
# 默认 3%
# 达到 3% 后停止开新仓
DAILY_PROFIT_TARGET_PCT = 0.03

# 当天最大亏损
# -2%
DAILY_MAX_LOSS_PCT = 0.02

# 连续亏损达到 3 次
MAX_CONSECUTIVE_LOSSES = 3

# 连续亏损后冷却
LOSS_COOLDOWN_SECONDS = 1800

# 每天最多完成多少笔
MAX_TRADES_PER_DAY = 8

# =========================================================
# 仓位数量
# =========================================================

# 同时最多一个仓位
MAX_OPEN_POSITIONS = 1

# =========================================================
# ATR 止损
# =========================================================

ATR_PERIOD = 14

# 止损 = ATR × 1.5
ATR_SL_MULTIPLIER = 1.5

# =========================================================
# RSI
# =========================================================

RSI_PERIOD = 14

# =========================================================
# EMA
# =========================================================

EMA_FAST = 9
EMA_SLOW = 21

EMA_15_FAST = 20
EMA_15_SLOW = 50

EMA_1H_FAST = 20
EMA_1H_SLOW = 50

# =========================================================
# MACD
# =========================================================

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# =========================================================
# 成交量
# =========================================================

VOLUME_PERIOD = 20

# =========================================================
# 强行情确认
# =========================================================

# 不再要求3根连续阳线
# 只要求最后两根方向一致
CONFIRM_CANDLES = 2

# =========================================================
# 状态文件
# =========================================================

STATE_FILE = "multi_coin_bot_state.json"


# =========================================================
# 全局
# =========================================================

session = requests.Session()

instrument_cache = {}
last_signal_candle = {}

daily_state = {
    "date": "",
    "start_equity": 0.0,
    "peak_equity": 0.0,
    "trades": 0,
    "consecutive_losses": 0,
    "cooldown_until": 0,
    "day_finished": False,
}

state_loaded = False


# =========================================================
# 工具函数
# =========================================================

def now_local():
    """
    使用 UTC+8 作为交易日。
    """
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz)


def today_string():
    return now_local().strftime("%Y-%m-%d")


def log(msg):
    print(f"[{now_local().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def safe_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def decimal_floor(value, step):
    """
    向下取整到交易所步长。
    """
    value = Decimal(str(value))
    step = Decimal(str(step))

    if step <= 0:
        return value

    units = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return units * step


def fmt_decimal(value, digits=12):
    d = Decimal(str(value))
    s = format(d, "f")

    if "." in s:
        s = s.rstrip("0").rstrip(".")

    if s == "-0":
        s = "0"

    return s


# =========================================================
# 状态保存
# =========================================================

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(daily_state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"保存状态失败: {e}")


def load_state(current_equity):
    global daily_state

    today = today_string()

    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            if data.get("date") == today:
                daily_state.update(data)

                if daily_state.get("start_equity", 0) <= 0:
                    daily_state["start_equity"] = current_equity

                if daily_state.get("peak_equity", 0) <= 0:
                    daily_state["peak_equity"] = current_equity

                log(
                    f"恢复今日状态："
                    f"起始权益={daily_state['start_equity']:.2f}, "
                    f"交易={daily_state['trades']}, "
                    f"连续亏损={daily_state['consecutive_losses']}"
                )

                return

    except Exception as e:
        log(f"读取状态文件失败: {e}")

    daily_state = {
        "date": today,
        "start_equity": current_equity,
        "peak_equity": current_equity,
        "trades": 0,
        "consecutive_losses": 0,
        "cooldown_until": 0,
        "day_finished": False,
    }

    save_state()

    log(f"创建新的交易日状态，起始权益={current_equity:.2f}")


def reset_daily_if_needed(current_equity):
    today = today_string()

    if daily_state.get("date") != today:
        daily_state.clear()

        daily_state.update({
            "date": today,
            "start_equity": current_equity,
            "peak_equity": current_equity,
            "trades": 0,
            "consecutive_losses": 0,
            "cooldown_until": 0,
            "day_finished": False,
        })

        save_state()

        log(
            f"新交易日开始："
            f"起始权益={current_equity:.2f}"
        )


# =========================================================
# OKX 签名
# =========================================================

def get_server_timestamp():
    url = BASE_URL + "/api/v5/public/time"

    r = session.get(url, timeout=10)
    r.raise_for_status()

    data = r.json()

    if data.get("code") != "0":
        raise RuntimeError(f"获取OKX服务器时间失败：{data}")

    ts = data["data"][0]["ts"]

    dt = datetime.fromtimestamp(
        int(ts) / 1000,
        tz=timezone.utc
    )

    return dt.isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def sign_request(timestamp, method, request_path, body=""):
    message = timestamp + method.upper() + request_path + body

    mac = hmac.new(
        SECRET_KEY.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    )

    return base64.b64encode(mac.digest()).decode()


def public_get(path, params=None):
    url = BASE_URL + path

    r = session.get(
        url,
        params=params,
        timeout=15
    )

    r.raise_for_status()

    data = r.json()

    if data.get("code") != "0":
        raise RuntimeError(
            f"OKX PUBLIC ERROR: {data}"
        )

    return data.get("data", [])


def private_get(path, params=None):
    if params:
        from urllib.parse import urlencode

        query = urlencode(params)
        request_path = path + "?" + query
    else:
        request_path = path

    timestamp = get_server_timestamp()

    sign = sign_request(
        timestamp,
        "GET",
        request_path,
        ""
    )

    headers = {
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type": "application/json",
    }

    if SIMULATED_TRADING == "1":
        headers["x-simulated-trading"] = "1"

    r = session.get(
        BASE_URL + request_path,
        headers=headers,
        timeout=15
    )

    r.raise_for_status()

    data = r.json()

    if data.get("code") != "0":
        raise RuntimeError(
            f"OKX PRIVATE GET ERROR: {data}"
        )

    return data.get("data", [])


def private_post(path, body):
    body_text = json.dumps(
        body,
        separators=(",", ":"),
        ensure_ascii=False
    )

    timestamp = get_server_timestamp()

    sign = sign_request(
        timestamp,
        "POST",
        path,
        body_text
    )

    headers = {
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type": "application/json",
    }

    if SIMULATED_TRADING == "1":
        headers["x-simulated-trading"] = "1"

    r = session.post(
        BASE_URL + path,
        headers=headers,
        data=body_text,
        timeout=15
    )

    r.raise_for_status()

    data = r.json()

    if data.get("code") != "0":
        raise RuntimeError(
            f"OKX PRIVATE POST ERROR: {data}"
        )

    return data.get("data", [])


# =========================================================
# 账户
# =========================================================

def get_account_config():
    data = private_get(
        "/api/v5/account/config"
    )

    if not data:
        raise RuntimeError("无法获取账户配置")

    return data[0]


def check_account_mode():
    config = get_account_config()

    pos_mode = config.get("posMode", "")

    log(f"账户持仓模式：{pos_mode}")

    # OKX API:
    # net_mode = 单向持仓
    # long_short_mode = 双向持仓
    if pos_mode != "net_mode":
        raise RuntimeError(
            f"当前账户不是单向持仓模式，"
            f"OKX API返回：{pos_mode}。"
            f"请在OKX中切换为单向持仓。"
        )

    log("账户持仓模式确认：单向持仓 OK")

    return config


def get_equity():
    data = private_get(
        "/api/v5/account/balance",
        {"ccy": "USDT"}
    )

    if not data:
        raise RuntimeError("无法获取账户余额")

    account = data[0]

    total_eq = safe_float(
        account.get("totalEq")
    )

    if total_eq > 0:
        return total_eq

    for item in account.get("details", []):
        if item.get("ccy") == "USDT":
            eq = safe_float(
                item.get("eq")
            )

            if eq > 0:
                return eq

            eq = safe_float(
                item.get("cashBal")
            )

            if eq > 0:
                return eq

    raise RuntimeError(
        f"无法解析账户权益：{account}"
    )


# =========================================================
# 仓位
# =========================================================

def get_open_positions():
    data = private_get(
        "/api/v5/account/positions",
        {"instType": "SWAP"}
    )

    positions = []

    for p in data:
        pos = safe_float(p.get("pos"))

        if abs(pos) <= 0:
            continue

        side = "LONG" if pos > 0 else "SHORT"

        positions.append({
            "instId": p.get("instId"),
            "pos": abs(pos),
            "side": side,
            "avgPx": safe_float(p.get("avgPx")),
            "markPx": safe_float(p.get("markPx")),
            "upl": safe_float(p.get("upl")),
            "uplRatio": safe_float(p.get("uplRatio")),
            "lever": safe_float(p.get("lever")),
            "liqPx": safe_float(p.get("liqPx")),
            "mgnMode": p.get("mgnMode"),
        })

    return positions


# =========================================================
# 市场产品
# =========================================================

def load_instruments():
    global instrument_cache

    data = public_get(
        "/api/v5/public/instruments",
        {
            "instType": "SWAP"
        }
    )

    result = {}

    for item in data:

        inst_id = item.get("instId", "")

        if not inst_id.endswith("-USDT-SWAP"):
            continue

        if item.get("state") != "live":
            continue

        if item.get("settleCcy") != "USDT":
            continue

        ct_val = safe_float(item.get("ctVal"))

        lot_sz = safe_float(item.get("lotSz"))
        min_sz = safe_float(item.get("minSz"))
        tick_sz = safe_float(item.get("tickSz"))

        if ct_val <= 0:
            continue

        if lot_sz <= 0:
            continue

        result[inst_id] = {
            "instId": inst_id,
            "ctVal": ct_val,
            "lotSz": lot_sz,
            "minSz": min_sz,
            "tickSz": tick_sz,
            "state": item.get("state"),
            "ctValCcy": item.get("ctValCcy"),
            "settleCcy": item.get("settleCcy"),
            "instFamily": item.get("instFamily"),
        }

    instrument_cache = result

    log(
        f"加载USDT永续合约："
        f"{len(instrument_cache)} 个"
    )

    return result


# =========================================================
# 全市场 Ticker
# =========================================================

def get_all_tickers():
    data = public_get(
        "/api/v5/market/tickers",
        {
            "instType": "SWAP"
        }
    )

    result = {}

    for item in data:

        inst_id = item.get("instId", "")

        if inst_id not in instrument_cache:
            continue

        last = safe_float(item.get("last"))

        if last <= 0:
            continue

        vol_quote = safe_float(
            item.get("volCcy24h")
        )

        open24 = safe_float(
            item.get("open24h")
        )

        if open24 > 0:
            chg24 = (
                last / open24 - 1
            )
        else:
            chg24 = 0

        result[inst_id] = {
            "instId": inst_id,
            "last": last,
            "vol24h_quote": vol_quote,
            "chg24": chg24,
        }

    return result


# =========================================================
# K线
# =========================================================

def get_candles(
    inst_id,
    bar,
    limit=100
):
    data = public_get(
        "/api/v5/market/candles",
        {
            "instId": inst_id,
            "bar": bar,
            "limit": str(limit),
        }
    )

    candles = []

    for row in data:

        if len(row) < 9:
            continue

        confirm = row[8]

        if confirm != "1":
            continue

        candles.append({
            "ts": int(row[0]),
            "o": safe_float(row[1]),
            "h": safe_float(row[2]),
            "l": safe_float(row[3]),
            "c": safe_float(row[4]),
            "vol": safe_float(row[5]),
        })

    candles.sort(
        key=lambda x: x["ts"]
    )

    return candles


# =========================================================
# 指标
# =========================================================

def sma(values, period):
    if len(values) < period:
        return None

    return sum(
        values[-period:]
    ) / period


def ema_series(values, period):
    if len(values) < period:
        return []

    multiplier = 2 / (period + 1)

    result = []

    ema_value = sum(
        values[:period]
    ) / period

    result.append(ema_value)

    for value in values[period:]:
        ema_value = (
            value - ema_value
        ) * multiplier + ema_value

        result.append(ema_value)

    return result


def ema(values, period):
    series = ema_series(
        values,
        period
    )

    if not series:
        return None

    return series[-1]


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        if change >= 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(
        gains[:period]
    ) / period

    avg_loss = sum(
        losses[:period]
    ) / period

    for i in range(
        period,
        len(gains)
    ):
        avg_gain = (
            avg_gain * (period - 1)
            + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1)
            + losses[i]
        ) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss

    return 100 - (
        100 / (1 + rs)
    )


def macd(values):
    if len(values) < MACD_SLOW + MACD_SIGNAL:
        return None, None, None

    fast = ema_series(
        values,
        MACD_FAST
    )

    slow = ema_series(
        values,
        MACD_SLOW
    )

    if not fast or not slow:
        return None, None, None

    # 对齐
    offset = len(fast) - len(slow)

    macd_line = []

    for i in range(
        len(slow)
    ):
        macd_line.append(
            fast[i + offset]
            - slow[i]
        )

    signal_line = ema_series(
        macd_line,
        MACD_SIGNAL
    )

    if not signal_line:
        return None, None, None

    macd_current = macd_line[-1]
    signal_current = signal_line[-1]

    hist = (
        macd_current
        - signal_current
    )

    return (
        macd_current,
        signal_current,
        hist
    )


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    trs = []

    for i in range(1, len(candles)):

        high = candles[i]["h"]
        low = candles[i]["l"]
        prev_close = candles[i - 1]["c"]

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )

        trs.append(tr)

    if len(trs) < period:
        return None

    atr_value = (
        sum(trs[:period]) / period
    )

    for tr in trs[period:]:
        atr_value = (
            atr_value * (period - 1)
            + tr
        ) / period

    return atr_value


# =========================================================
# 单币种快速5m扫描
# =========================================================

def fast_scan_symbol(inst_id, ticker):
    try:

        candles = get_candles(
            inst_id,
            FAST_BAR,
            FAST_LIMIT
        )

        if len(candles) < 60:
            return None

        closes = [
            x["c"]
            for x in candles
        ]

        highs = [
            x["h"]
            for x in candles
        ]

        lows = [
            x["l"]
            for x in candles
        ]

        volumes = [
            x["vol"]
            for x in candles
        ]

        price = closes[-1]

        atr_value = atr(
            candles,
            ATR_PERIOD
        )

        if not atr_value:
            return None

        atr_pct = (
            atr_value / price
        )

        if atr_pct < MIN_ATR_PCT:
            return None

        if atr_pct > MAX_ATR_PCT:
            return None

        ema_fast = ema(
            closes,
            EMA_FAST
        )

        ema_slow = ema(
            closes,
            EMA_SLOW
        )

        if ema_fast is None or ema_slow is None:
            return None

        rsi_value = rsi(
            closes,
            RSI_PERIOD
        )

        if rsi_value is None:
            return None

        macd_value, signal_value, hist = macd(
            closes
        )

        if macd_value is None:
            return None

        avg_volume = sma(
            volumes,
            VOLUME_PERIOD
        )

        if not avg_volume or avg_volume <= 0:
            return None

        volume_ratio = (
            volumes[-1] / avg_volume
        )

        previous_high = max(
            highs[
                -BREAKOUT_LOOKBACK - 1:-1
            ]
        )

        previous_low = min(
            lows[
                -BREAKOUT_LOOKBACK - 1:-1
            ]
        )

        long_breakout = (
            price > previous_high
        )

        short_breakout = (
            price < previous_low
        )

        # 最近两根已完成K线
        last_two = candles[-2:]

        two_green = all(
            x["c"] > x["o"]
            for x in last_two
        )

        two_red = all(
            x["c"] < x["o"]
            for x in last_two
        )

        score_long = 0
        score_short = 0

        # =================================================
        # EMA趋势
        # =================================================

        if ema_fast > ema_slow:
            score_long += 2

        if ema_fast < ema_slow:
            score_short += 2

        # =================================================
        # 价格位置
        # =================================================

        if price > ema_fast:
            score_long += 1

        if price < ema_fast:
            score_short += 1

        # =================================================
        # MACD
        # =================================================

        if macd_value > signal_value and hist > 0:
            score_long += 1

        if macd_value < signal_value and hist < 0:
            score_short += 1

        # =================================================
        # RSI
        # =================================================

        if 52 <= rsi_value <= 72:
            score_long += 1

        if 28 <= rsi_value <= 48:
            score_short += 1

        # =================================================
        # 成交量
        # =================================================

        if volume_ratio >= 1.5:
            if price > candles[-2]["c"]:
                score_long += 1

            if price < candles[-2]["c"]:
                score_short += 1

        # =================================================
        # 突破
        # =================================================

        if long_breakout:
            score_long += 2

        if short_breakout:
            score_short += 2

        # =================================================
        # K线确认
        # =================================================

        if two_green:
            score_long += 1

        if two_red:
            score_short += 1

        # =================================================
        # 动量
        # =================================================

        momentum = (
            closes[-1]
            / closes[-4]
            - 1
        )

        if momentum > 0.006:
            score_long += 1

        if momentum < -0.006:
            score_short += 1

        direction = "WAIT"

        if (
            score_long >= MIN_SIGNAL_SCORE
            and score_long > score_short
        ):
            direction = "LONG"

        elif (
            score_short >= MIN_SIGNAL_SCORE
            and score_short > score_long
        ):
            direction = "SHORT"

        return {
            "instId": inst_id,
            "direction": direction,
            "score_long": score_long,
            "score_short": score_short,
            "price": price,
            "atr": atr_value,
            "atr_pct": atr_pct,
            "rsi": rsi_value,
            "macd": macd_value,
            "macd_signal": signal_value,
            "volume_ratio": volume_ratio,
            "breakout_long": long_breakout,
            "breakout_short": short_breakout,
            "momentum": momentum,
            "candle_ts": candles[-1]["ts"],
            "candles": candles,
            "ticker": ticker,
        }

    except Exception:
        return None


# =========================================================
# 多周期分析
# =========================================================

def analyze_multi_timeframe(candidate):
    inst_id = candidate["instId"]

    try:

        candles15 = get_candles(
            inst_id,
            MID_BAR,
            MID_LIMIT
        )

        candles1h = get_candles(
            inst_id,
            SLOW_BAR,
            SLOW_LIMIT
        )

        if len(candles15) < 60:
            return None

        if len(candles1h) < 60:
            return None

        close15 = [
            x["c"]
            for x in candles15
        ]

        close1h = [
            x["c"]
            for x in candles1h
        ]

        price = candidate["price"]

        ema15_fast = ema(
            close15,
            EMA_15_FAST
        )

        ema15_slow = ema(
            close15,
            EMA_15_SLOW
        )

        ema1h_fast = ema(
            close1h,
            EMA_1H_FAST
        )

        ema1h_slow = ema(
            close1h,
            EMA_1H_SLOW
        )

        if None in (
            ema15_fast,
            ema15_slow,
            ema1h_fast,
            ema1h_slow
        ):
            return None

        score_long = candidate["score_long"]
        score_short = candidate["score_short"]

        # =================================================
        # 15m
        # =================================================

        trend15 = "RANGE"

        if (
            price > ema15_fast
            and ema15_fast > ema15_slow
        ):
            score_long += 2
            trend15 = "LONG"

        elif (
            price < ema15_fast
            and ema15_fast < ema15_slow
        ):
            score_short += 2
            trend15 = "SHORT"

        # =================================================
        # 1H
        # =================================================

        trend1h = "RANGE"

        if (
            price > ema1h_fast
            and ema1h_fast > ema1h_slow
        ):
            score_long += 2
            trend1h = "LONG"

        elif (
            price < ema1h_fast
            and ema1h_fast < ema1h_slow
        ):
            score_short += 2
            trend1h = "SHORT"

        # =================================================
        # 多周期一致性
        # =================================================

        if (
            trend15 == "LONG"
            and trend1h == "LONG"
            and candidate["direction"] == "LONG"
        ):
            final_direction = "LONG"

        elif (
            trend15 == "SHORT"
            and trend1h == "SHORT"
            and candidate["direction"] == "SHORT"
        ):
            final_direction = "SHORT"

        else:
            final_direction = "WAIT"

        final_score = max(
            score_long,
            score_short
        )

        return {
            **candidate,
            "score_long_final": score_long,
            "score_short_final": score_short,
            "final_score": final_score,
            "trend15": trend15,
            "trend1h": trend1h,
            "direction_final": final_direction,
        }

    except Exception:
        return None


# =========================================================
# 全市场扫描
# =========================================================

def scan_market():
    log("====================================================")
    log("开始扫描 OKX 全部 USDT 永续合约")
    log("====================================================")

    tickers = get_all_tickers()

    symbols = []

    for inst_id, ticker in tickers.items():

        volume = ticker["vol24h_quote"]

        if volume < MIN_24H_VOLUME_USDT:
            continue

        symbols.append(
            (inst_id, ticker)
        )

    log(
        f"流动性筛选："
        f"{len(tickers)} → {len(symbols)}"
    )

    # 先按照24h成交额排序
    symbols.sort(
        key=lambda x: x[1]["vol24h_quote"],
        reverse=True
    )

    candidates = []

    with ThreadPoolExecutor(
        max_workers=SCAN_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                fast_scan_symbol,
                inst_id,
                ticker
            ): inst_id
            for inst_id, ticker in symbols
        }

        for future in as_completed(futures):

            result = future.result()

            if not result:
                continue

            if result["direction"] == "WAIT":
                continue

            candidates.append(result)

    if not candidates:
        log("5m没有发现强行情")
        return []

    log(
        f"5m强行情候选："
        f"{len(candidates)}"
    )

    # =====================================================
    # 先取分数最高的前20个
    # 再进行15m/1H
    # =====================================================

    candidates.sort(
        key=lambda x: (
            max(
                x["score_long"],
                x["score_short"]
            ),
            x["volume_ratio"],
            abs(x["momentum"])
        ),
        reverse=True
    )

    candidates = candidates[:20]

    final_candidates = []

    for candidate in candidates:

        result = analyze_multi_timeframe(
            candidate
        )

        if not result:
            continue

        if result["direction_final"] == "WAIT":
            continue

        if result["final_score"] < MIN_SIGNAL_SCORE:
            continue

        final_candidates.append(result)

    # =====================================================
    # 排序
    # =====================================================

    final_candidates.sort(
        key=lambda x: (
            x["final_score"],
            x["volume_ratio"],
            abs(x["momentum"])
        ),
        reverse=True
    )

    # =====================================================
    # 输出排行榜
    # =====================================================

    if final_candidates:

        log("--------------- 强行情排行榜 ---------------")

        for i, x in enumerate(
            final_candidates[:10],
            1
        ):
            direction = x["direction_final"]

            if direction == "LONG":
                score = x["score_long_final"]
            else:
                score = x["score_short_final"]

            log(
                f"{i:02d}. "
                f"{x['instId']:20s} "
                f"{direction:5s} "
                f"score={score:02d} "
                f"5m={x['score_long']}/{x['score_short']} "
                f"15m={x['trend15']:5s} "
                f"1H={x['trend1h']:5s} "
                f"RSI={x['rsi']:.1f} "
                f"VOL={x['volume_ratio']:.2f}x"
            )

        log("--------------------------------------------")

    return final_candidates


# =========================================================
# 杠杆
# =========================================================

def choose_leverage(score):
    if score >= 11:
        return 15

    if score >= 10:
        return 12

    if score >= 9:
        return 10

    if score >= 8:
        return 7

    return 5


def set_leverage(inst_id, leverage):
    body = {
        "instId": inst_id,
        "lever": str(leverage),
        "mgnMode": TD_MODE,
    }

    data = private_post(
        "/api/v5/account/set-leverage",
        body
    )

    return data


# =========================================================
# 获取产品参数
# =========================================================

def get_instrument(inst_id):
    if inst_id in instrument_cache:
        return instrument_cache[inst_id]

    data = public_get(
        "/api/v5/public/instruments",
        {
            "instType": "SWAP",
            "instId": inst_id,
        }
    )

    if not data:
        raise RuntimeError(
            f"找不到产品信息：{inst_id}"
        )

    item = data[0]

    result = {
        "instId": inst_id,
        "ctVal": safe_float(item.get("ctVal")),
        "lotSz": safe_float(item.get("lotSz")),
        "minSz": safe_float(item.get("minSz")),
        "tickSz": safe_float(item.get("tickSz")),
    }

    instrument_cache[inst_id] = result

    return result


# =========================================================
# 仓位计算
# =========================================================

def calculate_position_size(
    inst_id,
    price,
    stop_distance,
    leverage,
    equity
):

    info = get_instrument(
        inst_id
    )

    ct_val = info["ctVal"]
    lot_sz = info["lotSz"]
    min_sz = info["minSz"]

    if ct_val <= 0:
        return None

    if stop_distance <= 0:
        return None

    # ---------------------------------------------
    # 最大风险金额
    # ---------------------------------------------

    risk_amount = (
        equity
        * RISK_PER_TRADE_PCT
    )

    # ---------------------------------------------
    # 每个合约如果触发止损，理论亏损
    # ---------------------------------------------

    risk_per_contract = (
        stop_distance
        * ct_val
    )

    if risk_per_contract <= 0:
        return None

    contracts_by_risk = (
        risk_amount
        / risk_per_contract
    )

    # ---------------------------------------------
    # 最大保证金限制
    # ---------------------------------------------

    max_margin = (
        equity
        * MAX_TRADE_MARGIN_PCT
    )

    max_notional = (
        max_margin
        * leverage
    )

    contracts_by_margin = (
        max_notional
        / (price * ct_val)
    )

    contracts = min(
        contracts_by_risk,
        contracts_by_margin
    )

    contracts = decimal_floor(
        contracts,
        lot_sz
    )

    if contracts < Decimal(str(min_sz)):
        return None

    contracts_float = float(
        contracts
    )

    notional = (
        contracts_float
        * ct_val
        * price
    )

    margin = (
        notional
        / leverage
    )

    theoretical_risk = (
        contracts_float
        * ct_val
        * stop_distance
    )

    if margin > max_margin:
        return None

    if theoretical_risk > risk_amount * 1.05:
        return None

    return {
        "contracts": contracts_float,
        "notional": notional,
        "margin": margin,
        "risk_amount": theoretical_risk,
        "ctVal": ct_val,
        "lotSz": lot_sz,
        "minSz": min_sz,
    }


# =========================================================
# TP / SL
# =========================================================

def calculate_tp_sl(
    direction,
    entry_price,
    atr_value,
    tick_sz
):

    stop_distance = (
        atr_value
        * ATR_SL_MULTIPLIER
    )

    if stop_distance <= 0:
        return None

    take_distance = (
        stop_distance
        * TARGET_RISK_REWARD
    )

    if direction == "LONG":

        sl = (
            entry_price
            - stop_distance
        )

        tp = (
            entry_price
            + take_distance
        )

    else:

        sl = (
            entry_price
            + stop_distance
        )

        tp = (
            entry_price
            - take_distance
        )

    sl = decimal_floor(
        sl,
        tick_sz
    )

    tp = decimal_floor(
        tp,
        tick_sz
    )

    if direction == "LONG":

        actual_sl_distance = (
            entry_price
            - float(sl)
        )

        actual_tp_distance = (
            float(tp)
            - entry_price
        )

    else:

        actual_sl_distance = (
            float(sl)
            - entry_price
        )

        actual_tp_distance = (
            entry_price
            - float(tp)
        )

    if actual_sl_distance <= 0:
        return None

    if actual_tp_distance <= 0:
        return None

    rr = (
        actual_tp_distance
        / actual_sl_distance
    )

    if rr < MIN_RISK_REWARD:
        return None

    return {
        "sl": float(sl),
        "tp": float(tp),
        "stop_distance": actual_sl_distance,
        "take_distance": actual_tp_distance,
        "rr": rr,
    }


# =========================================================
# 下单
# =========================================================

def place_market_order(
    inst_id,
    direction,
    contracts,
    leverage,
    tp,
    sl
):

    side = (
        "buy"
        if direction == "LONG"
        else "sell"
    )

    client_id = (
        "MC"
        + str(int(time.time() * 1000))
    )[-32:]

    algo_client_id = (
        "TP"
        + str(int(time.time() * 1000))
    )[-32:]

    body = {
        "instId": inst_id,
        "tdMode": TD_MODE,
        "side": side,
        "ordType": "market",
        "sz": fmt_decimal(contracts),
        "lever": str(leverage),
        "clOrdId": client_id,

        "attachAlgoOrds": [
            {
                "attachAlgoClOrdId": algo_client_id,

                "tpTriggerPx": fmt_decimal(tp),
                "tpOrdPx": "-1",
                "tpTriggerPxType": "mark",

                "slTriggerPx": fmt_decimal(sl),
                "slOrdPx": "-1",
                "slTriggerPxType": "mark",
            }
        ],
    }

    log(
        f"准备下单："
        f"{inst_id} "
        f"{direction} "
        f"{contracts}张 "
        f"{leverage}x "
        f"TP={tp} "
        f"SL={sl}"
    )

    data = private_post(
        "/api/v5/trade/order",
        body
    )

    if not data:
        raise RuntimeError(
            "OKX没有返回订单数据"
        )

    item = data[0]

    if item.get("sCode") != "0":
        raise RuntimeError(
            f"下单失败：{item}"
        )

    ord_id = item.get("ordId")

    log(
        f"订单已提交：ordId={ord_id}"
    )

    return {
        "ordId": ord_id,
        "clOrdId": client_id,
        "algoClOrdId": algo_client_id,
    }


# =========================================================
# 查询订单
# =========================================================

def get_order(
    inst_id,
    ord_id
):

    data = private_get(
        "/api/v5/trade/order",
        {
            "instId": inst_id,
            "ordId": ord_id,
        }
    )

    if not data:
        return None

    return data[0]


# =========================================================
# 等待成交
# =========================================================

def wait_order_fill(
    inst_id,
    ord_id,
    timeout_seconds=12
):

    start = time.time()

    last_state = ""

    while (
        time.time() - start
        < timeout_seconds
    ):

        order = get_order(
            inst_id,
            ord_id
        )

        if not order:
            time.sleep(0.5)
            continue

        state = order.get("state", "")

        if state != last_state:
            log(
                f"订单状态："
                f"{inst_id} "
                f"{ord_id} "
                f"{state}"
            )
            last_state = state

        if state == "filled":

            avg_px = safe_float(
                order.get("avgPx")
            )

            fill_sz = safe_float(
                order.get("accFillSz")
            )

            log(
                f"订单完全成交："
                f"价格={avg_px} "
                f"数量={fill_sz}"
            )

            return order

        if state == "canceled":
            return order

        time.sleep(0.5)

    return get_order(
        inst_id,
        ord_id
    )


# =========================================================
# 取消订单
# =========================================================

def cancel_order(
    inst_id,
    ord_id
):

    try:

        return private_post(
            "/api/v5/trade/cancel-order",
            {
                "instId": inst_id,
                "ordId": ord_id,
            }
        )

    except Exception as e:

        log(
            f"取消订单失败：{e}"
        )

        return None


# =========================================================
# 紧急平仓
# =========================================================

def close_position_market(
    inst_id,
    direction,
    size
):

    side = (
        "sell"
        if direction == "LONG"
        else "buy"
    )

    body = {
        "instId": inst_id,
        "tdMode": TD_MODE,
        "side": side,
        "ordType": "market",
        "sz": fmt_decimal(size),
        "reduceOnly": "true",
        "clOrdId": (
            "EM"
            + str(int(time.time() * 1000))
        )[-32:],
    }

    try:

        data = private_post(
            "/api/v5/trade/order",
            body
        )

        log(
            f"紧急平仓请求："
            f"{inst_id} "
            f"{direction} "
            f"{size}"
        )

        return data

    except Exception as e:

        log(
            f"紧急平仓失败：{e}"
        )

        return None


# =========================================================
# 交易前风险检查
# =========================================================

def risk_blocked(equity):

    if daily_state.get("day_finished"):
        return True, "今日已完成"

    start_equity = safe_float(
        daily_state.get(
            "start_equity"
        )
    )

    if start_equity <= 0:
        return True, "起始权益无效"

    daily_return = (
        equity
        / start_equity
        - 1
    )

    if daily_return >= DAILY_PROFIT_TARGET_PCT:

        daily_state["day_finished"] = True

        save_state()

        return True, (
            f"达到今日收益目标 "
            f"{daily_return * 100:.2f}%"
        )

    if daily_return <= -DAILY_MAX_LOSS_PCT:

        daily_state["day_finished"] = True

        save_state()

        return True, (
            f"达到今日最大亏损 "
            f"{daily_return * 100:.2f}%"
        )

    cooldown_until = safe_float(
        daily_state.get(
            "cooldown_until"
        )
    )

    if time.time() < cooldown_until:

        remain = (
            cooldown_until
            - time.time()
        )

        return True, (
            f"连续亏损冷却中 "
            f"{remain / 60:.1f}分钟"
        )

    trades = int(
        daily_state.get(
            "trades",
            0
        )
    )

    if trades >= MAX_TRADES_PER_DAY:

        daily_state["day_finished"] = True

        save_state()

        return True, (
            f"今日交易次数达到上限 "
            f"{MAX_TRADES_PER_DAY}"
        )

    consecutive = int(
        daily_state.get(
            "consecutive_losses",
            0
        )
    )

    if consecutive >= MAX_CONSECUTIVE_LOSSES:

        daily_state["day_finished"] = True

        save_state()

        return True, (
            f"连续亏损达到 "
            f"{MAX_CONSECUTIVE_LOSSES}"
        )

    return False, ""


# =========================================================
# 检测最近交易结果
# =========================================================

def update_trade_statistics():
    """
    使用最近成交记录更新连续盈亏。

    注意：
    这里不直接用“最后一笔 fill”简单判断，
    因为一个订单可能有多个 fill。

    通过 fills-history 对同一 ordId 汇总。
    """

    try:

        data = private_get(
            "/api/v5/trade/fills",
            {
                "instType": "SWAP",
                "limit": "100",
            }
        )

        if not data:
            return

        groups = {}

        for fill in data:

            ord_id = fill.get("ordId")

            if not ord_id:
                continue

            fill_pnl = safe_float(
                fill.get("fillPnl")
            )

            fee = safe_float(
                fill.get("fee")
            )

            funding = safe_float(
                fill.get("fundingFee")
            )

            if ord_id not in groups:

                groups[ord_id] = {
                    "pnl": 0.0,
                    "fee": 0.0,
                    "funding": 0.0,
                    "instId": fill.get("instId"),
                }

            groups[ord_id]["pnl"] += fill_pnl
            groups[ord_id]["fee"] += fee
            groups[ord_id]["funding"] += funding

        # 本版本不自动改变连续亏损状态，
        # 避免同一个订单在每一轮重复统计。
        #
        # 连续亏损主要在“发现仓位从有变成无”时处理。
        #
        # 所以这里只输出最近结果。

        recent = []

        for ord_id, g in groups.items():

            net = (
                g["pnl"]
                + g["fee"]
                + g["funding"]
            )

            recent.append(
                (
                    ord_id,
                    net,
                    g["instId"]
                )
            )

        recent.sort(
            key=lambda x: x[0],
            reverse=True
        )

        if recent:

            ord_id, net, inst_id = recent[0]

            log(
                f"最近成交汇总："
                f"{inst_id} "
                f"ordId={ord_id} "
                f"net={net:.4f}"
            )

    except Exception as e:

        log(
            f"读取成交记录失败：{e}"
        )


# =========================================================
# 单笔交易
# =========================================================

def execute_candidate(
    candidate,
    equity
):

    inst_id = candidate["instId"]

    direction = candidate[
        "direction_final"
    ]

    if direction not in (
        "LONG",
        "SHORT"
    ):
        return False

    score = (
        candidate["score_long_final"]
        if direction == "LONG"
        else candidate["score_short_final"]
    )

    # =====================================================
    # 防止同一根K线重复下单
    # =====================================================

    candle_ts = candidate[
        "candle_ts"
    ]

    if last_signal_candle.get(
        inst_id
    ) == candle_ts:

        log(
            f"{inst_id} "
            f"本根5m已经处理过"
        )

        return False

    # =====================================================
    # 当前价格
    # =====================================================

    price = candidate["price"]

    atr_value = candidate["atr"]

    info = get_instrument(
        inst_id
    )

    tick_sz = info["tickSz"]

    # =====================================================
    # 杠杆
    # =====================================================

    leverage = choose_leverage(
        score
    )

    leverage = max(
        MIN_LEVERAGE,
        min(
            MAX_LEVERAGE,
            leverage
        )
    )

    # =====================================================
    # TP / SL
    # =====================================================

    tp_sl = calculate_tp_sl(
        direction,
        price,
        atr_value,
        tick_sz
    )

    if not tp_sl:
        log(
            f"{inst_id} "
            f"TP/SL计算不合格"
        )
        return False

    stop_distance = tp_sl[
        "stop_distance"
    ]

    # =====================================================
    # 仓位
    # =====================================================

    sizing = calculate_position_size(
        inst_id,
        price,
        stop_distance,
        leverage,
        equity
    )

    if not sizing:

        log(
            f"{inst_id} "
            f"无法满足最小下单量/风险限制"
        )

        return False

    contracts = sizing[
        "contracts"
    ]

    # =====================================================
    # 输出
    # =====================================================

    log("================================================")
    log("发现最终交易机会")
    log(f"币种       : {inst_id}")
    log(f"方向       : {direction}")
    log(f"评分       : {score}")
    log(f"价格       : {price}")
    log(f"ATR        : {atr_value}")
    log(f"ATR%       : {candidate['atr_pct'] * 100:.3f}%")
    log(f"RSI        : {candidate['rsi']:.2f}")
    log(f"成交量     : {candidate['volume_ratio']:.2f}x")
    log(f"15m趋势    : {candidate['trend15']}")
    log(f"1H趋势     : {candidate['trend1h']}")
    log(f"杠杆       : {leverage}x")
    log(f"合约数量   : {contracts}")
    log(f"名义价值   : {sizing['notional']:.2f} USDT")
    log(f"保证金     : {sizing['margin']:.2f} USDT")
    log(f"理论风险   : {sizing['risk_amount']:.2f} USDT")
    log(f"止损       : {tp_sl['sl']}")
    log(f"止盈       : {tp_sl['tp']}")
    log(f"RR         : {tp_sl['rr']:.2f}")
    log("================================================")

    # =====================================================
    # 设置杠杆
    # =====================================================

    try:

        set_leverage(
            inst_id,
            leverage
        )

    except Exception as e:

        log(
            f"设置 {leverage}x 失败：{e}"
        )

        # 自动逐级降低
        fallback_levels = [
            12,
            10,
            7,
            5,
        ]

        success = False

        for lev in fallback_levels:

            if lev >= leverage:
                continue

            try:

                set_leverage(
                    inst_id,
                    lev
                )

                leverage = lev

                # 重新计算仓位
                sizing = calculate_position_size(
                    inst_id,
                    price,
                    stop_distance,
                    leverage,
                    equity
                )

                if not sizing:
                    return False

                contracts = sizing[
                    "contracts"
                ]

                log(
                    f"自动降低杠杆至 "
                    f"{lev}x"
                )

                success = True
                break

            except Exception:
                continue

        if not success:

            log(
                "无法设置任何可用杠杆，放弃交易"
            )

            return False

    # =====================================================
    # 下单前最后检查
    # =====================================================

    positions = get_open_positions()

    if positions:

        log(
            "下单前发现已有持仓，放弃本次机会"
        )

        return False

    # =====================================================
    # 记录处理K线
    # =====================================================

    last_signal_candle[
        inst_id
    ] = candle_ts

    # =====================================================
    # 下单
    # =====================================================

    order_info = place_market_order(
        inst_id,
        direction,
        contracts,
        leverage,
        tp_sl["tp"],
        tp_sl["sl"]
    )

    ord_id = order_info[
        "ordId"
    ]

    # =====================================================
    # 验证订单
    # =====================================================

    order = wait_order_fill(
        inst_id,
        ord_id
    )

    if not order:

        log(
            "无法获取订单最终状态"
        )

        return False

    state = order.get(
        "state"
    )

    if state != "filled":

        log(
            f"主订单没有完全成交："
            f"state={state}"
        )

        if state in (
            "live",
            "partially_filled"
        ):

            cancel_order(
                inst_id,
                ord_id
            )

        # 检查是否已经形成仓位
        time.sleep(1)

        positions = get_open_positions()

        for p in positions:

            if p["instId"] == inst_id:

                log(
                    "警告：订单未完全成交但已经形成仓位，"
                    "执行紧急平仓"
                )

                close_position_market(
                    inst_id,
                    p["side"],
                    p["pos"]
                )

        return False

    # =====================================================
    # 实际成交价
    # =====================================================

    actual_entry = safe_float(
        order.get("avgPx")
    )

    actual_fill_sz = safe_float(
        order.get("accFillSz")
    )

    if actual_entry <= 0:
        actual_entry = price

    if actual_fill_sz <= 0:
        log(
            "订单显示成交但成交数量异常"
        )
        return False

    # =====================================================
    # 注意：
    # OKX附带TP/SL会在主订单完全成交后生成。
    #
    # 这里等待一下，让附带策略单生成。
    # =====================================================

    time.sleep(1.5)

    try:

        verified_order = get_order(
            inst_id,
            ord_id
        )

        attach_info = (
            verified_order.get(
                "attachAlgoOrds",
                []
            )
            if verified_order
            else []
        )

        log(
            f"附带TP/SL信息："
            f"{attach_info}"
        )

    except Exception as e:

        log(
            f"验证附带TP/SL失败：{e}"
        )

    # =====================================================
    # 交易统计
    # =====================================================

    daily_state["trades"] = (
        int(
            daily_state.get(
                "trades",
                0
            )
        )
        + 1
    )

    save_state()

    log("================================================")
    log("交易成功建立")
    log(f"币种       : {inst_id}")
    log(f"方向       : {direction}")
    log(f"实际成交价 : {actual_entry}")
    log(f"实际数量   : {actual_fill_sz}")
    log(f"TP         : {tp_sl['tp']}")
    log(f"SL         : {tp_sl['sl']}")
    log("================================================")

    return True


# =========================================================
# 主循环
# =========================================================

def process():

    global state_loaded

    # =====================================================
    # 权益
    # =====================================================

    equity = get_equity()

    if not state_loaded:

        load_state(
            equity
        )

        state_loaded = True

    reset_daily_if_needed(
        equity
    )

    # 更新 peak
    if equity > safe_float(
        daily_state.get(
            "peak_equity",
            0
        )
    ):

        daily_state[
            "peak_equity"
        ] = equity

        save_state()

    # =====================================================
    # 今日收益
    # =====================================================

    start_equity = safe_float(
        daily_state[
            "start_equity"
        ]
    )

    if start_equity > 0:

        daily_return = (
            equity
            / start_equity
            - 1
        )

    else:

        daily_return = 0

    log("================================================")
    log(
        f"账户权益={equity:.2f} USDT"
    )

    log(
        f"今日收益="
        f"{daily_return * 100:.2f}%"
    )

    log(
        f"今日交易="
        f"{daily_state['trades']}"
    )

    log(
        f"连续亏损="
        f"{daily_state['consecutive_losses']}"
    )

    log("================================================")

    # =====================================================
    # 已有仓位
    # =====================================================

    positions = get_open_positions()

    if positions:

        log(
            "当前已有持仓，不再寻找新仓位："
        )

        for p in positions:

            log(
                f"{p['instId']} "
                f"{p['side']} "
                f"数量={p['pos']} "
                f"开仓={p['avgPx']} "
                f"标记={p['markPx']} "
                f"UPL={p['upl']:.4f}"
            )

        # 有仓位就不继续扫描
        return

    # =====================================================
    # 风险检查
    # =====================================================

    blocked, reason = risk_blocked(
        equity
    )

    if blocked:

        log(
            f"当前禁止开新仓：{reason}"
        )

        return

    # =====================================================
    # 最近成交
    # =====================================================

    update_trade_statistics()

    # =====================================================
    # 全市场扫描
    # =====================================================

    candidates = scan_market()

    if not candidates:

        return

    # =====================================================
    # 只选择第一名
    # =====================================================

    best = candidates[0]

    direction = best[
        "direction_final"
    ]

    score = (
        best["score_long_final"]
        if direction == "LONG"
        else best["score_short_final"]
    )

    log(
        f"最终选择："
        f"{best['instId']} "
        f"{direction} "
        f"score={score}"
    )

    # =====================================================
    # 执行
    # =====================================================

    execute_candidate(
        best,
        equity
    )


# =========================================================
# 启动
# =========================================================

def startup():

    log("")
    log("====================================================")
    log(" OKX 全市场永续强行情机器人 V2")
    log("====================================================")
    log(f"模式        : {'模拟盘' if SIMULATED_TRADING == '1' else '实盘'}")
    log(f"产品        : USDT 永续")
    log(f"保证金模式  : {TD_MODE}")
    log(f"扫描周期    : {SCAN_INTERVAL_SECONDS} 秒")
    log(f"最低信号分  : {MIN_SIGNAL_SCORE}")
    log(f"单笔风险    : {RISK_PER_TRADE_PCT * 100:.2f}%")
    log(f"单笔最大保证金 : {MAX_TRADE_MARGIN_PCT * 100:.2f}%")
    log(f"日目标      : {DAILY_PROFIT_TARGET_PCT * 100:.2f}%")
    log(f"日最大亏损  : {DAILY_MAX_LOSS_PCT * 100:.2f}%")
    log(f"最低RR      : {MIN_RISK_REWARD}")
    log(f"目标RR      : {TARGET_RISK_REWARD}")
    log(f"杠杆范围    : {MIN_LEVERAGE}x - {MAX_LEVERAGE}x")
    log("====================================================")

    if not API_KEY:
        raise RuntimeError(
            "没有读取到 OKX_API_KEY"
        )

    if not SECRET_KEY:
        raise RuntimeError(
            "没有读取到 OKX_SECRET_KEY"
        )

    if not PASSPHRASE:
        raise RuntimeError(
            "没有读取到 OKX_PASSPHRASE"
        )

    # =====================================================
    # 检查账户模式
    # =====================================================

    check_account_mode()

    # =====================================================
    # 产品
    # =====================================================

    load_instruments()

    # =====================================================
    # 账户
    # =====================================================

    equity = get_equity()

    load_state(
        equity
    )

    log(
        f"启动账户权益："
        f"{equity:.2f} USDT"
    )

    log(
        "机器人启动完成"
    )


def main():

    startup()

    while True:

        try:

            process()

        except KeyboardInterrupt:

            log(
                "收到停止指令，机器人退出"
            )

            break

        except Exception as e:

            log(
                f"主循环异常：{e}"
            )

            traceback.print_exc()

        time.sleep(
            SCAN_INTERVAL_SECONDS
        )


if __name__ == "__main__":
    main()
