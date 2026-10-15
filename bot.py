# -*- coding: utf-8 -*-

"""
============================================================
OKX 永续自动交易模拟盘 V2.9.2
============================================================

V3.0.0 更新：
- 全新权威方向判定逻辑（大周期定方向 + 小周期回调入场）
- 1H EMA21/55 决定只做多或只做空
- 明确过滤震荡市
- 5m 等待回调再入场，避免追涨杀跌
- 仓位继续使用保守的 USDT 百分比控制

V2.9.4 更新：
- 重写仓位计算逻辑：明确按 USDT 保证金百分比控制仓位
- 下单前打印目标保证金 / 实际保证金 / 名义价值
- 进一步降低风险参数

V2.9.3 更新：
- 大幅降低仓位风险参数（防止仓位过大）
- 增加单笔名义价值硬性上限

V2.9.2 更新：
- 增强 API 请求重试机制（带指数退避）
- 改用标准 logging 模块
- 完善文档与注释
- 小幅健壮性优化

V2.9.1 修复：
1. consecutive_losses 全局变量错误
2. 连续亏损保护无法正常运行
3. OCO 验证逻辑
4. 持仓实时 Mark Price
5. 实时浮盈/浮亏/ROI
6. TP/SL 实时显示
7. Top30 新开仓限制
8. 已有仓位继续管理
9. 动态杠杆 5x ~ 20x
10. 自动适配合约最大杠杆
11. 平仓后读取 realizedPnl
12. UTC+8 日志
13. 清理重复函数
============================================================
"""

import os
import time
import json
import hmac
import base64
import hashlib
import logging
import threading
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

import requests


# ============================================================
# 版本
# ============================================================

VERSION = "V3.0.0"


# ============================================================
# OKX 基础设置
# ============================================================

BASE_URL = "https://www.okx.com"

INST_TYPE = "SWAP"
SETTLE_CCY = "USDT"

TD_MODE = "cross"
POS_MODE = "net"

# 1 = 模拟盘
SIMULATED_TRADING = "1"


# ============================================================
# 指定交易品种
# ============================================================

# 留空 = 自动扫描 Top30
# 如果以后只想测试 BTC，可以改成：
# TARGET_INST_ID = "BTC-USDT-SWAP"

TARGET_INST_ID = ""


# ============================================================
# K线
# ============================================================

SIGNAL_BAR = "5m"
TREND_BAR_15M = "15m"
TREND_BAR_1H = "1H"

LOOP_SECONDS = 20


# ============================================================
# CoinGecko Top30
# ============================================================

TOP30_CACHE_SECONDS = 600

SCAN_LIMIT = 30


# ============================================================
# 流动性
# ============================================================

MIN_24H_VOLUME_USD = Decimal("3000000")


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
# RSI
# ============================================================

RSI_LONG_MIN = Decimal("52")
RSI_LONG_MAX = Decimal("72")

RSI_SHORT_MIN = Decimal("28")
RSI_SHORT_MAX = Decimal("48")


# ============================================================
# 信号
# ============================================================

MIN_SIGNAL_SCORE = 4


# ============================================================
# 仓位风险
# ============================================================

MAX_MARGIN_PCT = Decimal("0.015")  # 单笔最大保证金占比 1.5%

RISK_PER_TRADE_PCT = Decimal("0.001")  # 单笔风险 0.1% 权益

MIN_LEVERAGE = Decimal("5")

MAX_LEVERAGE = Decimal("20")


# ============================================================
# 连续亏损保护
# ============================================================

MAX_CONSECUTIVE_LOSSES = 3

LOSS_COOLDOWN_SECONDS = 1800


# ============================================================
# TP / SL
# ============================================================

TARGET_SL_ROI_PCT = Decimal("1.5")

TARGET_TP_ROI_PCT = Decimal("3.5")

RISK_REWARD = Decimal("1.8")

ATR_SL_FRACTION = Decimal("0.60")

MIN_SL_PRICE_PCT = Decimal("0.0025")

MAX_SL_PRICE_PCT = Decimal("0.025")

MAX_TP_PRICE_PCT = Decimal("0.045")

MIN_TRIGGER_GAP_PCT = Decimal("0.0010")


# ============================================================
# OCO
# ============================================================

OCO_RETRY_COUNT = 3

OCO_VERIFY_WAIT = 1.2

EMERGENCY_CLOSE_ON_NEW_POSITION_PROTECTION_FAILURE = True


# ============================================================
# UTC+8
# ============================================================

UTC8 = timezone(
    timedelta(hours=8)
)


def now_str():
    return datetime.now(UTC8).strftime("%Y-%m-%d %H:%M:%S")


# 使用标准 logging，同时保持原有 print 风格输出
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# 强制使用 UTC+8 时间显示
logging.Formatter.converter = lambda *args: datetime.now(UTC8).timetuple()

logger = logging.getLogger("okx-bot")


def log(msg):
    """兼容旧代码的日志函数"""
    logger.info(msg)


# ============================================================
# Decimal
# ============================================================

def D(value, default="0"):

    try:

        if value is None or value == "":

            return Decimal(default)

        return Decimal(
            str(value)
        )

    except Exception:

        return Decimal(default)


def fmt_decimal(value, digits=12):

    value = D(value)

    s = format(
        value,
        "f"
    )

    if "." in s:

        a, b = s.split(
            ".",
            1
        )

        b = b[:digits]

        s = a + "." + b

    s = s.rstrip("0").rstrip(".")

    if s in (
        "",
        "-0"
    ):

        return "0"

    return s


