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
# OKX BTC 永续自动交易机器人
# 模拟盘
#
# 策略结构：
#
# 日线：
# MA5 / MA20 / MA60 / MA200
#
# 1小时：
# 趋势过滤
#
# 15分钟：
# 支撑 / 阻力
#
# 5分钟：
# MACD + RSI + 成交量 + MA20
# 至少连续3根K线确认
#
# ★☆☆☆☆ 1个条件：不交易
# ★★☆☆☆ 2个条件：不交易
# ★★★☆☆ 3个条件：小风险
# ★★★★☆ 4个条件：正常风险
# ★★★★★ 5个条件：较高风险
#
# 仓位：
# 强多头：最多80%
# 多头：最多60%
# 震荡：最多30%
# 空头：0%
#
# 单笔保证金：最多总资金5%
#
# 重要：
# 已经持仓时，不因为5分钟反向信号直接平仓。
# 持仓主要等待 TP / SL。
# ============================================================
# ============================================================
# OKX 配置
# ============================================================
API_KEY = os.getenv("OKX_API_KEY")
SECRET_KEY = os.getenv("OKX_SECRET_KEY")
PASSPHRASE = os.getenv("OKX_PASSPHRASE")
BASE_URL = "https://openapi.okx.com"
INST_ID = "BTC-USDT-SWAP"
INST_TYPE = "SWAP"
# 1 = 模拟盘
SIMULATED_TRADING = "1"
# 全仓
TD_MODE = "cross"
# 每20秒检查一次
LOOP_SECONDS = 20
# ============================================================
# 风险控制
# ============================================================
# 单笔最多使用账户总资金5%作为保证金
PER_TRADE_MARGIN_PCT = Decimal("0.05")
# 单笔理论止损风险
RISK_PER_TRADE_PCT = Decimal("0.008")
# 杠杆范围
MIN_LEVERAGE = 5
MAX_LEVERAGE = 15
# 最大日亏损
MAX_DAILY_LOSS_PCT = Decimal("0.02")
# 连续亏损3次停止新开仓
MAX_CONSECUTIVE_LOSSES = 3
# 亏损后冷却30分钟
LOSS_COOLDOWN_SECONDS = 30 * 60
# 每个UTC日最多开5笔
MAX_TRADES_PER_DAY = 5
# ============================================================
# TP / SL
# ============================================================
ATR_PERIOD = 14
# 止损 = ATR × 1.5
ATR_SL_MULTIPLIER = Decimal("1.5")
# TP = 止损距离 × 1.8
RISK_REWARD = Decimal("1.8")
# ============================================================
# 5分钟指标
# ============================================================
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
# 最少连续3根K线
MIN_5M_CONFIRM = 3
# ============================================================
# 支撑 / 阻力
# ============================================================
SR_LOOKBACK = 80
# 距支撑3%以内，视为支撑附近
SR_NEAR_PCT = Decimal("0.03")
# 最低盈亏比
MIN_RR = Decimal("1.5")
# ============================================================
# 成交量
# ============================================================
VOLUME_PERIOD = 20
# 放量必须 >= 20周期均量 ×1.5
BREAKOUT_VOLUME_MULTIPLIER = Decimal("1.5")
# ============================================================
# BTC Dominance
# ============================================================
BTC_DOM_HIGH = Decimal("58")
BTC_DOM_LOW = Decimal("55")
DOMINANCE_CHECK_SECONDS = 15 * 60
# ============================================================
# 全局状态
# ============================================================
instrument = None
last_processed_candle = None
cooldown_until = 0
consecutive_losses = 0
reported_close_orders = set()
session_date = None
session_start_equity = Decimal("0")
daily_realized_pnl = Decimal("0")
trades_today = 0
last_dom_check = 0
btc_dominance = None
# ============================================================
# Decimal工具
# ============================================================
def D(value):
    return Decimal(str(value))
