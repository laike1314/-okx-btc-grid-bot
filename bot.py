# -*- coding: utf-8 -*-

"""
OKX 永续自动交易模拟盘 V2.9

主要功能：
1. OKX 模拟盘
2. 自动扫描 CoinGecko 市值 Top30
3. OKX USDT 永续合约
4. EMA + RSI + MACD + ATR
5. 5m 信号 + 15m趋势 + 1H趋势
6. 动态杠杆 5x ~ 20x
7. 自动根据合约实际最大杠杆限制
8. 自动仓位计算
9. 自动开多 / 开空
10. 独立 OCO 止盈止损
11. 实时 Mark Price
12. 实时浮盈 / 浮亏 / ROI
13. 平仓后读取 OKX realizedPnl
14. 连续亏损保护
15. 同时只允许一个持仓
16. UTC+8 日志
"""

import os
import time
import json
import hmac
import base64
import hashlib
import threading
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

import requests


# =========================================================
# 版本
# =========================================================

VERSION = "V2.9"


# =========================================================
# OKX 基础配置
# =========================================================

BASE_URL = "https://www.okx.com"

INST_TYPE = "SWAP"
SETTLE_CCY = "USDT"

TD_MODE = "cross"
POS_MODE = "net"

# 1 = 模拟盘
SIMULATED_TRADING = "1"

# 默认扫描所有符合条件的 USDT 永续
TARGET_INST_ID = ""


# =========================================================
# K线
# =========================================================

SIGNAL_BAR = "5m"
TREND_BAR_15M = "15m"
TREND_BAR_1H = "1H"

LOOP_SECONDS = 20


# =========================================================
# Top30
# =========================================================

TOP30_CACHE_SECONDS = 600

MIN_24H_VOLUME_USD = Decimal("3000000")

SCAN_LIMIT = 30


# =========================================================
# 指标参数
# =========================================================

EMA_FAST = 9
EMA_SLOW = 21

RSI_PERIOD = 14

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

ATR_PERIOD = 14


# =========================================================
# RSI
# =========================================================

RSI_LONG_MIN = Decimal("52")
RSI_LONG_MAX = Decimal("72")

RSI_SHORT_MIN = Decimal("28")
RSI_SHORT_MAX = Decimal("48")


# =========================================================
# 风控
# =========================================================

MIN_SIGNAL_SCORE = 4

MAX_MARGIN_PCT = Decimal("0.08")

RISK_PER_TRADE_PCT = Decimal("0.005")

MIN_LEVERAGE = Decimal("5")

MAX_LEVERAGE = Decimal("20")

MAX_CONSECUTIVE_LOSSES = 3

LOSS_COOLDOWN_SECONDS = 1800


# =========================================================
# TP / SL
# =========================================================

TARGET_SL_ROI_PCT = Decimal("1.5")

TARGET_TP_ROI_PCT = Decimal("3.5")

RISK_REWARD = Decimal("1.8")

ATR_SL_FRACTION = Decimal("0.60")

MIN_SL_PRICE_PCT = Decimal("0.0025")

MAX_SL_PRICE_PCT = Decimal("0.025")

MAX_TP_PRICE_PCT = Decimal("0.045")

MIN_TRIGGER_GAP_PCT = Decimal("0.0010")


# =========================================================
# OCO
# =========================================================

OCO_RETRY_COUNT = 3

OCO_VERIFY_WAIT = 1.2

EMERGENCY_CLOSE_ON_NEW_POSITION_PROTECTION_FAILURE = True


# =========================================================
# Session
# =========================================================

session = requests.Session()

session.headers.update({
    "Content-Type": "application/json",
    "x-simulated-trading": SIMULATED_TRADING
})


# =========================================================
# API KEY
# =========================================================

API_KEY = os.getenv("OKX_API_KEY")
SECRET_KEY = os.getenv("OKX_SECRET_KEY")
PASSPHRASE = os.getenv("OKX_PASSPHRASE")

if not API_KEY or not SECRET_KEY or not PASSPHRASE:
    raise RuntimeError(
        "缺少 OKX API 环境变量："
        "OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE"
    )


# =========================================================
# UTC+8
# =========================================================

UTC8 = timezone(timedelta(hours=8))


def now_ms():
    return str(int(time.time() * 1000))


def now_str():
    return datetime.now(UTC8).strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print(f"[{now_str()}] {msg}", flush=True)


# =========================================================
# Decimal 工具
# =========================================================

def D(value, default="0"):
    try:
        if value is None or value == "":
            return Decimal(default)
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def fmt_decimal(value, digits=12):
    value = D(value)

    s = format(value, "f")

    if "." in s:
        a, b = s.split(".", 1)
        b = b[:digits]
        s = a + "." + b

    s = s.rstrip("0").rstrip(".")

    if s in ("", "-0"):
        return "0"

    return s


def round_down(value, step):
    value = D(value)
    step = D(step)

    if step <= 0:
        return value

    return (value / step).to_integral_value(
        rounding=ROUND_DOWN
    ) * step


def round_up(value, step):
    value = D(value)
    step = D(step)

    if step <= 0:
        return value

    return (value / step).to_integral_value(
        rounding=ROUND_UP
    ) * step


def clamp(value, low, high):
    value = D(value)

    if value < low:
        return low

    if value > high:
        return high

    return value


# =========================================================
# OKX 签名
# =========================================================

def get_timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00",
        "Z"
    )


def make_signature(timestamp, method, request_path, body=""):
    message = timestamp + method.upper() + request_path + body

    mac = hmac.new(
        SECRET_KEY.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    )

    return base64.b64encode(mac.digest()).decode()


# =========================================================
# OKX 请求
# =========================================================

def okx_request(
    method,
    path,
    params=None,
    body=None,
    private=False,
    timeout=15
):

    method = method.upper()

    params = params or {}

    body = body or {}

    query = ""

    if params:
        clean_params = {}

        for k, v in params.items():
            if v is not None and v != "":
                clean_params[k] = v

        if clean_params:
            query = "?" + urlencode(clean_params)

    request_path = path + query

    body_text = ""

    if method in ("POST", "PUT"):
        body_text = json.dumps(
            body,
            separators=(",", ":"),
            ensure_ascii=False
        )

    headers = {
        "Content-Type": "application/json",
        "x-simulated-trading": SIMULATED_TRADING
    }

    if private:

        timestamp = get_timestamp()

        sign = make_signature(
            timestamp,
            method,
            request_path,
            body_text
        )

        headers.update({
            "OK-ACCESS-KEY": API_KEY,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": PASSPHRASE
        })

    url = BASE_URL + request_path

    try:

        if method == "GET":

            response = session.get(
                url,
                headers=headers,
                timeout=timeout
            )

        elif method == "POST":

            response = session.post(
                url,
                headers=headers,
                data=body_text,
                timeout=timeout
            )

        elif method == "DELETE":

            response = session.delete(
                url,
                headers=headers,
                timeout=timeout
            )

        else:

            raise RuntimeError(
                f"不支持 HTTP 方法: {method}"
            )

        response.raise_for_status()

        data = response.json()

    except Exception as e:

        raise RuntimeError(
            f"OKX 请求失败 {method} {request_path}: {e}"
        )

    if str(data.get("code")) != "0":

        raise RuntimeError(
            f"OKX API错误: {data}"
        )

    return data.get("data", [])