def round_down(
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


def round_up(
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


def clamp(
    value,
    low,
    high
):

    value = D(value)

    if value < low:

        return low

    if value > high:

        return high

    return value


# ============================================================
# API KEY
# ============================================================

API_KEY = os.getenv(
    "OKX_API_KEY"
)

SECRET_KEY = os.getenv(
    "OKX_SECRET_KEY"
)

PASSPHRASE = os.getenv(
    "OKX_PASSPHRASE"
)

if not API_KEY or not SECRET_KEY or not PASSPHRASE:

    raise RuntimeError(
        "缺少 OKX API 环境变量："
        "OKX_API_KEY / "
        "OKX_SECRET_KEY / "
        "OKX_PASSPHRASE"
    )


# ============================================================
# Session
# ============================================================

session = requests.Session()

session.headers.update(
    {
        "Content-Type": "application/json",
        "x-simulated-trading":
            SIMULATED_TRADING
    }
)


# ============================================================
# OKX 签名
# ============================================================

def get_timestamp():

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

    mac = hmac.new(
        SECRET_KEY.encode(
            "utf-8"
        ),
        message.encode(
            "utf-8"
        ),
        hashlib.sha256
    )

    return base64.b64encode(
        mac.digest()
    ).decode()


# ============================================================
# OKX 请求（带简单重试）
# ============================================================

def okx_request(
    method,
    path,
    params=None,
    body=None,
    private=False,
    timeout=15,
    max_retries=3
):
    """发送 OKX API 请求，失败时自动重试（指数退避）"""

    method = method.upper()
    params = params or {}
    body = body or {}

    query = ""
    if params:
        clean_params = {
            k: v for k, v in params.items()
            if v is not None and v != ""
        }
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

    last_error = None

    for attempt in range(1, max_retries + 1):
        headers = {
            "Content-Type": "application/json",
            "x-simulated-trading": SIMULATED_TRADING
        }

        if private:
            timestamp = get_timestamp()
            sign = make_signature(
                timestamp, method, request_path, body_text
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
                response = session.get(url, headers=headers, timeout=timeout)
            elif method == "POST":
                response = session.post(
                    url, headers=headers, data=body_text, timeout=timeout
                )
            elif method == "DELETE":
                response = session.delete(url, headers=headers, timeout=timeout)
            else:
                raise RuntimeError(f"不支持 HTTP 方法: {method}")

            response.raise_for_status()
            data = response.json()

            if str(data.get("code")) != "0":
                # 业务错误通常不重试（除非是限流）
                err_code = str(data.get("code", ""))
                if err_code in ("50011", "50013", "50026"):  # 常见限流/临时错误
                    raise RuntimeError(f"OKX 临时错误: {data}")
                raise RuntimeError(f"OKX API错误: {data}")

            return data.get("data", [])

        except Exception as e:
            last_error = e
            if attempt < max_retries:
                sleep_sec = min(2 ** attempt, 8)
                log(f"⚠️ 请求失败 (第{attempt}次) {method} {path}: {e}，{sleep_sec}s后重试")
                time.sleep(sleep_sec)
            else:
                break

    raise RuntimeError(
        f"OKX 请求失败（已重试{max_retries}次） {method} {request_path}: {last_error}"
    )


# ============================================================
# CoinGecko Top30
# ============================================================

top30_cache = {
    "time": 0,
    "symbols": set(),
    "rank": {}
}


def get_top30_market_caps():

    global top30_cache

    now = time.time()

    if (
        now
        - top30_cache["time"]
        < TOP30_CACHE_SECONDS
    ):

        return (
            top30_cache["symbols"],
            top30_cache["rank"]
        )

    try:

        response = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "order":
                    "market_cap_desc",
                "per_page": "30",
                "page": "1",
                "sparkline": "false"
            },
            timeout=15
        )

        response.raise_for_status()

        rows = response.json()

        symbols = set()

        ranks = {}

        for index, row in enumerate(
            rows,
            start=1
        ):

            symbol = str(
                row.get(
                    "symbol",
                    ""
                )
            ).upper().strip()

            if not symbol:

                continue

            symbols.add(
                symbol
            )

            ranks[symbol] = index

        if not symbols:

            raise RuntimeError(
                "CoinGecko Top30为空"
            )

        top30_cache = {
            "time": now,
            "symbols": symbols,
            "rank": ranks
        }

        return (
            symbols,
            ranks
        )

    except Exception as e:

        log(
            f"⚠️ CoinGecko Top30获取失败: {e}"
        )

        return (
            set(),
            {}
        )


# ============================================================
# 合约缓存
# ============================================================

instrument_cache = {}

instrument_lock = threading.Lock()


def get_instrument(
    inst_id
):

    with instrument_lock:

        if inst_id in instrument_cache:

            return instrument_cache[
                inst_id
            ]

    rows = okx_request(
        "GET",
        "/api/v5/public/instruments",
        params={
            "instType":
                INST_TYPE,
            "instId":
                inst_id
        }
    )

    if not rows:

        raise RuntimeError(
            f"找不到合约: {inst_id}"
        )

    info = rows[0]

    with instrument_lock:

        instrument_cache[
            inst_id
        ] = info

    return info


# ============================================================
# 全部活跃USDT永续
# ============================================================

def get_all_active_usdt_swaps():

    rows = okx_request(
        "GET",
        "/api/v5/public/instruments",
        params={
            "instType":
                INST_TYPE
        }
    )

    result = []

    for row in rows:

        inst_id = row.get(
            "instId",
            ""
        )

        if not inst_id.endswith(
            "-USDT-SWAP"
        ):

            continue

        if row.get(
            "state"
        ) != "live":

            continue

        if row.get(
            "settleCcy"
        ) != "USDT":

            continue

        result.append(
            row
        )

        with instrument_lock:

            instrument_cache[
                inst_id
            ] = row

    return result


# ============================================================
# Top30 OKX交易品种
# ============================================================

def get_top30_okx_instruments():

    symbols, ranks = (
        get_top30_market_caps()
    )

    if not symbols:

        return []

    all_swaps = (
        get_all_active_usdt_swaps()
    )

    result = []

    for row in all_swaps:

        inst_id = row.get(
            "instId",
            ""
        )

        parts = inst_id.split(
            "-"
        )

        if len(parts) < 3:

            continue

        base = (
            parts[0]
            .upper()
        )

        if base not in symbols:

            continue

        item = dict(
            row
        )

        item["_baseCcy"] = base

        item["_top30_rank"] = (
            ranks.get(
                base,
                999
            )
        )

        result.append(
            item
        )

    result.sort(
        key=lambda x:
            x.get(
                "_top30_rank",
                999
            )
    )

    if TARGET_INST_ID:

        result = [
            x
            for x in result
            if x.get(
                "instId"
            ) == TARGET_INST_ID
        ]

    return result[
        :SCAN_LIMIT
    ]


# ============================================================
# Ticker
# ============================================================

def get_ticker(
    inst_id
):

    rows = okx_request(
        "GET",
        "/api/v5/market/ticker",
        params={
            "instId":
                inst_id
        }
    )

    if not rows:

        raise RuntimeError(
            f"ticker为空: {inst_id}"
        )

    row = rows[0]

    mark = D(
        row.get(
            "markPx"
        )
        or row.get(
            "last"
        )
    )

    last = D(
        row.get(
            "last"
        )
    )

    return {
        "markPx":
            mark,
        "last":
            last,
        "raw":
            row
    }


def get_all_tickers():

    rows = okx_request(
        "GET",
        "/api/v5/market/tickers",
        params={
            "instType":
                INST_TYPE
        }
    )

    result = {}

    for row in rows:

        inst_id = row.get(
            "instId"
        )

        if inst_id:

            result[
                inst_id
            ] = row

    return result


# ============================================================
# K线
# ============================================================

def get_candles(
    inst_id,
    bar=SIGNAL_BAR,
    limit=120
):

    rows = okx_request(
        "GET",
        "/api/v5/market/candles",
        params={
            "instId":
                inst_id,
            "bar":
                bar,
            "limit":
                str(limit)
        }
    )

    candles = []

    for row in rows:

        if len(row) < 9:

            continue

        try:

            candles.append(
                {
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

                    "vol":
                        D(row[5]),

                    "confirm":
                        str(row[8])
                }
            )

        except Exception:

            continue

    candles.sort(
        key=lambda x:
            x["ts"]
    )

    return [
        x
        for x in candles
        if x["confirm"] == "1"
    ]


# ============================================================
# EMA
# ============================================================

def ema(
    values,
    period
):

    values = [
        D(x)
        for x in values
    ]

    if len(values) < period:

        return None

    alpha = (
        Decimal("2")
        / (
            Decimal(period)
            + Decimal("1")
        )
    )

    result = values[0]

    for price in values[1:]:

        result = (
            price * alpha
            + result
            * (
                Decimal("1")
                - alpha
            )
        )

    return result


# ============================================================
# RSI
# ============================================================

def calculate_rsi(
    values,
    period=14
):

    values = [
        D(x)
        for x in values
    ]

    if len(values) < (
        period + 1
    ):

        return None

    gains = []

    losses = []

    for i in range(
        1,
        len(values)
    ):

        change = (
            values[i]
            - values[i - 1]
        )

        if change > 0:

            gains.append(
                change
            )

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
        / Decimal(period)
    )

    avg_loss = (
        sum(
            losses[:period],
            Decimal("0")
        )
        / Decimal(period)
    )

    for i in range(
        period,
        len(gains)
    ):

        avg_gain = (
            avg_gain
            * Decimal(
                period - 1
            )
            + gains[i]
        ) / Decimal(period)

        avg_loss = (
            avg_loss
            * Decimal(
                period - 1
            )
            + losses[i]
        ) / Decimal(period)

    if avg_loss == 0:

        return Decimal("100")

    rs = (
        avg_gain
        / avg_loss
    )

    return (
        Decimal("100")
        - (
            Decimal("100")
            / (
                Decimal("1")
                + rs
            )
        )
    )