def decimal_to_string(value):
    s = format(D(value), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"
def floor_to_step(value, step):
    value = D(value)
    step = D(step)
    if step <= 0:
        return value
    return (
        value / step
    ).to_integral_value(
        rounding=ROUND_DOWN
    ) * step
def price_to_tick(price):
    return floor_to_step(
        price,
        instrument["tickSz"]
    )
def size_to_lot(size):
    return floor_to_step(
        size,
        instrument["lotSz"]
    )
def utc_now():
    return datetime.now(timezone.utc)
# ============================================================
# 每日状态
# ============================================================
def reset_daily_state_if_needed(equity):
    global session_date
    global session_start_equity
    global daily_realized_pnl
    global trades_today
    today = utc_now().date()
    if session_date != today:
        session_date = today
        session_start_equity = D(equity)
        daily_realized_pnl = Decimal("0")
        trades_today = 0
        print(
            f"[DAY] 新交易日 "
            f"起始权益={decimal_to_string(session_start_equity)}"
        )
# ============================================================
# 配置检查
# ============================================================
def check_config():
    missing = []
    if not API_KEY:
        missing.append("OKX_API_KEY")
    if not SECRET_KEY:
        missing.append("OKX_SECRET_KEY")
    if not PASSPHRASE:
        missing.append("OKX_PASSPHRASE")
    if missing:
        raise RuntimeError(
            "缺少环境变量: "
            + ", ".join(missing)
        )
# ============================================================
# OKX签名
# ============================================================
def get_server_time():
    r = requests.get(
        BASE_URL + "/api/v5/public/time",
        timeout=10
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "0":
        raise RuntimeError(data)
    return data["data"][0]["ts"]
def make_signature(
    timestamp,
    method,
    request_path,
    body=""
):
    message = (
        timestamp
        + method.upper()
        + request_path
        + body
    )
    digest = hmac.new(
        SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256
    ).digest()
    return base64.b64encode(
        digest
    ).decode()
# ============================================================
# 私有API
# ============================================================
def private_request(
    method,
    path,
    params=None,
    body=None
):
    params = params or {}
    body = body or {}
    method = method.upper()
    if method == "GET":
        if params:
            from urllib.parse import urlencode
            query = urlencode(params)
            request_path = (
                path
                + "?"
                + query
            )
        else:
            request_path = path
        body_text = ""
    else:
        request_path = path
        body_text = json.dumps(
            body,
            separators=(",", ":")
        )
    server_ms = int(
        get_server_time()
    )
    timestamp = (
        datetime.fromtimestamp(
            server_ms / 1000,
            timezone.utc
        )
        .isoformat(
            timespec="milliseconds"
        )
        .replace(
            "+00:00",
            "Z"
        )
    )
    headers = {
        "OK-ACCESS-KEY":
            API_KEY,
        "OK-ACCESS-SIGN":
            make_signature(
                timestamp,
                method,
                request_path,
                body_text
            ),
        "OK-ACCESS-TIMESTAMP":
            timestamp,
        "OK-ACCESS-PASSPHRASE":
            PASSPHRASE,
        "Content-Type":
            "application/json",
        "x-simulated-trading":
            SIMULATED_TRADING,
    }
    url = BASE_URL + request_path
    response = requests.request(
        method,
        url,
        headers=headers,
        data=(
            body_text
            if method != "GET"
            else None
        ),
        timeout=15
    )
    response.raise_for_status()
    data = response.json()
    if data.get("code") != "0":
        raise RuntimeError(
            f"OKX API错误 "
            f"code={data.get('code')} "
            f"msg={data.get('msg')}"
        )
    return data
# ============================================================
# 公共API
# ============================================================
def public_get(
    path,
    params=None
):
    response = requests.get(
        BASE_URL + path,
        params=params or {},
        timeout=15
    )
    response.raise_for_status()
    data = response.json()
    if data.get("code") != "0":
        raise RuntimeError(
            f"OKX公共API错误 "
            f"code={data.get('code')} "
            f"msg={data.get('msg')}"
        )
    return data
# ============================================================
# 获取合约信息
# ============================================================
def get_instrument():
    data = public_get(
        "/api/v5/public/instruments",
        {
            "instType":
                INST_TYPE,
            "instId":
                INST_ID
        }
    )
    if not data["data"]:
        raise RuntimeError(
            "找不到BTC-USDT-SWAP合约信息"
        )
    item = data["data"][0]
    return {
        "ctVal":
            D(item["ctVal"]),
        "lotSz":
            D(item["lotSz"]),
        "minSz":
            D(item["minSz"]),
        "tickSz":
            D(item["tickSz"]),
    }
# ============================================================
# 获取账户权益
# ============================================================
def get_account_balance():
    data = private_request(
        "GET",
        "/api/v5/account/balance",
        {
            "ccy":
                "USDT"
        }
    )
    total_equity = Decimal("0")
    for account in data["data"]:
        for detail in account.get(
            "details",
            []
        ):
            if detail.get("ccy") == "USDT":
                eq = detail.get(
                    "eq",
                    ""
                )
                if eq:
                    total_equity = D(eq)
    if total_equity <= 0:
        for account in data["data"]:
            if account.get("totalEq"):
                total_equity = D(
                    account["totalEq"]
                )
                break
    return total_equity
# ============================================================
# 获取账户配置
# ============================================================
def get_account_config():
    data = private_request(
        "GET",
        "/api/v5/account/config"
    )
    return data["data"][0]
# ============================================================
# 设置杠杆
# ============================================================
def set_leverage(leverage):
    body = {
        "instId":
            INST_ID,
        "lever":
            str(leverage),
        "mgnMode":
            TD_MODE
    }
    return private_request(
        "POST",
        "/api/v5/account/set-leverage",
        body=body
    )
# ============================================================
# 获取最新价格
# ============================================================
def get_ticker():
    data = public_get(
        "/api/v5/market/ticker",
        {
            "instId":
                INST_ID
        }
    )
    return D(
        data["data"][0]["last"]
    )
# ============================================================
# 获取K线
# ============================================================
def get_candles(
    bar,
    limit=250
):
    data = public_get(
        "/api/v5/market/candles",
        {
            "instId":
                INST_ID,
            "bar":
                bar,
            "limit":
                str(limit)
        }
    )
    candles = []
    for row in data["data"]:
        candles.append({
            "ts":
                int(row[0]),
            "open":
                D(row[1]),
            "high":
                D(row[2]),
            "low":
                D(row[3]),
            "close":
                D(row[4]),
            "volume":
                D(row[5]),
            "confirm":
                row[8]
        })
    candles.sort(
        key=lambda x:
            x["ts"]
    )
    return candles
def closed_candles(candles):
    return [
        x
        for x in candles
        if x["confirm"] == "1"
    ]
# ============================================================
# SMA
# ============================================================
def sma(
    values,
    period
):
    if len(values) < period:
        return None
    return (
        sum(
            values[-period:],
            Decimal("0")
        )
        / D(period)
    )
def sma_series(
    values,
    period
):
    result = [
        None
    ] * len(values)
    if len(values) < period:
        return result
    for i in range(
        period - 1,
        len(values)
    ):
        result[i] = (
            sum(
                values[
                    i - period + 1:
                    i + 1
                ],
                Decimal("0")
            )
            / D(period)
        )
    return result
# ============================================================
# EMA
# ============================================================
def ema_series(
    values,
    period
):
    if len(values) < period:
        return []
    k = (
        Decimal("2")
        / D(period)
    )
    first = (
        sum(
            values[:period],
            Decimal("0")
        )
        / D(period)
    )
    result = [
        None
    ] * (period - 1)
    result.append(first)
    previous = first
    for price in values[period:]:
        current = (
            (price - previous)
            * k
            + previous
        )
        result.append(current)
        previous = current
    return result
def ema(
    values,
    period
):
    series = ema_series(
        values,
        period
    )
    if not series:
        return None
    return series[-1]
# ============================================================
# RSI
# ============================================================
def calculate_rsi(
    closes,
    period=14
):
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(
        1,
        len(closes)
    ):
        change = (
            closes[i]
            - closes[i - 1]
        )
        if change > 0:
            gains.append(change)
            losses.append(
                Decimal("0")
            )
        else:
            gains.append(
                Decimal("0")
            )
            losses.append(
                abs(change)
            )
    avg_gain = (
        sum(
            gains[:period],
            Decimal("0")
        )
        / D(period)
    )
    avg_loss = (
        sum(
            losses[:period],
            Decimal("0")
        )
        / D(period)
    )
    if avg_loss == 0:
        return D("100")
    for i in range(
        period,
        len(gains)
    ):
        avg_gain = (
            avg_gain
            * D(period - 1)
            + gains[i]
        ) / D(period)
        avg_loss = (
            avg_loss
            * D(period - 1)
            + losses[i]
        ) / D(period)
    if avg_loss == 0:
        return D("100")
    rs = avg_gain / avg_loss
    return (
        D("100")
        - D("100")
        / (
            D("1")
            + rs
        )
    )
# ============================================================
# MACD
# ============================================================
def calculate_macd(
    closes
):
    fast = ema_series(
        closes,
        MACD_FAST
    )
    slow = ema_series(
        closes,
        MACD_SLOW
    )
    if not fast or not slow:
        return None
    macd_values = []
    start = max(
        MACD_FAST - 1,
        MACD_SLOW - 1
    )
    for i in range(
        start,
        len(closes)
    ):
        if (
            fast[i] is not None
            and
            slow[i] is not None
        ):
            macd_values.append(
                fast[i] - slow[i]
            )
    signal_values = ema_series(
        macd_values,
        MACD_SIGNAL
    )
    if not signal_values:
        return None
    if (
        len(macd_values) < 2
        or
        len(signal_values) < 2
    ):
        return None
    return {
        "macd":
            macd_values[-1],
        "signal":
            signal_values[-1],
        "hist":
            macd_values[-1]
            - signal_values[-1],
        "prev_macd":
            macd_values[-2],
        "prev_signal":
            signal_values[-2]
    }
# ============================================================
# ATR
# ============================================================
def calculate_atr(
    candles,
    period=14
):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(
        1,
        len(candles)
    ):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = (
            candles[i - 1]["close"]
        )
        tr = max(
            high - low,
            abs(
                high - prev_close
            ),
            abs(
                low - prev_close
            )
        )
        trs.append(tr)
    return (
        sum(
            trs[-period:],
            Decimal("0")
        )
        / D(period)
    )
# ============================================================
# 日线趋势
# ============================================================
def daily_trend(
    daily,
    current_price
):
    closes = [
        x["close"]
        for x in daily
    ]
    if len(closes) < 205:
        return {
            "trend":
                "UNKNOWN",
            "ma5":
                None,
            "ma20":
                None,
            "ma60":
                None,
            "ma200":
                None
        }
    ma5 = sma(
        closes,
        5
    )
    ma20 = sma(
        closes,
        20
    )
    ma60 = sma(
        closes,
        60
    )
    ma200 = sma(
        closes,
        200
    )
    # ========================================================
    # 按照你确定的日线规则
    # ========================================================
    if (
        current_price > ma200
        and
        ma60 > ma200
        and
        ma20 > ma60
    ):
        trend = "STRONG_LONG"
    elif (
        current_price > ma200
        and
        ma60 > ma200
    ):
        trend = "LONG"
    elif (
        current_price < ma200
        and
        ma60 < ma200
    ):
        trend = "SHORT"
    elif (
        current_price < ma200
        and
        ma60 > ma200
    ):
        trend = "WARNING"
    else:
        trend = "RANGE"
    return {
        "trend":
            trend,
        "ma5":
            ma5,
        "ma20":
            ma20,
        "ma60":
            ma60,
        "ma200":
            ma200
    }
# ============================================================
# 1小时趋势
# ============================================================
def hourly_trend(
    candles
):
    closes = [
        x["close"]
        for x in candles
    ]
    if len(closes) < 205:
        return "UNKNOWN"
    ma20 = sma(
        closes,
        20
    )
    ma60 = sma(
        closes,
        60
    )
    ma200 = sma(
        closes,
        200
    )
    price = closes[-1]
    if (
        price > ma200
        and ma20 > ma60
    ):
        return "LONG"
    if (
        price < ma200
        and ma20 < ma60
    ):
        return "SHORT"
    return "RANGE"
# ============================================================
# 15分钟支撑 / 阻力
# ============================================================
def get_support_resistance(
    candles
):
    recent = candles[
        -SR_LOOKBACK:
    ]
    if len(recent) < 20:
        return None, None
    price = recent[-1]["close"]
    supports = []
    resistances = []
    for i in range(
        2,
        len(recent) - 2
    ):
        low = recent[i]["low"]
        high = recent[i]["high"]
        # 局部低点
        if (
            low <= recent[i - 1]["low"]
            and
            low <= recent[i - 2]["low"]
            and
            low <= recent[i + 1]["low"]
            and
            low <= recent[i + 2]["low"]
        ):
            if low < price:
                supports.append(low)
        # 局部高点
        if (
            high >= recent[i - 1]["high"]
            and
            high >= recent[i - 2]["high"]
            and
            high >= recent[i + 1]["high"]
            and
            high >= recent[i + 2]["high"]
        ):
            if high > price:
                resistances.append(high)
    support = (
        max(supports)
        if supports
        else None
    )
    resistance = (
        min(resistances)
        if resistances
        else None
    )
    return (
        support,
        resistance
    )
# ============================================================
# MACD背离
# ============================================================
def detect_macd_divergence(
    candles,
    direction
):
    if len(candles) < 50:
        return False
    closes = [
        x["close"]
        for x in candles
    ]
    fast = ema_series(
        closes,
        MACD_FAST
    )
    slow = ema_series(
        closes,
        MACD_SLOW
    )
    macd_values = []
    start = MACD_SLOW - 1
    for i in range(
        start,
        len(closes)
    ):
        if (
            fast[i] is not None
            and
            slow[i] is not None
        ):
            macd_values.append(
                fast[i] - slow[i]
            )
    if len(macd_values) < 30:
        return False
    if direction == "LONG":
        price_a = min(
            closes[-30:-15]
        )
        price_b = min(
            closes[-15:]
        )
        macd_a = min(
            macd_values[-30:-15]
        )
        macd_b = min(
            macd_values[-15:]
        )
        return (
            price_b < price_a
            and
            macd_b > macd_a
        )
    price_a = max(
        closes[-30:-15]
    )
    price_b = max(
        closes[-15:]
    )
    macd_a = max(
        macd_values[-30:-15]
    )
    macd_b = max(
        macd_values[-15:]
    )
    return (
        price_b > price_a
        and
        macd_b < macd_a
    )
# ============================================================
# 5分钟信号
# ============================================================
def five_min_signal(
    candles,
    support,
    resistance
):
    if len(candles) < 80:
        return {
            "signal":
                "WAIT",
            "stars":
                0,
            "score":
                0,
            "reason":
                "5m数据不足"
        }
    closes = [
        x["close"]
        for x in candles
    ]
    opens = [
        x["open"]
        for x in candles
    ]
    highs = [
        x["high"]
        for x in candles
    ]
    lows = [
        x["low"]
        for x in candles
    ]
    volumes = [
        x["volume"]
        for x in candles
    ]
    ema9 = ema(
        closes,
        EMA_FAST
    )
    ema21 = ema(
        closes,
        EMA_SLOW
    )
    rsi = calculate_rsi(
        closes,
        RSI_PERIOD
    )
    macd = calculate_macd(
        closes
    )
    atr = calculate_atr(
        candles,
        ATR_PERIOD
    )
    if (
        ema9 is None
        or
        ema21 is None
        or
        rsi is None
        or
        macd is None
        or
        atr is None
    ):
        return {
            "signal":
                "WAIT",
            "stars":
                0,
            "score":
                0,
            "reason":
                "指标不足"
        }
    # ========================================================
    # 最近3根已经确认的K线
    # ========================================================
    last3 = candles[-3:]
    three_long = all(
        x["close"] > x["open"]
        for x in last3
    )
    three_short = all(
        x["close"] < x["open"]
        for x in last3
    )
    price = closes[-1]
    # ========================================================
    # MA20
    # ========================================================
    ma20_values = sma_series(
        closes,
        20
    )
    ma20_now = ma20_values[-1]
    ma20_prev = ma20_values[-2]
    ma20_up = (
        ma20_now > ma20_prev
    )
    ma20_down_or_flat = (
        ma20_now <= ma20_prev
    )
    # ========================================================
    # 做多：价格回踩MA20但不破
    # ========================================================
    long_ma20 = (
        price >= ma20_now
        and
        price <= (
            ma20_now
            + atr * D("0.5")
        )
        and
        ma20_up
    )
    # ========================================================
    # 做空条件
    # ========================================================
    short_ma20 = (
        price <= ma20_now
        and
        price >= (
            ma20_now
            - atr * D("0.5")
        )
        and
        ma20_down_or_flat
    )
    # ========================================================
    # MACD
    # ========================================================
    long_macd = (
        macd["macd"]
        > macd["signal"]
        and
        macd["macd"]
        > 0
    )
    short_macd = (
        macd["macd"]
        < macd["signal"]
        and
        macd["macd"]
        < 0
    )
    # 金叉
    long_golden = (
        macd["prev_macd"]
        <=
        macd["prev_signal"]
        and
        macd["macd"]
        >
        macd["signal"]
    )
    # 死叉
    short_death = (
        macd["prev_macd"]
        >=
        macd["prev_signal"]
        and
        macd["macd"]
        <
        macd["signal"]
    )
    # 底背离
    bottom_div = detect_macd_divergence(
        candles,
        "LONG"
    )
    # 顶背离
    top_div = detect_macd_divergence(
        candles,
        "SHORT"
    )
    # ========================================================
    # RSI
    # ========================================================
    rsi_prev = calculate_rsi(
        closes[:-1],
        RSI_PERIOD
    )
    rsi_recover_long = (
        rsi < D("35")
        and
        rsi_prev is not None
        and
        rsi > rsi_prev
    )
    rsi_recover_short = (
        rsi > D("75")
        and
        rsi_prev is not None
        and
        rsi < rsi_prev
    )
    # ========================================================
    # 成交量
    # ========================================================
    previous20_high = max(
        highs[-21:-1]
    )
    previous20_low = min(
        lows[-21:-1]
    )
    avg_volume20 = sma(
        volumes[:-1],
        VOLUME_PERIOD
    )
    current_volume = volumes[-1]
    volume_breakout_long = (
        price > previous20_high
        and
        avg_volume20 is not None
        and
        current_volume
        >=
        avg_volume20
        * BREAKOUT_VOLUME_MULTIPLIER
    )
    volume_breakout_short = (
        price < previous20_low
        and
        avg_volume20 is not None
        and
        current_volume
        >=
        avg_volume20
        * BREAKOUT_VOLUME_MULTIPLIER
    )
    # ========================================================
    # 高位异常放量但价格不涨
    # ========================================================
    high_volume_no_rise = (
        avg_volume20 is not None
        and
        current_volume
        >=
        avg_volume20
        * BREAKOUT_VOLUME_MULTIPLIER
        and
        price < previous20_high
    )
    # ========================================================
    # 支撑/阻力距离
    # ========================================================
    support_near = False
    resistance_near = False
    if support is not None:
        support_near = (
            abs(
                price - support
            )
            / price
            <=
            SR_NEAR_PCT
        )
    if resistance is not None:
        resistance_near = (
            abs(
                resistance - price
            )
            / price
            <=
            SR_NEAR_PCT
        )
    # ========================================================
    # 多头5项
    # ========================================================
    long_score = 0
    long_reasons = []
    if long_ma20:
        long_score += 1
        long_reasons.append(
            "MA20回踩不破"
        )
    if (
        long_macd
        or long_golden
        or bottom_div
    ):
        long_score += 1
        long_reasons.append(
            "MACD多头"
        )
    if rsi_recover_long:
        long_score += 1
        long_reasons.append(
            "RSI超卖回升"
        )
    if volume_breakout_long:
        long_score += 1
        long_reasons.append(
            "放量突破"
        )
    if support_near:
        long_score += 1
        long_reasons.append(
            "支撑附近"
        )
    # ========================================================
    # 空头5项
    # ========================================================
    short_score = 0
    short_reasons = []
    if short_ma20:
        short_score += 1
        short_reasons.append(
            "MA20偏空"
        )
    if (
        short_macd
        or short_death
        or top_div
    ):
        short_score += 1
        short_reasons.append(
            "MACD空头"
        )
    if rsi_recover_short:
        short_score += 1
        short_reasons.append(
            "RSI超买回落"
        )
    if (
        volume_breakout_short
        or
        high_volume_no_rise
    ):
        short_score += 1
        short_reasons.append(
            "量价偏空"
        )
    if (
        support is not None
        and
        price < support
    ):
        short_score += 1
        short_reasons.append(
            "跌破支撑"
        )
    # ========================================================
    # 最终信号
    # ========================================================
    if (
        long_score >= 3
        and
        three_long
        and
        long_score > short_score
    ):
        signal = "LONG"
        score = long_score
        stars = min(
            5,
            long_score
        )
        reason = " / ".join(
            long_reasons
        )
    elif (
        short_score >= 3
        and
        three_short
        and
        short_score > long_score
    ):
        signal = "SHORT"
        score = short_score
        stars = min(
            5,
            short_score
        )
        reason = " / ".join(
            short_reasons
        )
    else:
        signal = "WAIT"
        score = max(
            long_score,
            short_score
        )
        stars = min(
            5,
            score
        )
        if (
            long_score
            >
            short_score
        ):
            reason = (
                "多头条件不足/"
                "三根K线未确认"
            )
        elif (
            short_score
            >
            long_score
        ):
            reason = (
                "空头条件不足/"
                "三根K线未确认"
            )
        else:
            reason = (
                "多空信号不明确"
            )
    return {
        "signal":
            signal,
        "score":
            score,
        "stars":
            stars,
        "reason":
            reason,
        "rsi":
            rsi,
        "atr":
            atr,
        "ema9":
            ema9,
        "ema21":
            ema21,
        "ma20":
            ma20_now,
        "long_score":
            long_score,
        "short_score":
            short_score,
        "three_long":
            three_long,
        "three_short":
            three_short,
        "support":
            support,
        "resistance":
            resistance
    }
# ============================================================
# 动态杠杆
# ============================================================
def choose_leverage(
    stars,
    daily_trend_name
):
    if stars <= 2:
        return MIN_LEVERAGE
    if stars == 3:
        return 7
    if stars == 4:
        return 10
    if stars >= 5:
        if daily_trend_name == "STRONG_LONG":
            return 15
        return 12
    return MIN_LEVERAGE
# ============================================================
# 根据日线趋势计算最大资金占用
# ============================================================
def max_margin_by_trend(
    equity,
    trend
):
    if trend == "STRONG_LONG":
        return (
            equity
            * D("0.80")
        )
    if trend == "LONG":
        return (
            equity
            * D("0.60")
        )
    if trend == "RANGE":
        return (
            equity
            * D("0.30")
        )
    # SHORT / WARNING
    return Decimal("0")
# ============================================================
# 仓位计算
# ============================================================
def calculate_order_size(
    equity,
    price,
    stop_distance,
    leverage,
    trend
):
    if stop_distance <= 0:
        return None
    # --------------------------------------------------------
    # 单笔保证金5%
    # --------------------------------------------------------
    max_trade_margin = (
        equity
        * PER_TRADE_MARGIN_PCT
    )
    # --------------------------------------------------------
    # 日线最大资金占用
    # --------------------------------------------------------
    max_total_margin = (
        max_margin_by_trend(
            equity,
            trend
        )
    )
    if max_total_margin <= 0:
        return None
    # --------------------------------------------------------
    # 风险金额
    # --------------------------------------------------------
    risk_amount = (
        equity
        * RISK_PER_TRADE_PCT
    )
    # 根据止损距离反推BTC数量
    btc_by_risk = (
        risk_amount
        / stop_distance
    )
    # 名义价值
    notional_by_risk = (
        btc_by_risk
        * price
    )
    # 所需保证金
    margin_by_risk = (
        notional_by_risk
        / D(leverage)
    )
    # 单笔最大5%
    target_margin = min(
        margin_by_risk,
        max_trade_margin,
        max_total_margin
    )
    if target_margin <= 0:
        return None
    target_notional = (
        target_margin
        * D(leverage)
    )
    btc_qty = (
        target_notional
        / price
    )
    contracts = (
        btc_qty
        / instrument["ctVal"]
    )
    contracts = size_to_lot(
        contracts
    )
    min_sz = instrument["minSz"]
    # 最小仓位如果超过风险要求，宁愿不下单
    if contracts < min_sz:
        return None
    actual_btc = (
        contracts
        * instrument["ctVal"]
    )
    actual_notional = (
        actual_btc
        * price
    )
    actual_margin = (
        actual_notional
        / D(leverage)
    )
    # 再次检查单笔5%
    if (
        actual_margin
        >
        max_trade_margin
    ):
        contracts = size_to_lot(
            max_trade_margin
            * D(leverage)
            / price
            / instrument["ctVal"]
        )
        if contracts < min_sz:
            return None
        actual_btc = (
            contracts
            * instrument["ctVal"]
        )
        actual_notional = (
            actual_btc
            * price
        )
        actual_margin = (
            actual_notional
            / D(leverage)
        )
    return {
        "contracts":
            contracts,
        "margin":
            actual_margin,
        "notional":
            actual_notional,
        "risk_amount":
            risk_amount,
        "leverage":
            leverage
    }
# ============================================================
# 获取当前持仓
# ============================================================
def get_position():
    data = private_request(
        "GET",
        "/api/v5/account/positions",
        {
            "instType":
                INST_TYPE,
            "instId":
                INST_ID
        }
    )
    for position in data["data"]:
        if (
            position.get("instId")
            != INST_ID
        ):
            continue
        pos = D(
            position.get(
                "pos",
                "0"
            )
        )
        if pos == 0:
            continue
        avg_px = D(
            position.get(
                "avgPx",
                "0"
            )
            or
            "0"
        )
        if pos > 0:
            side = "LONG"
        else:
            side = "SHORT"
        return {
            "side":
                side,
            "pos":
                abs(pos),
            "raw_pos":
                pos,
            "avgPx":
                avg_px
        }
    return None
# ============================================================
# 下单
# ============================================================
def place_order(
    side,
    size,
    leverage,
    entry_price,
    stop_price,
    take_price
):
    if side == "LONG":
        order_side = "buy"
    else:
        order_side = "sell"
    now_ms = int(
        time.time() * 1000
    )
    cl_ord_id = (
        "B"
        +
        datetime.now(
            timezone.utc
        ).strftime(
            "%m%d%H%M%S"
        )
        +
        str(now_ms)[-5:]
    )
    algo_id = (
        "A"
        +
        datetime.now(
            timezone.utc
        ).strftime(
            "%m%d%H%M%S"
        )
        +
        str(now_ms)[-5:]
    )
    body = {
        "instId":
            INST_ID,
        "tdMode":
            TD_MODE,
        "side":
            order_side,
        "ordType":
            "market",
        "sz":
            decimal_to_string(
                size
            ),
        "clOrdId":
            cl_ord_id,
        "attachAlgoOrds": [
            {
                "attachAlgoClOrdId":
                    algo_id,
                "tpTriggerPx":
                    decimal_to_string(
                        take_price
                    ),
                "tpOrdPx":
                    "-1",
                "tpTriggerPxType":
                    "mark",
                "slTriggerPx":
                    decimal_to_string(
                        stop_price
                    ),
                "slOrdPx":
                    "-1",
                "slTriggerPxType":
                    "mark"
            }
        ]
    }
    print(
        f"[ORDER] {side} "
        f"size={decimal_to_string(size)} "
        f"leverage={leverage}x "
        f"entry≈{decimal_to_string(entry_price)} "
        f"SL={decimal_to_string(stop_price)} "
        f"TP={decimal_to_string(take_price)}"
    )
    return private_request(
        "POST",
        "/api/v5/trade/order",
        body=body
    )
# ============================================================
# 手动平仓函数
#
# 注意：
# 正常策略不会因为反向信号调用它。
# 只保留给以后人工/特殊风控使用。
# ============================================================
def close_position(
    position
):
    pos = position["pos"]
    if position["side"] == "LONG":
        side = "sell"
    else:
        side = "buy"
    body = {
        "instId":
            INST_ID,
        "tdMode":
            TD_MODE,
        "side":
            side,
        "ordType":
            "market",
        "sz":
            decimal_to_string(
                pos
            ),
        "reduceOnly":
            "true"
    }
    print(
        f"[CLOSE] 平仓 "
        f"{position['side']} "
        f"{decimal_to_string(pos)}张"
    )
    return private_request(
        "POST",
        "/api/v5/trade/order",
        body=body
    )
# ============================================================
# 查询最近成交
# ============================================================
def report_recent_closed_trades():
    global consecutive_losses
    global cooldown_until
    global daily_realized_pnl
    global reported_close_orders
    try:
        data = private_request(
            "GET",
            "/api/v5/trade/fills",
            {
                "instType":
                    INST_TYPE,
                "instId":
                    INST_ID,
                "limit":
                    "100"
            }
        )
        grouped = {}
        for fill in data["data"]:
            ord_id = fill.get(
                "ordId",
                ""
            )
            if not ord_id:
                continue
            fill_pnl = D(
                fill.get(
                    "fillPnl",
                    "0"
                )
                or
                "0"
            )
            fee = D(
                fill.get(
                    "fee",
                    "0"
                )
                or
                "0"
            )
            # 开仓成交通常fillPnl=0
            if fill_pnl == 0:
                continue
            if ord_id not in grouped:
                grouped[ord_id] = {
                    "pnl":
                        Decimal("0"),
                    "fee":
                        Decimal("0"),
                    "time":
                        fill.get(
                            "fillTime",
                            ""
                        )
                }
            grouped[ord_id]["pnl"] += (
                fill_pnl
            )
            grouped[ord_id]["fee"] += (
                fee
            )
        for ord_id, item in grouped.items():
            if ord_id in reported_close_orders:
                continue
            reported_close_orders.add(
                ord_id
            )
            net_pnl = (
                item["pnl"]
                +
                item["fee"]
            )
            daily_realized_pnl += (
                net_pnl
            )
            print(
                f"[PNL] "
                f"order={ord_id} "
                f"gross={decimal_to_string(item['pnl'])} "
                f"fee={decimal_to_string(item['fee'])} "
                f"net={decimal_to_string(net_pnl)}"
            )
            if net_pnl < 0:
                consecutive_losses += 1
                cooldown_until = (
                    time.time()
                    +
                    LOSS_COOLDOWN_SECONDS
                )
                print(
                    f"[RISK] "
                    f"连续亏损="
                    f"{consecutive_losses} "
                    f"冷却30分钟"
                )
            else:
                consecutive_losses = 0
    except Exception as e:
        print(
            f"[PNL] 查询失败: {e}"
        )
# ============================================================
# 风险检查
# ============================================================
def risk_blocked(
    equity
):
    if (
        session_start_equity
        <= 0
    ):
        return False
    # --------------------------------------------------------
    # 当日亏损2%
    # --------------------------------------------------------
    if (
        daily_realized_pnl
        <=
        -session_start_equity
        * MAX_DAILY_LOSS_PCT
    ):
        print(
            "[RISK] "
            "本轮运行期间日亏损达到2%，"
            "停止新开仓"
        )
        return True
    # --------------------------------------------------------
    # 连续亏损
    # --------------------------------------------------------
    if (
        consecutive_losses
        >=
        MAX_CONSECUTIVE_LOSSES
    ):
        print(
            "[RISK] "
            "连续亏损达到3次，"
            "停止新开仓"
        )
        return True
    # --------------------------------------------------------
    # 冷却
    # --------------------------------------------------------
    if (
        time.time()
        <
        cooldown_until
    ):
        remain = int(
            cooldown_until
            -
            time.time()
        )
        print(
            f"[RISK] "
            f"冷却中，"
            f"还剩约{remain // 60}分钟"
        )
        return True
    # --------------------------------------------------------
    # 当日交易次数
    # --------------------------------------------------------
    if (
        trades_today
        >=
        MAX_TRADES_PER_DAY
    ):
        print(
            f"[RISK] "
            f"今日已经开仓"
            f"{MAX_TRADES_PER_DAY}次"
        )
        return True
    return False
# ============================================================
# BTC Dominance
# ============================================================
def get_btc_dominance():
    global last_dom_check
    global btc_dominance
    now = time.time()
    if (
        btc_dominance is not None
        and
        now - last_dom_check
        <
        DOMINANCE_CHECK_SECONDS
    ):
        return btc_dominance
    try:
        response = requests.get(
            "https://api.coingecko.com/api/v3/global",
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        btc = (
            data["data"]
            ["market_cap_percentage"]
            ["btc"]
        )
        btc_dominance = D(btc)
        last_dom_check = now
        return btc_dominance
    except Exception as e:
        print(
            f"[DOM] "
            f"BTC Dominance获取失败: {e}"
        )
        return btc_dominance
# ============================================================
# 主策略
# ============================================================
def process_signal():
    global last_processed_candle
    global trades_today
    # --------------------------------------------------------
    # 账户
    # --------------------------------------------------------
    equity = get_account_balance()
    if equity <= 0:
        print(
            "[ACCOUNT] "
            "USDT权益无效"
        )
        return
    reset_daily_state_if_needed(
        equity
    )
    # --------------------------------------------------------
    # 查询已经结束的交易
    # --------------------------------------------------------
    report_recent_closed_trades()
    # --------------------------------------------------------
    # 如果已经有仓位
    # --------------------------------------------------------
    position = get_position()
    if position:
        print(
            f"[POSITION] "
            f"{position['side']} "
            f"{decimal_to_string(position['pos'])}张 "
            f"均价="
            f"{decimal_to_string(position['avgPx'])}"
        )
        print(
            "[POSITION] "
            "已有仓位，"
            "不因为反向信号平仓，"
            "等待TP/SL"
        )
        return
    # --------------------------------------------------------
    # 风控
    # --------------------------------------------------------
    if risk_blocked(
        equity
    ):
        return
    # --------------------------------------------------------
    # 当前价格
    # --------------------------------------------------------
    current_price = get_ticker()
    # --------------------------------------------------------
    # 日线
    # --------------------------------------------------------
    daily = closed_candles(
        get_candles(
            "1D",
            250
        )
    )
    # --------------------------------------------------------
    # 1小时
    # --------------------------------------------------------
    hourly = closed_candles(
        get_candles(
            "1H",
            250
        )
    )
    # --------------------------------------------------------
    # 15分钟
    # --------------------------------------------------------
    m15 = closed_candles(
        get_candles(
            "15m",
            120
        )
    )
    # --------------------------------------------------------
    # 5分钟
    # --------------------------------------------------------
    m5 = closed_candles(
        get_candles(
            "5m",
            150
        )
    )
    if (
        not daily
        or
        not hourly
        or
        not m15
        or
        not m5
    ):
        print(
            "[DATA] "
            "多周期数据不足"
        )
        return
    # --------------------------------------------------------
    # 同一根5分钟K线只判断一次
    # --------------------------------------------------------
    candle_id = m5[-1]["ts"]
    if (
        candle_id
        ==
        last_processed_candle
    ):
        return
    last_processed_candle = candle_id
    # ========================================================
    # 日线趋势
    # ========================================================
    dt = daily_trend(
        daily,
        current_price
    )
    trend = dt["trend"]
    print("")
    print("========================================")
    print(
        f"[PRICE] "
        f"{decimal_to_string(current_price)}"
    )
    print(
        f"[DAILY] "
        f"{trend}"
    )
    print(
        f"[MA] "
        f"MA5="
        f"{decimal_to_string(dt['ma5']) if dt['ma5'] else '-'} "
        f"MA20="
        f"{decimal_to_string(dt['ma20']) if dt['ma20'] else '-'} "
        f"MA60="
        f"{decimal_to_string(dt['ma60']) if dt['ma60'] else '-'} "
        f"MA200="
        f"{decimal_to_string(dt['ma200']) if dt['ma200'] else '-'}"
    )
    # ========================================================
    # 空头趋势 = 0%仓位
    # ========================================================
    if trend == "SHORT":
        print(
            "[FILTER] "
            "日线空头趋势，"
            "按照当前仓位规则保持空仓"
        )
        return
    # WARNING不做
    if trend == "WARNING":
        print(
            "[FILTER] "
            "日线WARNING，"
            "等待趋势明确"
        )
        return
    # ========================================================
    # 1H
    # ========================================================
    h1_trend = hourly_trend(
        hourly
    )
    print(
        f"[1H] {h1_trend}"
    )
    # ========================================================
    # 15m支撑阻力
    # ========================================================
    support, resistance = (
        get_support_resistance(
            m15
        )
    )
    print(
        f"[15M] "
        f"支撑="
        f"{decimal_to_string(support) if support else '-'} "
        f"阻力="
        f"{decimal_to_string(resistance) if resistance else '-'}"
    )
    # ========================================================
    # 5m
    # ========================================================
    signal_data = five_min_signal(
        m5,
        support,
        resistance
    )
    stars = signal_data["stars"]
    star_text = (
        "★" * stars
        +
        "☆" * (5 - stars)
    )
    print(
        f"[5M] "
        f"signal={signal_data['signal']} "
        f"{star_text} "
        f"score={signal_data['score']} "
        f"RSI="
        f"{decimal_to_string(signal_data['rsi']) if signal_data.get('rsi') else '-'}"
    )
    print(
        f"[5M] "
        f"{signal_data['reason']}"
    )
    # ========================================================
    # BTC Dominance
    # ========================================================
    dom = get_btc_dominance()
    if dom is not None:
        if dom >= BTC_DOM_HIGH:
            dom_state = "HIGH"
        elif dom <= BTC_DOM_LOW:
            dom_state = "LOW"
        else:
            dom_state = "NORMAL"
        print(
            f"[DOM] "
            f"BTC Dominance="
            f"{decimal_to_string(dom)}% "
            f"{dom_state}"
        )
    # ========================================================
    # 少于3星不交易
    # ========================================================
    if stars < 3:
        print(
            "[FILTER] "
            "共振不足3星，"
            "宁愿空仓"
        )
        return
    signal = signal_data[
        "signal"
    ]
    # ========================================================
    # 当前仓位规则下：
    #
    # 日线多头才做多
    # 日线空头0%
    #
    # 所以SHORT不会真正开仓
    # ========================================================
    if signal == "SHORT":
        print(
            "[FILTER] "
            "出现空头信号，"
            "但当前账户仓位规则规定"
            "空头趋势0%仓位，保持空仓"
        )
        return
    if signal != "LONG":
        print(
            "[FILTER] "
            "没有明确做多信号"
        )
        return
    # ========================================================
    # 日线过滤
    # ========================================================
    if trend not in (
        "LONG",
        "STRONG_LONG"
    ):
        print(
            "[FILTER] "
            "日线不支持做多"
        )
        return
    # ========================================================
    # 1H过滤
    # ========================================================
    if h1_trend != "LONG":
        print(
            "[FILTER] "
            "1H趋势没有确认做多"
        )
        return
    # ========================================================
    # 阻力过滤
    #
    # 如果阻力距离当前价格 <=1%
    # 不追多
    # ========================================================
    if resistance is not None:
        distance_to_resistance = (
            resistance
            -
            current_price
        ) / current_price
        if (
            distance_to_resistance
            <=
            D("0.01")
        ):
            print(
                "[FILTER] "
                "上方阻力太近，"
                "不追多"
            )
            return
    # ========================================================
    # ATR
    # ========================================================
    atr = signal_data[
        "atr"
    ]
    if atr <= 0:
        print(
            "[FILTER] "
            "ATR无效"
        )
        return
    # ========================================================
    # 止损
    # ========================================================
    stop_distance = (
        atr
        *
        ATR_SL_MULTIPLIER
    )
    stop_price = (
        current_price
        -
        stop_distance
    )
    take_price = (
        current_price
        +
        stop_distance
        *
        RISK_REWARD
    )
    stop_price = price_to_tick(
        stop_price
    )
    take_price = price_to_tick(
        take_price
    )
    # ========================================================
    # 检查风险收益比
    # ========================================================
    risk = (
        current_price
        -
        stop_price
    )
    reward = (
        take_price
        -
        current_price
    )
    if (
        risk <= 0
        or
        reward <= 0
    ):
        print(
            "[FILTER] "
            "TP/SL价格无效"
        )
        return
    rr = reward / risk
    if rr < MIN_RR:
        print(
            f"[FILTER] "
            f"RR={decimal_to_string(rr)} "
            f"< {decimal_to_string(MIN_RR)}"
        )
        return
    # ========================================================
    # 杠杆
    # ========================================================
    leverage = choose_leverage(
        stars,
        trend
    )
    # ========================================================
    # 设置杠杆
    # ========================================================
    set_leverage(
        leverage
    )
    # ========================================================
    # 计算仓位
    # ========================================================
    sizing = calculate_order_size(
        equity=
            equity,
        price=
            current_price,
        stop_distance=
            stop_distance,
        leverage=
            leverage,
        trend=
            trend
    )
    if not sizing:
        print(
            "[SIZE] "
            "根据最小下单单位和风险限制，"
            "本次无法安全开仓"
        )
        return
    size = sizing[
        "contracts"
    ]
    # ========================================================
    # 最终安全检查
    # ========================================================
    if sizing["margin"] > (
        equity
        *
        PER_TRADE_MARGIN_PCT
    ):
        print(
            "[SAFETY] "
            "保证金超过5%，"
            "取消开仓"
        )
        return
    # ========================================================
    # 打印最终信号
    # ========================================================
    print("")
    print(
        "========== 最终交易信号 =========="
    )
    print(
        f"方向：{signal}"
    )
    print(
        f"趋势：{trend}"
    )
    print(
        f"信号等级：{star_text}"
    )
    print(
        f"当前价格："
        f"{decimal_to_string(current_price)}"
    )
    print(
        f"杠杆：{leverage}x"
    )
    print(
        f"合约数量："
        f"{decimal_to_string(size)}"
    )
    print(
        f"保证金："
        f"{decimal_to_string(sizing['margin'])}"
    )
    print(
        f"名义仓位："
        f"{decimal_to_string(sizing['notional'])}"
    )
    print(
        f"止损："
        f"{decimal_to_string(stop_price)}"
    )
    print(
        f"止盈："
        f"{decimal_to_string(take_price)}"
    )
    print(
        f"盈亏比："
        f"{decimal_to_string(rr)}"
    )
    print(
        "=================================="
    )
    # ========================================================
    # 下单前再次检查有没有仓位
    # ========================================================
    if get_position():
        print(
            "[SAFETY] "
            "下单前发现已有仓位，"
            "取消本次交易"
        )
        return
    # ========================================================
    # 正式开仓
    # ========================================================
    result = place_order(
        side=
            signal,
        size=
            size,
        leverage=
            leverage,
        entry_price=
            current_price,
        stop_price=
            stop_price,
        take_price=
            take_price
    )
    trades_today += 1
    print(
        "[ORDER] "
        "下单结果："
    )
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2
        )
    )
# ============================================================
# 启动检查
# ============================================================
def startup_check():
    global instrument
    check_config()
    instrument = get_instrument()
    print("")
    print(
        "========================================"
    )
    print(
        "OKX BTC 永续自动交易机器人"
    )
    print(
        "模式：模拟盘"
    )
    print(
        f"合约：{INST_ID}"
    )
    print(
        f"保证金模式：{TD_MODE}"
    )
    print(
        "策略：多周期趋势 + 共振信号"
    )
    print(
        "5m确认：至少连续3根K线"
    )
    print(
        "单笔最大保证金：5%"
    )
    print(
        "单笔理论风险：0.8%"
    )
    print(
        "最大杠杆：15x"
    )
    print(
        "强多头最大资金占用：80%"
    )
    print(
        "多头最大资金占用：60%"
    )
    print(
        "震荡最大资金占用：30%"
    )
    print(
        "空头：0%"
    )
    print(
        "持仓期间：反向信号不直接平仓"
    )
    print(
        "========================================"
    )
    print(
        f"[INSTRUMENT] "
        f"ctVal={instrument['ctVal']} "
        f"lotSz={instrument['lotSz']} "
        f"minSz={instrument['minSz']} "
        f"tickSz={instrument['tickSz']}"
    )
    config = get_account_config()
    print(
        f"[ACCOUNT] "
        f"acctLv={config.get('acctLv')} "
        f"posMode={config.get('posMode')}"
    )
    equity = get_account_balance()
    if equity <= 0:
        raise RuntimeError(
            "账户USDT权益无效"
        )
    reset_daily_state_if_needed(
        equity
    )
    print(
        f"[ACCOUNT] "
        f"USDT权益="
        f"{decimal_to_string(equity)}"
    )
# ============================================================
# 主循环
# ============================================================
def main():
    startup_check()
    while True:
        try:
            process_signal()
        except KeyboardInterrupt:
            print(
                "程序停止"
            )
            break
        except Exception as e:
            print(
                f"[ERROR] "
                f"{type(e).__name__}: {e}"
            )
        time.sleep(
            LOOP_SECONDS
        )
# ============================================================
# 程序入口
# ============================================================
if __name__ == "__main__":
    main()