# =========================================================
# CoinGecko Top30
# =========================================================

top30_cache = {
    "time": 0,
    "symbols": set(),
    "rank": {}
}


def get_top30_market_caps():

    global top30_cache

    now = time.time()

    if now - top30_cache["time"] < TOP30_CACHE_SECONDS:

        return (
            top30_cache["symbols"],
            top30_cache["rank"]
        )

    url = (
        "https://api.coingecko.com/api/v3/coins/markets"
    )

    params = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": "30",
        "page": "1",
        "sparkline": "false"
    }

    try:

        r = requests.get(
            url,
            params=params,
            timeout=15
        )

        r.raise_for_status()

        rows = r.json()

        symbols = set()
        rank = {}

        for i, row in enumerate(rows, start=1):

            symbol = str(
                row.get("symbol", "")
            ).upper().strip()

            if not symbol:
                continue

            symbols.add(symbol)
            rank[symbol] = i

        if not symbols:
            raise RuntimeError(
                "CoinGecko Top30 返回为空"
            )

        top30_cache = {
            "time": now,
            "symbols": symbols,
            "rank": rank
        }

        return symbols, rank

    except Exception as e:

        log(f"⚠️ CoinGecko Top30 获取失败: {e}")

        return (
            set(),
            {}
        )


# =========================================================
# 合约缓存
# =========================================================

instrument_cache = {}

instrument_cache_lock = threading.Lock()


def get_instrument(inst_id):

    with instrument_cache_lock:

        if inst_id in instrument_cache:

            return instrument_cache[inst_id]

    rows = okx_request(
        "GET",
        "/api/v5/public/instruments",
        params={
            "instType": INST_TYPE,
            "instId": inst_id
        }
    )

    if not rows:

        raise RuntimeError(
            f"找不到合约信息: {inst_id}"
        )

    info = rows[0]

    with instrument_cache_lock:

        instrument_cache[inst_id] = info

    return info


# =========================================================
# 获取全部 USDT 永续
# =========================================================

def get_all_active_usdt_swaps():

    rows = okx_request(
        "GET",
        "/api/v5/public/instruments",
        params={
            "instType": INST_TYPE
        }
    )

    result = []

    for row in rows:

        inst_id = row.get("instId", "")

        if not inst_id.endswith("-USDT-SWAP"):
            continue

        if row.get("state") != "live":
            continue

        if row.get("settleCcy") != "USDT":
            continue

        result.append(row)

        with instrument_cache_lock:

            instrument_cache[inst_id] = row

    return result


# =========================================================
# 获取 Top30 OKX 合约
# =========================================================

def get_top30_okx_instruments():

    symbols, ranks = get_top30_market_caps()

    if not symbols:

        return []

    all_swaps = get_all_active_usdt_swaps()

    result = []

    for row in all_swaps:

        inst_id = row.get("instId", "")

        parts = inst_id.split("-")

        if len(parts) < 3:
            continue

        base = parts[0].upper()

        if base not in symbols:
            continue

        row = dict(row)

        row["_top30_rank"] = ranks.get(
            base,
            999
        )

        row["_baseCcy"] = base

        result.append(row)

    result.sort(
        key=lambda x: x.get(
            "_top30_rank",
            999
        )
    )

    if TARGET_INST_ID:

        result = [
            x for x in result
            if x.get("instId") == TARGET_INST_ID
        ]

    return result[:SCAN_LIMIT]


# =========================================================
# Ticker
# =========================================================

def get_ticker(inst_id):

    rows = okx_request(
        "GET",
        "/api/v5/market/ticker",
        params={
            "instId": inst_id
        }
    )

    if not rows:

        raise RuntimeError(
            f"ticker为空: {inst_id}"
        )

    row = rows[0]

    mark = D(
        row.get("markPx") or row.get("last")
    )

    last = D(
        row.get("last")
    )

    return {
        "markPx": mark,
        "last": last,
        "raw": row
    }


def get_all_tickers():

    rows = okx_request(
        "GET",
        "/api/v5/market/tickers",
        params={
            "instType": INST_TYPE
        }
    )

    result = {}

    for row in rows:

        inst_id = row.get("instId")

        if inst_id:

            result[inst_id] = row

    return result


# =========================================================
# K线
# =========================================================

def get_candles(
    inst_id,
    bar=SIGNAL_BAR,
    limit=120
):

    rows = okx_request(
        "GET",
        "/api/v5/market/candles",
        params={
            "instId": inst_id,
            "bar": bar,
            "limit": str(limit)
        }
    )

    candles = []

    for row in rows:

        if len(row) < 9:
            continue

        try:

            candles.append({
                "ts": int(row[0]),
                "open": D(row[1]),
                "high": D(row[2]),
                "low": D(row[3]),
                "close": D(row[4]),
                "vol": D(row[5]),
                "confirm": str(row[8])
            })

        except Exception:
            continue

    candles.sort(
        key=lambda x: x["ts"]
    )

    # 只保留已完成K线
    candles = [
        x for x in candles
        if x["confirm"] == "1"
    ]

    return candles


# =========================================================
# EMA
# =========================================================

def ema(values, period):

    values = [D(x) for x in values]

    if len(values) < period:
        return None

    alpha = Decimal("2") / (
        Decimal(period) + Decimal("1")
    )

    result = values[0]

    for price in values[1:]:

        result = (
            price * alpha
            + result * (Decimal("1") - alpha)
        )

    return result


# =========================================================
# RSI
# =========================================================

def calculate_rsi(values, period=14):

    values = [D(x) for x in values]

    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):

        change = values[i] - values[i - 1]

        if change > 0:

            gains.append(change)
            losses.append(Decimal("0"))

        else:

            gains.append(Decimal("0"))
            losses.append(abs(change))

    avg_gain = sum(
        gains[:period],
        Decimal("0")
    ) / Decimal(period)

    avg_loss = sum(
        losses[:period],
        Decimal("0")
    ) / Decimal(period)

    for i in range(period, len(gains)):

        avg_gain = (
            avg_gain * Decimal(period - 1)
            + gains[i]
        ) / Decimal(period)

        avg_loss = (
            avg_loss * Decimal(period - 1)
            + losses[i]
        ) / Decimal(period)

    if avg_loss == 0:

        return Decimal("100")

    rs = avg_gain / avg_loss

    return Decimal("100") - (
        Decimal("100") / (
            Decimal("1") + rs
        )
    )