# ============================================================
# MACD
# ============================================================

def calculate_macd(
    values
):

    values = [
        D(x)
        for x in values
    ]

    if len(values) < (
        MACD_SLOW
        + MACD_SIGNAL
    ):

        return (
            None,
            None
        )

    macd_values = []

    for i in range(
        MACD_SLOW,
        len(values) + 1
    ):

        subset = values[:i]

        fast = ema(
            subset,
            MACD_FAST
        )

        slow = ema(
            subset,
            MACD_SLOW
        )

        if (
            fast is not None
            and slow is not None
        ):

            macd_values.append(
                fast - slow
            )

    if len(macd_values) < (
        MACD_SIGNAL
    ):

        return (
            None,
            None
        )

    signal = ema(
        macd_values,
        MACD_SIGNAL
    )

    return (
        macd_values[-1],
        signal
    )


# ============================================================
# ATR
# ============================================================

def calculate_atr(
    candles,
    period=14
):

    if len(candles) < (
        period + 1
    ):

        return None

    trs = []

    for i in range(
        1,
        len(candles)
    ):

        current = candles[i]

        previous = candles[
            i - 1
        ]

        high = current[
            "high"
        ]

        low = current[
            "low"
        ]

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

        trs.append(
            tr
        )

    if len(trs) < period:

        return None

    return (
        sum(
            trs[-period:],
            Decimal("0")
        )
        / Decimal(period)
    )


# ============================================================
# 趋势
# ============================================================


def get_higher_trend(inst_id, bar="1H"):
    """
    大周期趋势判断（使用 EMA21 + EMA55）
    返回: "LONG" / "SHORT" / "RANGE"
    """
    candles = get_candles(inst_id, bar, 120)
    if len(candles) < 60:
        return "RANGE"

    closes = [x["close"] for x in candles]
    ema21 = ema(closes, 21)
    ema55 = ema(closes, 55)

    if ema21 is None or ema55 is None:
        return "RANGE"

    price = closes[-1]
    if price <= 0:
        return "RANGE"

    # 计算两条均线的距离百分比
    distance_pct = abs(ema21 - ema55) / price

    # 距离过近视为震荡
    if distance_pct < Decimal("0.004"):  # 0.4%
        return "RANGE"

    if ema21 > ema55:
        return "LONG"
    elif ema21 < ema55:
        return "SHORT"
    else:
        return "RANGE"


def get_trend(inst_id, bar):
    """兼容旧调用，内部转接到新逻辑"""
    return get_higher_trend(inst_id, bar)


def calculate_signal(inst_id):
    """
    权威版信号逻辑 V3：
    1. 1小时定方向（最高优先级）
    2. 震荡市直接放弃
    3. 5分钟等待回调入场（不追涨杀跌）
    """

    # ---------- 1. 大周期方向 ----------
    higher_trend = get_higher_trend(inst_id, "1H")

    if higher_trend == "RANGE":
        return None  # 震荡，不做

    # ---------- 2. 获取5分钟数据 ----------
    candles = get_candles(inst_id, SIGNAL_BAR, 140)
    if len(candles) < 60:
        return None

    closes = [x["close"] for x in candles]
    price = closes[-1]

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    rsi = calculate_rsi(closes, RSI_PERIOD)
    macd_line, macd_signal = calculate_macd(closes)
    atr = calculate_atr(candles, ATR_PERIOD)

    if any(x is None for x in (ema9, ema21, rsi, macd_line, macd_signal, atr)):
        return None

    if price <= 0:
        return None

    atr_pct = atr / price
    if atr_pct < Decimal("0.0015"):
        return None  # 波动太小

    # ---------- 3. 15分钟辅助确认 ----------
    trend_15m = get_higher_trend(inst_id, "15m")

    # ---------- 4. 做多条件（回调入场） ----------
    long_ok = False
    if higher_trend == "LONG":
        # 价格回踩到 EMA21 附近（允许稍微跌破）
        near_ema21 = price <= ema21 * Decimal("1.003") and price >= ema21 * Decimal("0.992")

        # RSI 从低位回升
        rsi_ok = Decimal("38") <= rsi <= Decimal("58")

        # MACD 支持
        macd_ok = macd_line > macd_signal or macd_line > 0

        # 价格重新站上 EMA9
        price_above_ema9 = price > ema9

        # 15分钟尽量同向
        trend15_ok = trend_15m in ("LONG", "RANGE")

        if near_ema21 and rsi_ok and macd_ok and price_above_ema9 and trend15_ok:
            long_ok = True

    # ---------- 5. 做空条件（回调入场） ----------
    short_ok = False
    if higher_trend == "SHORT":
        near_ema21 = price >= ema21 * Decimal("0.997") and price <= ema21 * Decimal("1.008")

        rsi_ok = Decimal("42") <= rsi <= Decimal("62")

        macd_ok = macd_line < macd_signal or macd_line < 0

        price_below_ema9 = price < ema9

        trend15_ok = trend_15m in ("SHORT", "RANGE")

        if near_ema21 and rsi_ok and macd_ok and price_below_ema9 and trend15_ok:
            short_ok = True

    # ---------- 6. 最终决策 ----------
    if long_ok and not short_ok:
        side = "LONG"
        score = 6
    elif short_ok and not long_ok:
        side = "SHORT"
        score = 6
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
        "trend1h": higher_trend
    }


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

        if item.get(
            "ccy"
        ) != "USDT":

            continue

        eq = D(
            item.get(
                "eq"
            )
        )

        if eq > 0:

            return eq

        cash = D(
            item.get(
                "cashBal"
            )
        )

        if cash > 0:

            return cash

        avail = D(
            item.get(
                "availBal"
            )
        )

        if avail > 0:

            return avail

    return Decimal("0")


