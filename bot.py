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

# 5分钟K线
BAR = "5m"

# 每20秒检查一次
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

# 止损 = ATR × 1.5
ATR_SL_MULTIPLIER = 1.5

# 止盈 = 止损距离 × 1.8
RISK_REWARD = 1.8

# 每笔交易最多使用账户权益8%作为保证金
MAX_MARGIN_PCT = 0.08

# 动态杠杆
MIN_LEVERAGE = 5
MAX_LEVERAGE = 20

# 最低信号分数
MIN_SIGNAL_SCORE = 4

# 连续亏损保护
MAX_CONSECUTIVE_LOSSES = 3
LOSS_COOLDOWN_SECONDS = 30 * 60


# ============================================================
# 全局状态
# ============================================================

instrument = None

last_processed_candle = None

cooldown_until = 0

consecutive_losses = 0

# 已经显示过的成交ID
reported_close_trades = set()


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
    print("模拟盘         : 是")
    print("交易品种       :", INST_ID)
    print("K线            :", BAR)
    print()


# ============================================================
# OKX服务器时间
# ============================================================

def get_server_time():

    response = requests.get(
        BASE_URL + "/api/v5/public/time",
        timeout=10
    )

    response.raise_for_status()

    result = response.json()

    if result.get("code") != "0":
        raise Exception(
            "获取服务器时间失败: " + str(result)
        )

    timestamp_ms = int(
        result["data"][0]["ts"]
    )

    dt = datetime.fromtimestamp(
        timestamp_ms / 1000,
        tz=timezone.utc
    )

    return (
        dt.strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3]
        + "Z"
    )


# ============================================================
# 签名
# ============================================================

def make_signature(
    timestamp,
    method,
    request_path,
    body
):

    message = (
        timestamp
        + method.upper()
        + request_path
        + body
    )

    mac = hmac.new(
        SECRET_KEY.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    )

    return base64.b64encode(
        mac.digest()
    ).decode()


# ============================================================
# 私有API
# ============================================================

def private_request(
    method,
    request_path,
    body=""
):

    method = method.upper()

    timestamp = get_server_time()

    signature = make_signature(
        timestamp,
        method,
        request_path,
        body
    )

    headers = {
        "Content-Type": "application/json",
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        "x-simulated-trading": SIMULATED_TRADING
    }

    url = BASE_URL + request_path

    if method == "GET":

        response = requests.get(
            url,
            headers=headers,
            timeout=10
        )

    else:

        response = requests.post(
            url,
            headers=headers,
            data=body,
            timeout=10
        )

    response.raise_for_status()

    result = response.json()

    if result.get("code") != "0":

        print("OKX API 错误:")

        print(
            json.dumps(
                result,
                indent=2,
                ensure_ascii=False
            )
        )

    return result


# ============================================================
# 公共API
# ============================================================

def public_get(path):

    response = requests.get(
        BASE_URL + path,
        timeout=10
    )

    response.raise_for_status()

    result = response.json()

    if result.get("code") != "0":

        raise Exception(
            "OKX 公共 API 错误: "
            + str(result)
        )

    return result


# ============================================================
# Decimal工具
# ============================================================

def D(value):

    return Decimal(str(value))


def decimal_to_string(
    value,
    places=8
):

    q = Decimal("1").scaleb(-places)

    return format(
        D(value).quantize(
            q,
            rounding=ROUND_DOWN
        ),
        "f"
    )


def floor_to_step(
    value,
    step
):

    value = D(value)

    step = D(step)

    if step <= 0:
        return value

    return (
        value / step
    ).to_integral_value(
        rounding=ROUND_DOWN
    ) * step


def price_to_tick(
    price,
    tick_size
):

    return floor_to_step(
        price,
        tick_size
    )


def size_to_lot(
    size,
    lot_size
):

    return floor_to_step(
        size,
        lot_size
    )


# ============================================================
# 获取合约信息
# ============================================================