# =========================================================
# MACD
# =========================================================

def calculate_macd(values):

    values = [D(x) for x in values]

    if len(values) < MACD_SLOW + MACD_SIGNAL:

        return None, None

    fast = ema(values, MACD_FAST)
    slow = ema(values, MACD_SLOW)

    if fast is None or slow is None:

        return None, None

    macd_line = fast - slow

    # 用历史 MACD 序列计算 signal
    macd_values = []

    for i in range(
        MACD_SLOW,
        len(values) + 1
    ):

        subset = values[:i]

        ef = ema(
            subset,
            MACD_FAST
        )

        es = ema(
            subset,
            MACD_SLOW
        )

        if ef is not None and es is not None:

            macd_values.append(
                ef - es
            )

    if len(macd_values) < MACD_SIGNAL:

        return macd_line, None

    signal = ema(
        macd_values,
        MACD_SIGNAL
    )

    return macd_line, signal


# =========================================================
# ATR
# =========================================================

def calculate_atr(candles, period=14):

    if len(candles) < period + 1:

        return None

    trs = []

    for i in range(1, len(candles)):

        current = candles[i]
        previous = candles[i - 1]

        high = current["high"]
        low = current["low"]
        prev_close = previous["close"]

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )

        trs.append(tr)

    if len(trs) < period:

        return None

    return sum(
        trs[-period:],
        Decimal("0")
    ) / Decimal(period)


# =========================================================
# 趋势
# =========================================================

def get_trend(inst_id, bar):

    candles = get_candles(
        inst_id,
        bar,
        100
    )

    if len(candles) < 30:

        return "NONE"

    closes = [
        x["close"]
        for x in candles
    ]

    fast = ema(
        closes,
        EMA_FAST
    )

    slow = ema(
        closes,
        EMA_SLOW
    )

    if fast is None or slow is None:

        return "NONE"

    if fast > slow:

        return "LONG"

    if fast < slow:

        return "SHORT"

    return "NONE"


# =========================================================
# 信号
# =========================================================

def calculate_signal(inst_id):

    candles = get_candles(
        inst_id,
        SIGNAL_BAR,
        140
    )

    if len(candles) < 60:

        return None

    closes = [
        x["close"]
        for x in candles
    ]

    price = closes[-1]

    fast = ema(
        closes,
        EMA_FAST
    )

    slow = ema(
        closes,
        EMA_SLOW
    )

    rsi = calculate_rsi(
        closes,
        RSI_PERIOD
    )

    macd_line, macd_signal = calculate_macd(
        closes
    )

    atr = calculate_atr(
        candles,
        ATR_PERIOD
    )

    if any(
        x is None
        for x in [
            fast,
            slow,
            rsi,
            macd_line,
            macd_signal,
            atr
        ]
    ):

        return None

    if price <= 0:

        return None

    atr_pct = atr / price

    if atr_pct < Decimal("0.0015"):

        return None

    trend_15m = get_trend(
        inst_id,
        TREND_BAR_15M
    )

    trend_1h = get_trend(
        inst_id,
        TREND_BAR_1H
    )

    long_score = 0
    short_score = 0

    # EMA
    if fast > slow:

        long_score += 2

    elif fast < slow:

        short_score += 2

    # 价格位置
    if price > fast:

        long_score += 1

    elif price < fast:

        short_score += 1

    # RSI
    if (
        RSI_LONG_MIN
        <= rsi
        <= RSI_LONG_MAX
    ):

        long_score += 1

    if (
        RSI_SHORT_MIN
        <= rsi
        <= RSI_SHORT_MAX
    ):

        short_score += 1

    # MACD
    if (
        macd_line > macd_signal
        and macd_line > 0
    ):

        long_score += 2

    elif (
        macd_line < macd_signal
        and macd_line < 0
    ):

        short_score += 2

    # 15m
    if trend_15m == "LONG":

        long_score += 1

    elif trend_15m == "SHORT":

        short_score += 1

    # 1H
    if trend_1h == "LONG":

        long_score += 1

    elif trend_1h == "SHORT":

        short_score += 1

    if (
        long_score < MIN_SIGNAL_SCORE
        and short_score < MIN_SIGNAL_SCORE
    ):

        return None

    if long_score > short_score:

        side = "LONG"
        score = long_score

    elif short_score > long_score:

        side = "SHORT"
        score = short_score

    else:

        return None

    return {
        "instId": inst_id,
        "side": side,
        "score": score,
        "price": price,
        "atr": atr,
        "atr_pct": atr_pct,
        "rsi": rsi,
        "trend15m": trend_15m,
        "trend1h": trend_1h
    }


# =========================================================
# 账户余额
# =========================================================

def get_equity():

    rows = okx_request(
        "GET",
        "/api/v5/account/balance",
        private=True
    )

    if not rows:

        return Decimal("0")

    details = rows[0].get(
        "details",
        []
    )

    for item in details:

        if item.get("ccy") != "USDT":
            continue

        eq = D(item.get("eq"))

        if eq > 0:

            return eq

        cash = D(item.get("cashBal"))

        if cash > 0:

            return cash

        avail = D(item.get("availBal"))

        if avail > 0:

            return avail

    return Decimal("0")


# =========================================================
# 仓位
# =========================================================

def get_positions(inst_id=None):

    params = {
        "instType": INST_TYPE
    }

    if inst_id:

        params["instId"] = inst_id

    rows = okx_request(
        "GET",
        "/api/v5/account/positions",
        params=params,
        private=True
    )

    result = []

    for row in rows:

        pos = D(row.get("pos"))

        if pos == 0:

            continue

        result.append(row)

    return result


def get_position(inst_id):

    positions = get_positions(inst_id)

    if positions:

        return positions[0]

    return None


def position_side(pos):

    amount = D(pos.get("pos"))

    if amount > 0:

        return "LONG"

    if amount < 0:

        return "SHORT"

    return "NONE"


# =========================================================
# 实时持仓显示
# =========================================================

