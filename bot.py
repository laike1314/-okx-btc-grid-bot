# -*- coding: utf-8 -*-
"""
OKX 全市场永续强行情自动交易机器人 V2.7
------------------------------------------------
核心修复：

1. 市价开仓后查询真实成交均价。
2. TP/SL 使用独立 OCO。
3. OCO 提交前重新读取最新 Mark Price。
4. 检查 TP/SL 是否已经被当前 Mark Price 穿越。
5. TP/SL 已经达到时，不再提交无效 OCO，直接市价平仓。
6. OCO 创建失败 -> 紧急平仓。
7. OCO 创建成功但验证失败 -> 紧急平仓。
8. OCO 验证同时检查：
   - ordType
   - state
   - side
   - posSide
   - 数量
   - TP
   - SL
9. 开仓成功后立即保存 active_trade。
10. 程序重启可以恢复本机器人自己的保护单。
11. 支持旧版 conditional SL 自动转换为 OCO。
12. TP/SL 使用 tickSz 精确处理。
13. 已修复 order 查询必须带 instId 的问题。
14. 交易结束后尝试统计本次成交 PnL。
15. 连续亏损限制、每日盈亏限制、每日交易次数限制保留。
16. 默认 OKX 模拟盘。

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
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from datetime import datetime, timezone

import requests
import pandas as pd


# ============================================================
# 基础配置
# ============================================================

BASE_URL = "https://www.okx.com"

# 1 = 模拟盘
# 0 = 实盘
SIMULATED_TRADING = "1"

INST_TYPE = "SWAP"
SETTLE_CCY = "USDT"
TD_MODE = "cross"

# ============================================================
# K线
# ============================================================

BAR = "5m"
LOOP_SECONDS = 20

CONFIRM_BAR_15M = "15m"
CONFIRM_BAR_1H = "1H"


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

# 单笔风险 = 权益 0.5%
RISK_PER_TRADE_PCT = 0.005

# 单笔最大保证金 = 权益 8%
MAX_MARGIN_PCT = 0.08

# 动态杠杆范围
MIN_LEVERAGE = 5
MAX_LEVERAGE = 15

# ATR 止损
ATR_SL_MULTIPLIER = 1.5

# 止盈风险比
RISK_REWARD = 1.8


# ============================================================
# 手续费
# ============================================================

# 吃单手续费估算
TAKER_FEE_RATE = 0.0005

# 手续费风险缓冲
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
# API 请求
# ============================================================

REQUEST_TIMEOUT = 12

ORDER_FILL_TIMEOUT = 15

PROTECTION_TIMEOUT = 12


# ============================================================
# 安全
# ============================================================

# OCO 失败以后是否立即平仓
EMERGENCY_CLOSE_IF_OCO_FAIL = True

# 是否尝试修复旧版 conditional SL
REPAIR_UNKNOWN_OLD_SL = True

# 如果账户中存在不知道来源的人工仓位
# 默认不猜 TP/SL
ALLOW_NEW_TRADE_WHEN_UNKNOWN_POSITION = False


# ============================================================
# 状态文件
# ============================================================

STATE_FILE = "bot_state_v27.json"


# ============================================================
# API 密钥
# ============================================================

API_KEY = os.getenv("OKX_API_KEY", "")
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")


if not API_KEY or not SECRET_KEY or not PASSPHRASE:
    raise RuntimeError(
        "缺少 OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE 环境变量。"
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

def load_state():

    default = {
        "date": datetime.now().strftime("%Y-%m-%d"),

        "start_equity": 0.0,

        "trades": 0,

        "consecutive_losses": 0,

        "cooldown_until": 0,

        "last_trade_inst": "",

        "active_trade": None,
    }

    try:

        if not os.path.exists(STATE_FILE):
            return default

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            state = json.load(f)

        for k, v in default.items():

            state.setdefault(k, v)

        return state

    except Exception as e:

        log(f"读取状态文件失败，使用新状态：{e}")

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

    os.replace(tmp, STATE_FILE)


state = load_state()


# ============================================================
# OKX 签名
# ============================================================

def iso_timestamp():

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def sign_okx(
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

    mac = hmac.new(
        SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256
    ).digest()

    return base64.b64encode(mac).decode()


# ============================================================
# OKX API
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

    # GET 参数必须参与签名
    if method == "GET" and params:

        from urllib.parse import urlencode

        request_path = (
            path
            + "?"
            + urlencode(params)
        )

    else:

        request_path = path

    body_text = ""

    if body is not None:

        body_text = json.dumps(
            body,
            separators=(",", ":"),
            ensure_ascii=False
        )

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    # 模拟盘
    if SIMULATED_TRADING == "1":

        headers["x-simulated-trading"] = "1"

    # 私有 API
    if private:

        ts = iso_timestamp()

        headers.update({

            "OK-ACCESS-KEY": API_KEY,

            "OK-ACCESS-SIGN": sign_okx(
                ts,
                method,
                request_path,
                body_text
            ),

            "OK-ACCESS-TIMESTAMP": ts,

            "OK-ACCESS-PASSPHRASE": PASSPHRASE,

        })

    url = BASE_URL + path

    last_error = None

    for attempt in range(3):

        try:

            if method == "GET":

                r = requests.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                )

            else:

                r = requests.request(
                    method,
                    url,
                    data=body_text,
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                )

            if r.status_code >= 500:

                raise RuntimeError(
                    f"HTTP {r.status_code}: "
                    f"{r.text[:500]}"
                )

            data = r.json()

            if str(data.get("code")) != "0":

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
                    0.7 * (attempt + 1)
                )

            else:

                raise

    raise last_error


# ============================================================
# Decimal 工具
# ============================================================

def D(x):

    return Decimal(str(x))


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


def round_price(
    value,
    tick,
    direction=None
):

    if direction == "UP":

        return ceil_step(
            value,
            tick
        )

    return floor_step(
        value,
        tick
    )


# ============================================================
# 合约信息
# ============================================================

_instrument_cache = {}


def get_instrument(inst_id):

    if inst_id in _instrument_cache:

        return _instrument_cache[inst_id]

    data = okx_request(
        "GET",
        "/api/v5/public/instruments",
        params={
            "instType": INST_TYPE,
            "instId": inst_id,
        },
        private=False,
    )["data"]

    if not data:

        raise RuntimeError(
            f"找不到合约信息：{inst_id}"
        )

    item = data[0]

    _instrument_cache[inst_id] = item

    return item


def get_all_swap_instruments():

    data = okx_request(
        "GET",
        "/api/v5/public/instruments",
        params={
            "instType": INST_TYPE
        },
        private=False,
    )["data"]

    result = []

    for x in data:

        if x.get("settleCcy") != SETTLE_CCY:

            continue

        if x.get("state") != "live":

            continue

        if x.get("ctType") not in (
            "linear",
            ""
        ):

            continue

        result.append(x)

    return result


# ============================================================
# 行情
# ============================================================

def get_tickers():

    data = okx_request(
        "GET",
        "/api/v5/market/tickers",
        params={
            "instType": INST_TYPE
        },
        private=False,
    )["data"]

    return {
        x["instId"]: x
        for x in data
    }


def get_ticker(inst_id):

    data = okx_request(
        "GET",
        "/api/v5/market/ticker",
        params={
            "instId": inst_id
        },
        private=False,
    )["data"]

    if not data:

        raise RuntimeError(
            f"ticker为空：{inst_id}"
        )

    return data[0]


def get_mark_price(inst_id):

    ticker = get_ticker(inst_id)

    try:

        mark = float(
            ticker.get("markPx") or 0
        )

    except Exception:

        mark = 0

    if mark <= 0:

        mark = float(
            ticker.get("last") or 0
        )

    if mark <= 0:

        raise RuntimeError(
            f"{inst_id} 无有效 Mark Price"
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
            "instId": inst_id,
            "bar": bar,
            "limit": str(
                min(limit, 300)
            ),
        },
        private=False,
    )["data"]

    if not data:

        return pd.DataFrame()

    rows = []

    for x in reversed(data):

        if len(x) < 9:

            continue

        rows.append({

            "ts": int(x[0]),

            "open": float(x[1]),

            "high": float(x[2]),

            "low": float(x[3]),

            "close": float(x[4]),

            "vol": float(x[5]),

            "volCcy": float(x[6]),

            "volCcyQuote": float(x[7]),

            "confirm": str(x[8]),

        })

    return pd.DataFrame(rows)


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
    ).max(axis=1)

    return tr.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()


def prepare_indicators(df):

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


# ============================================================
# 只使用已经确认的 K 线
# ============================================================

def closed_df(df):

    if df.empty:

        return df

    if (
        str(
            df.iloc[-1]
            .get(
                "confirm",
                "1"
            )
        )
        == "1"
    ):

        return df

    return df.iloc[:-1].copy()


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

    df = closed_df(df)

    if len(df) < 40:

        return 0

    df = prepare_indicators(df)

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

    df = closed_df(df)

    if len(df) < 60:

        return None

    df = prepare_indicators(df)

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

    if atr_pct < MIN_ATR_PCT:

        return None

    long_score = 0

    short_score = 0

    # --------------------------------------------------------
    # EMA
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # 价格相对 EMA
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # MACD
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # 15m / 1H
    # --------------------------------------------------------

    trend15 = timeframe_trend(
        inst_id,
        CONFIRM_BAR_15M
    )

    trend1h = timeframe_trend(
        inst_id,
        CONFIRM_BAR_1H
    )

    if trend15 == 1:

        long_score += 1

    elif trend15 == -1:

        short_score += 1

    if trend1h == 1:

        long_score += 1

    elif trend1h == -1:

        short_score += 1

    # --------------------------------------------------------
    # 最终方向
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # 动态杠杆
    # --------------------------------------------------------

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

        "instId": inst_id,

        "direction": direction,

        "score": score,

        "leverage": leverage,

        "price": price,

        "atr": atr_value,

        "atr_pct": atr_pct,

        "rsi": float(
            x["rsi"]
        ),

        "trend15": trend15,

        "trend1h": trend1h,
    }


# ============================================================
# 账户
# ============================================================

def get_equity():

    data = okx_request(
        "GET",
        "/api/v5/account/balance",
        private=True,
    )["data"]

    if not data:

        raise RuntimeError(
            "账户余额返回为空"
        )

    details = data[0].get(
        "details",
        []
    )

    for x in details:

        if (
            x.get("ccy")
            ==
            SETTLE_CCY
        ):

            eq = (
                x.get("eq")
                or
                x.get("cashBal")
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
        "没有找到 USDT 权益"
    )


# ============================================================
# 持仓
# ============================================================

def get_positions():

    data = okx_request(
        "GET",
        "/api/v5/account/positions",
        params={
            "instType": INST_TYPE
        },
        private=True,
    )["data"]

    result = []

    for p in data:

        try:

            pos = float(
                p.get(
                    "pos",
                    "0"
                )
                or
                0
            )

        except Exception:

            pos = 0

        if abs(pos) <= 0:

            continue

        result.append(p)

    return result


def get_bot_position():

    positions = get_positions()

    if not positions:

        return None

    # 当前机器人设计：
    # 账户只允许一个仓位
    return positions[0]


def position_direction(pos):

    pos_side = pos.get(
        "posSide",
        "net"
    )

    pos_qty = float(
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

    if pos_qty > 0:

        return "LONG"

    return "SHORT"


def get_position_avg_px(pos):

    for k in (
        "avgPx",
        "openAvgPx"
    ):

        try:

            px = float(
                pos.get(k)
                or
                0
            )

            if px > 0:

                return px

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

        "instId": inst_id,

        "lever": str(
            leverage
        ),

        "mgnMode": TD_MODE,

        "posSide": "net",
    }

    data = okx_request(
        "POST",
        "/api/v5/account/set-leverage",
        body=body,
        private=True,
    )["data"]

    if (
        data
        and
        str(
            data[0]
            .get(
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

    return True


# ============================================================
# TP / SL 计算
# ============================================================

def build_protection_plan(
    candidate,
    avg_px=None,
    sl_override=None
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

    avg_px = float(
        avg_px
        or
        candidate["price"]
    )

    atr_value = float(
        candidate["atr"]
    )

    if avg_px <= 0:

        raise RuntimeError(
            f"{inst_id} 成交价无效"
        )

    if atr_value <= 0:

        raise RuntimeError(
            f"{inst_id} ATR无效"
        )

    direction = candidate[
        "direction"
    ]

    # --------------------------------------------------------
    # 使用旧 SL
    # --------------------------------------------------------

    if (
        sl_override is not None
        and
        float(sl_override) > 0
    ):

        sl = float(
            sl_override
        )

        risk_distance = abs(
            avg_px
            -
            sl
        )

    else:

        risk_distance = (
            atr_value
            *
            ATR_SL_MULTIPLIER
        )

        if direction == "LONG":

            sl = (
                avg_px
                -
                risk_distance
            )

        else:

            sl = (
                avg_px
                +
                risk_distance
            )

    if risk_distance <= 0:

        raise RuntimeError(
            f"{inst_id} TP/SL距离无效"
        )

    # --------------------------------------------------------
    # LONG
    # --------------------------------------------------------

    if direction == "LONG":

        sl = float(
            round_price(
                sl,
                tick,
                "DOWN"
            )
        )

        tp = float(
            round_price(
                avg_px
                +
                risk_distance
                *
                RISK_REWARD,
                tick,
                "UP"
            )
        )

        if sl >= avg_px:

            sl = float(
                round_price(
                    avg_px - tick,
                    tick,
                    "DOWN"
                )
            )

        if tp <= avg_px:

            tp = float(
                round_price(
                    avg_px + tick,
                    tick,
                    "UP"
                )
            )

    # --------------------------------------------------------
    # SHORT
    # --------------------------------------------------------

    else:

        sl = float(
            round_price(
                sl,
                tick,
                "UP"
            )
        )

        tp = float(
            round_price(
                avg_px
                -
                risk_distance
                *
                RISK_REWARD,
                tick,
                "DOWN"
            )
        )

        if sl <= avg_px:

            sl = float(
                round_price(
                    avg_px + tick,
                    tick,
                    "UP"
                )
            )

        if tp >= avg_px:

            tp = float(
                round_price(
                    avg_px - tick,
                    tick,
                    "DOWN"
                )
            )

    return {

        "instId": inst_id,

        "direction": direction,

        "avgPx": avg_px,

        "tp": tp,

        "sl": sl,

        "tickSz": str(tick),

        "lotSz": str(
            info["lotSz"]
        ),

        "minSz": str(
            info["minSz"]
        ),

        "ctVal": str(
            info["ctVal"]
        ),
    }


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
            str(
                ATR_SL_MULTIPLIER
            )
        )
    )

    if stop_distance <= 0:

        return 0.0

    risk_budget = (
        D(str(equity))
        *
        D(
            str(
                RISK_PER_TRADE_PCT
            )
        )
    )

    # 开仓 + 止损平仓
    fee_per_contract = (
        price
        *
        ct_val
        *
        D(
            str(
                TAKER_FEE_RATE
            )
        )
        *
        D("2")
        *
        D(
            str(
                FEE_BUFFER_MULTIPLIER
            )
        )
    )

    denominator = (
        stop_distance
        *
        ct_val
        +
        fee_per_contract
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
            str(
                MAX_MARGIN_PCT
            )
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

    cl_ord_id = (
        "MCV27"
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

        "instId": inst_id,

        "tdMode": TD_MODE,

        "side": side,

        "ordType": "market",

        "sz": fmt_decimal(sz),

        "posSide": "net",

        "clOrdId": cl_ord_id,
    }

    resp = okx_request(
        "POST",
        "/api/v5/trade/order",
        body=body,
        private=True,
    )

    item = resp[
        "data"
    ][0]

    if (
        str(
            item.get(
                "sCode",
                "0"
            )
        )
        !=
        "0"
    ):

        raise RuntimeError(
            f"开仓被OKX拒绝：{item}"
        )

    ord_id = item.get(
        "ordId"
    )

    if not ord_id:

        raise RuntimeError(
            f"OKX未返回ordId：{resp}"
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
            "instId": inst_id,
            "ordId": order_id,
        },
        private=True,
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
    ord_id,
    timeout=ORDER_FILL_TIMEOUT
):

    end = (
        time.time()
        +
        timeout
    )

    last = None

    while time.time() < end:

        try:

            data = okx_request(
                "GET",
                "/api/v5/trade/order",
                params={
                    "instId": inst_id,
                    "ordId": ord_id,
                },
                private=True,
            )["data"]

            if data:

                row = data[0]

                last = row

                state_now = str(
                    row.get(
                        "state",
                        ""
                    )
                )

                if state_now == "filled":

                    avg_px = float(
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

                    fill_sz = float(
                        row.get(
                            "accFillSz"
                        )
                        or
                        row.get(
                            "fillSz"
                        )
                        or
                        row.get(
                            "sz"
                        )
                        or
                        0
                    )

                    if (
                        avg_px > 0
                        and
                        fill_sz > 0
                    ):

                        return row

                if state_now in (
                    "canceled",
                    "mmp_canceled"
                ):

                    raise RuntimeError(
                        f"开仓订单已取消：{row}"
                    )

        except Exception as e:

            log(
                f"查询成交状态异常：{e}"
            )

        time.sleep(0.7)

    # 最后通过持仓确认
    pos = get_bot_position()

    if (
        pos
        and
        pos.get("instId")
        ==
        inst_id
    ):

        return {

            "avgPx": (
                pos.get(
                    "avgPx"
                )
                or
                pos.get(
                    "openAvgPx"
                )
            ),

            "accFillSz": pos.get(
                "pos"
            ),

            "state":
                "filled_by_position_check",
        }

    raise RuntimeError(
        f"订单 {ord_id} "
        f"超时未确认成交，"
        f"last={last}"
    )


# ============================================================
# TP/SL 当前价格关系
# ============================================================

def protection_relation(
    direction,
    tp,
    sl,
    mark,
    tick
):

    eps = float(
        D(tick)
    )

    if direction == "LONG":

        if mark >= tp - eps:

            return "TP_REACHED"

        if mark <= sl + eps:

            return "SL_REACHED"

        if not (
            tp > mark + eps
            and
            sl < mark - eps
        ):

            return "INVALID"

    else:

        if mark <= tp + eps:

            return "TP_REACHED"

        if mark >= sl - eps:

            return "SL_REACHED"

        if not (
            tp < mark - eps
            and
            sl > mark + eps
        ):

            return "INVALID"

    return "OK"


# ============================================================
# OCO 提交前重新校验
# ============================================================

def validate_and_prepare_protection(
    candidate,
    plan
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

    mark = D(
        str(
            get_mark_price(
                inst_id
            )
        )
    )

    tp = D(
        str(
            plan["tp"]
        )
    )

    sl = D(
        str(
            plan["sl"]
        )
    )

    avg = D(
        str(
            plan["avgPx"]
        )
    )

    direction = candidate[
        "direction"
    ]

    # ========================================================
    # 先判断是否已经触发
    # ========================================================

    if direction == "LONG":

        if mark >= tp:

            return (
                "TP_REACHED",
                float(mark),
                plan
            )

        if mark <= sl:

            return (
                "SL_REACHED",
                float(mark),
                plan
            )

        # TP 至少距离 Mark 两个 tick
        if tp <= mark + tick:

            tp = ceil_step(
                mark
                +
                tick * 2,
                tick
            )

        # SL 至少距离 Mark 两个 tick
        if sl >= mark - tick:

            sl = floor_step(
                mark
                -
                tick * 2,
                tick
            )

    else:

        if mark <= tp:

            return (
                "TP_REACHED",
                float(mark),
                plan
            )

        if mark >= sl:

            return (
                "SL_REACHED",
                float(mark),
                plan
            )

        if tp >= mark - tick:

            tp = floor_step(
                mark
                -
                tick * 2,
                tick
            )

        if sl <= mark + tick:

            sl = ceil_step(
                mark
                +
                tick * 2,
                tick
            )

    # ========================================================
    # 最终关系检查
    # ========================================================

    if direction == "LONG":

        if not (
            tp > mark
            and
            sl < mark
            and
            sl < avg < tp
        ):

            raise RuntimeError(
                "OCO价格关系非法 LONG："
                f"avg={avg} "
                f"mark={mark} "
                f"tp={tp} "
                f"sl={sl}"
            )

    else:

        if not (
            tp < mark
            and
            sl > mark
            and
            tp < avg < sl
        ):

            raise RuntimeError(
                "OCO价格关系非法 SHORT："
                f"avg={avg} "
                f"mark={mark} "
                f"tp={tp} "
                f"sl={sl}"
            )

    new_plan = dict(plan)

    new_plan["tp"] = float(tp)

    new_plan["sl"] = float(sl)

    new_plan["markAtValidation"] = float(mark)

    return (
        "OK",
        float(mark),
        new_plan
    )


# ============================================================
# 创建 OCO
# ============================================================

def place_protection_order(
    candidate,
    plan,
    algo_cl_ord_id=None
):

    inst_id = candidate[
        "instId"
    ]

    direction = candidate[
        "direction"
    ]

    # LONG 平仓 = sell
    # SHORT 平仓 = buy
    exit_side = (
        "sell"
        if direction == "LONG"
        else
        "buy"
    )

    if not algo_cl_ord_id:

        algo_cl_ord_id = (
            "OCO27"
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

        "instId": inst_id,

        "tdMode": TD_MODE,

        "side": exit_side,

        "posSide": "net",

        "ordType": "oco",

        "sz": fmt_decimal(
            plan["sz"]
        ),

        "tpTriggerPx":
            fmt_decimal(
                plan["tp"]
            ),

        "tpOrdPx": "-1",

        "tpTriggerPxType":
            "mark",

        "slTriggerPx":
            fmt_decimal(
                plan["sl"]
            ),

        "slOrdPx": "-1",

        "slTriggerPxType":
            "mark",

        "reduceOnly": True,

        "algoClOrdId":
            algo_cl_ord_id,
    }

    log(
        "提交 OCO："
        f"side={exit_side} "
        f"sz={body['sz']} "
        f"TP={body['tpTriggerPx']} "
        f"SL={body['slTriggerPx']} "
        f"trigger=mark"
    )

    resp = okx_request(
        "POST",
        "/api/v5/trade/order-algo",
        body=body,
        private=True,
    )

    item = resp[
        "data"
    ][0]

    if (
        str(
            item.get(
                "sCode",
                "0"
            )
        )
        !=
        "0"
    ):

        raise RuntimeError(
            f"OCO TP/SL 被 OKX拒绝：{item}"
        )

    algo_id = item.get(
        "algoId"
    )

    if not algo_id:

        raise RuntimeError(
            f"OKX未返回OCO algoId：{resp}"
        )

    return (
        algo_id,
        algo_cl_ord_id
    )


# ============================================================
# OCO 详情
# ============================================================

def get_algo_detail(
    algo_id=None,
    algo_cl_ord_id=None
):

    params = {}

    if algo_id:

        params["algoId"] = str(
            algo_id
        )

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
        private=True,
    )["data"]

    return (
        data[0]
        if data
        else
        None
    )


# ============================================================
# 严格验证 OCO
# ============================================================

def verify_protection_by_algo(
    inst_id,
    algo_id,
    algo_cl_ord_id,
    expected_sz,
    direction,
    expected_tp,
    expected_sl,
    timeout_sec=PROTECTION_TIMEOUT
):

    end = (
        time.time()
        +
        timeout_sec
    )

    last = None

    exit_side = (
        "sell"
        if direction == "LONG"
        else
        "buy"
    )

    while time.time() < end:

        try:

            row = get_algo_detail(
                algo_id=algo_id,
                algo_cl_ord_id=algo_cl_ord_id
            )

            if row:

                last = row

                state_now = str(
                    row.get(
                        "state"
                    )
                    or
                    ""
                )

                ord_type = str(
                    row.get(
                        "ordType"
                    )
                    or
                    ""
                )

                side = str(
                    row.get(
                        "side"
                    )
                    or
                    ""
                )

                pos_side = str(
                    row.get(
                        "posSide"
                    )
                    or
                    ""
                )

                tp_px = row.get(
                    "tpTriggerPx"
                )

                sl_px = row.get(
                    "slTriggerPx"
                )

                row_sz = (
                    row.get("sz")
                    or
                    row.get(
                        "actualSz"
                    )
                    or
                    ""
                )

                log(
                    "OCO检查："
                    f"state={state_now} "
                    f"ordType={ord_type} "
                    f"side={side} "
                    f"posSide={pos_side} "
                    f"sz={row_sz} "
                    f"TP={tp_px} "
                    f"SL={sl_px}"
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

                if ord_type != "oco":

                    time.sleep(0.6)

                    continue

                if side != exit_side:

                    time.sleep(0.6)

                    continue

                if pos_side != "net":

                    time.sleep(0.6)

                    continue

                # ------------------------------------------------
                # TP / SL 精度验证
                # ------------------------------------------------

                try:

                    tp_ok = (
                        abs(
                            float(tp_px)
                            -
                            float(expected_tp)
                        )
                        <=
                        max(
                            abs(
                                float(
                                    expected_tp
                                )
                            )
                            *
                            1e-7,
                            1e-12
                        )
                    )

                except Exception:

                    tp_ok = False

                try:

                    sl_ok = (
                        abs(
                            float(sl_px)
                            -
                            float(expected_sl)
                        )
                        <=
                        max(
                            abs(
                                float(
                                    expected_sl
                                )
                            )
                            *
                            1e-7,
                            1e-12
                        )
                    )

                except Exception:

                    sl_ok = False

                # ------------------------------------------------
                # 数量验证
                # ------------------------------------------------

                try:

                    sz_ok = (
                        abs(
                            float(row_sz)
                            -
                            float(expected_sz)
                        )
                        <=
                        max(
                            abs(
                                float(
                                    expected_sz
                                )
                            )
                            *
                            0.0001,
                            1e-12
                        )
                    )

                except Exception:

                    sz_ok = True

                if (
                    state_now
                    in (
                        "live",
                        "effective",
                        "partially_effective"
                    )
                    and
                    tp_ok
                    and
                    sl_ok
                    and
                    sz_ok
                ):

                    return (
                        True,
                        row
                    )

        except Exception as e:

            log(
                f"OCO策略单查询异常：{e}"
            )

        time.sleep(0.7)

    return (
        False,
        last
    )


# ============================================================
# 查询策略单
# ============================================================

def get_pending_algos(
    inst_id,
    ord_type
):

    return okx_request(
        "GET",
        "/api/v5/trade/orders-algo-pending",
        params={
            "ordType": ord_type,
            "instType": INST_TYPE,
            "instId": inst_id,
        },
        private=True,
    )["data"]


# ============================================================
# 寻找当前 OCO
# ============================================================

def find_valid_oco(
    inst_id,
    position_sz,
    direction,
    algo_id=None
):

    try:

        algos = get_pending_algos(
            inst_id,
            "oco"
        )

        exit_side = (
            "sell"
            if direction == "LONG"
            else
            "buy"
        )

        for a in algos:

            if (
                str(
                    a.get(
                        "ordType"
                    )
                )
                !=
                "oco"
            ):

                continue

            if (
                str(
                    a.get(
                        "state"
                    )
                    or
                    ""
                )
                not in (
                    "live",
                    "effective",
                    "partially_effective"
                )
            ):

                continue

            if (
                str(
                    a.get(
                        "side"
                    )
                    or
                    ""
                )
                !=
                exit_side
            ):

                continue

            if (
                str(
                    a.get(
                        "posSide"
                    )
                    or
                    "net"
                )
                !=
                "net"
            ):

                continue

            try:

                if (
                    float(
                        a.get(
                            "tpTriggerPx"
                        )
                        or
                        0
                    )
                    <=
                    0
                ):

                    continue

                if (
                    float(
                        a.get(
                            "slTriggerPx"
                        )
                        or
                        0
                    )
                    <=
                    0
                ):

                    continue

            except Exception:

                continue

            if (
                algo_id
                and
                str(
                    a.get(
                        "algoId"
                    )
                )
                ==
                str(algo_id)
            ):

                return a

            try:

                a_sz = float(
                    a.get(
                        "sz"
                    )
                    or
                    a.get(
                        "actualSz"
                    )
                    or
                    0
                )

                if (
                    position_sz > 0
                    and
                    abs(
                        a_sz
                        -
                        position_sz
                    )
                    <=
                    max(
                        position_sz
                        *
                        0.0001,
                        1e-12
                    )
                ):

                    return a

            except Exception:

                pass

        return None

    except Exception as e:

        log(
            f"寻找有效 OCO 失败：{e}"
        )

        return None


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
            "instId": inst_id,
            "algoId": str(
                algo_id
            ),
        }
    ]

    try:

        resp = okx_request(
            "POST",
            "/api/v5/trade/cancel-algos",
            body=body,
            private=True,
        )

        item = resp.get(
            "data",
            [{}]
        )[0]

        if (
            str(
                item.get(
                    "sCode",
                    "0"
                )
            )
            !=
            "0"
        ):

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
# 紧急平仓
# ============================================================

def emergency_close_position(pos):

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
                "0"
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
        f"⚠️ 紧急平仓："
        f"{inst_id} "
        f"{direction} "
        f"qty={qty}"
    )

    body = {

        "instId": inst_id,

        "tdMode": TD_MODE,

        "side": side,

        "ordType": "market",

        "sz": fmt_decimal(
            qty
        ),

        "posSide": "net",

        "reduceOnly": True,

        "clOrdId":
            "EMC27"
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
        private=True,
    )

    item = resp[
        "data"
    ][0]

    if (
        str(
            item.get(
                "sCode",
                "0"
            )
        )
        !=
        "0"
    ):

        raise RuntimeError(
            f"紧急平仓被拒绝：{item}"
        )

    return True


# ============================================================
# 已有持仓保护恢复
# ============================================================

def repair_existing_position(pos):

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
                "0"
            )
            or
            0
        )
    )

    avg_px = get_position_avg_px(
        pos
    )

    if (
        qty <= 0
        or
        avg_px <= 0
    ):

        return False

    active = state.get(
        "active_trade"
    )

    # ========================================================
    # A：本机器人自己有 active_trade
    # ========================================================

    if (
        active
        and
        active.get(
            "instId"
        )
        ==
        inst_id
        and
        active.get(
            "direction"
        )
        ==
        direction
        and
        float(
            active.get(
                "avgPx",
                0
            )
            or
            0
        )
        > 0
        and
        float(
            active.get(
                "tp",
                0
            )
            or
            0
        )
        > 0
        and
        float(
            active.get(
                "sl",
                0
            )
            or
            0
        )
        > 0
    ):

        algo_id = active.get(
            "algoId"
        )

        algo_cl_ord_id = (
            active.get(
                "algoClOrdId"
            )
        )

        # ----------------------------------------------------
        # 先找现有 OCO
        # ----------------------------------------------------

        valid = find_valid_oco(
            inst_id,
            qty,
            direction,
            algo_id=algo_id
        )

        if valid:

            log(
                f"已有持仓保护正常："
                f"{inst_id} "
                f"TP={valid.get('tpTriggerPx')} "
                f"SL={valid.get('slTriggerPx')}"
            )

            return True

        log(
            f"发现 {inst_id} 持仓，"
            f"但原 OCO 不存在，准备恢复"
        )

        # ----------------------------------------------------
        # 构建原计划
        # ----------------------------------------------------

        plan = {

            "instId":
                inst_id,

            "direction":
                direction,

            "avgPx":
                avg_px,

            "tp":
                float(
                    active["tp"]
                ),

            "sl":
                float(
                    active["sl"]
                ),

            "sz":
                qty,
        }

        candidate = {

            "instId":
                inst_id,

            "direction":
                direction,

            "price":
                avg_px,

            "atr":
                abs(
                    avg_px
                    -
                    float(
                        active["sl"]
                    )
                )
                /
                ATR_SL_MULTIPLIER,
        }

        try:

            status, mark, plan = (
                validate_and_prepare_protection(
                    candidate,
                    plan
                )
            )

            if status in (
                "TP_REACHED",
                "SL_REACHED"
            ):

                log(
                    f"{inst_id} "
                    f"恢复时发现保护价已触发："
                    f"{status} "
                    f"mark={mark}"
                )

                emergency_close_position(
                    pos
                )

                state[
                    "active_trade"
                ] = None

                save_state(state)

                return True

            new_id, clid = (
                place_protection_order(
                    candidate,
                    plan,
                    algo_cl_ord_id
                )
            )

            ok, row = (
                verify_protection_by_algo(
                    inst_id,
                    new_id,
                    clid,
                    qty,
                    direction,
                    plan["tp"],
                    plan["sl"],
                    PROTECTION_TIMEOUT
                )
            )

            if not ok:

                raise RuntimeError(
                    f"OCO恢复失败：{row}"
                )

            active.update({

                "algoId":
                    new_id,

                "algoClOrdId":
                    clid,

                "sz":
                    qty,

                "avgPx":
                    avg_px,

                "tp":
                    plan["tp"],

                "sl":
                    plan["sl"],
            })

            state[
                "active_trade"
            ] = active

            save_state(state)

            log(
                f"✅ OCO已恢复："
                f"TP={plan['tp']} "
                f"SL={plan['sl']}"
            )

            return True

        except Exception as e:

            log(
                f"恢复 OCO 失败：{e}"
            )

            if EMERGENCY_CLOSE_IF_OCO_FAIL:

                try:

                    emergency_close_position(
                        pos
                    )

                    state[
                        "active_trade"
                    ] = None

                    save_state(state)

                except Exception as close_e:

                    log(
                        "❌ 恢复失败且"
                        f"紧急平仓失败：{close_e}"
                    )

            return False

    # ========================================================
    # B：旧版 conditional SL
    # ========================================================

    if REPAIR_UNKNOWN_OLD_SL:

        try:

            algos = get_pending_algos(
                inst_id,
                "conditional"
            )

            old_sl = None

            old_algo_id = None

            for a in algos:

                if (
                    str(
                        a.get(
                            "ordType"
                        )
                    )
                    !=
                    "conditional"
                ):

                    continue

                sl_px = a.get(
                    "slTriggerPx"
                )

                try:

                    sl_val = float(
                        sl_px or 0
                    )

                except Exception:

                    continue

                if sl_val > 0:

                    old_sl = sl_val

                    old_algo_id = (
                        a.get(
                            "algoId"
                        )
                    )

                    break

            if old_sl:

                log(
                    f"检测到旧版 "
                    f"conditional SL："
                    f"{inst_id} "
                    f"SL={old_sl}"
                )

                # 取消旧保护
                if old_algo_id:

                    cancel_algo(
                        inst_id,
                        old_algo_id
                    )

                candidate = {

                    "instId":
                        inst_id,

                    "direction":
                        direction,

                    "price":
                        avg_px,

                    "atr":
                        max(
                            abs(
                                avg_px
                                -
                                old_sl
                            )
                            /
                            ATR_SL_MULTIPLIER,

                            avg_px
                            *
                            0.001
                        ),
                }

                plan = (
                    build_protection_plan(
                        candidate,
                        avg_px=avg_px,
                        sl_override=old_sl
                    )
                )

                plan["sz"] = qty

                status, mark, plan = (
                    validate_and_prepare_protection(
                        candidate,
                        plan
                    )
                )

                if status in (
                    "TP_REACHED",
                    "SL_REACHED"
                ):

                    log(
                        "旧SL转换前"
                        f"价格已触发：{status}"
                    )

                    emergency_close_position(
                        pos
                    )

                    return True

                algo_id_new, clid = (
                    place_protection_order(
                        candidate,
                        plan
                    )
                )

                ok, row = (
                    verify_protection_by_algo(
                        inst_id,
                        algo_id_new,
                        clid,
                        qty,
                        direction,
                        plan["tp"],
                        plan["sl"],
                        PROTECTION_TIMEOUT
                    )
                )

                if not ok:

                    raise RuntimeError(
                        f"旧 SL 转 OCO 失败：{row}"
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
                        avg_px,

                    "tp":
                        plan["tp"],

                    "sl":
                        plan["sl"],

                    "algoId":
                        algo_id_new,

                    "algoClOrdId":
                        clid,

                    "openedAt":
                        int(
                            time.time()
                        ),
                }

                save_state(state)

                log(
                    f"✅ 旧 SL 已转换为 OCO："
                    f"TP={plan['tp']} "
                    f"SL={plan['sl']}"
                )

                return True

        except Exception as e:

            log(
                f"旧 SL 自动转换 OCO失败：{e}"
            )

    # ========================================================
    # 未知仓位
    # ========================================================

    log(
        f"⚠️ {inst_id} 存在持仓，"
        f"但无法确定原始 TP/SL。"
        f"机器人不会猜价格。"
    )

    return False


# ============================================================
# 每日状态
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

        state["date"] = today

        state[
            "start_equity"
        ] = float(equity)

        state["trades"] = 0

        state[
            "consecutive_losses"
        ] = 0

        state[
            "cooldown_until"
        ] = 0

        state[
            "last_trade_inst"
        ] = ""

        save_state(state)

        log(
            f"新交易日，"
            f"起始权益={equity:.4f}"
        )

    if not state.get(
        "start_equity"
    ):

        state[
            "start_equity"
        ] = float(equity)

        save_state(state)


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
            "达到今日最大交易次数，"
            "暂停开新仓"
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

        remain = int(
            state[
                "cooldown_until"
            ]
            -
            time.time()
        )

        log(
            "连续亏损冷却中，"
            f"剩余 {max(remain, 0)} 秒"
        )

        return False

    start = float(
        state[
            "start_equity"
        ]
    )

    if start > 0:

        change_pct = (
            equity
            -
            start
        ) / start

        if (
            change_pct
            <=
            -MAX_DAILY_LOSS_PCT
        ):

            log(
                "达到每日最大亏损限制："
                f"{change_pct:.2%}"
            )

            return False

        if (
            change_pct
            >=
            MAX_DAILY_PROFIT_PCT
        ):

            log(
                "达到每日盈利限制："
                f"{change_pct:.2%}"
            )

            return False

    return True


# ============================================================
# 已实现 PnL
# ============================================================

def get_realized_pnl_for_position(
    inst_id,
    opened_at=None
):

    try:

        data = okx_request(
            "GET",
            "/api/v5/trade/fills-history",
            params={
                "instType": INST_TYPE,
                "instId": inst_id,
                "limit": "100",
            },
            private=True,
        )["data"]

        if not data:

            return None

        cutoff_ms = (
            int(
                opened_at or 0
            )
            *
            1000
        )

        total = 0.0

        found = False

        for x in data:

            try:

                ts = int(
                    x.get(
                        "ts"
                    )
                    or
                    0
                )

            except Exception:

                ts = 0

            if (
                cutoff_ms
                and
                ts < cutoff_ms
            ):

                continue

            try:

                pnl = float(
                    x.get(
                        "pnl"
                    )
                    or
                    0
                )

                fee = float(
                    x.get(
                        "fee"
                    )
                    or
                    0
                )

                total += (
                    pnl
                    +
                    fee
                )

                found = True

            except Exception:

                pass

        if found:

            return total

    except Exception as e:

        log(
            f"读取成交盈亏失败：{e}"
        )

    return None


# ============================================================
# 检查仓位是否结束
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

    except Exception as e:

        log(
            f"检查持仓结束状态失败：{e}"
        )

        return

    for p in positions:

        if (
            p.get(
                "instId"
            )
            ==
            inst_id
        ):

            return

    # --------------------------------------------------------
    # 仓位已经不存在
    # --------------------------------------------------------

    pnl = get_realized_pnl_for_position(
        inst_id,
        active.get(
            "openedAt"
        )
    )

    if pnl is not None:

        log(
            f"检测到仓位结束："
            f"{inst_id}，"
            f"本次成交净PnL≈"
            f"{pnl:.4f} USDT "
            f"（含成交手续费，"
            f"未单独计 funding）"
        )

        if pnl < 0:

            state[
                "consecutive_losses"
            ] += 1

            if (
                state[
                    "consecutive_losses"
                ]
                >=
                MAX_CONSECUTIVE_LOSSES
            ):

                state[
                    "cooldown_until"
                ] = (
                    time.time()
                    +
                    LOSS_COOLDOWN_SECONDS
                )

                log(
                    "连续亏损达到 "
                    f"{MAX_CONSECUTIVE_LOSSES} 次，"
                    f"冷却 "
                    f"{LOSS_COOLDOWN_SECONDS // 60} 分钟"
                )

        elif pnl > 0:

            state[
                "consecutive_losses"
            ] = 0

    state[
        "active_trade"
    ] = None

    save_state(state)


# ============================================================
# 全市场扫描
# ============================================================

def scan_candidates():

    instruments = (
        get_all_swap_instruments()
    )

    tickers = get_tickers()

    rows = []

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

            vol24 = float(
                ticker.get(
                    "volCcy24h"
                )
                or
                0
            )

            last = float(
                ticker.get(
                    "last"
                )
                or
                0
            )

            if last <= 0:

                continue

            if (
                vol24
                <
                MIN_24H_VOL_USDT
            ):

                continue

            candidate = (
                calculate_signal(
                    inst_id,
                    ticker
                )
            )

            if not candidate:

                continue

            volume_score = min(
                vol24
                /
                50_000_000,
                3.0
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

            rows.append(
                candidate
            )

        except Exception:

            continue

    rows.sort(
        key=lambda x:
            x["rank_score"],
        reverse=True
    )

    return rows[
        :SCAN_LIMIT
    ]


# ============================================================
# 执行候选
# ============================================================

def execute_candidate(
    candidate,
    equity
):

    inst_id = candidate[
        "instId"
    ]

    # ========================================================
    # 防止刚交易同一标的重复进入
    # ========================================================

    if (
        state.get(
            "last_trade_inst"
        )
        ==
        inst_id
    ):

        log(
            f"{inst_id} "
            "为最近交易标的，跳过一次"
        )

        return False

    # ========================================================
    # 设置杠杆
    # ========================================================

    set_leverage(
        inst_id,
        candidate[
            "leverage"
        ]
    )

    # ========================================================
    # 计算数量
    # ========================================================

    sz = calculate_size(
        candidate,
        equity
    )

    if sz <= 0:

        log(
            f"{inst_id} "
            "计算出的合约数量"
            "低于最小下单量，跳过。"
            f"价格={candidate['price']}"
        )

        return False

    info = get_instrument(
        inst_id
    )

    min_sz = float(
        info["minSz"]
    )

    if sz < min_sz:

        log(
            f"{inst_id} "
            f"sz={sz} "
            f"< minSz={min_sz}，跳过"
        )

        return False

    log(
        f"准备开仓："
        f"{inst_id} "
        f"{candidate['direction']} "
        f"score={candidate['score']} "
        f"lev={candidate['leverage']}x "
        f"sz={sz} "
        f"price={candidate['price']:.8f}"
    )

    # ========================================================
    # 1. 市价开仓
    # ========================================================

    ord_id = place_market_order(
        candidate,
        sz
    )

    log(
        f"开仓订单已提交："
        f"ordId={ord_id}"
    )

    # ========================================================
    # 2. 等待真实成交
    # ========================================================

    fill = wait_order_filled(
        inst_id,
        ord_id
    )

    avg_px = float(
        fill.get(
            "avgPx"
        )
        or
        fill.get(
            "fillPx"
        )
        or
        0
    )

    actual_sz = float(
        fill.get(
            "accFillSz"
        )
        or
        fill.get(
            "fillSz"
        )
        or
        fill.get(
            "pos"
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

        if (
            not pos
            or
            pos.get(
                "instId"
            )
            !=
            inst_id
        ):

            raise RuntimeError(
                "开仓后无法取得"
                "真实成交价/数量"
            )

        avg_px = (
            get_position_avg_px(
                pos
            )
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

    if (
        avg_px <= 0
        or
        actual_sz <= 0
    ):

        raise RuntimeError(
            "开仓后真实成交价/数量无效"
        )

    log(
        f"真实成交："
        f"{inst_id} "
        f"avgPx={avg_px} "
        f"sz={actual_sz}"
    )

    # ========================================================
    # 3. 根据真实成交价计算 TP/SL
    # ========================================================

    plan = build_protection_plan(
        candidate,
        avg_px=avg_px
    )

    plan["sz"] = actual_sz

    log(
        f"初始保护计划："
        f"TP={plan['tp']} "
        f"SL={plan['sl']} "
        f"RR={RISK_REWARD}"
    )

    # ========================================================
    # 4. 立即保存 active_trade
    #
    # 即使之后 OCO 创建失败，
    # 重启以后仍然知道这笔仓位属于机器人。
    # ========================================================

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
            None,

        "algoClOrdId":
            None,

        "openedAt":
            int(
                time.time()
            ),
    }

    save_state(state)

    # ========================================================
    # 5. OCO 提交前重新读取 Mark Price
    # ========================================================

    status, mark, plan = (
        validate_and_prepare_protection(
            candidate,
            plan
        )
    )

    # --------------------------------------------------------
    # 如果开仓瞬间 TP/SL 已经被穿越
    # --------------------------------------------------------

    if status in (
        "TP_REACHED",
        "SL_REACHED"
    ):

        log(
            "⚠️ 开仓后保护价"
            "已经被 Mark Price 穿越："
            f"{status} "
            f"mark={mark}"
        )

        pos = get_bot_position()

        if pos:

            emergency_close_position(
                pos
            )

        state[
            "active_trade"
        ] = None

        save_state(state)

        return False

    # ========================================================
    # 6. 保存经过 Mark Price 修正后的 TP/SL
    # ========================================================

    state[
        "active_trade"
    ]["tp"] = plan[
        "tp"
    ]

    state[
        "active_trade"
    ]["sl"] = plan[
        "sl"
    ]

    save_state(state)

    # ========================================================
    # 7. 创建 OCO
    # ========================================================

    algo_id = None

    algo_cl_id = None

    try:

        algo_id, algo_cl_id = (
            place_protection_order(
                candidate,
                plan
            )
        )

        state[
            "active_trade"
        ]["algoId"] = algo_id

        state[
            "active_trade"
        ]["algoClOrdId"] = (
            algo_cl_id
        )

        save_state(state)

        log(
            "OCO策略单已提交："
            f"algoId={algo_id} "
            f"客户ID={algo_cl_id}"
        )

        # ====================================================
        # 8. 严格验证 OCO
        # ====================================================

        ok, algo = (
            verify_protection_by_algo(

                inst_id,

                algo_id,

                algo_cl_id,

                actual_sz,

                candidate[
                    "direction"
                ],

                plan["tp"],

                plan["sl"],

                PROTECTION_TIMEOUT
            )
        )

        if not ok:

            raise RuntimeError(
                f"OCO TP/SL 未确认：{algo}"
            )

        log(
            "✅ OCO TP/SL 已确认："
            f"algoId={algo_id} "
            f"TP={algo.get('tpTriggerPx')} "
            f"SL={algo.get('slTriggerPx')}"
        )

    except Exception as e:

        log(
            f"❌ OCO创建/验证失败：{e}"
        )

        # ====================================================
        # 9. OCO 失败 -> 紧急平仓
        # ====================================================

        if EMERGENCY_CLOSE_IF_OCO_FAIL:

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

                try:

                    emergency_close_position(
                        pos
                    )

                    state[
                        "active_trade"
                    ] = None

                    save_state(state)

                    log(
                        "✅ OCO失败后的"
                        "紧急平仓完成"
                    )

                except Exception as close_e:

                    log(
                        "❌ OCO失败后的"
                        f"紧急平仓也失败：{close_e}"
                    )

                    # 不清 active_trade
                    # 下一轮继续尝试保护/恢复

        raise

    # ========================================================
    # 10. 交易正式建立
    # ========================================================

    state[
        "trades"
    ] += 1

    state[
        "last_trade_inst"
    ] = inst_id

    save_state(state)

    log(
        f"🎯 交易建立完成："
        f"{inst_id} "
        f"{candidate['direction']} "
        f"TP={plan['tp']} "
        f"SL={plan['sl']}"
    )

    return True


# ============================================================
# 主循环
# ============================================================

def main():

    log(
        "=" * 70
    )

    log(
        "OKX 全市场永续"
        "强行情自动交易机器人 V2.7 启动"
    )

    log(
        f"模拟盘={SIMULATED_TRADING} "
        f"| BAR={BAR} "
        f"| TD_MODE={TD_MODE}"
    )

    log(
        "TP/SL模式："
        "OCO + Mark Price预校验 + 严格验证"
    )

    log(
        "=" * 70
    )

    # ========================================================
    # 启动账户检查
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
    # 启动时检查已有仓位
    # ========================================================

    try:

        pos = get_bot_position()

        if pos:

            log(
                "启动发现已有持仓："
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

                save_state(state)

    except Exception as e:

        log(
            f"启动持仓保护检查失败：{e}"
        )

    # ========================================================
    # 主循环
    # ========================================================

    while True:

        try:

            # ------------------------------------------------
            # 更新权益
            # ------------------------------------------------

            equity = get_equity()

            reset_daily_if_needed(
                equity
            )

            # ------------------------------------------------
            # 检查之前交易是否已经结束
            # ------------------------------------------------

            check_position_closed()

            # ------------------------------------------------
            # 当前仓位
            # ------------------------------------------------

            pos = get_bot_position()

            if pos:

                # 有仓位绝不继续开仓
                repair_existing_position(
                    pos
                )

                log(
                    "当前已有持仓："
                    f"{pos.get('instId')} "
                    f"{position_direction(pos)} "
                    f"qty={pos.get('pos')} "
                    "，等待 OCO TP/SL 结束"
                )

                time.sleep(
                    LOOP_SECONDS
                )

                continue

            # ------------------------------------------------
            # 没仓位
            # ------------------------------------------------

            if state.get(
                "active_trade"
            ):

                state[
                    "active_trade"
                ] = None

                save_state(state)

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
            # 扫描市场
            # ------------------------------------------------

            candidates = (
                scan_candidates()
            )

            if not candidates:

                log(
                    "本轮没有满足条件的强信号"
                )

                time.sleep(
                    LOOP_SECONDS
                )

                continue

            # ------------------------------------------------
            # 输出前五名
            # ------------------------------------------------

            preview = []

            for c in candidates[:5]:

                preview.append(

                    f"{c['instId']} "
                    f"{c['direction']} "
                    f"S{c['score']} "
                    f"rank={c['rank_score']:.2f}"

                )

            log(
                "候选："
                +
                " | ".join(
                    preview
                )
            )

            # ------------------------------------------------
            # 尝试最强候选
            # ------------------------------------------------

            traded = False

            for candidate in candidates:

                # 再检查一次仓位
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
                        f"执行 "
                        f"{candidate['instId']} "
                        f"失败：{e}"
                    )

                    traceback.print_exc()

                    # 如果已经形成仓位
                    # 不允许继续尝试其它币
                    if get_bot_position():

                        break

            if not traded:

                log(
                    "本轮没有成功建立交易"
                )

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
            LOOP_SECONDS
        )


# ============================================================
# 启动
# ============================================================

if __name__ == "__main__":

    main()