def get_instrument():

    global instrument

    path = (
        "/api/v5/public/instruments"
        "?instType=SWAP"
        f"&instId={INST_ID}"
    )

    result = public_get(path)

    if not result.get("data"):

        raise Exception(
            f"找不到合约: {INST_ID}"
        )

    instrument = result["data"][0]

    print("=" * 60)
    print("BTC 永续合约信息")
    print("=" * 60)

    print(
        "instId :",
        instrument.get("instId")
    )

    print(
        "ctVal  :",
        instrument.get("ctVal")
    )

    print(
        "lotSz  :",
        instrument.get("lotSz")
    )

    print(
        "minSz  :",
        instrument.get("minSz")
    )

    print(
        "tickSz :",
        instrument.get("tickSz")
    )

    print()


def get_lot_size():

    return D(
        instrument.get(
            "lotSz",
            "1"
        )
    )


def get_min_size():

    return D(
        instrument.get(
            "minSz",
            "1"
        )
    )


def get_tick_size():

    return D(
        instrument.get(
            "tickSz",
            "0.1"
        )
    )


def get_contract_value():

    return D(
        instrument.get(
            "ctVal",
            "0.01"
        )
    )


# ============================================================
# 获取账户
# ============================================================

def get_account_balance():

    result = private_request(
        "GET",
        "/api/v5/account/balance"
    )

    if result.get("code") != "0":

        raise Exception(
            "获取账户余额失败"
        )

    data = result.get(
        "data",
        []
    )

    if not data:
        return D("0")

    details = data[0].get(
        "details",
        []
    )

    for item in details:

        if item.get("ccy") == "USDT":

            eq = (
                item.get("eq")
                or item.get("cashBal")
                or "0"
            )

            return D(eq)

    return D("0")


# ============================================================
# 账户配置
# ============================================================

def get_account_config():

    result = private_request(
        "GET",
        "/api/v5/account/config"
    )

    if result.get("code") != "0":

        raise Exception(
            "获取账户配置失败"
        )

    data = result.get(
        "data",
        []
    )

    if not data:

        raise Exception(
            "OKX没有返回账户配置"
        )

    return data[0]


# ============================================================
# 设置杠杆
# ============================================================

def set_leverage(leverage):

    leverage = max(
        MIN_LEVERAGE,
        min(
            MAX_LEVERAGE,
            int(leverage)
        )
    )

    body_data = {

        "instId": INST_ID,

        "lever": str(leverage),

        "mgnMode": TD_MODE
    }

    body = json.dumps(
        body_data,
        separators=(",", ":")
    )

    result = private_request(
        "POST",
        "/api/v5/account/set-leverage",
        body
    )

    if result.get("code") != "0":

        raise Exception(
            "设置杠杆失败: "
            + json.dumps(
                result,
                ensure_ascii=False
            )
        )

    print(
        f"杠杆已设置为 {leverage}x"
    )

    return leverage


# ============================================================
# K线
# ============================================================

def get_candles(limit=150):

    path = (
        "/api/v5/market/candles"
        f"?instId={INST_ID}"
        f"&bar={BAR}"
        f"&limit={limit}"
    )

    result = public_get(path)

    rows = result.get(
        "data",
        []
    )

    candles = []

    for row in reversed(rows):

        if len(row) < 9:
            continue

        candles.append({

            "ts": int(row[0]),

            "open": D(row[1]),

            "high": D(row[2]),

            "low": D(row[3]),

            "close": D(row[4]),

            "volume": D(row[5]),

            "confirm": row[8]
        })

    return candles


# ============================================================
# EMA
# ============================================================

def ema(
    values,
    period
):

    if len(values) < period:
        return None

    multiplier = (
        D("2")
        / D(period + 1)
    )

    result = (
        sum(values[:period])
        / D(period)
    )

    for price in values[period:]:

        result = (
            (price - result)
            * multiplier
        ) + result

    return result


# ============================================================
# EMA序列
# ============================================================

def ema_series(
    values,
    period
):

    if len(values) < period:
        return []

    multiplier = (
        D("2")
        / D(period + 1)
    )

    first = (
        sum(values[:period])
        / D(period)
    )

    result = [None] * (
        period - 1
    )

    result.append(first)

    previous = first

    for price in values[period:]:

        current = (
            (price - previous)
            * multiplier
        ) + previous

        result.append(current)

        previous = current

    return result


