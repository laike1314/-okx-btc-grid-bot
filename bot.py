# -*- coding: utf-8 -*-

"""
OKX 全市场永续强行情自动交易机器人 V2.8
================================================

V2.8 核心升级：

1. 新开仓：
   - 市价开仓
   - 获取真实成交均价
   - 根据 ATR 计算 TP/SL
   - OCO 保护
   - 严格验证 OCO
   - OCO 失败自动紧急平仓

2. 已有仓位：
   - 自动检查是否存在有效 OCO
   - 没有 OCO -> 自动接管
   - 自动读取当前 Mark Price
   - 自动读取当前 ATR
   - 根据当前市场价格重新计算 TP/SL
   - 创建新的 OCO
   - 验证成功后继续运行

3. 旧 conditional：
   - 自动发现旧 conditional TP/SL
   - 取消旧保护
   - 转换成新的 OCO

4. 防止 TP/SL 已经穿价：
   - LONG：要求 SL < Mark < TP
   - SHORT：要求 TP < Mark < SL

5. OCO 创建失败：
   - 默认紧急市价平仓

6. 默认 OKX 模拟盘。

环境变量：

OKX_API_KEY
OKX_SECRET_KEY
OKX_PASSPHRASE

依赖：

pip install requests pandas
"""

import os
import time
import json
import hmac
import base64
import hashlib
import traceback
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP

import requests
import pandas as pd


# ============================================================
# 基础配置
# ============================================================

BASE_URL = "https://www.okx.com"

SIMULATED_TRADING = "1"

INST_TYPE = "SWAP"

SETTLE_CCY = "USDT"

TD_MODE = "cross"

POS_MODE = "net"


# ============================================================
# K线
# ============================================================

BAR = "5m"

LOOP_SECONDS = 20


# ============================================================
# 指标
# ============================================================

EMA_FAST = 9
EMA_SLOW = 21

RSI_PERIOD = 14

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

ATR_PERIOD = 14


# ============================================================
# 信号
# ============================================================

MIN_SIGNAL_SCORE = 4

RSI_LONG_MIN = 52
RSI_LONG_MAX = 72

RSI_SHORT_MIN = 28
RSI_SHORT_MAX = 48


# ============================================================
# 风险
# ============================================================

RISK_PER_TRADE_PCT = 0.005

MAX_MARGIN_PCT = 0.08

MIN_LEVERAGE = 5

MAX_LEVERAGE = 15

ATR_SL_MULTIPLIER = 1.5

RISK_REWARD = 1.8


# ============================================================
# 手续费
# ============================================================

TAKER_FEE_RATE = 0.0005

FEE_BUFFER_MULTIPLIER = 1.0


# ============================================================
# 连续亏损
# ============================================================

MAX_CONSECUTIVE_LOSSES = 3

LOSS_COOLDOWN_SECONDS = 1800


# ============================================================
# 每日限制
# ============================================================

MAX_DAILY_LOSS_PCT = 0.03

MAX_DAILY_PROFIT_PCT = 0.05

MAX_DAILY_TRADES = 12


# ============================================================
# 全市场扫描
# ============================================================

SCAN_LIMIT = 80

MIN_24H_VOL_USDT = 3_000_000

MIN_ATR_PCT = 0.0015


# ============================================================
# API
# ============================================================

REQUEST_TIMEOUT = 12

ORDER_FILL_TIMEOUT = 15

PROTECTION_TIMEOUT = 12


# ============================================================
# 安全
# ============================================================

EMERGENCY_CLOSE_IF_OCO_FAIL = True

REPAIR_UNKNOWN_OLD_SL = True


# ============================================================
# V2.8 新增
# ============================================================

# 发现没有 TP/SL 的旧仓位时：
# True = 自动接管
AUTO_ADOPT_UNPROTECTED_POSITION = True

# 接管旧仓位时：
# 使用当前 Mark + ATR 重新计算保护
ADOPT_USE_CURRENT_MARK = True

# 如果 ATR 获取失败，允许使用价格百分比作为备用
ADOPT_FALLBACK_ATR_PCT = 0.005


# ============================================================
# 状态文件
# ============================================================

STATE_FILE = "bot_state_v28.json"

LEGACY_STATE_FILE = "bot_state_v27.json"


# ============================================================
# API 密钥
# ============================================================

API_KEY = os.getenv(
    "OKX_API_KEY",
    ""
)

SECRET_KEY = os.getenv(
    "OKX_SECRET_KEY",
    ""
)

PASSPHRASE = os.getenv(
    "OKX_PASSPHRASE",
    ""
)


if (
    not API_KEY
    or
    not SECRET_KEY
    or
    not PASSPHRASE
):

    raise RuntimeError(
        "缺少 OKX_API_KEY / "
        "OKX_SECRET_KEY / "
        "OKX_PASSPHRASE 环境变量"
    )


# ============================================================
# 日志
# ============================================================

def log(msg):

    print(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}",
        flush=True
    )


# ============================================================
# 状态
# ============================================================

def default_state():

    return {

        "date":
            datetime.now().strftime(
                "%Y-%m-%d"
            ),

        "start_equity":
            0.0,

        "trades":
            0,

        "consecutive_losses":
            0,

        "cooldown_until":
            0,

        "last_trade_inst":
            "",

        "active_trade":
            None,
    }


def load_state():

    default = default_state()

    # --------------------------------------------------------
    # 优先 V2.8
    # --------------------------------------------------------

    file_to_load = None

    if os.path.exists(
        STATE_FILE
    ):

        file_to_load = STATE_FILE

    elif os.path.exists(
        LEGACY_STATE_FILE
    ):

        file_to_load = (
            LEGACY_STATE_FILE
        )

        log(
            "发现 V2.7 状态文件，"
            "准备迁移到 V2.8"
        )

    if not file_to_load:

        return default

    try:

        with open(
            file_to_load,
            "r",
            encoding="utf-8"
        ) as f:

            state = json.load(f)

        for key, value in default.items():

            state.setdefault(
                key,
                value
            )

        return state

    except Exception as e:

        log(
            f"读取状态文件失败：{e}"
        )

        return default


def save_state(state):

    tmp = STATE_FILE + ".tmp"

    with open(
        tmp,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2
        )

    os.replace(
        tmp,
        STATE_FILE
    )


state = load_state()


# ============================================================
# Decimal
# ============================================================

def D(x):

    return Decimal(
        str(x)
    )


def fmt_decimal(x):

    s = format(
        D(x),
        "f"
    )

    if "." in s:

        s = (
            s
            .rstrip("0")
            .rstrip(".")
        )

    return s or "0"