# ============================================================
# 持仓
# ============================================================

def get_positions(
    inst_id=None
):

    params = {
        "instType":
            INST_TYPE
    }

    if inst_id:

        params[
            "instId"
        ] = inst_id

    rows = okx_request(
        "GET",
        "/api/v5/account/positions",
        params=params,
        private=True
    )

    result = []

    for row in rows:

        pos = D(
            row.get(
                "pos"
            )
        )

        if pos == 0:

            continue

        result.append(
            row
        )

    return result


def get_position(
    inst_id
):

    rows = get_positions(
        inst_id
    )

    if rows:

        return rows[0]

    return None


def position_side(
    pos
):

    amount = D(
        pos.get(
            "pos"
        )
    )

    if amount > 0:

        return "LONG"

    if amount < 0:

        return "SHORT"

    return "NONE"


# ============================================================
# 实时持仓
# ============================================================

def print_live_position(
    pos
):

    inst_id = pos.get(
        "instId"
    )

    ticker = get_ticker(
        inst_id
    )

    mark = ticker[
        "markPx"
    ]

    avg = D(
        pos.get(
            "avgPx"
        )
    )

    qty = abs(
        D(
            pos.get(
                "pos"
            )
        )
    )

    lever = D(
        pos.get(
            "lever"
        )
    )

    upl = D(
        pos.get(
            "upl"
        )
    )

    upl_ratio = D(
        pos.get(
            "uplRatio"
        )
    )

    side = position_side(
        pos
    )

    info = get_instrument(
        inst_id
    )

    ct_val = D(
        info.get(
            "ctVal"
        )
    )

    if avg > 0:

        if side == "LONG":

            price_change = (
                mark - avg
            ) / avg * 100

        else:

            price_change = (
                avg - mark
            ) / avg * 100

    else:

        price_change = Decimal("0")

    local_pnl = Decimal("0")

    if (
        ct_val > 0
        and avg > 0
    ):

        if side == "LONG":

            local_pnl = (
                mark - avg
            ) * qty * ct_val

        elif side == "SHORT":

            local_pnl = (
                avg - mark
            ) * qty * ct_val

    tp = Decimal("0")
    sl = Decimal("0")

    algos = pos.get(
        "closeOrderAlgo"
    )

    if isinstance(
        algos,
        list
    ):

        for algo in algos:

            algo_tp = D(
                algo.get(
                    "tpTriggerPx"
                )
            )

            algo_sl = D(
                algo.get(
                    "slTriggerPx"
                )
            )

            if algo_tp > 0:

                tp = algo_tp

            if algo_sl > 0:

                sl = algo_sl

    tp_distance = Decimal("0")
    sl_distance = Decimal("0")

    if mark > 0:

        if tp > 0:

            tp_distance = (
                abs(
                    tp - mark
                )
                / mark
                * 100
            )

        if sl > 0:

            sl_distance = (
                abs(
                    mark - sl
                )
                / mark
                * 100
            )

    liq_px = D(
        pos.get(
            "liqPx"
        )
    )

    log(
        f"📊 {inst_id} | "
        f"{side} | "
        f"开仓={fmt_decimal(avg)} | "
        f"Mark={fmt_decimal(mark)} | "
        f"杠杆={fmt_decimal(lever)}x | "
        f"数量={fmt_decimal(qty)}"
    )

    log(
        f"💰 浮盈亏={fmt_decimal(upl, 8)} USDT | "
        f"ROI={fmt_decimal(upl_ratio * 100, 4)}% | "
        f"价格变化={fmt_decimal(price_change, 4)}%"
    )

    log(
        f"📐 本地PnL约="
        f"{fmt_decimal(local_pnl, 8)} USDT"
    )

    if tp > 0:

        log(
            f"🎯 TP={fmt_decimal(tp)} | "
            f"距离={fmt_decimal(tp_distance, 4)}%"
        )

    else:

        log(
            "⚠️ 当前没有检测到TP"
        )

    if sl > 0:

        log(
            f"🛑 SL={fmt_decimal(sl)} | "
            f"距离={fmt_decimal(sl_distance, 4)}%"
        )

    else:

        log(
            "⚠️ 当前没有检测到SL"
        )

    if liq_px > 0:

        log(
            f"☠️ 强平价="
            f"{fmt_decimal(liq_px)}"
        )

    return {
        "mark":
            mark,

        "upl":
            upl,

        "roi":
            upl_ratio,

        "tp":
            tp,

        "sl":
            sl
    }


# ============================================================
# 动态杠杆
# ============================================================

def leverage_from_score(
    score
):

    if score <= 4:

        return Decimal("5")

    if score == 5:

        return Decimal("8")

    if score == 6:

        return Decimal("10")

    if score == 7:

        return Decimal("15")

    return Decimal("20")


def get_max_allowed_leverage(
    inst_id
):

    info = get_instrument(
        inst_id
    )

    max_lev = D(
        info.get(
            "lever"
        )
    )

    if max_lev <= 0:

        return MAX_LEVERAGE

    return max_lev


def get_actual_leverage(
    inst_id,
    desired
):

    max_allowed = (
        get_max_allowed_leverage(
            inst_id
        )
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
            f"低于机器人最低杠杆"
        )

    return actual


def set_leverage(
    inst_id,
    leverage
):

    return okx_request(
        "POST",
        "/api/v5/account/set-leverage",
        body={
            "instId":
                inst_id,

            "lever":
                fmt_decimal(
                    leverage
                ),

            "mgnMode":
                TD_MODE
        },
        private=True
    )


# ============================================================
# TP/SL 计算
# ============================================================