# ============================================================
# RSI
# ============================================================

def calculate_rsi(
    closes,
    period=14
):

    if len(closes) <= period:
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
                D("0")
            )

        else:

            gains.append(
                D("0")
            )

            losses.append(
                abs(change)
            )

    avg_gain = (
        sum(gains[:period])
        / D(period)
    )

    avg_loss = (
        sum(losses[:period])
        / D(period)
    )

    for i in range(
        period,
        len(gains)
    ):

        avg_gain = (
            (
                avg_gain
                * D(period - 1)
            )
            + gains[i]
        ) / D(period)

        avg_loss = (
            (
                avg_loss
                * D(period - 1)
            )
            + losses[i]
        ) / D(period)

    if avg_loss == 0:

        return D("100")

    rs = (
        avg_gain
        / avg_loss
    )

    return D("100") - (
        D("100")
        / (
            D("1")
            + rs
        )
    )


# ============================================================
# MACD
# ============================================================

def calculate_macd(closes):

    fast_series = ema_series(
        closes,
        MACD_FAST
    )

    slow_series = ema_series(
        closes,
        MACD_SLOW
    )

    if not slow_series:
        return None

    macd_values = []

    for i in range(
        len(closes)
    ):

        if (
            fast_series[i] is None
            or slow_series[i] is None
        ):

            macd_values.append(
                None
            )

        else:

            macd_values.append(
                fast_series[i]
                - slow_series[i]
            )

    clean_macd = [
        x
        for x in macd_values
        if x is not None
    ]

    if len(clean_macd) < MACD_SIGNAL:
        return None

    signal_series = ema_series(
        clean_macd,
        MACD_SIGNAL
    )

    signal = signal_series[-1]

    if signal is None:
        return None

    macd = clean_macd[-1]

    return (
        macd,
        signal
    )


# ============================================================
# ATR
# ============================================================

def calculate_atr(
    candles,
    period=14
):

    if len(candles) <= period:
        return None

    trs = []

    for i in range(
        1,
        len(candles)
    ):

        current = candles[i]

        previous = candles[i - 1]

        high = current["high"]

        low = current["low"]

        previous_close = (
            previous["close"]
        )

        tr = max(

            high - low,

            abs(
                high
                - previous_close
            ),

            abs(
                low
                - previous_close
            )
        )

        trs.append(tr)

    atr = (
        sum(trs[:period])
        / D(period)
    )

    for tr in trs[period:]:

        atr = (
            (
                atr
                * D(period - 1)
            )
            + tr
        ) / D(period)

    return atr


# ============================================================
# 信号
# ============================================================

def calculate_signal(
    candles
):

    closed = [
        c
        for c in candles
        if c["confirm"] == "1"
    ]

    if len(closed) < 60:

        return {
            "signal": "WAIT",
            "score": 0
        }

    closes = [
        c["close"]
        for c in closed
    ]

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

    macd_data = calculate_macd(
        closes
    )

    atr = calculate_atr(
        closed,
        ATR_PERIOD
    )

    if None in (
        fast,
        slow,
        rsi,
        atr
    ):

        return {
            "signal": "WAIT",
            "score": 0
        }

    if macd_data is None:

        return {
            "signal": "WAIT",
            "score": 0
        }

    macd, macd_signal = (
        macd_data
    )

    price = closes[-1]

    long_score = 0

    short_score = 0

    # EMA趋势

    if fast > slow:
        long_score += 2

    if fast < slow:
        short_score += 2

    # 价格位置

    if price > fast:
        long_score += 1

    if price < fast:
        short_score += 1

    # RSI

    if (
        LONG_RSI_MIN
        <= rsi
        <= LONG_RSI_MAX
    ):

        long_score += 1

    if (
        SHORT_RSI_MIN
        <= rsi
        <= SHORT_RSI_MAX
    ):

        short_score += 1

    # MACD

    if (
        macd > macd_signal
        and macd > 0
    ):

        long_score += 2

    if (
        macd < macd_signal
        and macd < 0
    ):

        short_score += 2

    # 最终信号

    if (
        long_score >= MIN_SIGNAL_SCORE
        and long_score > short_score
    ):

        signal = "LONG"

        score = long_score

    elif (
        short_score >= MIN_SIGNAL_SCORE
        and short_score > long_score
    ):

        signal = "SHORT"

        score = short_score

    else:

        signal = "WAIT"

        score = max(
            long_score,
            short_score
        )

    return {

        "signal": signal,

        "score": score,

        "price": price,

        "ema_fast": fast,

        "ema_slow": slow,

        "rsi": rsi,

        "macd": macd,

        "macd_signal": macd_signal,

        "atr": atr,

        "candle_ts": closed[-1]["ts"]
    }