def print_live_position(pos):

    inst_id = pos.get("instId")

    ticker = get_ticker(inst_id)

    mark = ticker["markPx"]

    avg = D(pos.get("avgPx"))

    qty = abs(D(pos.get("pos")))

    lever = D(pos.get("lever"))

    upl = D(pos.get("upl"))

    upl_ratio = D(pos.get("uplRatio"))

    side = position_side(pos)

    ct_val = D(
        get_instrument(inst_id).get(
            "ctVal"
        )
    )

    if avg > 0:

        if side == "LONG":

            price_change_pct = (
                mark - avg
            ) / avg * Decimal("100")

        else:

            price_change_pct = (
                avg - mark
            ) / avg * Decimal("100")

    else:

        price_change_pct = Decimal("0")

    local_pnl = Decimal("0")

    if ct_val > 0 and avg > 0:

        if side == "LONG":

            local_pnl = (
                mark - avg
            ) * qty * ct_val

        else:

            local_pnl = (
                avg - mark
            ) * qty * ct_val

    tp = Decimal("0")
    sl = Decimal("0")

    close_algos = pos.get(
        "closeOrderAlgo"
    )

    if isinstance(close_algos, list):

        for algo in close_algos:

            tp = D(
                algo.get("tpTriggerPx")
            )

            sl = D(
                algo.get("slTriggerPx")
            )

            if tp > 0 or sl > 0:

                break

    tp_distance = Decimal("0")
    sl_distance = Decimal("0")

    if mark > 0:

        if tp > 0:

            tp_distance = abs(
                tp - mark
            ) / mark * Decimal("100")

        if sl > 0:

            sl_distance = abs(
                mark - sl
            ) / mark * Decimal("100")

    liq_px = D(
        pos.get("liqPx")
    )

    log(
        f"📊 持仓 {inst_id} | "
        f"{side} | "
        f"开仓 {fmt_decimal(avg)} | "
        f"Mark {fmt_decimal(mark)} | "
        f"杠杆 {fmt_decimal(lever)}x | "
        f"数量 {fmt_decimal(qty)}"
    )

    log(
        f"💰 浮盈亏 {fmt_decimal(upl, 8)} USDT | "
        f"ROI {fmt_decimal(upl_ratio * 100, 4)}% | "
        f"价格变化 {fmt_decimal(price_change_pct, 4)}%"
    )

    log(
        f"📌 本地计算PnL约 "
        f"{fmt_decimal(local_pnl, 8)} USDT"
    )

    if tp > 0:

        log(
            f"🎯 TP {fmt_decimal(tp)} | "
            f"距离当前 {fmt_decimal(tp_distance, 4)}%"
        )

    else:

        log("⚠️ 当前没有检测到 TP")

    if sl > 0:

        log(
            f"🛑 SL {fmt_decimal(sl)} | "
            f"距离当前 {fmt_decimal(sl_distance, 4)}%"
        )

    else:

        log("⚠️ 当前没有检测到 SL")

    if liq_px > 0:

        log(
            f"☠️ 预估强平价 {fmt_decimal(liq_px)}"
        )

    return {
        "mark": mark,
        "upl": upl,
        "roi": upl_ratio,
        "tp": tp,
        "sl": sl
    }


# =========================================================
# 动态杠杆
# =========================================================

def leverage_from_score(score):

    if score <= 4:

        return Decimal("5")

    if score == 5:

        return Decimal("8")

    if score == 6:

        return Decimal("10")

    if score == 7:

        return Decimal("15")

    return Decimal("20")


def get_max_allowed_leverage(inst_id):

    info = get_instrument(inst_id)

    max_lev = D(
        info.get("lever")
    )

    if max_lev <= 0:

        # 如果 instrument 没给，
        # 默认按照用户策略最大20倍
        return MAX_LEVERAGE

    return max_lev


def get_actual_leverage(inst_id, desired):

    max_allowed = get_max_allowed_leverage(
        inst_id
    )

    actual = min(
        D(desired),
        MAX_LEVERAGE,
        max_allowed
    )

    if actual < MIN_LEVERAGE:

        raise RuntimeError(
            f"{inst_id} 最大杠杆 "
            f"{fmt_decimal(max_allowed)}x "
            f"低于机器人最低杠杆 "
            f"{fmt_decimal(MIN_LEVERAGE)}x"
        )

    return actual


def set_leverage(inst_id, leverage):

    result = okx_request(
        "POST",
        "/api/v5/account/set-leverage",
        body={
            "instId": inst_id,
            "lever": fmt_decimal(leverage),
            "mgnMode": TD_MODE
        },
        private=True
    )

    return result


# =========================================================
# 保护价格
# =========================================================

def calculate_protection(
    pos,
    atr=None,
    adopted=False
):

    inst_id = pos.get("instId")

    side = position_side(pos)

    avg_px = D(pos.get("avgPx"))

    lever = D(pos.get("lever"))

    if lever <= 0:

        lever = MIN_LEVERAGE

    ticker = get_ticker(inst_id)

    mark = ticker["markPx"]

    if mark <= 0:

        raise RuntimeError(
            "Mark Price 无效"
        )

    # 如果没有ATR，重新计算
    if atr is None:

        candles = get_candles(
            inst_id,
            SIGNAL_BAR,
            100
        )

        atr = calculate_atr(
            candles,
            ATR_PERIOD
        )

    if atr is None:

        raise RuntimeError(
            "无法计算 ATR"
        )

    atr_pct = atr / mark

    atr_pct = clamp(
        atr_pct,
        Decimal("0.0005"),
        Decimal("0.08")
    )

    # ROI -> 价格波动
    target_sl_pct = (
        TARGET_SL_ROI_PCT
        / lever
        / Decimal("100")
    )

    target_tp_pct = (
        TARGET_TP_ROI_PCT
        / lever
        / Decimal("100")
    )

    atr_sl_pct = (
        atr_pct
        * ATR_SL_FRACTION
    )

    sl_pct = max(
        target_sl_pct,
        atr_sl_pct,
        MIN_SL_PRICE_PCT
    )

    sl_pct = min(
        sl_pct,
        MAX_SL_PRICE_PCT
    )

    tp_pct = max(
        target_tp_pct,
        sl_pct * RISK_REWARD
    )

    tp_pct = min(
        tp_pct,
        MAX_TP_PRICE_PCT
    )

    # 新持仓用平均开仓价
    # 已有持仓如果重新补保护，
    # 采用当前 Mark 重新计算时，
    # 必须确保不会把触发价放到当前价格错误的一侧
    base = avg_px

    if base <= 0:

        base = mark

    # 如果已有仓位已经明显盈利，
    # 重新挂保护时允许以当前价作为计算基础
    if adopted:

        if side == "LONG" and mark > avg_px:

            base = mark

        elif side == "SHORT" and mark < avg_px:

            base = mark

    if side == "LONG":

        tp = base * (
            Decimal("1") + tp_pct
        )

        sl = base * (
            Decimal("1") - sl_pct
        )

        # 确保 TP / SL 相对于当前 Mark 在正确位置
        if tp <= mark:

            tp = mark * (
                Decimal("1")
                + MIN_TRIGGER_GAP_PCT
            )

        if sl >= mark:

            sl = mark * (
                Decimal("1")
                - MIN_TRIGGER_GAP_PCT
            )

    elif side == "SHORT":

        tp = base * (
            Decimal("1") - tp_pct
        )

        sl = base * (
            Decimal("1") + sl_pct
        )

        if tp >= mark:

            tp = mark * (
                Decimal("1")
                - MIN_TRIGGER_GAP_PCT
            )

        if sl <= mark:

            sl = mark * (
                Decimal("1")
                + MIN_TRIGGER_GAP_PCT
            )

    else:

        raise RuntimeError(
            "无法识别仓位方向"
        )

    info = get_instrument(inst_id)

    tick_sz = D(
        info.get("tickSz")
    )

    if tick_sz > 0:

        if side == "LONG":

            tp = round_up(
                tp,
                tick_sz
            )

            sl = round_down(
                sl,
                tick_sz
            )

        else:

            tp = round_down(
                tp,
                tick_sz
            )

            sl = round_up(
                sl,
                tick_sz
            )

    return {
        "instId": inst_id,
        "side": side,
        "base": base,
        "mark": mark,
        "atr": atr,
        "atr_pct": atr_pct,
        "sl_pct": sl_pct,
        "tp_pct": tp_pct,
        "tp": tp,
        "sl": sl,
        "lever": lever
    }