def calculate_protection(
    pos,
    atr=None,
    adopted=False
):

    inst_id = pos.get(
        "instId"
    )

    side = position_side(
        pos
    )

    avg_px = D(
        pos.get(
            "avgPx"
        )
    )

    leverage = D(
        pos.get(
            "lever"
        )
    )

    if leverage <= 0:

        leverage = MIN_LEVERAGE

    mark = get_ticker(
        inst_id
    )["markPx"]

    if mark <= 0:

        raise RuntimeError(
            "Mark Price无效"
        )

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
            "ATR计算失败"
        )

    atr_pct = (
        atr / mark
    )

    atr_pct = clamp(
        atr_pct,
        Decimal("0.0005"),
        Decimal("0.08")
    )

    target_sl_pct = (
        TARGET_SL_ROI_PCT
        / leverage
        / 100
    )

    target_tp_pct = (
        TARGET_TP_ROI_PCT
        / leverage
        / 100
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

    base = avg_px

    if base <= 0:

        base = mark

    # 已有仓位重新保护时：
    # 如果已经盈利，则以当前Mark重新计算
    if adopted:

        if (
            side == "LONG"
            and mark > avg_px
        ):

            base = mark

        elif (
            side == "SHORT"
            and mark < avg_px
        ):

            base = mark

    if side == "LONG":

        tp = (
            base
            * (
                Decimal("1")
                + tp_pct
            )
        )

        sl = (
            base
            * (
                Decimal("1")
                - sl_pct
            )
        )

        if tp <= mark:

            tp = (
                mark
                * (
                    Decimal("1")
                    + MIN_TRIGGER_GAP_PCT
                )
            )

        if sl >= mark:

            sl = (
                mark
                * (
                    Decimal("1")
                    - MIN_TRIGGER_GAP_PCT
                )
            )

    elif side == "SHORT":

        tp = (
            base
            * (
                Decimal("1")
                - tp_pct
            )
        )

        sl = (
            base
            * (
                Decimal("1")
                + sl_pct
            )
        )

        if tp >= mark:

            tp = (
                mark
                * (
                    Decimal("1")
                    - MIN_TRIGGER_GAP_PCT
                )
            )

        if sl <= mark:

            sl = (
                mark
                * (
                    Decimal("1")
                    + MIN_TRIGGER_GAP_PCT
                )
            )

    else:

        raise RuntimeError(
            "无法识别仓位方向"
        )

    info = get_instrument(
        inst_id
    )

    tick = D(
        info.get(
            "tickSz"
        )
    )

    if tick > 0:

        if side == "LONG":

            tp = round_up(
                tp,
                tick
            )

            sl = round_down(
                sl,
                tick
            )

        else:

            tp = round_down(
                tp,
                tick
            )

            sl = round_up(
                sl,
                tick
            )

    return {
        "instId":
            inst_id,

        "side":
            side,

        "base":
            base,

        "mark":
            mark,

        "atr":
            atr,

        "atr_pct":
            atr_pct,

        "sl_pct":
            sl_pct,

        "tp_pct":
            tp_pct,

        "tp":
            tp,

        "sl":
            sl,

        "leverage":
            leverage
    }


# ============================================================
# 数量计算
# ============================================================


def calculate_size(
    equity,
    price,
    leverage,
    sl_pct,
    inst_id
):
    """
    按账户权益的 USDT 百分比来计算仓位，而不是直接按代币数量乱下。
    返回值仍然是 OKX 下单需要的合约数量 (sz)。
    """

    info = get_instrument(inst_id)

    ct_val = D(info.get("ctVal"))
    lot_sz = D(info.get("lotSz"))
    min_sz = D(info.get("minSz"))

    if ct_val <= 0:
        raise RuntimeError("ctVal无效")
    if lot_sz <= 0:
        raise RuntimeError("lotSz无效")
    if min_sz <= 0:
        min_sz = lot_sz

    # ---------- 1. 先按 USDT 决定仓位 ----------
    # 最大可用保证金（账户权益的百分比）
    max_margin_usdt = equity * MAX_MARGIN_PCT

    # 按风险预算反推（如果止损被打，最多亏 RISK_PER_TRADE_PCT 的权益）
    # 风险金额 = 名义价值 * sl_pct
    # 名义价值 = 保证金 * 杠杆
    # 所以：风险金额 = 保证金 * 杠杆 * sl_pct
    # 保证金 = 风险金额 / (杠杆 * sl_pct)
    risk_budget_usdt = equity * RISK_PER_TRADE_PCT

    if sl_pct <= 0:
        raise RuntimeError("止损比例无效")

    margin_by_risk = risk_budget_usdt / (leverage * sl_pct)

    # 取更保守的那个
    target_margin_usdt = min(max_margin_usdt, margin_by_risk)

    # 硬性上限：单笔名义价值不超过账户权益的 25%
    max_notional_usdt = equity * Decimal("0.25")
    margin_by_notional = max_notional_usdt / leverage
    target_margin_usdt = min(target_margin_usdt, margin_by_notional)

    if target_margin_usdt <= 0:
        raise RuntimeError("计算出的目标保证金无效")

    # ---------- 2. 把 USDT 保证金转换成合约数量 ----------
    # 名义价值 = 保证金 * 杠杆
    target_notional_usdt = target_margin_usdt * leverage

    # 合约数量 sz = 名义价值 / (价格 * ctVal)
    qty = target_notional_usdt / (price * ct_val)

    qty = round_down(qty, lot_sz)

    if qty < min_sz:
        # 最小下单量都超过我们的限制，就放弃
        required_margin = (min_sz * price * ct_val) / leverage
        if required_margin > max_margin_usdt:
            raise RuntimeError(
                f"{inst_id} 最小下单量所需保证金 "
                f"{fmt_decimal(required_margin, 2)} USDT "
                f"已超过限制 {fmt_decimal(max_margin_usdt, 2)} USDT"
            )
        qty = min_sz

    # 最终实际会用到的保证金和名义价值（用于日志）
    final_notional = qty * price * ct_val
    final_margin = final_notional / leverage

    log(
        f"📏 仓位计算 | "
        f"目标保证金={fmt_decimal(target_margin_usdt, 2)}U | "
        f"实际保证金≈{fmt_decimal(final_margin, 2)}U | "
        f"名义价值≈{fmt_decimal(final_notional, 2)}U | "
        f"数量={fmt_decimal(qty)}"
    )

    return qty


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
        "instId":
            inst_id,

        "tdMode":
            TD_MODE,

        "side":
            order_side,

        "posSide":
            "net",

        "ordType":
            "market",

        "sz":
            fmt_decimal(qty)
    }

    rows = okx_request(
        "POST",
        "/api/v5/trade/order",
        body=body,
        private=True
    )

    if not rows:

        raise RuntimeError(
            "开仓接口没有返回"
        )

    return rows[0]


# ============================================================
# 查询订单
# ============================================================

def get_order(
    inst_id,
    ord_id
):

    rows = okx_request(
        "GET",
        "/api/v5/trade/order",
        params={
            "instId":
                inst_id,

            "ordId":
                ord_id
        },
        private=True
    )

    if not rows:

        return None

    return rows[0]


def wait_order_filled(
    inst_id,
    ord_id,
    timeout=20
):

    start = time.time()

    while (
        time.time() - start
        < timeout
    ):

        order = get_order(
            inst_id,
            ord_id
        )

        if not order:

            time.sleep(
                0.5
            )

            continue

        state = order.get(
            "state"
        )

        if state == "filled":

            return order

        if state in (
            "canceled",
            "mmp_canceled"
        ):

            raise RuntimeError(
                f"订单未成交，状态={state}"
            )

        time.sleep(
            0.5
        )

    raise RuntimeError(
        "等待订单成交超时"
    )