# ============================================================
# 动态杠杆
# ============================================================

def choose_leverage(
    signal_info
):

    score = signal_info["score"]

    if score <= 4:
        return 5

    if score == 5:
        return 8

    if score == 6:
        return 10

    if score == 7:
        return 15

    return 20


# ============================================================
# 当前持仓
# ============================================================

def get_position():

    path = (
        "/api/v5/account/positions"
        f"?instType={INST_TYPE}"
        f"&instId={INST_ID}"
    )

    result = private_request(
        "GET",
        path
    )

    if result.get("code") != "0":

        raise Exception(
            "获取持仓失败"
        )

    positions = result.get(
        "data",
        []
    )

    for p in positions:

        pos = D(
            p.get(
                "pos",
                "0"
            )
        )

        if pos != 0:

            return p

    return None


# ============================================================
# 计算仓位
# ============================================================

def calculate_order_size(
    price,
    leverage
):

    equity = get_account_balance()

    if equity <= 0:

        raise Exception(
            "USDT账户权益为0"
        )

    margin = (
        equity
        * D(
            str(
                MAX_MARGIN_PCT
            )
        )
    )

    notional = (
        margin
        * D(leverage)
    )

    btc_size = (
        notional
        / price
    )

    ct_val = (
        get_contract_value()
    )

    contracts = (
        btc_size
        / ct_val
    )

    contracts = size_to_lot(
        contracts,
        get_lot_size()
    )

    min_size = (
        get_min_size()
    )

    if contracts < min_size:

        contracts = min_size

    print()
    print("仓位计算")
    print("账户权益 :", equity)
    print("使用保证金:", margin)
    print("杠杆     :", leverage)
    print("名义价值 :", notional)
    print("合约面值 :", ct_val)
    print("下单张数 :", contracts)
    print()

    return contracts


# ============================================================
# 开仓
# ============================================================

def place_order(
    direction,
    size,
    leverage,
    entry_price,
    stop_loss,
    take_profit
):

    side = (
        "buy"
        if direction == "LONG"
        else "sell"
    )

    cl_ord_id = (
        "autobot"
        + direction.lower()
        + str(int(time.time()))
    )[-32:]

    attach_algo = {

        "tpTriggerPx":
            decimal_to_string(
                take_profit,
                2
            ),

        "tpOrdPx": "-1",

        "tpTriggerPxType": "mark",

        "slTriggerPx":
            decimal_to_string(
                stop_loss,
                2
            ),

        "slOrdPx": "-1",

        "slTriggerPxType": "mark"
    }

    body_data = {

        "instId": INST_ID,

        "tdMode": TD_MODE,

        "clOrdId": cl_ord_id,

        "side": side,

        "ordType": "market",

        "sz":
            decimal_to_string(
                size,
                8
            ),

        "attachAlgoOrds":
            [attach_algo]
    }

    body = json.dumps(
        body_data,
        separators=(",", ":")
    )

    print("=" * 60)
    print("准备开仓")
    print("=" * 60)
    print("方向       :", direction)
    print("杠杆       :", leverage, "x")
    print("数量       :", size)
    print("参考入场价 :", entry_price)
    print("止损       :", stop_loss)
    print("止盈       :", take_profit)
    print()

    result = private_request(
        "POST",
        "/api/v5/trade/order",
        body
    )

    print(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False
        )
    )

    if result.get("code") != "0":

        raise Exception(
            "开仓失败: "
            + json.dumps(
                result,
                ensure_ascii=False
            )
        )

    data = result.get(
        "data",
        []
    )

    if not data:

        raise Exception(
            "OKX没有返回订单数据"
        )

    order = data[0]

    if order.get("sCode") != "0":

        raise Exception(
            "OKX拒绝订单: "
            + str(
                order.get("sCode")
            )
            + " "
            + str(
                order.get("sMsg")
            )
        )

    print()
    print("✅ 开仓订单已提交")
    print(
        "订单ID:",
        order.get("ordId")
    )
    print()