def floor_step(
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


def ceil_step(
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
        rounding=ROUND_UP
    ) * step


# ============================================================
# OKX 签名
# ============================================================

def iso_timestamp():

    return (
        datetime.now(
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


def sign_okx(
    timestamp,
    method,
    request_path,
    body=""
):

    message = (
        timestamp
        +
        method.upper()
        +
        request_path
        +
        body
    )

    mac = hmac.new(
        SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256
    ).digest()

    return base64.b64encode(
        mac
    ).decode()


# ============================================================
# API 请求
# ============================================================

def okx_request(
    method,
    path,
    params=None,
    body=None,
    private=False
):

    method = method.upper()

    params = params or {}

    request_path = path

    if (
        method == "GET"
        and
        params
    ):

        from urllib.parse import urlencode

        request_path = (
            path
            +
            "?"
            +
            urlencode(params)
        )

    body_text = ""

    if body is not None:

        body_text = json.dumps(
            body,
            separators=(",", ":"),
            ensure_ascii=False
        )

    headers = {

        "Content-Type":
            "application/json",

        "Accept":
            "application/json",
    }

    if SIMULATED_TRADING == "1":

        headers[
            "x-simulated-trading"
        ] = "1"

    if private:

        ts = iso_timestamp()

        headers.update({

            "OK-ACCESS-KEY":
                API_KEY,

            "OK-ACCESS-SIGN":
                sign_okx(
                    ts,
                    method,
                    request_path,
                    body_text
                ),

            "OK-ACCESS-TIMESTAMP":
                ts,

            "OK-ACCESS-PASSPHRASE":
                PASSPHRASE,
        })

    url = BASE_URL + path

    last_error = None

    for attempt in range(3):

        try:

            if method == "GET":

                response = requests.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=REQUEST_TIMEOUT
                )

            else:

                response = requests.request(
                    method,
                    url,
                    data=body_text,
                    headers=headers,
                    timeout=REQUEST_TIMEOUT
                )

            if response.status_code >= 500:

                raise RuntimeError(
                    f"HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )

            data = response.json()

            if str(
                data.get("code")
            ) != "0":

                raise RuntimeError(
                    f"OKX API错误 "
                    f"code={data.get('code')} "
                    f"msg={data.get('msg')} "
                    f"data={data.get('data')}"
                )

            return data

        except Exception as e:

            last_error = e

            if attempt < 2:

                time.sleep(
                    0.7 *
                    (attempt + 1)
                )

            else:

                raise

    raise last_error


# ============================================================
# 合约信息
# ============================================================

_instrument_cache = {}


def get_instrument(
    inst_id
):

    if inst_id in _instrument_cache:

        return _instrument_cache[
            inst_id
        ]

    data = okx_request(
        "GET",
        "/api/v5/public/instruments",
        params={
            "instType":
                INST_TYPE,

            "instId":
                inst_id,
        }
    )["data"]

    if not data:

        raise RuntimeError(
            f"找不到合约：{inst_id}"
        )

    item = data[0]

    _instrument_cache[
        inst_id
    ] = item

    return item


def get_all_swap_instruments():

    data = okx_request(
        "GET",
        "/api/v5/public/instruments",
        params={
            "instType":
                INST_TYPE
        }
    )["data"]

    result = []

    for item in data:

        if item.get(
            "settleCcy"
        ) != SETTLE_CCY:

            continue

        if item.get(
            "state"
        ) != "live":

            continue

        if item.get(
            "ctType"
        ) not in (
            "linear",
            ""
        ):

            continue

        result.append(
            item
        )

    return result


# ============================================================
# 行情
# ============================================================

def get_tickers():

    data = okx_request(
        "GET",
        "/api/v5/market/tickers",
        params={
            "instType":
                INST_TYPE
        }
    )["data"]

    return {
        x["instId"]: x
        for x in data
    }


def get_ticker(
    inst_id
):

    data = okx_request(
        "GET",
        "/api/v5/market/ticker",
        params={
            "instId":
                inst_id
        }
    )["data"]

    if not data:

        raise RuntimeError(
            f"ticker为空：{inst_id}"
        )

    return data[0]


def get_mark_price(
    inst_id
):

    ticker = get_ticker(
        inst_id
    )

    try:

        mark = float(
            ticker.get(
                "markPx"
            )
            or
            0
        )

    except Exception:

        mark = 0

    if mark <= 0:

        mark = float(
            ticker.get(
                "last"
            )
            or
            0
        )

    if mark <= 0:

        raise RuntimeError(
            f"{inst_id} "
            "没有有效 Mark Price"
        )

    return mark


# ============================================================
# K线
# ============================================================

def get_candles(
    inst_id,
    bar=BAR,
    limit=120
):

    data = okx_request(
        "GET",
        "/api/v5/market/candles",
        params={

            "instId":
                inst_id,

            "bar":
                bar,

            "limit":
                str(
                    min(
                        limit,
                        300
                    )
                ),
        }
    )["data"]

    if not data:

        return pd.DataFrame()

    rows = []

    for x in reversed(data):

        if len(x) < 9:

            continue

        rows.append({

            "ts":
                int(x[0]),

            "open":
                float(x[1]),

            "high":
                float(x[2]),

            "low":
                float(x[3]),

            "close":
                float(x[4]),

            "vol":
                float(x[5]),

            "volCcy":
                float(x[6]),

            "volCcyQuote":
                float(x[7]),

            "confirm":
                str(x[8]),
        })

    return pd.DataFrame(
        rows
    )


def closed_df(df):

    if df.empty:

        return df

    if (
        str(
            df.iloc[-1].get(
                "confirm",
                "1"
            )
        )
        == "1"
    ):

        return df

    return df.iloc[:-1].copy()


# ============================================================
# 指标
# ============================================================

def ema(
    series,
    period
):

    return series.ewm(
        span=period,
        adjust=False
    ).mean()


def rsi(
    series,
    period=14
):

    delta = series.diff()

    gain = delta.clip(
        lower=0
    )

    loss = -delta.clip(
        upper=0
    )

    avg_gain = gain.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    rs = (
        avg_gain
        /
        avg_loss.replace(
            0,
            float("nan")
        )
    )

    result = (
        100
        -
        (
            100
            /
            (1 + rs)
        )
    )

    return result.fillna(50)


def macd(
    series,
    fast=12,
    slow=26,
    signal=9
):

    fast_ema = ema(
        series,
        fast
    )

    slow_ema = ema(
        series,
        slow
    )

    line = (
        fast_ema
        -
        slow_ema
    )

    sig = ema(
        line,
        signal
    )

    hist = (
        line
        -
        sig
    )

    return (
        line,
        sig,
        hist
    )


def atr(
    df,
    period=14
):

    prev_close = (
        df["close"]
        .shift(1)
    )

    tr1 = (
        df["high"]
        -
        df["low"]
    )

    tr2 = (
        df["high"]
        -
        prev_close
    ).abs()

    tr3 = (
        df["low"]
        -
        prev_close
    ).abs()

    tr = pd.concat(
        [
            tr1,
            tr2,
            tr3
        ],
        axis=1
    ).max(
        axis=1
    )

    return tr.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()


def prepare_indicators(
    df
):

    if len(df) < 60:

        return df

    df = df.copy()

    df["ema_fast"] = ema(
        df["close"],
        EMA_FAST
    )

    df["ema_slow"] = ema(
        df["close"],
        EMA_SLOW
    )

    df["rsi"] = rsi(
        df["close"],
        RSI_PERIOD
    )

    (
        df["macd"],
        df["macd_signal"],
        df["macd_hist"]
    ) = macd(
        df["close"],
        MACD_FAST,
        MACD_SLOW,
        MACD_SIGNAL
    )

    df["atr"] = atr(
        df,
        ATR_PERIOD
    )

    return df


def get_current_atr(
    inst_id
):

    df = get_candles(
        inst_id,
        BAR,
        100
    )

    df = closed_df(
        df
    )

    if len(df) < 30:

        return None

    df = prepare_indicators(
        df
    )

    value = float(
        df.iloc[-1]["atr"]
    )

    if value <= 0:

        return None

    return value


# ============================================================
# 多周期趋势
# ============================================================

def timeframe_trend(
    inst_id,
    bar
):

    df = get_candles(
        inst_id,
        bar,
        80
    )

    df = closed_df(
        df
    )

    if len(df) < 40:

        return 0

    df = prepare_indicators(
        df
    )

    x = df.iloc[-1]

    if (
        x["ema_fast"]
        >
        x["ema_slow"]
    ):

        return 1

    if (
        x["ema_fast"]
        <
        x["ema_slow"]
    ):

        return -1

    return 0


# ============================================================
# 信号
# ============================================================

def calculate_signal(
    inst_id,
    ticker
):

    df = get_candles(
        inst_id,
        BAR,
        120
    )

    df = closed_df(
        df
    )

    if len(df) < 60:

        return None

    df = prepare_indicators(
        df
    )

    x = df.iloc[-1]

    price = float(
        ticker["last"]
    )

    atr_value = float(
        x["atr"]
    )

    if price <= 0:

        return None

    if atr_value <= 0:

        return None

    atr_pct = (
        atr_value
        /
        price
    )

    if (
        atr_pct
        <
        MIN_ATR_PCT
    ):

        return None

    long_score = 0

    short_score = 0

    if (
        x["ema_fast"]
        >
        x["ema_slow"]
    ):

        long_score += 2

    elif (
        x["ema_fast"]
        <
        x["ema_slow"]
    ):

        short_score += 2

    if (
        price
        >
        x["ema_fast"]
    ):

        long_score += 1

    elif (
        price
        <
        x["ema_fast"]
    ):

        short_score += 1

    if (
        RSI_LONG_MIN
        <=
        x["rsi"]
        <=
        RSI_LONG_MAX
    ):

        long_score += 1

    if (
        RSI_SHORT_MIN
        <=
        x["rsi"]
        <=
        RSI_SHORT_MAX
    ):

        short_score += 1

    if (
        x["macd"]
        >
        x["macd_signal"]
        and
        x["macd"]
        >
        0
    ):

        long_score += 2

    elif (
        x["macd"]
        <
        x["macd_signal"]
        and
        x["macd"]
        <
        0
    ):

        short_score += 2

    trend15 = timeframe_trend(
        inst_id,
        "15m"
    )

    trend1h = timeframe_trend(
        inst_id,
        "1H"
    )

    if trend15 == 1:

        long_score += 1

    elif trend15 == -1:

        short_score += 1

    if trend1h == 1:

        long_score += 1

    elif trend1h == -1:

        short_score += 1

    if (
        long_score
        >=
        MIN_SIGNAL_SCORE
        and
        long_score
        >
        short_score
    ):

        direction = "LONG"

        score = long_score

    elif (
        short_score
        >=
        MIN_SIGNAL_SCORE
        and
        short_score
        >
        long_score
    ):

        direction = "SHORT"

        score = short_score

    else:

        return None

    if score <= 4:

        leverage = 5

    elif score == 5:

        leverage = 8

    elif score == 6:

        leverage = 10

    else:

        leverage = 15

    leverage = max(
        MIN_LEVERAGE,
        min(
            MAX_LEVERAGE,
            leverage
        )
    )

    return {

        "instId":
            inst_id,

        "direction":
            direction,

        "score":
            score,

        "leverage":
            leverage,

        "price":
            price,

        "atr":
            atr_value,

        "atr_pct":
            atr_pct,

        "rsi":
            float(x["rsi"]),

        "trend15":
            trend15,

        "trend1h":
            trend1h,
    }


# ============================================================
# 账户
# ============================================================

def get_equity():

    data = okx_request(
        "GET",
        "/api/v5/account/balance",
        private=True
    )["data"]

    if not data:

        raise RuntimeError(
            "账户余额为空"
        )

    details = data[0].get(
        "details",
        []
    )

    for item in details:

        if item.get(
            "ccy"
        ) == SETTLE_CCY:

            eq = (
                item.get("eq")
                or
                item.get("cashBal")
                or
                "0"
            )

            return float(eq)

    total = data[0].get(
        "totalEq"
    )

    if total:

        return float(total)

    raise RuntimeError(
        "找不到 USDT 权益"
    )


# ============================================================
# 持仓
# ============================================================

def get_positions():

    data = okx_request(
        "GET",
        "/api/v5/account/positions",
        params={
            "instType":
                INST_TYPE
        },
        private=True
    )["data"]

    result = []

    for item in data:

        try:

            pos = float(
                item.get(
                    "pos",
                    "0"
                )
                or
                0
            )

        except Exception:

            pos = 0

        if abs(pos) > 0:

            result.append(
                item
            )

    return result


def get_bot_position():

    positions = get_positions()

    if not positions:

        return None

    # 本机器人一次只允许一个持仓
    return positions[0]


def position_direction(
    pos
):

    pos_side = pos.get(
        "posSide",
        "net"
    )

    qty = float(
        pos.get(
            "pos",
            "0"
        )
        or
        0
    )

    if pos_side == "long":

        return "LONG"

    if pos_side == "short":

        return "SHORT"

    if qty > 0:

        return "LONG"

    return "SHORT"


def get_position_avg_px(
    pos
):

    for key in (
        "avgPx",
        "openAvgPx"
    ):

        try:

            value = float(
                pos.get(
                    key
                )
                or
                0
            )

            if value > 0:

                return value

        except Exception:

            pass

    return 0.0


# ============================================================
# 杠杆
# ============================================================

def set_leverage(
    inst_id,
    leverage
):

    body = {

        "instId":
            inst_id,

        "lever":
            str(leverage),

        "mgnMode":
            TD_MODE,

        "posSide":
            "net",
    }

    data = okx_request(
        "POST",
        "/api/v5/account/set-leverage",
        body=body,
        private=True
    )["data"]

    if (
        data
        and
        str(
            data[0].get(
                "sCode",
                "0"
            )
        )
        !=
        "0"
    ):

        raise RuntimeError(
            f"设置杠杆失败：{data[0]}"
        )


# ============================================================
# 新仓位 TP/SL
# ============================================================

def build_new_protection(
    candidate,
    avg_px
):

    inst_id = candidate[
        "instId"
    ]

    info = get_instrument(
        inst_id
    )

    tick = D(
        info["tickSz"]
    )

    atr_value = D(
        candidate["atr"]
    )

    avg = D(
        avg_px
    )

    distance = (
        atr_value
        *
        D(
            ATR_SL_MULTIPLIER
        )
    )

    direction = candidate[
        "direction"
    ]

    if direction == "LONG":

        sl = floor_step(
            avg - distance,
            tick
        )

        tp = ceil_step(
            avg
            +
            distance
            *
            D(RISK_REWARD),
            tick
        )

    else:

        sl = ceil_step(
            avg + distance,
            tick
        )

        tp = floor_step(
            avg
            -
            distance
            *
            D(RISK_REWARD),
            tick
        )

    return {

        "instId":
            inst_id,

        "direction":
            direction,

        "avgPx":
            avg_px,

        "tp":
            float(tp),

        "sl":
            float(sl),

        "tickSz":
            str(tick),

        "sz":
            None,
    }


# ============================================================
# V2.8：
# 为已有未保护仓位计算 TP/SL
# ============================================================

def build_adopt_protection(
    pos
):

    inst_id = pos[
        "instId"
    ]

    direction = position_direction(
        pos
    )

    avg_px = get_position_avg_px(
        pos
    )

    if avg_px <= 0:

        raise RuntimeError(
            f"{inst_id} "
            "无法取得持仓均价"
        )

    # --------------------------------------------------------
    # 当前 Mark
    # --------------------------------------------------------

    mark = get_mark_price(
        inst_id
    )

    # --------------------------------------------------------
    # 当前 ATR
    # --------------------------------------------------------

    atr_value = get_current_atr(
        inst_id
    )

    if (
        atr_value is None
        or
        atr_value <= 0
    ):

        atr_value = (
            mark
            *
            ADOPT_FALLBACK_ATR_PCT
        )

        log(
            f"{inst_id} "
            f"ATR获取失败，"
            f"使用备用 ATR="
            f"{atr_value}"
        )

    info = get_instrument(
        inst_id
    )

    tick = D(
        info["tickSz"]
    )

    distance = (
        D(str(atr_value))
        *
        D(
            ATR_SL_MULTIPLIER
        )
    )

    # ========================================================
    # 非常重要：
    #
    # 已有仓位采用“当前 Mark”作为重新计算基准，
    # 而不是使用历史开仓价。
    # ========================================================

    if direction == "LONG":

        sl = floor_step(
            D(str(mark))
            -
            distance,
            tick
        )

        tp = ceil_step(
            D(str(mark))
            +
            distance
            *
            D(RISK_REWARD),
            tick
        )

    else:

        sl = ceil_step(
            D(str(mark))
            +
            distance,
            tick
        )

        tp = floor_step(
            D(str(mark))
            -
            distance
            *
            D(RISK_REWARD),
            tick
        )

    # --------------------------------------------------------
    # 确保保护价在当前 Mark 外侧
    # --------------------------------------------------------

    if direction == "LONG":

        if sl >= D(str(mark)):

            sl = floor_step(
                D(str(mark))
                -
                tick * 2,
                tick
            )

        if tp <= D(str(mark)):

            tp = ceil_step(
                D(str(mark))
                +
                tick * 2,
                tick
            )

    else:

        if tp >= D(str(mark)):

            tp = floor_step(
                D(str(mark))
                -
                tick * 2,
                tick
            )

        if sl <= D(str(mark)):

            sl = ceil_step(
                D(str(mark))
                +
                tick * 2,
                tick
            )

    log(
        f"🛡️ 自动接管 {inst_id}："
        f"方向={direction} "
        f"持仓均价={avg_px} "
        f"Mark={mark} "
        f"ATR={atr_value} "
        f"TP={tp} "
        f"SL={sl}"
    )

    return {

        "instId":
            inst_id,

        "direction":
            direction,

        "avgPx":
            avg_px,

        "markPx":
            mark,

        "atr":
            atr_value,

        "tp":
            float(tp),

        "sl":
            float(sl),

        "tickSz":
            str(tick),

        "sz":
            abs(
                float(
                    pos.get(
                        "pos",
                        0
                    )
                )
            ),
    }


# ============================================================
# 检查 TP/SL 是否位于当前价格正确一侧
# ============================================================

def validate_protection(
    plan
):

    direction = plan[
        "direction"
    ]

    inst_id = plan[
        "instId"
    ]

    tick = D(
        plan["tickSz"]
    )

    tp = D(
        plan["tp"]
    )

    sl = D(
        plan["sl"]
    )

    mark = D(
        str(
            get_mark_price(
                inst_id
            )
        )
    )

    if direction == "LONG":

        if mark >= tp:

            return (
                "TP_REACHED",
                float(mark)
            )

        if mark <= sl:

            return (
                "SL_REACHED",
                float(mark)
            )

        if not (
            sl < mark
            and
            mark < tp
        ):

            return (
                "INVALID",
                float(mark)
            )

        # 至少隔两个 tick
        if (
            tp - mark
            <
            tick * 2
        ):

            tp = ceil_step(
                mark
                +
                tick * 2,
                tick
            )

            plan["tp"] = float(tp)

        if (
            mark - sl
            <
            tick * 2
        ):

            sl = floor_step(
                mark
                -
                tick * 2,
                tick
            )

            plan["sl"] = float(sl)

    else:

        if mark <= tp:

            return (
                "TP_REACHED",
                float(mark)
            )

        if mark >= sl:

            return (
                "SL_REACHED",
                float(mark)
            )

        if not (
            tp < mark
            and
            mark < sl
        ):

            return (
                "INVALID",
                float(mark)
            )

        if (
            mark - tp
            <
            tick * 2
        ):

            tp = floor_step(
                mark
                -
                tick * 2,
                tick
            )

            plan["tp"] = float(tp)

        if (
            sl - mark
            <
            tick * 2
        ):

            sl = ceil_step(
                mark
                +
                tick * 2,
                tick
            )

            plan["sl"] = float(sl)

    return (
        "OK",
        float(mark)
    )


# ============================================================
# 计算仓位
# ============================================================

def calculate_size(
    candidate,
    equity
):

    info = get_instrument(
        candidate["instId"]
    )

    lot = D(
        info["lotSz"]
    )

    min_sz = D(
        info["minSz"]
    )

    ct_val = D(
        info["ctVal"]
    )

    price = D(
        candidate["price"]
    )

    atr_value = D(
        candidate["atr"]
    )

    leverage = D(
        candidate["leverage"]
    )

    stop_distance = (
        atr_value
        *
        D(
            ATR_SL_MULTIPLIER
        )
    )

    if stop_distance <= 0:

        return 0.0

    risk_budget = (
        D(str(equity))
        *
        D(
            RISK_PER_TRADE_PCT
        )
    )

    fee = (
        price
        *
        ct_val
        *
        D(
            TAKER_FEE_RATE
        )
        *
        D(2)
        *
        D(
            FEE_BUFFER_MULTIPLIER
        )
    )

    denominator = (
        stop_distance
        *
        ct_val
        +
        fee
    )

    if denominator <= 0:

        return 0.0

    raw_sz = (
        risk_budget
        /
        denominator
    )

    max_margin = (
        D(str(equity))
        *
        D(
            MAX_MARGIN_PCT
        )
    )

    max_contracts = (
        max_margin
        *
        leverage
        /
        (
            price
            *
            ct_val
        )
    )

    sz = min(
        raw_sz,
        max_contracts
    )

    sz = floor_step(
        sz,
        lot
    )

    if sz < min_sz:

        return 0.0

    return float(sz)


# ============================================================
# 市价开仓
# ============================================================

def place_market_order(
    candidate,
    sz
):

    inst_id = candidate[
        "instId"
    ]

    direction = candidate[
        "direction"
    ]

    side = (
        "buy"
        if direction == "LONG"
        else
        "sell"
    )

    cl_id = (
        "MCV28"
        +
        str(
            int(
                time.time()
                *
                1000
            )
        )[-20:]
    )

    body = {

        "instId":
            inst_id,

        "tdMode":
            TD_MODE,

        "side":
            side,

        "ordType":
            "market",

        "sz":
            fmt_decimal(sz),

        "posSide":
            "net",

        "clOrdId":
            cl_id,
    }

    resp = okx_request(
        "POST",
        "/api/v5/trade/order",
        body=body,
        private=True
    )

    item = resp[
        "data"
    ][0]

    if str(
        item.get(
            "sCode",
            "0"
        )
    ) != "0":

        raise RuntimeError(
            f"开仓失败：{item}"
        )

    ord_id = item.get(
        "ordId"
    )

    if not ord_id:

        raise RuntimeError(
            f"没有返回 ordId：{resp}"
        )

    return ord_id


# ============================================================
# 查询订单
# ============================================================

def get_order(
    inst_id,
    order_id
):

    data = okx_request(
        "GET",
        "/api/v5/trade/order",
        params={
            "instId":
                inst_id,

            "ordId":
                order_id,
        },
        private=True
    )["data"]

    return (
        data[0]
        if data
        else
        None
    )


# ============================================================
# 等待成交
# ============================================================

def wait_order_filled(
    inst_id,
    ord_id
):

    end = (
        time.time()
        +
        ORDER_FILL_TIMEOUT
    )

    last = None

    while time.time() < end:

        try:

            row = get_order(
                inst_id,
                ord_id
            )

            if row:

                last = row

                order_state = str(
                    row.get(
                        "state",
                        ""
                    )
                )

                if order_state == "filled":

                    avg = float(
                        row.get(
                            "avgPx"
                        )
                        or
                        row.get(
                            "fillPx"
                        )
                        or
                        0
                    )

                    size = float(
                        row.get(
                            "accFillSz"
                        )
                        or
                        row.get(
                            "fillSz"
                        )
                        or
                        0
                    )

                    if (
                        avg > 0
                        and
                        size > 0
                    ):

                        return row

                if order_state in (
                    "canceled",
                    "mmp_canceled"
                ):

                    raise RuntimeError(
                        f"订单已取消：{row}"
                    )

        except Exception as e:

            log(
                f"查询成交异常：{e}"
            )

        time.sleep(
            0.7
        )

    pos = get_bot_position()

    if (
        pos
        and
        pos.get(
            "instId"
        )
        ==
        inst_id
    ):

        return {

            "avgPx":
                get_position_avg_px(
                    pos
                ),

            "accFillSz":
                abs(
                    float(
                        pos.get(
                            "pos"
                        )
                        or
                        0
                    )
                ),

            "state":
                "position_confirmed",
        }

    raise RuntimeError(
        f"订单超时：{ord_id} "
        f"last={last}"
    )


# ============================================================
# 创建 OCO
# ============================================================

def place_oco(
    plan
):

    inst_id = plan[
        "instId"
    ]

    direction = plan[
        "direction"
    ]

    exit_side = (
        "sell"
        if direction == "LONG"
        else
        "buy"
    )

    sz = float(
        plan["sz"]
    )

    cl_id = (
        "OCO28"
        +
        str(
            int(
                time.time()
                *
                1000
            )
        )[-20:]
    )

    body = {

        "instId":
            inst_id,

        "tdMode":
            TD_MODE,

        "side":
            exit_side,

        "posSide":
            "net",

        "ordType":
            "oco",

        "sz":
            fmt_decimal(sz),

        "tpTriggerPx":
            fmt_decimal(
                plan["tp"]
            ),

        "tpOrdPx":
            "-1",

        "tpTriggerPxType":
            "mark",

        "slTriggerPx":
            fmt_decimal(
                plan["sl"]
            ),

        "slOrdPx":
            "-1",

        "slTriggerPxType":
            "mark",

        "reduceOnly":
            True,

        "algoClOrdId":
            cl_id,
    }

    log(
        f"提交 OCO："
        f"{inst_id} "
        f"{direction} "
        f"sz={body['sz']} "
        f"TP={body['tpTriggerPx']} "
        f"SL={body['slTriggerPx']}"
    )

    resp = okx_request(
        "POST",
        "/api/v5/trade/order-algo",
        body=body,
        private=True
    )

    item = resp[
        "data"
    ][0]

    if str(
        item.get(
            "sCode",
            "0"
        )
    ) != "0":

        raise RuntimeError(
            f"OCO创建失败：{item}"
        )

    algo_id = item.get(
        "algoId"
    )

    if not algo_id:

        raise RuntimeError(
            f"OCO没有返回algoId：{resp}"
        )

    return (
        algo_id,
        cl_id
    )


# ============================================================
# 策略单
# ============================================================

def get_algo_detail(
    algo_id=None,
    algo_cl_ord_id=None
):

    params = {}

    if algo_id:

        params[
            "algoId"
        ] = str(algo_id)

    elif algo_cl_ord_id:

        params[
            "algoClOrdId"
        ] = str(
            algo_cl_ord_id
        )

    else:

        return None

    data = okx_request(
        "GET",
        "/api/v5/trade/order-algo",
        params=params,
        private=True
    )["data"]

    return (
        data[0]
        if data
        else
        None
    )


def get_pending_algos(
    inst_id,
    ord_type
):

    return okx_request(
        "GET",
        "/api/v5/trade/orders-algo-pending",
        params={

            "ordType":
                ord_type,

            "instType":
                INST_TYPE,

            "instId":
                inst_id,
        },
        private=True
    )["data"]


# ============================================================
# 找有效 OCO
# ============================================================

def find_valid_oco(
    inst_id,
    position_sz,
    direction
):

    try:

        algos = get_pending_algos(
            inst_id,
            "oco"
        )

        expected_side = (
            "sell"
            if direction == "LONG"
            else
            "buy"
        )

        for item in algos:

            if str(
                item.get(
                    "ordType",
                    ""
                )
            ) != "oco":

                continue

            if str(
                item.get(
                    "state",
                    ""
                )
            ) not in (
                "live",
                "effective",
                "partially_effective"
            ):

                continue

            if str(
                item.get(
                    "side",
                    ""
                )
            ) != expected_side:

                continue

            if str(
                item.get(
                    "posSide",
                    "net"
                )
            ) != "net":

                continue

            try:

                tp = float(
                    item.get(
                        "tpTriggerPx"
                    )
                    or
                    0
                )

                sl = float(
                    item.get(
                        "slTriggerPx"
                    )
                    or
                    0
                )

                if tp <= 0 or sl <= 0:

                    continue

            except Exception:

                continue

            try:

                algo_sz = float(
                    item.get(
                        "sz"
                    )
                    or
                    item.get(
                        "actualSz"
                    )
                    or
                    0
                )

                if (
                    position_sz > 0
                    and
                    algo_sz > 0
                    and
                    abs(
                        algo_sz
                        -
                        position_sz
                    )
                    >
                    max(
                        position_sz
                        *
                        0.001,
                        1e-12
                    )
                ):

                    continue

            except Exception:

                pass

            return item

    except Exception as e:

        log(
            f"查询 OCO 异常：{e}"
        )

    return None


# ============================================================
# 找旧 conditional
# ============================================================

def find_old_conditional(
    inst_id
):

    try:

        algos = get_pending_algos(
            inst_id,
            "conditional"
        )

        result = []

        for item in algos:

            state_now = str(
                item.get(
                    "state",
                    ""
                )
            )

            if state_now not in (
                "live",
                "effective",
                "partially_effective"
            ):

                continue

            result.append(
                item
            )

        return result

    except Exception as e:

        log(
            f"查询 conditional 异常：{e}"
        )

        return []


# ============================================================
# 取消策略单
# ============================================================

def cancel_algo(
    inst_id,
    algo_id
):

    if not algo_id:

        return False

    body = [

        {
            "instId":
                inst_id,

            "algoId":
                str(algo_id)
        }

    ]

    try:

        resp = okx_request(
            "POST",
            "/api/v5/trade/cancel-algos",
            body=body,
            private=True
        )

        item = resp.get(
            "data",
            [{}]
        )[0]

        if str(
            item.get(
                "sCode",
                "0"
            )
        ) != "0":

            log(
                f"取消策略单失败：{item}"
            )

            return False

        return True

    except Exception as e:

        log(
            f"取消策略单异常：{e}"
        )

        return False


# ============================================================
# 验证 OCO
# ============================================================

def verify_oco(
    plan,
    algo_id,
    cl_id
):

    end = (
        time.time()
        +
        PROTECTION_TIMEOUT
    )

    expected_side = (
        "sell"
        if plan["direction"]
        ==
        "LONG"
        else
        "buy"
    )

    while time.time() < end:

        try:

            row = get_algo_detail(
                algo_id=algo_id,
                algo_cl_ord_id=cl_id
            )

            if row:

                state_now = str(
                    row.get(
                        "state",
                        ""
                    )
                )

                ord_type = str(
                    row.get(
                        "ordType",
                        ""
                    )
                )

                side = str(
                    row.get(
                        "side",
                        ""
                    )
                )

                tp = float(
                    row.get(
                        "tpTriggerPx"
                    )
                    or
                    0
                )

                sl = float(
                    row.get(
                        "slTriggerPx"
                    )
                    or
                    0
                )

                if state_now in (
                    "order_failed",
                    "partially_failed",
                    "canceled"
                ):

                    return (
                        False,
                        row
                    )

                if (
                    ord_type == "oco"
                    and
                    side == expected_side
                    and
                    tp > 0
                    and
                    sl > 0
                    and
                    state_now in (
                        "live",
                        "effective",
                        "partially_effective"
                    )
                ):

                    tp_ok = (
                        abs(
                            tp
                            -
                            float(
                                plan["tp"]
                            )
                        )
                        <=
                        max(
                            abs(
                                float(
                                    plan["tp"]
                                )
                            )
                            *
                            1e-7,
                            1e-12
                        )
                    )

                    sl_ok = (
                        abs(
                            sl
                            -
                            float(
                                plan["sl"]
                            )
                        )
                        <=
                        max(
                            abs(
                                float(
                                    plan["sl"]
                                )
                            )
                            *
                            1e-7,
                            1e-12
                        )
                    )

                    if tp_ok and sl_ok:

                        return (
                            True,
                            row
                        )

        except Exception as e:

            log(
                f"OCO验证异常：{e}"
            )

        time.sleep(
            0.7
        )

    return (
        False,
        None
    )


# ============================================================
# 紧急平仓
# ============================================================

def emergency_close_position(
    pos
):

    if not pos:

        return True

    inst_id = pos[
        "instId"
    ]

    direction = position_direction(
        pos
    )

    qty = abs(
        float(
            pos.get(
                "pos",
                0
            )
            or
            0
        )
    )

    if qty <= 0:

        return True

    side = (
        "sell"
        if direction == "LONG"
        else
        "buy"
    )

    log(
        f"🚨 紧急平仓："
        f"{inst_id} "
        f"{direction} "
        f"qty={qty}"
    )

    body = {

        "instId":
            inst_id,

        "tdMode":
            TD_MODE,

        "side":
            side,

        "ordType":
            "market",

        "sz":
            fmt_decimal(qty),

        "posSide":
            "net",

        "reduceOnly":
            True,

        "clOrdId":
            "EMC28"
            +
            str(
                int(
                    time.time()
                    *
                    1000
                )
            )[-20:],
    }

    resp = okx_request(
        "POST",
        "/api/v5/trade/order",
        body=body,
        private=True
    )

    item = resp[
        "data"
    ][0]

    if str(
        item.get(
            "sCode",
            "0"
        )
    ) != "0":

        raise RuntimeError(
            f"紧急平仓失败：{item}"
        )

    return True


# ============================================================
# V2.8 核心：
# 自动接管没有保护的持仓
# ============================================================

def adopt_existing_position(
    pos
):

    inst_id = pos[
        "instId"
    ]

    direction = position_direction(
        pos
    )

    qty = abs(
        float(
            pos.get(
                "pos",
                0
            )
            or
            0
        )
    )

    if qty <= 0:

        return False

    log(
        "=" * 60
    )

    log(
        f"🛡️ 自动接管已有持仓："
        f"{inst_id} "
        f"{direction} "
        f"qty={qty}"
    )

    # ========================================================
    # 第一层：检查 OCO
    # ========================================================

    existing_oco = find_valid_oco(
        inst_id,
        qty,
        direction
    )

    if existing_oco:

        log(
            f"✅ 已存在有效 OCO："
            f"algoId="
            f"{existing_oco.get('algoId')} "
            f"TP="
            f"{existing_oco.get('tpTriggerPx')} "
            f"SL="
            f"{existing_oco.get('slTriggerPx')}"
        )

        state[
            "active_trade"
        ] = {

            "instId":
                inst_id,

            "direction":
                direction,

            "sz":
                qty,

            "avgPx":
                get_position_avg_px(
                    pos
                ),

            "tp":
                float(
                    existing_oco.get(
                        "tpTriggerPx"
                    )
                ),

            "sl":
                float(
                    existing_oco.get(
                        "slTriggerPx"
                    )
                ),

            "algoId":
                existing_oco.get(
                    "algoId"
                ),

            "algoClOrdId":
                existing_oco.get(
                    "algoClOrdId"
                ),

            "openedAt":
                int(
                    time.time()
                ),
        }

        save_state(
            state
        )

        return True

    # ========================================================
    # 第二层：检查旧 conditional
    # ========================================================

    old_algos = find_old_conditional(
        inst_id
    )

    if old_algos:

        log(
            f"发现 {len(old_algos)} 个旧 conditional"
        )

        if REPAIR_UNKNOWN_OLD_SL:

            for old in old_algos:

                old_id = old.get(
                    "algoId"
                )

                if old_id:

                    log(
                        f"取消旧 conditional："
                        f"{old_id}"
                    )

                    cancel_algo(
                        inst_id,
                        old_id
                    )

    # ========================================================
    # 第三层：自动计算当前保护
    # ========================================================

    if not AUTO_ADOPT_UNPROTECTED_POSITION:

        log(
            "自动接管未保护仓位功能关闭"
        )

        return False

    try:

        plan = build_adopt_protection(
            pos
        )

        # ----------------------------------------------------
        # 重新读取 Mark
        # 防止计算和提交之间价格变化
        # ----------------------------------------------------

        status, mark = (
            validate_protection(
                plan
            )
        )

        if status in (
            "TP_REACHED",
            "SL_REACHED"
        ):

            log(
                f"⚠️ 接管时保护价格"
                f"已经被当前价格穿越："
                f"{status} "
                f"mark={mark}"
            )

            emergency_close_position(
                pos
            )

            return True

        if status != "OK":

            raise RuntimeError(
                f"接管保护价格非法："
                f"status={status} "
                f"mark={mark}"
            )

        # ----------------------------------------------------
        # 再次读取仓位
        # ----------------------------------------------------

        latest_pos = get_bot_position()

        if not latest_pos:

            log(
                "接管过程中仓位已经消失"
            )

            return True

        if (
            latest_pos.get(
                "instId"
            )
            !=
            inst_id
        ):

            return False

        plan["sz"] = abs(
            float(
                latest_pos.get(
                    "pos",
                    0
                )
            )
        )

        if plan["sz"] <= 0:

            return True

        # ----------------------------------------------------
        # 创建 OCO
        # ----------------------------------------------------

        algo_id, cl_id = place_oco(
            plan
        )

        log(
            f"OCO已创建："
            f"algoId={algo_id}"
        )

        # ----------------------------------------------------
        # 严格验证
        # ----------------------------------------------------

        ok, row = verify_oco(
            plan,
            algo_id,
            cl_id
        )

        if not ok:

            raise RuntimeError(
                f"自动接管 OCO 验证失败："
                f"{row}"
            )

        # ----------------------------------------------------
        # 保存
        # ----------------------------------------------------

        state[
            "active_trade"
        ] = {

            "instId":
                inst_id,

            "direction":
                direction,

            "sz":
                plan["sz"],

            "avgPx":
                plan["avgPx"],

            "tp":
                plan["tp"],

            "sl":
                plan["sl"],

            "algoId":
                algo_id,

            "algoClOrdId":
                cl_id,

            "openedAt":
                int(
                    time.time()
                ),

            "adopted":
                True,
        }

        save_state(
            state
        )

        log(
            "=" * 60
        )

        log(
            f"✅ 已成功接管 {inst_id}"
        )

        log(
            f"方向：{direction}"
        )

        log(
            f"持仓：{plan['sz']}"
        )

        log(
            f"原始均价：{plan['avgPx']}"
        )

        log(
            f"当前 Mark：{plan['markPx']}"
        )

        log(
            f"ATR：{plan['atr']}"
        )

        log(
            f"TP：{plan['tp']}"
        )

        log(
            f"SL：{plan['sl']}"
        )

        log(
            f"OCO：{algo_id}"
        )

        log(
            "=" * 60
        )

        return True

    except Exception as e:

        log(
            f"❌ 自动接管 {inst_id} 失败：{e}"
        )

        traceback.print_exc()

        # ====================================================
        # 保护失败
        # ====================================================

        if EMERGENCY_CLOSE_IF_OCO_FAIL:

            try:

                latest_pos = get_bot_position()

                if (
                    latest_pos
                    and
                    latest_pos.get(
                        "instId"
                    )
                    ==
                    inst_id
                ):

                    emergency_close_position(
                        latest_pos
                    )

                    state[
                        "active_trade"
                    ] = None

                    save_state(
                        state
                    )

                    log(
                        "✅ 因无法建立保护，"
                        "已经紧急平仓"
                    )

            except Exception as close_e:

                log(
                    "🚨 自动接管失败，"
                    f"且紧急平仓失败：{close_e}"
                )

        return False


# ============================================================
# 修复已有持仓
# ============================================================

def repair_existing_position(
    pos
):

    inst_id = pos[
        "instId"
    ]

    qty = abs(
        float(
            pos.get(
                "pos",
                0
            )
            or
            0
        )
    )

    direction = position_direction(
        pos
    )

    # --------------------------------------------------------
    # 先检查 OCO
    # --------------------------------------------------------

    existing = find_valid_oco(
        inst_id,
        qty,
        direction
    )

    if existing:

        log(
            f"✅ {inst_id} "
            f"OCO正常，继续监控："
            f"TP={existing.get('tpTriggerPx')} "
            f"SL={existing.get('slTriggerPx')}"
        )

        # 如果状态里没有，就补记录
        if not state.get(
            "active_trade"
        ):

            state[
                "active_trade"
            ] = {

                "instId":
                    inst_id,

                "direction":
                    direction,

                "sz":
                    qty,

                "avgPx":
                    get_position_avg_px(
                        pos
                    ),

                "tp":
                    float(
                        existing.get(
                            "tpTriggerPx"
                        )
                    ),

                "sl":
                    float(
                        existing.get(
                            "slTriggerPx"
                        )
                    ),

                "algoId":
                    existing.get(
                        "algoId"
                    ),

                "algoClOrdId":
                    existing.get(
                        "algoClOrdId"
                    ),

                "openedAt":
                    int(
                        time.time()
                    ),
            }

            save_state(
                state
            )

        return True

    # --------------------------------------------------------
    # 没有 OCO
    # --------------------------------------------------------

    log(
        f"⚠️ {inst_id} "
        f"没有发现有效 OCO，"
        f"进入自动接管流程"
    )

    return adopt_existing_position(
        pos
    )


# ============================================================
# 每日限制
# ============================================================

def reset_daily_if_needed(
    equity
):

    today = datetime.now().strftime(
        "%Y-%m-%d"
    )

    if state.get(
        "date"
    ) != today:

        state[
            "date"
        ] = today

        state[
            "start_equity"
        ] = float(equity)

        state[
            "trades"
        ] = 0

        state[
            "consecutive_losses"
        ] = 0

        state[
            "cooldown_until"
        ] = 0

        state[
            "last_trade_inst"
        ] = ""

        save_state(
            state
        )

    if not state.get(
        "start_equity"
    ):

        state[
            "start_equity"
        ] = float(equity)

        save_state(
            state
        )


def daily_allowed(
    equity
):

    reset_daily_if_needed(
        equity
    )

    if (
        state["trades"]
        >=
        MAX_DAILY_TRADES
    ):

        log(
            "达到每日交易次数限制"
        )

        return False

    if (
        time.time()
        <
        float(
            state.get(
                "cooldown_until",
                0
            )
            or
            0
        )
    ):

        return False

    start = float(
        state[
            "start_equity"
        ]
    )

    if start > 0:

        change = (
            equity
            -
            start
        ) / start

        if (
            change
            <=
            -MAX_DAILY_LOSS_PCT
        ):

            log(
                f"达到每日亏损限制："
                f"{change:.2%}"
            )

            return False

        if (
            change
            >=
            MAX_DAILY_PROFIT_PCT
        ):

            log(
                f"达到每日盈利限制："
                f"{change:.2%}"
            )

            return False

    return True


# ============================================================
# 检查交易是否结束
# ============================================================

def check_position_closed():

    active = state.get(
        "active_trade"
    )

    if not active:

        return

    inst_id = active.get(
        "instId"
    )

    if not inst_id:

        return

    try:

        positions = get_positions()

    except Exception:

        return

    for pos in positions:

        if (
            pos.get(
                "instId"
            )
            ==
            inst_id
        ):

            return

    # 仓位消失
    log(
        f"检测到 {inst_id} "
        "仓位已经结束"
    )

    state[
        "active_trade"
    ] = None

    save_state(
        state
    )


# ============================================================
# 全市场扫描
# ============================================================

def scan_candidates():

    instruments = (
        get_all_swap_instruments()
    )

    tickers = get_tickers()

    result = []

    for info in instruments:

        inst_id = info[
            "instId"
        ]

        ticker = tickers.get(
            inst_id
        )

        if not ticker:

            continue

        try:

            volume = float(
                ticker.get(
                    "volCcy24h"
                )
                or
                0
            )

            if (
                volume
                <
                MIN_24H_VOL_USDT
            ):

                continue

            candidate = calculate_signal(
                inst_id,
                ticker
            )

            if not candidate:

                continue

            volume_score = min(
                volume
                /
                50_000_000,
                3
            )

            candidate[
                "rank_score"
            ] = (
                candidate[
                    "score"
                ]
                +
                volume_score
                +
                candidate[
                    "atr_pct"
                ]
                *
                100
            )

            result.append(
                candidate
            )

        except Exception:

            continue

    result.sort(
        key=lambda x:
            x["rank_score"],
        reverse=True
    )

    return result[
        :SCAN_LIMIT
    ]


# ============================================================
# 执行新交易
# ============================================================

def execute_candidate(
    candidate,
    equity
):

    inst_id = candidate[
        "instId"
    ]

    if (
        state.get(
            "last_trade_inst"
        )
        ==
        inst_id
    ):

        return False

    set_leverage(
        inst_id,
        candidate[
            "leverage"
        ]
    )

    sz = calculate_size(
        candidate,
        equity
    )

    if sz <= 0:

        log(
            f"{inst_id} "
            "下单数量低于最小数量"
        )

        return False

    info = get_instrument(
        inst_id
    )

    if sz < float(
        info["minSz"]
    ):

        return False

    # --------------------------------------------------------
    # 开仓
    # --------------------------------------------------------

    ord_id = place_market_order(
        candidate,
        sz
    )

    log(
        f"开仓订单：{ord_id}"
    )

    fill = wait_order_filled(
        inst_id,
        ord_id
    )

    avg_px = float(
        fill.get(
            "avgPx"
        )
        or
        0
    )

    actual_sz = float(
        fill.get(
            "accFillSz"
        )
        or
        0
    )

    if (
        avg_px <= 0
        or
        actual_sz <= 0
    ):

        pos = get_bot_position()

        if not pos:

            raise RuntimeError(
                "开仓后没有持仓"
            )

        avg_px = get_position_avg_px(
            pos
        )

        actual_sz = abs(
            float(
                pos.get(
                    "pos"
                )
                or
                0
            )
        )

    log(
        f"真实成交："
        f"avgPx={avg_px} "
        f"sz={actual_sz}"
    )

    # --------------------------------------------------------
    # 计算保护
    # --------------------------------------------------------

    plan = build_new_protection(
        candidate,
        avg_px
    )

    plan["sz"] = actual_sz

    status, mark = validate_protection(
        plan
    )

    if status != "OK":

        log(
            f"开仓后保护价格异常："
            f"{status} "
            f"mark={mark}"
        )

        pos = get_bot_position()

        if pos:

            emergency_close_position(
                pos
            )

        return False

    # --------------------------------------------------------
    # OCO
    # --------------------------------------------------------

    try:

        algo_id, cl_id = place_oco(
            plan
        )

        ok, row = verify_oco(
            plan,
            algo_id,
            cl_id
        )

        if not ok:

            raise RuntimeError(
                f"OCO验证失败：{row}"
            )

        state[
            "active_trade"
        ] = {

            "instId":
                inst_id,

            "direction":
                candidate[
                    "direction"
                ],

            "sz":
                actual_sz,

            "avgPx":
                avg_px,

            "tp":
                plan["tp"],

            "sl":
                plan["sl"],

            "algoId":
                algo_id,

            "algoClOrdId":
                cl_id,

            "openedAt":
                int(
                    time.time()
                ),

            "adopted":
                False,
        }

        state[
            "trades"
        ] += 1

        state[
            "last_trade_inst"
        ] = inst_id

        save_state(
            state
        )

        log(
            f"🎯 新交易建立成功："
            f"{inst_id} "
            f"{candidate['direction']} "
            f"TP={plan['tp']} "
            f"SL={plan['sl']}"
        )

        return True

    except Exception as e:

        log(
            f"❌ OCO失败：{e}"
        )

        if EMERGENCY_CLOSE_IF_OCO_FAIL:

            pos = get_bot_position()

            if pos:

                try:

                    emergency_close_position(
                        pos
                    )

                except Exception as close_e:

                    log(
                        f"🚨 紧急平仓失败："
                        f"{close_e}"
                    )

        raise


# ============================================================
# 主循环
# ============================================================

def main():

    log(
        "=" * 70
    )

    log(
        "OKX 全市场永续强行情"
        "自动交易机器人 V2.8 启动"
    )

    log(
        "V2.8："
        "自动接管未保护持仓"
    )

    log(
        f"模拟盘={SIMULATED_TRADING} "
        f"| BAR={BAR} "
        f"| TD_MODE={TD_MODE}"
    )

    log(
        "TP/SL："
        "OCO + Mark Price + ATR"
    )

    log(
        "=" * 70
    )

    # ========================================================
    # 启动账户
    # ========================================================

    equity = get_equity()

    reset_daily_if_needed(
        equity
    )

    log(
        f"当前 USDT 权益："
        f"{equity:.4f}"
    )

    # ========================================================
    # 启动立即接管已有仓位
    # ========================================================

    try:

        pos = get_bot_position()

        if pos:

            log(
                "🚨 启动发现已有持仓："
                f"{pos.get('instId')} "
                f"{position_direction(pos)} "
                f"qty={pos.get('pos')}"
            )

            repair_existing_position(
                pos
            )

        else:

            if state.get(
                "active_trade"
            ):

                state[
                    "active_trade"
                ] = None

                save_state(
                    state
                )

    except Exception as e:

        log(
            f"启动持仓检查异常：{e}"
        )

        traceback.print_exc()

    # ========================================================
    # 主循环
    # ========================================================

    while True:

        try:

            equity = get_equity()

            reset_daily_if_needed(
                equity
            )

            check_position_closed()

            # ------------------------------------------------
            # 任何时候发现持仓
            # 第一优先级都是保护持仓
            # ------------------------------------------------

            pos = get_bot_position()

            if pos:

                repair_existing_position(
                    pos
                )

                log(
                    f"当前持仓："
                    f"{pos.get('instId')} "
                    f"{position_direction(pos)} "
                    f"qty={pos.get('pos')} "
                    f"→ 等待 TP/SL"
                )

                time.sleep(
                    LOOP_SECONDS
                )

                continue

            # ------------------------------------------------
            # 没有持仓
            # ------------------------------------------------

            if state.get(
                "active_trade"
            ):

                state[
                    "active_trade"
                ] = None

                save_state(
                    state
                )

            # ------------------------------------------------
            # 每日限制
            # ------------------------------------------------

            if not daily_allowed(
                equity
            ):

                time.sleep(
                    LOOP_SECONDS
                )

                continue

            # ------------------------------------------------
            # 扫描
            # ------------------------------------------------

            candidates = scan_candidates()

            if not candidates:

                log(
                    "本轮没有满足条件的强信号"
                )

                time.sleep(
                    LOOP_SECONDS
                )

                continue

            preview = []

            for c in candidates[:5]:

                preview.append(
                    f"{c['instId']} "
                    f"{c['direction']} "
                    f"S{c['score']}"
                )

            log(
                "候选："
                +
                " | ".join(
                    preview
                )
            )

            traded = False

            for candidate in candidates:

                if get_bot_position():

                    break

                try:

                    if execute_candidate(
                        candidate,
                        equity
                    ):

                        traded = True

                        break

                except Exception as e:

                    log(
                        f"{candidate['instId']} "
                        f"执行失败：{e}"
                    )

                    traceback.print_exc()

                    if get_bot_position():

                        break

            if not traded:

                log(
                    "本轮没有成功建立交易"
                )

        except KeyboardInterrupt:

            log(
                "机器人停止"
            )

            break

        except Exception as e:

            log(
                f"主循环异常：{e}"
            )

            traceback.print_exc()

        time.sleep(
            LOOP_SECONDS
        )


# ============================================================
# 启动
# ============================================================

if __name__ == "__main__":

    main()