# ============================================================
# 查询挂单保护
# ============================================================

def get_pending_algos(
    inst_id,
    ord_type="oco"
):

    return okx_request(
        "GET",
        "/api/v5/trade/orders-algo-pending",
        params={
            "ordType":
                ord_type,

            "instId":
                inst_id
        },
        private=True
    )


# ============================================================
# 取消Algo
# ============================================================

def cancel_algo(
    inst_id,
    algo_id
):

    if not algo_id:

        return

    try:

        okx_request(
            "POST",
            "/api/v5/trade/cancel-algos",
            body=[
                {
                    "instId":
                        inst_id,

                    "algoId":
                        str(algo_id)
                }
            ],
            private=True
        )

    except Exception as e:

        log(
            f"⚠️ 取消保护单失败: {e}"
        )


def cancel_existing_algos(
    inst_id
):

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
                        f"♻️ 取消旧"
                        f"{ord_type}保护单 "
                        f"{algo_id}"
                    )

                    cancel_algo(
                        inst_id,
                        algo_id
                    )

        except Exception as e:

            log(
                f"⚠️ 查询{ord_type}"
                f"保护单失败: {e}"
            )


# ============================================================
# 提交OCO
# ============================================================

def place_oco(
    pos,
    plan
):

    inst_id = pos.get(
        "instId"
    )

    side = position_side(
        pos
    )

    if side == "LONG":

        close_side = "sell"

    elif side == "SHORT":

        close_side = "buy"

    else:

        raise RuntimeError(
            "OCO方向错误"
        )

    body = {
        "instId":
            inst_id,

        "tdMode":
            TD_MODE,

        "side":
            close_side,

        "posSide":
            "net",

        "ordType":
            "oco",

        "closeFraction":
            "1",

        "reduceOnly":
            True,

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
            "mark"
    }

    log(
        f"📌 提交OCO | "
        f"{inst_id} | "
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
            "OCO没有返回"
        )

    algo_id = rows[0].get(
        "algoId"
    )

    if not algo_id:

        raise RuntimeError(
            f"OCO没有algoId: {rows}"
        )

    log(
        f"✅ OCO提交成功 "
        f"algoId={algo_id}"
    )

    return rows[0]


# ============================================================
# 验证OCO
# ============================================================

def verify_oco(
    pos
):

    inst_id = pos.get(
        "instId"
    )

    side = position_side(
        pos
    )

    if side not in (
        "LONG",
        "SHORT"
    ):

        return False

    rows = get_pending_algos(
        inst_id,
        "oco"
    )

    if not rows:

        return False

    mark = get_ticker(
        inst_id
    )["markPx"]

    for row in rows:

        state = row.get(
            "state",
            ""
        )

        if state not in (
            "",
            "live",
            "effective",
            "partially_effective"
        ):

            continue

        tp = D(
            row.get(
                "tpTriggerPx"
            )
        )

        sl = D(
            row.get(
                "slTriggerPx"
            )
        )

        if (
            tp <= 0
            or sl <= 0
        ):

            continue

        if side == "LONG":

            valid = (
                tp > mark
                and sl < mark
            )

        else:

            valid = (
                tp < mark
                and sl > mark
            )

        if not valid:

            continue

        log(
            f"🟢 OCO验证成功 | "
            f"Mark={fmt_decimal(mark)} | "
            f"TP={fmt_decimal(tp)} | "
            f"SL={fmt_decimal(sl)}"
        )

        return True

    return False


# ============================================================
# 紧急平仓
# ============================================================

def emergency_close(
    inst_id
):

    log(
        f"🚨 启动紧急平仓: {inst_id}"
    )

    try:

        okx_request(
            "POST",
            "/api/v5/trade/close-position",
            body={
                "instId":
                    inst_id,

                "mgnMode":
                    TD_MODE
            },
            private=True
        )

        log(
            "🚨 紧急平仓请求已发送"
        )

    except Exception as e:

        log(
            f"❌ 紧急平仓失败: {e}"
        )


# ============================================================
# 确保TP/SL
# ============================================================

def ensure_protection(
    pos,
    adopted=False,
    new_position=False,
    atr=None
):

    inst_id = pos.get(
        "instId"
    )

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
                    f"⚠️ {inst_id}已经没有持仓"
                )

                return True

            # 每次都读取最新Mark
            mark = get_ticker(
                inst_id
            )["markPx"]

            log(
                f"🔄 TP/SL检查 "
                f"{attempt}/{OCO_RETRY_COUNT} | "
                f"Mark={fmt_decimal(mark)}"
            )

            # 先检查是否已有有效保护
            try:

                if verify_oco(
                    current_pos
                ):

                    log(
                        "🟢 当前OCO已经有效"
                    )

                    return True

            except Exception as e:

                log(
                    f"⚠️ OCO检查异常: {e}"
                )

            # 没有有效OCO
            cancel_existing_algos(
                inst_id
            )

            time.sleep(
                0.3
            )

            fresh_candles = get_candles(
                inst_id,
                SIGNAL_BAR,
                100
            )

            fresh_atr = calculate_atr(
                fresh_candles,
                ATR_PERIOD
            )

            if fresh_atr is None:

                raise RuntimeError(
                    "ATR计算失败"
                )

            plan = calculate_protection(
                current_pos,
                atr=(
                    atr
                    if atr is not None
                    else fresh_atr
                ),
                adopted=adopted
            )

            log(
                f"🎯 保护计划 | "
                f"TP={fmt_decimal(plan['tp'])} | "
                f"SL={fmt_decimal(plan['sl'])} | "
                f"TP距离={fmt_decimal(plan['tp_pct'] * 100, 4)}% | "
                f"SL距离={fmt_decimal(plan['sl_pct'] * 100, 4)}%"
            )

            place_oco(
                current_pos,
                plan
            )

            time.sleep(
                OCO_VERIFY_WAIT
            )

            verify_position = get_position(
                inst_id
            )

            if not verify_position:

                log(
                    "ℹ️ OCO提交后仓位已经不存在"
                )

                return True

            if verify_oco(
                verify_position
            ):

                log(
                    f"🟢 TP/SL正式生效: "
                    f"{inst_id}"
                )

                return True

            log(
                f"⚠️ 第{attempt}次OCO验证失败"
            )

        except Exception as e:

            log(
                f"❌ 第{attempt}次TP/SL失败: {e}"
            )

        time.sleep(
            1
        )

    log(
        f"❌ {inst_id} TP/SL连续失败"
    )

    if (
        new_position
        and
        EMERGENCY_CLOSE_ON_NEW_POSITION_PROTECTION_FAILURE
    ):

        emergency_close(
            inst_id
        )

    return False