# ============================================================
# 平仓
# ============================================================

def close_position(
    position
):

    pos = D(
        position.get(
            "pos",
            "0"
        )
    )

    if pos == 0:
        return

    position_side = (
        position.get(
            "posSide",
            "net"
        )
    )

    if position_side == "long":

        side = "sell"

        pos_side = "long"

    elif position_side == "short":

        side = "buy"

        pos_side = "short"

    else:

        side = (
            "sell"
            if pos > 0
            else "buy"
        )

        pos_side = "net"

    size = abs(pos)

    body_data = {

        "instId": INST_ID,

        "tdMode": TD_MODE,

        "side": side,

        "ordType": "market",

        "sz":
            decimal_to_string(
                size,
                8
            ),

        "reduceOnly": True
    }

    if pos_side in (
        "long",
        "short"
    ):

        body_data["posSide"] = (
            pos_side
        )

    body = json.dumps(
        body_data,
        separators=(",", ":")
    )

    print("=" * 60)
    print("准备平仓")
    print("=" * 60)
    print("当前持仓:", pos)
    print("平仓方向:", side)
    print()

    result = private_request(
        "POST",
        "/api/v5/trade/order",
        body
    )

    print(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False
        )
    )

    if result.get("code") != "0":

        raise Exception(
            "平仓失败: "
            + json.dumps(
                result,
                ensure_ascii=False
            )
        )

    data = result.get(
        "data",
        []
    )

    if not data:

        raise Exception(
            "OKX没有返回平仓订单数据"
        )

    order = data[0]

    if order.get("sCode") != "0":

        raise Exception(
            "OKX拒绝平仓: "
            + str(
                order.get("sCode")
            )
            + " "
            + str(
                order.get("sMsg")
            )
        )

    ord_id = order.get(
        "ordId"
    )

    print()
    print("✅ 平仓订单已提交")
    print(
        "平仓订单ID:",
        ord_id
    )

    # 等待成交记录生成
    time.sleep(3)

    # 查询本次平仓到底赚还是亏
    report_order_pnl(
        ord_id
    )


# ============================================================
# 查询某个平仓订单的真实盈亏
# ============================================================

def report_order_pnl(
    ord_id
):

    if not ord_id:
        return

    path = (
        "/api/v5/trade/fills"
        f"?instType={INST_TYPE}"
        f"&instId={INST_ID}"
        f"&ordId={ord_id}"
        "&limit=100"
    )

    result = private_request(
        "GET",
        path
    )

    if result.get("code") != "0":

        print(
            "⚠️ 暂时无法查询平仓盈亏"
        )

        return

    fills = result.get(
        "data",
        []
    )

    if not fills:

        print(
            "⚠️ 暂时没有成交记录"
        )

        return

    total_pnl = D("0")

    total_fee = D("0")

    for fill in fills:

        total_pnl += D(
            fill.get(
                "fillPnl",
                "0"
            )
            or "0"
        )

        total_fee += D(
            fill.get(
                "fee",
                "0"
            )
            or "0"
        )

    net_pnl = (
        total_pnl
        + total_fee
    )

    print()
    print("=" * 60)
    print("📊 本次平仓结果")
    print("=" * 60)
    print("订单ID         :", ord_id)
    print("平仓盈亏       :", total_pnl, "USDT")
    print("手续费         :", total_fee, "USDT")
    print("扣手续费净盈亏 :", net_pnl, "USDT")

    if net_pnl > 0:

        print()
        print("🟢🟢🟢 这笔交易：盈利 🟢🟢🟢")

    elif net_pnl < 0:

        print()
        print("🔴🔴🔴 这笔交易：亏损 🔴🔴🔴")

    else:

        print()
        print("⚪ 这笔交易：基本持平")

    print("=" * 60)
    print()