# =========================================================
# 仓位数量
# =========================================================

def calculate_size(
    equity,
    price,
    leverage,
    sl_pct,
    inst_id
):

    info = get_instrument(
        inst_id
    )

    ct_val = D(
        info.get("ctVal")
    )

    lot_sz = D(
        info.get("lotSz")
    )

    min_sz = D(
        info.get("minSz")
    )

    if ct_val <= 0:

        raise RuntimeError(
            f"{inst_id} ctVal 无效"
        )

    if lot_sz <= 0:

        raise RuntimeError(
            f"{inst_id} lotSz 无效"
        )

    if min_sz <= 0:

        min_sz = lot_sz

    # 单笔最大保证金
    max_margin = (
        equity
        * MAX_MARGIN_PCT
    )

    # 风险预算
    risk_budget = (
        equity
        * RISK_PER_TRADE_PCT
    )

    price_risk = (
        price
        * sl_pct
    )

    if price_risk <= 0:

        raise RuntimeError(
            "价格风险无效"
        )

    # 按风险计算数量
    qty_by_risk = (
        risk_budget
        / (
            price_risk
            * ct_val
        )
    )

    # 按保证金计算最大数量
    qty_by_margin = (
        max_margin
        * leverage
        / (
            price
            * ct_val
        )
    )

    qty = min(
        qty_by_risk,
        qty_by_margin
    )

    qty = round_down(
        qty,
        lot_sz
    )

    if qty < min_sz:

        # 尝试最小下单量
        qty = min_sz

        required_margin = (
            qty
            * price
            * ct_val
            / leverage
        )

        if required_margin > max_margin:

            raise RuntimeError(
                f"{inst_id} 最小下单量 "
                f"{fmt_decimal(min_sz)} "
                f"超过当前保证金限制"
            )

    return qty


# =========================================================
# 市价开仓
# =========================================================

def place_market_order(
    inst_id,
    side,
    qty
):

    order_side = (
        "buy"
        if side == "LONG"
        else "sell"
    )

    body = {
        "instId": inst_id,
        "tdMode": TD_MODE,
        "side": order_side,
        "posSide": "net",
        "ordType": "market",
        "sz": fmt_decimal(qty)
    }

    rows = okx_request(
        "POST",
        "/api/v5/trade/order",
        body=body,
        private=True
    )

    if not rows:

        raise RuntimeError(
            "下单接口没有返回数据"
        )

    return rows[0]


# =========================================================
# 查询订单
# =========================================================

def get_order(
    inst_id,
    ord_id
):

    rows = okx_request(
        "GET",
        "/api/v5/trade/order",
        params={
            "instId": inst_id,
            "ordId": ord_id
        },
        private=True
    )

    if not rows:

        return None

    return rows[0]


def wait_order_filled(
    inst_id,
    ord_id,
    timeout=15
):

    start = time.time()

    while time.time() - start < timeout:

        order = get_order(
            inst_id,
            ord_id
        )

        if not order:

            time.sleep(0.5)
            continue

        state = order.get("state")

        if state == "filled":

            return order

        if state in (
            "canceled",
            "mmp_canceled"
        ):

            raise RuntimeError(
                f"订单未成交，状态={state}"
            )

        time.sleep(0.5)

    raise RuntimeError(
        "等待订单成交超时"
    )


# =========================================================
# OCO 查询
# =========================================================

def get_pending_algos(
    inst_id,
    ord_type="oco"
):

    rows = okx_request(
        "GET",
        "/api/v5/trade/orders-algo-pending",
        params={
            "ordType": ord_type,
            "instId": inst_id
        },
        private=True
    )

    return rows


# =========================================================
# 取消 Algo
# =========================================================

def cancel_algo(
    inst_id,
    algo_id
):

    if not algo_id:

        return

    body = [
        {
            "algoId": str(algo_id),
            "instId": inst_id
        }
    ]

    try:

        okx_request(
            "POST",
            "/api/v5/trade/cancel-algos",
            body=body,
            private=True
        )

    except Exception as e:

        log(
            f"⚠️ 取消保护单失败 "
            f"{inst_id} {algo_id}: {e}"
        )


def cancel_existing_algos(inst_id):

    for ord_type in (
        "oco",
        "conditional"
    ):

        try:

            rows = get_pending_algos(
                inst_id,
                ord_type
            )

            for row in rows:

                algo_id = row.get(
                    "algoId"
                )

                if algo_id:

                    log(
                        f"♻️ 取消旧保护单 "
                        f"{ord_type} {algo_id}"
                    )

                    cancel_algo(
                        inst_id,
                        algo_id
                    )

        except Exception as e:

            log(
                f"⚠️ 查询 {ord_type} 保护单失败: {e}"
            )


# =========================================================
# 下 OCO
# =========================================================

def place_oco(
    pos,
    plan
):

    inst_id = pos.get("instId")

    side = position_side(pos)

    if side == "LONG":

        close_side = "sell"

    elif side == "SHORT":

        close_side = "buy"

    else:

        raise RuntimeError(
            "OCO无法识别仓位方向"
        )

    # 注意：
    # 永续净持仓全平 OCO：
    # closeFraction = 1
    # reduceOnly = true
    # 不再强制同时传 sz
    body = {
        "instId": inst_id,
        "tdMode": TD_MODE,
        "side": close_side,
        "posSide": "net",
        "ordType": "oco",
        "closeFraction": "1",
        "reduceOnly": True,

        "tpTriggerPx": fmt_decimal(
            plan["tp"]
        ),

        "tpOrdPx": "-1",

        "tpTriggerPxType": "mark",

        "slTriggerPx": fmt_decimal(
            plan["sl"]
        ),

        "slOrdPx": "-1",

        "slTriggerPxType": "mark"
    }

    log(
        f"📌 准备挂 OCO | "
        f"{inst_id} | "
        f"{side} | "
        f"TP={fmt_decimal(plan['tp'])} | "
        f"SL={fmt_decimal(plan['sl'])}"
    )

    rows = okx_request(
        "POST",
        "/api/v5/trade/order-algo",
        body=body,
        private=True
    )

    if not rows:

        raise RuntimeError(
            "OCO接口没有返回数据"
        )

    algo_id = rows[0].get(
        "algoId"
    )

    if not algo_id:

        raise RuntimeError(
            f"OCO没有返回 algoId: {rows}"
        )

    log(
        f"✅ OCO 已提交 | "
        f"algoId={algo_id}"
    )

    return rows[0]