# ============================================================
# 历史平仓
# ============================================================

def get_position_history(
    inst_id
):

    return okx_request(
        "GET",
        "/api/v5/account/positions-history",
        params={
            "instType":
                "SWAP",

            "instId":
                inst_id,

            "limit":
                "20"
        },
        private=True
    )


def get_latest_closed_result(
    inst_id
):

    rows = get_position_history(
        inst_id
    )

    if not rows:

        return None

    rows.sort(
        key=lambda x:
            D(
                x.get(
                    "uTime"
                )
            ),
        reverse=True
    )

    for row in rows:

        if D(
            row.get(
                "closeTotalPos"
            )
        ) > 0:

            return row

    return rows[0]


def print_closed_position_result(
    inst_id
):

    try:

        row = get_latest_closed_result(
            inst_id
        )

        if not row:

            log(
                f"⚠️ 没找到 "
                f"{inst_id}历史平仓"
            )

            return None

        direction = row.get(
            "direction",
            ""
        )

        open_px = D(
            row.get(
                "openAvgPx"
            )
        )

        close_px = D(
            row.get(
                "closeAvgPx"
            )
        )

        pnl = D(
            row.get(
                "pnl"
            )
        )

        fee = D(
            row.get(
                "fee"
            )
        )

        funding = D(
            row.get(
                "fundingFee"
            )
        )

        realized = D(
            row.get(
                "realizedPnl"
            )
        )

        pnl_ratio = D(
            row.get(
                "pnlRatio"
            )
        )

        log(
            "━━━━━━━━━━━━━━━━━━━━━━"
        )

        log(
            f"🏁 {inst_id} 平仓结果"
        )

        log(
            f"方向: {direction}"
        )

        log(
            f"开仓均价: "
            f"{fmt_decimal(open_px)}"
        )

        log(
            f"平仓均价: "
            f"{fmt_decimal(close_px)}"
        )

        log(
            f"价格盈亏: "
            f"{fmt_decimal(pnl, 8)} USDT"
        )

        log(
            f"手续费: "
            f"{fmt_decimal(fee, 8)} USDT"
        )

        log(
            f"资金费: "
            f"{fmt_decimal(funding, 8)} USDT"
        )

        log(
            f"最终realizedPnl: "
            f"{fmt_decimal(realized, 8)} USDT"
        )

        log(
            f"OKX记录ROI: "
            f"{fmt_decimal(pnl_ratio * 100, 4)}%"
        )

        if realized > 0:

            log(
                "🟢 本次交易盈利"
            )

        elif realized < 0:

            log(
                "🔴 本次交易亏损"
            )

        else:

            log(
                "⚪ 本次交易基本持平"
            )

        log(
            "━━━━━━━━━━━━━━━━━━━━━━"
        )

        return {
            "realizedPnl":
                realized,

            "pnl":
                pnl,

            "fee":
                fee,

            "fundingFee":
                funding,

            "pnlRatio":
                pnl_ratio
        }

    except Exception as e:

        log(
            f"⚠️ 获取历史平仓结果失败: {e}"
        )

        return None


# ============================================================
# 连续亏损状态
# ============================================================

consecutive_losses = 0

last_loss_time = 0.0


# ============================================================
# 更新交易结果
# ============================================================

def update_trade_result(
    result
):

    global consecutive_losses
    global last_loss_time

    if not result:

        return

    realized = D(
        result.get(
            "realizedPnl"
        )
    )

    if realized < 0:

        consecutive_losses += 1

        last_loss_time = time.time()

        log(
            f"🔴 连续亏损 "
            f"{consecutive_losses}/"
            f"{MAX_CONSECUTIVE_LOSSES}"
        )

    elif realized > 0:

        consecutive_losses = 0

        last_loss_time = 0.0

        log(
            "🟢 本次盈利，"
            "连续亏损计数归零"
        )


# ============================================================
# 连续亏损冷却
# ============================================================

def in_loss_cooldown():

    global consecutive_losses
    global last_loss_time

    if (
        consecutive_losses
        < MAX_CONSECUTIVE_LOSSES
    ):

        return False

    elapsed = (
        time.time()
        - last_loss_time
    )

    if (
        elapsed
        < LOSS_COOLDOWN_SECONDS
    ):

        remaining = (
            LOSS_COOLDOWN_SECONDS
            - elapsed
        )

        log(
            f"🛑 连续亏损保护中 | "
            f"剩余约{int(remaining)}秒"
        )

        return True

    # 冷却结束
    consecutive_losses = 0

    last_loss_time = 0.0

    log(
        "🟢 连续亏损冷却结束"
    )

    return False


# ============================================================
# 持仓快照
# ============================================================

previous_positions = {}


def detect_closed_positions(
    current_positions
):

    global previous_positions

    current_map = {}

    for pos in current_positions:

        inst_id = pos.get(
            "instId"
        )

        if inst_id:

            current_map[
                inst_id
            ] = pos

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
            f"📕 检测到仓位结束: "
            f"{inst_id}"
        )

        result = (
            print_closed_position_result(
                inst_id
            )
        )

        update_trade_result(
            result
        )

        previous_positions.pop(
            inst_id,
            None
        )

    previous_positions = (
        current_map
    )


# ============================================================
# 管理已有仓位
# ============================================================

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

        try:

            print_live_position(
                pos
            )

        except Exception as e:

            log(
                f"⚠️ 实时持仓显示失败: {e}"
            )

        try:

            ensure_protection(
                pos,
                adopted=True,
                new_position=False
            )

        except Exception as e:

            log(
                f"⚠️ TP/SL管理异常: {e}"
            )

    return True


# ============================================================
# 判断是否可以新开仓
# ============================================================

def can_open_new_trade():

    positions = get_positions()

    return (
        len(positions)
        == 0
    )


# ============================================================
# 执行交易
# ============================================================