# ============================================================
# 查询最近自动平仓
# ============================================================

def report_recent_closed_trades():

    global reported_close_trades

    path = (
        "/api/v5/trade/fills"
        f"?instType={INST_TYPE}"
        f"&instId={INST_ID}"
        "&limit=20"
    )

    result = private_request(
        "GET",
        path
    )

    if result.get("code") != "0":
        return

    fills = result.get(
        "data",
        []
    )

    for fill in reversed(fills):

        trade_id = str(
            fill.get(
                "tradeId",
                ""
            )
        )

        if not trade_id:
            continue

        if trade_id in reported_close_trades:
            continue

        fill_pnl = D(
            fill.get(
                "fillPnl",
                "0"
            )
            or "0"
        )

        fee = D(
            fill.get(
                "fee",
                "0"
            )
            or "0"
        )

        # 开仓成交一般 fillPnl = 0
        if fill_pnl == 0:
            continue

        net_pnl = (
            fill_pnl
            + fee
        )

        print()
        print("=" * 60)
        print("📊 检测到平仓成交")
        print("=" * 60)
        print(
            "订单ID       :",
            fill.get("ordId")
        )
        print(
            "成交价格     :",
            fill.get("fillPx")
        )
        print(
            "成交数量     :",
            fill.get("fillSz")
        )
        print(
            "平仓盈亏     :",
            fill_pnl,
            "USDT"
        )
        print(
            "手续费       :",
            fee,
            "USDT"
        )
        print(
            "净盈亏       :",
            net_pnl,
            "USDT"
        )

        if net_pnl > 0:

            print(
                "🟢 本次交易：盈利"
            )

        elif net_pnl < 0:

            print(
                "🔴 本次交易：亏损"
            )

        else:

            print(
                "⚪ 本次交易：持平"
            )

        print("=" * 60)
        print()

        reported_close_trades.add(
            trade_id
        )


# ============================================================
# 处理信号
# ============================================================