# =========================================================
# 验证 OCO
# =========================================================

def verify_oco(
    pos,
    plan
):

    inst_id = pos.get("instId")

    rows = get_pending_algos(
        inst_id,
        "oco"
    )

    if not rows:

        return False

    mark = get_ticker(
        inst_id
    )["markPx"]

    side = position_side(pos)

    for row in rows:

        state = row.get("state", "")

        if state not in (
            "",
            "live",
            "effective",
            "partially_effective"
        ):

            continue

        tp = D(
            row.get("tpTriggerPx")
        )

        sl = D(
            row.get("slTriggerPx")
        )

        if tp <= 0 or sl <= 0:

            continue

        if side == "LONG":

            if not (
                tp > mark
                and sl < mark
            ):

                continue

        elif side == "SHORT":

            if not (
                tp < mark
                and sl > mark
            ):

                continue

        else:

            continue

        log(
            f"🟢 OCO验证成功 | "
            f"TP={fmt_decimal(tp)} | "
            f"SL={fmt_decimal(sl)} | "
            f"Mark={fmt_decimal(mark)}"
        )

        return True

    return False


# =========================================================
# 紧急平仓
# =========================================================

def emergency_close(inst_id):

    log(
        f"🚨 启动紧急平仓: {inst_id}"
    )

    try:

        okx_request(
            "POST",
            "/api/v5/trade/close-position",
            body={
                "instId": inst_id,
                "mgnMode": TD_MODE
            },
            private=True
        )

        log(
            f"🚨 紧急平仓请求已发送: {inst_id}"
        )

    except Exception as e:

        log(
            f"❌ 紧急平仓失败: {e}"
        )


# =========================================================
# 确保保护单
# =========================================================

def ensure_protection(
    pos,
    adopted=False,
    new_position=False,
    atr=None
):

    inst_id = pos.get("instId")

    for attempt in range(
        1,
        OCO_RETRY_COUNT + 1
    ):

        try:

            current_pos = get_position(
                inst_id
            )

            if not current_pos:

                log(
                    f"⚠️ {inst_id} 已无持仓，"
                    f"停止挂保护"
                )

                return False

            # 每次都重新获取 Mark
            ticker = get_ticker(
                inst_id
            )

            mark = ticker["markPx"]

            log(
                f"🔄 TP/SL第 {attempt}/{OCO_RETRY_COUNT} 次检查 | "
                f"Mark={fmt_decimal(mark)}"
            )

            # 先检查当前是否已有有效 OCO
            try:

                if verify_oco(
                    current_pos,
                    {}
                ):

                    log(
                        f"🟢 {inst_id} 已存在有效 OCO"
                    )

                    return True

            except Exception as e:

                log(
                    f"⚠️ 检查现有OCO失败: {e}"
                )

            # 清理旧保护单
            cancel_existing_algos(
                inst_id
            )

            time.sleep(0.3)

            # 重新计算 ATR
            candles = get_candles(
                inst_id,
                SIGNAL_BAR,
                100
            )

            fresh_atr = calculate_atr(
                candles,
                ATR_PERIOD
            )

            if fresh_atr is None:

                raise RuntimeError(
                    "ATR计算失败"
                )

            plan = calculate_protection(
                current_pos,
                atr=fresh_atr,
                adopted=adopted
            )

            log(
                f"🎯 TP/SL计划 | "
                f"Mark={fmt_decimal(plan['mark'])} | "
                f"TP={fmt_decimal(plan['tp'])} | "
                f"SL={fmt_decimal(plan['sl'])} | "
                f"SL距离={fmt_decimal(plan['sl_pct'] * 100, 4)}% | "
                f"TP距离={fmt_decimal(plan['tp_pct'] * 100, 4)}%"
            )

            place_oco(
                current_pos,
                plan
            )

            time.sleep(
                OCO_VERIFY_WAIT
            )

            verify_pos = get_position(
                inst_id
            )

            if not verify_pos:

                log(
                    "⚠️ 下保护单后仓位已经消失"
                )

                return True

            if verify_oco(
                verify_pos,
                plan
            ):

                log(
                    f"🟢 TP/SL保护正式生效: {inst_id}"
                )

                return True

            log(
                f"⚠️ 第 {attempt} 次 OCO 验证失败"
            )

        except Exception as e:

            log(
                f"❌ 第 {attempt} 次 TP/SL处理失败: {e}"
            )

        time.sleep(1)

    log(
        f"❌ {inst_id} 连续 {OCO_RETRY_COUNT} 次保护失败"
    )

    if (
        new_position
        and EMERGENCY_CLOSE_ON_NEW_POSITION_PROTECTION_FAILURE
    ):

        emergency_close(
            inst_id
        )

    return False


# =========================================================
# 历史平仓结果
# =========================================================

def get_position_history(inst_id):

    rows = okx_request(
        "GET",
        "/api/v5/account/positions-history",
        params={
            "instType": "SWAP",
            "instId": inst_id,
            "limit": "20"
        },
        private=True
    )

    return rows


def get_latest_closed_result(inst_id):

    rows = get_position_history(
        inst_id
    )

    if not rows:

        return None

    # 按 uTime 从新到旧
    rows.sort(
        key=lambda x: D(
            x.get("uTime")
        ),
        reverse=True
    )

    for row in rows:

        close_total_pos = D(
            row.get("closeTotalPos")
        )

        if close_total_pos <= 0:
            continue

        return row

    return rows[0]