def execute_candidate(
    candidate
):

    inst_id = candidate[
        "instId"
    ]

    side = candidate[
        "side"
    ]

    score = candidate[
        "score"
    ]

    atr = candidate[
        "atr"
    ]

    log(
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

    log(
        f"🚀 准备开仓 | "
        f"{inst_id} | "
        f"{side} | "
        f"Score={score}"
    )

    if not can_open_new_trade():

        log(
            "⛔ 已经存在持仓"
        )

        return False

    # 动态杠杆
    desired = (
        leverage_from_score(
            score
        )
    )

    leverage = (
        get_actual_leverage(
            inst_id,
            desired
        )
    )

    if leverage != desired:

        log(
            f"ℹ️ 合约最大杠杆限制 | "
            f"计划={fmt_decimal(desired)}x | "
            f"实际={fmt_decimal(leverage)}x"
        )

    else:

        log(
            f"⚙️ 动态杠杆="
            f"{fmt_decimal(leverage)}x"
        )

    set_leverage(
        inst_id,
        leverage
    )

    log(
        "✅ 杠杆设置成功"
    )

    # 最新Mark
    price = get_ticker(
        inst_id
    )["markPx"]

    atr_pct = (
        atr / price
    )

    target_sl_pct = (
        TARGET_SL_ROI_PCT
        / leverage
        / 100
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

    equity = get_equity()

    if equity <= 0:

        raise RuntimeError(
            "账户权益无效"
        )

    qty = calculate_size(
        equity,
        price,
        leverage,
        sl_pct,
        inst_id
    )

    log(
        f"💰 权益="
        f"{fmt_decimal(equity, 4)} USDT"
    )

    log(
        f"📦 下单数量="
        f"{fmt_decimal(qty)}"
    )

    log(
        f"🛑 初始SL距离="
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
            f"没有ordId: {order}"
        )

    log(
        f"📨 开仓订单="
        f"{ord_id}"
    )

    filled = wait_order_filled(
        inst_id,
        ord_id
    )

    fill_px = D(
        filled.get(
            "avgPx"
        )
    )

    fill_sz = D(
        filled.get(
            "accFillSz"
        )
        or filled.get(
            "fillSz"
        )
    )

    log(
        f"✅ 开仓成交 | "
        f"价格={fmt_decimal(fill_px)} | "
        f"数量={fmt_decimal(fill_sz)}"
    )

    # 等待真实持仓
    position = None

    for _ in range(10):

        position = get_position(
            inst_id
        )

        if position:

            break

        time.sleep(
            0.5
        )

    if not position:

        raise RuntimeError(
            "成交后没有读取到持仓"
        )

    # 实时显示
    print_live_position(
        position
    )

    # 立即保护
    protected = (
        ensure_protection(
            position,
            adopted=False,
            new_position=True,
            atr=atr
        )
    )

    if not protected:

        log(
            "🚨 新仓TP/SL保护失败"
        )

        return False

    log(
        f"🟢 {inst_id} "
        f"开仓+TP/SL全部完成"
    )

    log(
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

    return True


# ============================================================
# 扫描
# ============================================================

def scan_candidates():

    instruments = (
        get_top30_okx_instruments()
    )

    if not instruments:

        log(
            "⚠️ 当前没有Top30可扫描品种"
        )

        return []

    tickers = (
        get_all_tickers()
    )

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

        volume = D(
            ticker.get(
                "volCcy24h"
            )
        )

        if volume <= 0:

            continue

        try:

            signal = (
                calculate_signal(
                    inst_id
                )
            )

            if not signal:

                continue

            signal[
                "volume"
            ] = volume

            signal[
                "rank"
            ] = info.get(
                "_top30_rank",
                999
            )

            candidates.append(
                signal
            )

        except Exception as e:

            log(
                f"⚠️ {inst_id} "
                f"信号计算失败: {e}"
            )

    candidates.sort(
        key=lambda x: (
            -x["score"],
            -x["volume"],
            x["rank"]
        )
    )

    return candidates


# ============================================================
# 打印候选
# ============================================================

def print_candidates(
    candidates
):

    if not candidates:

        log(
            "🔎 没有符合条件的交易信号"
        )

        return

    log(
        f"🔎 找到 "
        f"{len(candidates)} 个候选"
    )

    for index, item in enumerate(
        candidates[:10],
        start=1
    ):

        log(
            f"{index}. "
            f"{item['instId']} | "
            f"{item['side']} | "
            f"Score={item['score']} | "
            f"RSI={fmt_decimal(item['rsi'], 2)} | "
            f"15m={item['trend15m']} | "
            f"1H={item['trend1h']}"
        )


# ============================================================
# 主程序
# ============================================================

def main():

    global previous_positions

    log(
        "=========================================="
    )

    log(
        f"🚀 OKX永续自动交易模拟盘 {VERSION}"
    )

    log(
        "📌 模式：模拟盘"
    )

    log(
        "📌 新开仓：CoinGecko市值Top30"
    )

    log(
        "📌 已有仓位：持续管理，不受Top30限制"
    )

    log(
        f"📌 杠杆："
        f"{fmt_decimal(MIN_LEVERAGE)}x ~ "
        f"{fmt_decimal(MAX_LEVERAGE)}x"
    )

    log(
        "📌 策略：EMA + RSI + MACD + ATR"
    )

    log(
        "📌 周期：5m + 15m + 1H"
    )

    log(
        "📌 TP/SL：OCO + Mark Price"
    )

    log(
        "📌 同时最多一个持仓"
    )

    log(
        "📌 连续亏损保护："
        f"{MAX_CONSECUTIVE_LOSSES}次"
    )

    log(
        "📌 日志时间：UTC+8"
    )

    log(
        "=========================================="
    )

    # ========================================================
    # 启动时检查持仓
    # ========================================================

    try:

        positions = get_positions()

        previous_positions = {
            p.get("instId"):
                p
            for p in positions
            if p.get("instId")
        }

        if positions:

            log(
                f"⚠️ 启动时检测到 "
                f"{len(positions)} 个持仓"
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

    # ========================================================
    # 主循环
    # ========================================================

    while True:

        try:

            # ------------------------------------------------
            # 第一优先级：已有持仓
            # ------------------------------------------------

            has_position = (
                manage_existing_positions()
            )

            if has_position:

                time.sleep(
                    LOOP_SECONDS
                )

                continue

            # ------------------------------------------------
            # 连续亏损保护
            # ------------------------------------------------

            if in_loss_cooldown():

                time.sleep(
                    LOOP_SECONDS
                )

                continue

            # ------------------------------------------------
            # 账户
            # ------------------------------------------------

            equity = get_equity()

            log(
                f"💰 当前权益="
                f"{fmt_decimal(equity, 4)} USDT"
            )

            if equity <= 0:

                log(
                    "⚠️ 账户权益无效"
                )

                time.sleep(
                    LOOP_SECONDS
                )

                continue

            # ------------------------------------------------
            # 扫描
            # ------------------------------------------------

            candidates = (
                scan_candidates()
            )

            print_candidates(
                candidates
            )

            if not candidates:

                time.sleep(
                    LOOP_SECONDS
                )

                continue

            # ------------------------------------------------
            # 选择最强信号
            # ------------------------------------------------

            candidate = (
                candidates[0]
            )

            log(
                f"🎯 最终候选 | "
                f"{candidate['instId']} | "
                f"{candidate['side']} | "
                f"Score={candidate['score']}"
            )

            # ------------------------------------------------
            # 执行
            # ------------------------------------------------

            execute_candidate(
                candidate
            )

            time.sleep(
                LOOP_SECONDS
            )

        except KeyboardInterrupt:

            log(
                "🛑 机器人停止"
            )

            break

        except Exception as e:

            log(
                f"❌ 主循环异常: {e}"
            )

            time.sleep(
                LOOP_SECONDS
            )


# ============================================================
# 启动
# ============================================================

if __name__ == "__main__":

    main()