def process_signal():

    global last_processed_candle
    global cooldown_until

    # 先检查有没有TP/SL自动平仓
    try:

        report_recent_closed_trades()

    except Exception as e:

        print(
            "查询平仓结果失败:",
            str(e)
        )

    candles = get_candles(
        150
    )

    if not candles:
        return

    signal_info = calculate_signal(
        candles
    )

    candle_ts = signal_info.get(
        "candle_ts"
    )

    if candle_ts is None:

        print(
            "K线不足，等待..."
        )

        return

    # 一根K线只处理一次
    if (
        candle_ts
        == last_processed_candle
    ):

        return

    last_processed_candle = (
        candle_ts
    )

    now = time.time()

    print()
    print("=" * 60)
    print("新K线信号")
    print("=" * 60)

    print(
        "信号:",
        signal_info["signal"]
    )

    print(
        "分数:",
        signal_info["score"]
    )

    if signal_info["signal"] != "WAIT":

        print(
            "价格:",
            signal_info["price"]
        )

        print(
            "EMA9 :",
            signal_info["ema_fast"]
        )

        print(
            "EMA21:",
            signal_info["ema_slow"]
        )

        print(
            "RSI  :",
            signal_info["rsi"]
        )

        print(
            "MACD :",
            signal_info["macd"]
        )

        print(
            "SIGNAL:",
            signal_info["macd_signal"]
        )

        print(
            "ATR  :",
            signal_info["atr"]
        )

    # 风控冷却

    if now < cooldown_until:

        remain = int(
            cooldown_until
            - now
        )

        print(
            f"🛑 风控冷却中，还剩 {remain} 秒"
        )

        return

    position = get_position()

    # WAIT

    if signal_info["signal"] == "WAIT":

        print(
            "没有足够强的交易信号，不交易。"
        )

        return

    # 已有仓位

    if position:

        pos = D(
            position.get(
                "pos",
                "0"
            )
        )

        if pos != 0:

            current_pos_side = (
                position.get(
                    "posSide"
                )
            )

            if current_pos_side == "long":

                current_direction = (
                    "LONG"
                )

            elif current_pos_side == "short":

                current_direction = (
                    "SHORT"
                )

            else:

                current_direction = (
                    "LONG"
                    if pos > 0
                    else "SHORT"
                )

            print(
                "当前持仓:",
                current_direction,
                pos
            )

            # 信号反转

            if (
                current_direction
                != signal_info["signal"]
            ):

                print(
                    "⚠️ 信号反转，先平掉当前仓位。"
                )

                close_position(
                    position
                )

                time.sleep(2)

                print(
                    "本轮不立即反向开仓，等待下一根确认K线。"
                )

            else:

                print(
                    "已有同方向仓位，不重复开仓。"
                )

            return

    # ========================================================
    # 没有持仓 -> 准备开仓
    # ========================================================

    leverage = choose_leverage(
        signal_info
    )

    set_leverage(
        leverage
    )

    price = signal_info["price"]

    atr = signal_info["atr"]

    stop_distance = (
        atr
        * D(
            str(
                ATR_SL_MULTIPLIER
            )
        )
    )

    if stop_distance <= 0:

        print(
            "ATR无效，跳过交易。"
        )

        return

    direction = (
        signal_info["signal"]
    )

    if direction == "LONG":

        stop_loss = (
            price
            - stop_distance
        )

        take_profit = (
            price
            + stop_distance
            * D(
                str(
                    RISK_REWARD
                )
            )
        )

    else:

        stop_loss = (
            price
            + stop_distance
        )

        take_profit = (
            price
            - stop_distance
            * D(
                str(
                    RISK_REWARD
                )
            )
        )

    tick = get_tick_size()

    stop_loss = price_to_tick(
        stop_loss,
        tick
    )

    take_profit = price_to_tick(
        take_profit,
        tick
    )

    # 安全检查

    if direction == "LONG":

        if stop_loss >= price:

            print(
                "多单止损价格异常，取消交易。"
            )

            return

        if take_profit <= price:

            print(
                "多单止盈价格异常，取消交易。"
            )

            return

    if direction == "SHORT":

        if stop_loss <= price:

            print(
                "空单止损价格异常，取消交易。"
            )

            return

        if take_profit >= price:

            print(
                "空单止盈价格异常，取消交易。"
            )

            return

    size = calculate_order_size(
        price,
        leverage
    )

    place_order(

        direction=direction,

        size=size,

        leverage=leverage,

        entry_price=price,

        stop_loss=stop_loss,

        take_profit=take_profit
    )


# ============================================================
# 启动检查
# ============================================================

def startup_check():

    print("=" * 60)
    print(
        "启动 OKX BTC 自动交易模拟盘"
    )
    print("=" * 60)

    config = get_account_config()

    print(
        "账户模式:",
        config.get("acctLv")
    )

    print(
        "持仓模式:",
        config.get("posMode")
    )

    print()

    if config.get(
        "posMode"
    ) != "net_mode":

        print(
            "⚠️ 当前不是 net_mode。"
        )

        print(
            "本程序默认按 net_mode 设计。"
        )

    get_instrument()

    balance = get_account_balance()

    print(
        "USDT账户权益:",
        balance
    )

    print()

    if balance <= 0:

        print(
            "⚠️ USDT权益为0。"
        )

        print(
            "请确认模拟盘有模拟资金。"
        )


# ============================================================
# 主程序
# ============================================================

def main():

    check_config()

    startup_check()

    print("=" * 60)
    print("策略参数")
    print("=" * 60)

    print(
        "交易品种       :",
        INST_ID
    )

    print(
        "K线周期        :",
        BAR
    )

    print(
        "EMA            :",
        EMA_FAST,
        "/",
        EMA_SLOW
    )

    print(
        "RSI            :",
        RSI