def print_closed_position_result(inst_id):

    try:

        row = get_latest_closed_result(
            inst_id
        )

        if not row:

            log(
                f"⚠️ 没找到 {inst_id} 的历史平仓记录"
            )

            return None

        direction = row.get(
            "direction",
            ""
        )

        open_px = D(
            row.get("openAvgPx")
        )

        close_px = D(
            row.get("closeAvgPx")
        )

        pnl = D(
            row.get("pnl")
        )

        fee = D(
            row.get("fee")
        )

        funding = D(
            row.get("fundingFee")
        )

        realized = D(
            row.get("realizedPnl")
        )

        pnl_ratio = D(
            row.get("pnlRatio")
        )

        log(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        log(
            f"🏁 {inst_id} 已平仓"
        )

        log(
            f"方向: {direction}"
        )

        if open_px > 0:

            log(
                f"开仓均价: {fmt_decimal(open_px)}"
            )

        if close_px > 0:

            log(
                f"平仓均价: {fmt_decimal(close_px)}"
            )

        log(
            f"价格盈亏: {fmt_decimal(pnl, 8)} USDT"
        )

        log(
            f"手续费: {fmt_decimal(fee, 8)} USDT"
        )

        log(
            f"资金费: {fmt_decimal(funding, 8)} USDT"
        )

        log(
            f"最终 realizedPnl: "
            f"{fmt_decimal(realized, 8)} USDT"
        )

        log(
            f"OKX记录 ROI: "
            f"{fmt_decimal(pnl_ratio * 100, 4)}%"
        )

        if realized > 0:

            log(
                "🟢 本次交易：盈利"
            )

        elif realized < 0:

            log(
                "🔴 本次交易：亏损"
            )

        else:

            log(
                "⚪ 本次交易：基本持平"
            )

        log(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        return {
            "realizedPnl": realized,
            "pnl": pnl,
            "fee": fee,
            "fundingFee": funding,
            "pnlRatio": pnl_ratio
        }

    except Exception as e:

        log(
            f"⚠️ 获取历史平仓结果失败: {e}"
        )

        return None


# =========================================================
# 连续亏损状态
# =========================================================

consecutive_losses = 0

last_loss_time = 0


def update_trade_result(result):

    global consecutive_losses
    global last_loss_time

    if not result:

        return

    realized = D(
        result.get("realizedPnl")
    )

    if realized < 0:

        consecutive_losses += 1

        last_loss_time = time.time()

        log(
            f"🔴 连续亏损次数: "
            f"{consecutive_losses}/{MAX_CONSECUTIVE_LOSSES}"
        )

    elif realized > 0:

        consecutive_losses = 0

        last_loss_time = 0

        log(
            "🟢 本次盈利，连续亏损计数归零"
        )


def in_loss_cooldown():

    if consecutive_losses < MAX_CONSECUTIVE_LOSSES:

        return False

    elapsed = (
        time.time()
        - last_loss_time
    )

    if elapsed < LOSS_COOLDOWN_SECONDS:

        remain = (
            LOSS_COOLDOWN_SECONDS
            - elapsed
        )

        log(
            f"🛑 连续亏损保护中，"
            f"剩余约 {int(remain)} 秒"
        )

        return True

    # 冷却结束
    consecutive_losses = 0

    last_loss_time = 0

    log(
        "🟢 连续亏损冷却结束，恢复扫描"
    )

    return False


# =========================================================
# 持仓快照
# =========================================================

previous_positions = {}


def detect_closed_positions(
    current_positions
):

    global previous_positions

    current_map = {}

    for pos in current_positions:

        inst_id = pos.get("instId")

        if inst_id:

            current_map[inst_id] = pos

    closed_ids = []

    for inst_id in list(
        previous_positions.keys()
    ):

        if inst_id not in current_map:

            closed_ids.append(
                inst_id
            )

    for inst_id in closed_ids:

        log(
            f"📕 检测到仓位消失: {inst_id}"
        )

        result = print_closed_position_result(
            inst_id
        )

        update_trade_result(
            result
        )

        previous_positions.pop(
            inst_id,
            None
        )

    previous_positions = current_map


# =========================================================
# 管理已有仓位
# =========================================================

def manage_existing_positions():

    positions = get_positions()

    detect_closed_positions(
        positions
    )

    if not positions:

        return False

    for pos in positions:

        inst_id = pos.get(
            "instId"
        )

        log(
            f"🔎 管理现有持仓: {inst_id}"
        )

        try:

            print_live_position(
                pos
            )

        except Exception as e:

            log(
                f"⚠️ 实时持仓显示失败: {e}"
            )

        # 已经有仓位，不管它是否仍在Top30，
        # 都必须继续管理TP/SL
        try:

            ensure_protection(
                pos,
                adopted=True,
                new_position=False
            )

        except Exception as e:

            log(
                f"⚠️ 持仓保护处理异常: {e}"
            )

    return True


# =========================================================
# 是否允许开新仓
# =========================================================

def can_open_new_trade():

    positions = get_positions()

    return len(positions) == 0


# =========================================================
# 执行交易
# =========================================================

def execute_candidate(
    candidate
):

    inst_id = candidate["instId"]

    side = candidate["side"]

    score = candidate["score"]

    price = candidate["price"]

    atr = candidate["atr"]

    log(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    log(
        f"🚀 准备开仓 | "
        f"{inst_id} | "
        f"{side} | "
        f"信号分数 {score}"
    )

    # 再次确认没有持仓
    if not can_open_new_trade():

        log(
            "⛔ 已存在其他持仓，取消新交易"
        )

        return False

    # 动态杠杆
    desired_leverage = leverage_from_score(
        score
    )

    leverage = get_actual_leverage(
        inst_id,
        desired_leverage
    )

    if leverage != desired_leverage:

        log(
            f"ℹ️ {inst_id} 最大允许杠杆 "
            f"限制为 {fmt_decimal(leverage)}x，"
            f"原计划 {fmt_decimal(desired_leverage)}x"
        )

    log(
        f"⚙️ 最终杠杆: {fmt_decimal(leverage)}x"
    )

    # 设置杠杆
    set_leverage(
        inst_id,
        leverage
    )

    log(
        f"✅ 杠杆设置成功: "
        f"{fmt_decimal(leverage)}x"
    )

    # 获取最新价格
    ticker = get_ticker(
        inst_id
    )

    current_price = ticker["markPx"]

    if current_price <= 0:

        raise RuntimeError(
            "当前Mark Price无效"
        )

    # 先计算一个初始保护比例
    atr_pct = atr / current_price

    target_sl_pct = (
        TARGET_SL_ROI_PCT
        / leverage
        / Decimal("100")
    )

    atr_sl_pct = (
        atr_pct
        * ATR_SL_FRACTION
    )

    sl_pct = max(
        target_sl_pct,
        atr_sl_pct,
        MIN_SL_PRICE_PCT
    )

    sl_pct = min(
        sl_pct,
        MAX_SL_PRICE_PCT
    )

    # 仓位
    equity = get_equity()

    if equity <= 0:

        raise RuntimeError(
            "账户权益无效"
        )

    qty = calculate_size(
        equity=equity,
        price=current_price,
        leverage=leverage,
        sl_pct=sl_pct,
        inst_id=inst_id
    )

    log(
        f"💰 账户权益: "
        f"{fmt_decimal(equity, 4)} USDT"
    )

    log(
        f"📦 下单数量: "
        f"{fmt_decimal(qty)}"
    )

    log(
        f"📉 风险止损距离约: "
        f"{fmt_decimal(sl_pct * 100, 4)}%"
    )

    # 开仓
    order = place_market_order(
        inst_id,
        side,
        qty
    )

    ord_id = order.get(
        "ordId"
    )

    if not ord_id:

        raise RuntimeError(
            f"开仓订单没有ordId: {order}"
        )

    log(
        f"📨 开仓订单已提交: {ord_id}"
    )

    # 等待成交
    filled_order = wait_order_filled(
        inst_id,
        ord_id
    )

    fill_px = D(
        filled_order.get("avgPx")
    )

    fill_sz = D(
        filled_order.get("accFillSz")
        or filled_order.get("fillSz")
    )

    log(
        f"✅ 开仓成交 | "
        f"价格 {fmt_decimal(fill_px)} | "
        f"数量 {fmt_decimal(fill_sz)}"
    )

    # 获取真实持仓
    position = None

    for _ in range(10):

        position = get_position(
            inst_id
        )

        if position:

            break

        time.sleep(0.5)

    if not position:

        raise RuntimeError(
            "订单成交，但未读取到持仓"
        )

    # 实时显示
    print_live_position(
        position
    )

    # 立即挂TP/SL
    protected = ensure_protection(
        position,
        adopted=False,
        new_position=True,
        atr=atr
    )

    if not protected:

        log(
            "🚨 新仓保护失败，已经执行紧急处理"
        )

        return False

    log(
        f"🟢 {inst_id} 开仓完成且TP/SL已确认"
    )

    log(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    return True


# =========================================================
# 扫描候选
# =========================================================

def scan_candidates():

    instruments = get_top30_okx_instruments()

    if not instruments:

        log(
            "⚠️ 当前没有可扫描的Top30合约"
        )

        return []

    tickers = get_all_tickers()

    candidates = []

    for info in instruments:

        inst_id = info.get(
            "instId"
        )

        ticker = tickers.get(
            inst_id
        )

        if not ticker:

            continue

        # OKX SWAP 的 volCcy24h 字段
        # 不同合约的具体计量方式以接口定义为准，
        # 这里只做流动性过滤，不把它硬解释成美元成交额
        volume = D(
            ticker.get("volCcy24h")
        )

        if volume <= 0:

            continue

        try:

            signal = calculate_signal(
                inst_id
            )

            if not signal:

                continue

            signal["volume"] = volume

            signal["rank"] = info.get(
                "_top30_rank",
                999
            )

            candidates.append(
                signal
            )

        except Exception as e:

            log(
                f"⚠️ {inst_id} 信号计算失败: {e}"
            )

    candidates.sort(
        key=lambda x: (
            -x["score"],
            -x["volume"],
            x["rank"]
        )
    )

    return candidates


# =========================================================
# 打印候选
# =========================================================

def print_candidates(candidates):

    if not candidates:

        log(
            "🔎 当前没有符合条件的交易信号"
        )

        return

    log(
        f"🔎 找到 {len(candidates)} 个候选"
    )

    for i, c in enumerate(
        candidates[:10],
        start=1
    ):

        log(
            f"{i}. "
            f"{c['instId']} | "
            f"{c['side']} | "
            f"分数={c['score']} | "
            f"RSI={fmt_decimal(c['rsi'], 2)} | "
            f"15m={c['trend15m']} | "
            f"1H={c['trend1h']}"
        )


# =========================================================
# 主循环
# =========================================================

def main():

    global previous_positions

    log(
        "=========================================="
    )

    log(
        f"🚀 OKX BTC/多币种永续自动交易模拟盘 {VERSION}"
    )

    log(
        f"📌 模式: OKX 模拟盘"
    )

    log(
        f"📌 杠杆范围: "
        f"{fmt_decimal(MIN_LEVERAGE)}x ~ "
        f"{fmt_decimal(MAX_LEVERAGE)}x"
    )

    log(
        "📌 自动读取合约最大杠杆"
    )

    log(
        "📌 策略: EMA + RSI + MACD + ATR"
    )

    log(
        "📌 周期: 5m + 15m + 1H"
    )

    log(
        "📌 TP/SL: OCO + Mark Price"
    )

    log(
        "📌 同时最多一个持仓"
    )

    log(
        "📌 日志时间: UTC+8"
    )

    log(
        "=========================================="
    )

    # 启动时先读取已有持仓
    try:

        positions = get_positions()

        previous_positions = {
            p.get("instId"): p
            for p in positions
            if p.get("instId")
        }

        if positions:

            log(
                f"⚠️ 启动时检测到 "
                f"{len(positions)} 个已有持仓"
            )

            for pos in positions:

                print_live_position(
                    pos
                )

                ensure_protection(
                    pos,
                    adopted=True,
                    new_position=False
                )

        else:

            log(
                "🟢 启动时没有持仓"
            )

    except Exception as e:

        log(
            f"⚠️ 启动持仓检查失败: {e}"
        )

    while True:

        try:

            # =================================================
            # 第一优先级：管理现有持仓
            # =================================================

            has_position = manage_existing_positions()

            if has_position:

                time.sleep(
                    LOOP_SECONDS
                )

                continue

            # =================================================
            # 连续亏损保护
            # =================================================

            if in_loss_cooldown():

                time.sleep(
                    LOOP_SECONDS
                )

                continue

            # =================================================
            # 账户
            # =================================================

            equity = get_equity()

            log(
                f"💰 当前账户权益: "
                f"{fmt_decimal(equity, 4)} USDT"
            )

            if equity <= 0:

                log(
                    "⚠️ 账户权益无效，暂不交易"
                )

                time.sleep(
                    LOOP_SECONDS
                )

                continue

            # =================================================
            # 扫描
            # =================================================

            candidates = scan_candidates()

            print_candidates(
                candidates
            )

            if not candidates:

                time.sleep(
                    LOOP_SECONDS
                )

                continue

            # =================================================
            # 只选择最强候选
            # =================================================

            candidate = candidates[0]

            log(
                f"🎯 最终候选: "
                f"{candidate['instId']} | "
                f"{candidate['side']} | "
                f"Score={candidate['score']}"
            )

            # =================================================
            # 执行
            # =================================================

            execute_candidate(
                candidate
            )

            time.sleep(
                LOOP_SECONDS
            )

        except KeyboardInterrupt:

            log(
                "🛑 收到停止指令，机器人退出"
            )

            break

        except Exception as e:

            log(
                f"❌ 主循环异常: {e}"
            )

            time.sleep(
                LOOP_SECONDS
            )


# =========================================================
# 启动
# =========================================================

if __name__ == "__main__":

    main()