# ============================================================
# OKX 永续自动交易模拟盘 V2.8
# ============================================================
#
# V2.8 本次升级：
# 1. 新开仓仅允许 CoinGecko 市值 Top30 对应的 OKX USDT 永续
# 2. 已有仓位不受 Top30 限制，继续管理
# 3. 持仓管理实时获取 OKX Mark Price
# 4. 实时显示浮盈/浮亏、ROI、TP、SL、距离 TP/SL
# 5. 自动发现已有仓位并补 TP/SL
# 6. ATR 异常保护，避免出现负数 TP/SL
# 7. TP/SL 主要按照目标仓位 ROI + 杠杆计算
# 8. ATR 只用于防止止损过近，并设置最大距离
# 9. OCO 失败自动刷新价格并重算，最多重试
# 10. 新仓保护最终失败才紧急平仓
# 11. 已有仓保护失败不立即强平，下一轮继续修复
# 12. 平仓后读取 OKX 历史持仓记录实际 PnL / Fee / Funding / ROI
# 13. 所有日志时间统一 UTC+8
# 14. 模拟盘
#
# 依赖：
# requests
# pandas
#
# GitHub Actions:
# pip install requests pandas
#
# 环境变量：
# OKX_API_KEY
# OKX_SECRET_KEY
# OKX_PASSPHRASE
#
# ============================================================

import os
import time
import json
import hmac
import base64
import hashlib
import traceback
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd


# ============================================================
# 基础配置
# ============================================================

VERSION = "V2.8"

BASE_URL = "https://www.okx.com"

INST_TYPE = "SWAP"
SETTLE_CCY = "USDT"

TD_MODE = "cross"
POS_MODE = "net"

SIMULATED_TRADING = "1"

BAR = "5m"
BAR_15M = "15m"
BAR_1H = "1H"

LOOP_SECONDS = 20

# ============================================================
# 市值 Top30
# ============================================================

TOP_N = 30

COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/coins/markets"
)

TOP30_CACHE_SECONDS = 600

top30_cache = {
    "timestamp": 0,
    "symbols": set(),
    "rank_map": {},
}


# ============================================================
# 扫描
# ============================================================

MIN_SIGNAL_SCORE = 4

MIN_24H_VOLUME_USD = 3_000_000

MIN_ATR_PCT = 0.0015

SCAN_LIMIT = 30


# ============================================================
# 风控
# ============================================================

MAX_MARGIN_PCT = 0.08

RISK_PER_TRADE_PCT = 0.005

MIN_LEVERAGE = 5

MAX_LEVERAGE = 15

MAX_CONSECUTIVE_LOSSES = 3

LOSS_COOLDOWN_SECONDS = 1800


# ============================================================
# TP / SL
# ============================================================

# 目标是“保证金收益率”，不是直接把价格乘杠杆。

TARGET_SL_ROI_PCT = 1.5

TARGET_TP_ROI_PCT = 3.5

RISK_REWARD = 1.8

# ATR 只负责避免止损距离太近。

ATR_SL_FRACTION = 0.60

# 最小价格距离。
MIN_SL_PRICE_PCT = 0.0025

# 最大止损价格距离。
MAX_SL_PRICE_PCT = 0.025

# 最大止盈价格距离。
MAX_TP_PRICE_PCT = 0.045

# OCO 距离当前 Mark 的最小安全间隔。
MIN_TRIGGER_GAP_PCT = 0.0010


# ============================================================
# OCO
# ============================================================

OCO_RETRIES = 3

OCO_VERIFY_WAIT = 1.2

EMERGENCY_CLOSE_IF_NEW_OCO_FAIL = True


# ============================================================
# 时间
# ============================================================

UTC8 = timezone(timedelta(hours=8))


def now_str():
    return datetime.now(UTC8).strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print(f"[{now_str()}] {msg}", flush=True)


# ============================================================
# API
# ============================================================

API_KEY = os.getenv("OKX_API_KEY", "").strip()
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "").strip()
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "").strip()


if not API_KEY or not SECRET_KEY or not PASSPHRASE:
    raise RuntimeError(
        "缺少 OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE"
    )


session = requests.Session()

session.headers.update({
    "Content-Type": "application/json",
    "x-simulated-trading": SIMULATED_TRADING,
})


# ============================================================
# Decimal 工具
# ============================================================

def D(x):
    return Decimal(str(x))


def fmt_decimal(x, digits=12):
    try:
        q = D("1").scaleb(-digits)
        return str(D(x).quantize(q).normalize())
    except Exception:
        return str(x)


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
    return max(low, min(high, value))


# ============================================================
# OKX 签名
# ============================================================

def okx_timestamp():
    return datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def okx_request(
    method,
    path,
    params=None,
    body=None,
    private=False,
):
    method = method.upper()

    params = params or {}

    if body is None:
        body_text = ""
    else:
        body_text = json.dumps(
            body,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    query = ""

    if params:
        parts = []

        for k, v in params.items():
            if v is None:
                continue

            parts.append(f"{k}={v}")

        if parts:
            query = "?" + "&".join(parts)

    request_path = path + query

    headers = {}

    if private:
        ts = okx_timestamp()

        prehash = (
            ts
            + method
            + request_path
            + body_text
        )

        sign = base64.b64encode(
            hmac.new(
                SECRET_KEY.encode(),
                prehash.encode(),
                hashlib.sha256,
            ).digest()
        ).decode()

        headers.update({
            "OK-ACCESS-KEY": API_KEY,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        })

    url = BASE_URL + request_path

    try:
        if method == "GET":
            r = session.get(
                url,
                headers=headers,
                timeout=15,
            )
        else:
            r = session.request(
                method,
                url,
                headers=headers,
                data=body_text,
                timeout=15,
            )

        r.raise_for_status()

        data = r.json()

        if data.get("code") != "0":
            raise RuntimeError(
                f"OKX code={data.get('code')} "
                f"msg={data.get('msg')}"
            )

        return data

    except Exception as e:
        log(f"❌ API请求失败 {method} {path}: {e}")
        raise


# ============================================================
# CoinGecko Top30
# ============================================================

def get_top30_market_caps(force=False):

    global top30_cache

    now = time.time()

    if (
        not force
        and top30_cache["symbols"]
        and now - top30_cache["timestamp"]
        < TOP30_CACHE_SECONDS
    ):
        return (
            top30_cache["symbols"],
            top30_cache["rank_map"],
        )

    params = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": TOP_N,
        "page": 1,
        "sparkline": "false",
    }

    try:
        r = requests.get(
            COINGECKO_URL,
            params=params,
            timeout=12,
            headers={
                "User-Agent": "OKX-Bot-V2.8"
            },
        )

        r.raise_for_status()

        data = r.json()

        if not isinstance(data, list) or not data:
            raise RuntimeError(
                "CoinGecko Top30 返回为空"
            )

        symbols = set()
        rank_map = {}

        for item in data[:TOP_N]:

            symbol = str(
                item.get("symbol", "")
            ).upper().strip()

            rank = item.get("market_cap_rank")

            if not symbol:
                continue

            symbols.add(symbol)

            if rank is not None:
                rank_map[symbol] = rank

        if not symbols:
            raise RuntimeError(
                "CoinGecko Top30 没有有效 symbol"
            )

        top30_cache = {
            "timestamp": now,
            "symbols": symbols,
            "rank_map": rank_map,
        }

        top_text = ", ".join(
            f"{s}(#{rank_map.get(s, '?')})"
            for s in sorted(
                symbols,
                key=lambda x: rank_map.get(x, 9999),
            )
        )

        log(f"🌐 CoinGecko 市值Top30: {top_text}")

        return symbols, rank_map

    except Exception as e:
        log(f"❌ 获取市值Top30失败: {e}")

        # 绝不因为 CoinGecko 失败而退回全币种交易。
        return set(), {}


# ============================================================
# OKX 合约信息
# ============================================================

instrument_cache = {}


def get_instrument(inst_id):

    if inst_id in instrument_cache:
        return instrument_cache[inst_id]

    data = okx_request(
        "GET",
        "/api/v5/public/instruments",
        params={
            "instType": INST_TYPE,
            "instId": inst_id,
        },
    )

    rows = data.get("data", [])

    if not rows:
        raise RuntimeError(
            f"找不到合约信息: {inst_id}"
        )

    item = rows[0]

    instrument_cache[inst_id] = item

    return item


def get_all_active_usdt_swaps():

    data = okx_request(
        "GET",
        "/api/v5/public/instruments",
        params={
            "instType": INST_TYPE,
            "uly": "",
        },
    )

    result = []

    for x in data.get("data", []):

        if x.get("settleCcy") != SETTLE_CCY:
            continue

        if x.get("state") != "live":
            continue

        inst_id = x.get("instId", "")

        if not inst_id.endswith(
            "-USDT-SWAP"
        ):
            continue

        result.append(x)

        instrument_cache[inst_id] = x

    return result


# ============================================================
# Top30 对应 OKX 合约
# ============================================================

def get_top30_okx_instruments():

    top_symbols, rank_map = get_top30_market_caps()

    if not top_symbols:
        return []

    instruments = get_all_active_usdt_swaps()

    result = []

    for item in instruments:

        base = str(
            item.get("baseCcy", "")
        ).upper()

        if base in top_symbols:

            item["_marketCapRank"] = rank_map.get(
                base,
                9999,
            )

            result.append(item)

    result.sort(
        key=lambda x: x.get(
            "_marketCapRank",
            9999,
        )
    )

    log(
        f"🔎 OKX可交易市值Top30合约: "
        f"{len(result)}个"
    )

    return result


# ============================================================
# Ticker
# ============================================================

def get_ticker(inst_id):

    data = okx_request(
        "GET",
        "/api/v5/market/ticker",
        params={
            "instId": inst_id,
        },
    )

    rows = data.get("data", [])

    if not rows:
        raise RuntimeError(
            f"Ticker为空: {inst_id}"
        )

    return rows[0]


def get_tickers():

    data = okx_request(
        "GET",
        "/api/v5/market/tickers",
        params={
            "instType": INST_TYPE,
        },
    )

    return data.get("data", [])


# ============================================================
# K线
# ============================================================

def get_candles(
    inst_id,
    bar=BAR,
    limit=120,
):

    data = okx_request(
        "GET",
        "/api/v5/market/candles",
        params={
            "instId": inst_id,
            "bar": bar,
            "limit": limit,
        },
    )

    rows = data.get("data", [])

    if not rows:
        return pd.DataFrame()

    records = []

    for x in rows:

        if len(x) < 6:
            continue

        records.append({
            "ts": int(x[0]),
            "open": float(x[1]),
            "high": float(x[2]),
            "low": float(x[3]),
            "close": float(x[4]),
            "vol": float(x[5]),
            "confirm": str(
                x[-1]
            ) if len(x) >= 9 else "1",
        })

    df = pd.DataFrame(records)

    if df.empty:
        return df

    df = df.sort_values("ts")

    # 只使用已经确认的K线
    if "confirm" in df.columns:
        df = df[
            df["confirm"] == "1"
        ]

    return df.reset_index(drop=True)


# ============================================================
# 指标
# ============================================================

def add_indicators(df):

    df = df.copy()

    if len(df) < 60:
        return df

    close = df["close"]

    df["ema_fast"] = (
        close.ewm(
            span=9,
            adjust=False,
        ).mean()
    )

    df["ema_slow"] = (
        close.ewm(
            span=21,
            adjust=False,
        ).mean()
    )

    delta = close.diff()

    gain = delta.clip(lower=0)

    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / 14,
        adjust=False,
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / 14,
        adjust=False,
    ).mean()

    rs = avg_gain / avg_loss.replace(
        0,
        pd.NA,
    )

    df["rsi"] = 100 - (
        100 / (1 + rs)
    )

    ema12 = close.ewm(
        span=12,
        adjust=False,
    ).mean()

    ema26 = close.ewm(
        span=26,
        adjust=False,
    ).mean()

    df["macd"] = ema12 - ema26

    df["macd_signal"] = (
        df["macd"]
        .ewm(
            span=9,
            adjust=False,
        )
        .mean()
    )

    prev_close = close.shift(1)

    tr1 = df["high"] - df["low"]

    tr2 = (
        df["high"] - prev_close
    ).abs()

    tr3 = (
        df["low"] - prev_close
    ).abs()

    df["tr"] = pd.concat(
        [tr1, tr2, tr3],
        axis=1,
    ).max(axis=1)

    df["atr"] = (
        df["tr"]
        .rolling(14)
        .mean()
    )

    return df


# ============================================================
# 趋势
# ============================================================

def get_trend(inst_id, bar):

    df = get_candles(
        inst_id,
        bar,
        80,
    )

    if df.empty or len(df) < 40:
        return None

    df = add_indicators(df)

    last = df.iloc[-1]

    if (
        pd.isna(last["ema_fast"])
        or pd.isna(last["ema_slow"])
    ):
        return None

    if last["ema_fast"] > last["ema_slow"]:
        return "LONG"

    if last["ema_fast"] < last["ema_slow"]:
        return "SHORT"

    return None


# ============================================================
# 信号
# ============================================================

def calculate_signal(inst_id):

    try:

        df = get_candles(
            inst_id,
            BAR,
            120,
        )

        if df.empty or len(df) < 60:
            return None

        df = add_indicators(df)

        if df.empty:
            return None

        last = df.iloc[-1]

        price = float(last["close"])

        atr = float(last["atr"])

        if price <= 0 or atr <= 0:
            return None

        atr_pct = atr / price

        if atr_pct < MIN_ATR_PCT:
            return None

        long_score = 0
        short_score = 0

        # EMA
        if last["ema_fast"] > last["ema_slow"]:
            long_score += 2
        elif last["ema_fast"] < last["ema_slow"]:
            short_score += 2

        # Price / EMA
        if price > last["ema_fast"]:
            long_score += 1
        elif price < last["ema_fast"]:
            short_score += 1

        # RSI
        rsi = float(last["rsi"])

        if 52 <= rsi <= 72:
            long_score += 1

        if 28 <= rsi <= 48:
            short_score += 1

        # MACD
        macd = float(last["macd"])
        macd_signal = float(
            last["macd_signal"]
        )

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

        # 15m
        trend15 = get_trend(
            inst_id,
            BAR_15M,
        )

        if trend15 == "LONG":
            long_score += 1

        elif trend15 == "SHORT":
            short_score += 1

        # 1H
        trend1h = get_trend(
            inst_id,
            BAR_1H,
        )

        if trend1h == "LONG":
            long_score += 1

        elif trend1h == "SHORT":
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
            "trend15": trend15,
            "trend1h": trend1h,
        }

    except Exception as e:

        log(
            f"⚠️ {inst_id} 信号计算失败: {e}"
        )

        return None


# ============================================================
# 账户
# ============================================================

def get_equity():

    data = okx_request(
        "GET",
        "/api/v5/account/balance",
        private=True,
    )

    rows = data.get("data", [])

    if not rows:
        return 0.0

    details = rows[0].get(
        "details",
        [],
    )

    for x in details:

        if x.get("ccy") == "USDT":

            for key in (
                "eq",
                "cashBal",
                "availBal",
            ):

                try:
                    value = float(
                        x.get(key, "")
                    )

                    if value > 0:
                        return value

                except Exception:
                    pass

    return 0.0


# ============================================================
# 持仓
# ============================================================

def get_positions(inst_id=None):

    params = {
        "instType": INST_TYPE,
    }

    if inst_id:
        params["instId"] = inst_id

    data = okx_request(
        "GET",
        "/api/v5/account/positions",
        params=params,
        private=True,
    )

    return [
        x
        for x in data.get("data", [])
        if abs(float(x.get("pos", "0") or 0)) > 0
    ]


def get_position(inst_id):

    rows = get_positions(inst_id)

    if not rows:
        return None

    return rows[0]


def position_side(pos):

    qty = float(
        pos.get("pos", "0")
    )

    if qty > 0:
        return "LONG"

    if qty < 0:
        return "SHORT"

    return None


# ============================================================
# 实时持仓显示
# ============================================================

def print_live_position(pos):

    inst_id = pos["instId"]

    side = position_side(pos)

    avg_px = float(
        pos.get("avgPx", "0") or 0
    )

    qty = abs(
        float(
            pos.get("pos", "0") or 0
        )
    )

    leverage = float(
        pos.get("lever", "0") or 0
    )

    # 这里强制重新获取实时 Mark Price
    ticker = get_ticker(inst_id)

    mark_px = float(
        ticker.get("markPx")
        or ticker.get("last")
        or 0
    )

    upl = float(
        pos.get("upl", "0") or 0
    )

    upl_ratio = float(
        pos.get("uplRatio", "0") or 0
    )

    if avg_px > 0 and mark_px > 0:

        if side == "LONG":
            local_pct = (
                mark_px - avg_px
            ) / avg_px

        else:
            local_pct = (
                avg_px - mark_px
            ) / avg_px

    else:
        local_pct = 0

    # 获取合约面值，用来做本地 PnL 校验
    try:

        inst = get_instrument(
            inst_id
        )

        ct_val = float(
            inst.get("ctVal", "0")
        )

        if (
            ct_val > 0
            and avg_px > 0
            and mark_px > 0
        ):

            if side == "LONG":
                local_upl = (
                    mark_px - avg_px
                ) * qty * ct_val

            else:
                local_upl = (
                    avg_px - mark_px
                ) * qty * ct_val

        else:
            local_upl = 0

    except Exception:
        local_upl = 0

    algos = pos.get(
        "closeOrderAlgo",
        [],
    )

    tp = None
    sl = None

    if algos:

        for a in algos:

            try:

                if (
                    a.get("tpTriggerPx")
                    and float(
                        a["tpTriggerPx"]
                    ) > 0
                ):
                    tp = float(
                        a["tpTriggerPx"]
                    )

                if (
                    a.get("slTriggerPx")
                    and float(
                        a["slTriggerPx"]
                    ) > 0
                ):
                    sl = float(
                        a["slTriggerPx"]
                    )

            except Exception:
                pass

    state = "🟢 浮盈"

    if upl < 0:
        state = "🔴 浮亏"

    roi_pct = upl_ratio * 100

    log(
        f"📊 {inst_id} "
        f"{'🟢 LONG' if side == 'LONG' else '🔴 SHORT'}"
    )

    log(
        f"   开仓价: {avg_px:.12g} | "
        f"实时Mark: {mark_px:.12g} | "
        f"杠杆: {leverage:g}x"
    )

    log(
        f"   数量: {qty:g} | "
        f"{state}: {upl:+.6f} USDT | "
        f"ROI: {roi_pct:+.2f}%"
    )

    log(
        f"   本地价格变动: "
        f"{local_pct * 100:+.3f}% | "
        f"本地PnL校验: {local_upl:+.6f} USDT"
    )

    if tp is not None:

        if side == "LONG":
            tp_dist = (
                tp - mark_px
            ) / mark_px * 100
        else:
            tp_dist = (
                mark_px - tp
            ) / mark_px * 100

        log(
            f"   🎯 TP: {tp:.12g} | "
            f"距离TP: {tp_dist:+.3f}%"
        )

    else:
        log("   ⚠️ TP: 缺失")

    if sl is not None:

        if side == "LONG":
            sl_dist = (
                sl - mark_px
            ) / mark_px * 100
        else:
            sl_dist = (
                mark_px - sl
            ) / mark_px * 100

        log(
            f"   🛑 SL: {sl:.12g} | "
            f"距离SL: {sl_dist:+.3f}%"
        )

    else:
        log("   ⚠️ SL: 缺失")

    return {
        "mark": mark_px,
        "upl": upl,
        "roi": roi_pct,
        "tp": tp,
        "sl": sl,
    }


# ============================================================
# 动态杠杆
# ============================================================

def leverage_from_score(score):

    if score <= 4:
        return 5

    if score == 5:
        return 8

    if score == 6:
        return 10

    if score >= 7:
        return 15

    return 5


def set_leverage(inst_id, leverage):

    leverage = int(
        clamp(
            leverage,
            MIN_LEVERAGE,
            MAX_LEVERAGE,
        )
    )

    body = {
        "instId": inst_id,
        "lever": str(leverage),
        "mgnMode": TD_MODE,
        "posSide": "net",
    }

    data = okx_request(
        "POST",
        "/api/v5/account/set-leverage",
        body=body,
        private=True,
    )

    return data


# ============================================================
# TP / SL 计算
# ============================================================

def calculate_protection(
    pos,
    atr=None,
    adopted=False,
):

    inst_id = pos["instId"]

    side = position_side(pos)

    avg_px = float(
        pos.get("avgPx", "0") or 0
    )

    leverage = float(
        pos.get("lever", "5") or 5
    )

    if avg_px <= 0:
        raise RuntimeError(
            f"{inst_id} avgPx无效"
        )

    leverage = clamp(
        leverage,
        MIN_LEVERAGE,
        MAX_LEVERAGE,
    )

    # 实时价格
    ticker = get_ticker(inst_id)

    mark_px = float(
        ticker.get("markPx")
        or ticker.get("last")
        or 0
    )

    if mark_px <= 0:
        raise RuntimeError(
            f"{inst_id} Mark Price无效"
        )

    # 如果没有传 ATR，则现场读取。
    if atr is None:

        df = get_candles(
            inst_id,
            BAR,
            80,
        )

        df = add_indicators(df)

        if df.empty:
            raise RuntimeError(
                "ATR数据不足"
            )

        atr = float(
            df.iloc[-1]["atr"]
        )

    if atr <= 0:
        raise RuntimeError(
            f"{inst_id} ATR无效: {atr}"
        )

    atr_pct = atr / avg_px

    # 防止异常 ATR。
    atr_pct = clamp(
        atr_pct,
        0.0005,
        0.08,
    )

    # --------------------------------------------------------
    # 目标 ROI -> 价格距离
    # --------------------------------------------------------

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

    # ATR 作为最小波动空间
    atr_sl_pct = (
        atr_pct
        * ATR_SL_FRACTION
    )

    sl_pct = max(
        target_sl_pct,
        atr_sl_pct,
        MIN_SL_PRICE_PCT,
    )

    sl_pct = min(
        sl_pct,
        MAX_SL_PRICE_PCT,
    )

    tp_pct = max(
        target_tp_pct,
        sl_pct * RISK_REWARD,
    )

    tp_pct = min(
        tp_pct,
        MAX_TP_PRICE_PCT,
    )

    # --------------------------------------------------------
    # 基准价格
    #
    # 新仓：以开仓均价为主
    # 已经存在较久且已经盈利很多：
    # 以当前 Mark 重新建立保护，避免出现
    # “TP已经在当前价格下面”的无效OCO。
    # --------------------------------------------------------

    if adopted:

        if side == "LONG" and mark_px > avg_px:

            base_px = mark_px

        elif side == "SHORT" and mark_px < avg_px:

            base_px = mark_px

        else:
            base_px = avg_px

    else:
        base_px = avg_px

    if side == "LONG":

        tp = base_px * (
            1 + tp_pct
        )

        sl = base_px * (
            1 - sl_pct
        )

        # 当前 Mark 必须处于 SL / TP 中间
        if tp <= mark_px:

            tp = mark_px * (
                1 + max(
                    MIN_TRIGGER_GAP_PCT,
                    tp_pct,
                )
            )

        if sl >= mark_px:

            sl = mark_px * (
                1 - max(
                    MIN_TRIGGER_GAP_PCT,
                    sl_pct,
                )
            )

    else:

        tp = base_px * (
            1 - tp_pct
        )

        sl = base_px * (
            1 + sl_pct
        )

        if tp >= mark_px:

            tp = mark_px * (
                1 - max(
                    MIN_TRIGGER_GAP_PCT,
                    tp_pct,
                )
            )

        if sl <= mark_px:

            sl = mark_px * (
                1 + max(
                    MIN_TRIGGER_GAP_PCT,
                    sl_pct,
                )
            )

    # --------------------------------------------------------
    # Tick 精度
    # --------------------------------------------------------

    inst = get_instrument(
        inst_id
    )

    tick_sz = D(
        inst.get("tickSz", "0")
    )

    if tick_sz <= 0:
        raise RuntimeError(
            f"{inst_id} tickSz无效"
        )

    if side == "LONG":

        tp = round_up(
            tp,
            tick_sz,
        )

        sl = round_down(
            sl,
            tick_sz,
        )

    else:

        tp = round_down(
            tp,
            tick_sz,
        )

        sl = round_up(
            sl,
            tick_sz,
        )

    # --------------------------------------------------------
    # 最终硬验证
    # --------------------------------------------------------

    if tp <= 0 or sl <= 0:
        raise RuntimeError(
            f"{inst_id} TP/SL <= 0"
        )

    if side == "LONG":

        if not (
            sl < mark_px < tp
        ):
            raise RuntimeError(
                f"{inst_id} LONG价格关系错误 "
                f"SL={sl} Mark={mark_px} TP={tp}"
            )

    else:

        if not (
            tp < mark_px < sl
        ):
            raise RuntimeError(
                f"{inst_id} SHORT价格关系错误 "
                f"TP={tp} Mark={mark_px} SL={sl}"
            )

    return {
        "tp": float(tp),
        "sl": float(sl),
        "mark": mark_px,
        "atr": atr,
        "atr_pct": atr_pct,
        "sl_pct": sl_pct,
        "tp_pct": tp_pct,
        "leverage": leverage,
    }


# ============================================================
# 计算下单数量
# ============================================================

def calculate_size(
    inst_id,
    price,
    leverage,
    sl_pct,
    equity,
):

    inst = get_instrument(
        inst_id
    )

    ct_val = D(
        inst.get("ctVal", "0")
    )

    lot_sz = D(
        inst.get("lotSz", "1")
    )

    min_sz = D(
        inst.get("minSz", "1")
    )

    if ct_val <= 0:
        raise RuntimeError(
            f"{inst_id} ctVal无效"
        )

    if lot_sz <= 0:
        lot_sz = D("1")

    if min_sz <= 0:
        min_sz = lot_sz

    # 按风险计算
    risk_usdt = (
        D(equity)
        * D(RISK_PER_TRADE_PCT)
    )

    price_risk = (
        D(price)
        * D(sl_pct)
    )

    if price_risk <= 0:
        return None

    qty_by_risk = (
        risk_usdt
        / (
            price_risk
            * ct_val
        )
    )

    # 按保证金上限计算
    max_margin = (
        D(equity)
        * D(MAX_MARGIN_PCT)
    )

    qty_by_margin = (
        max_margin
        * D(leverage)
        / (
            D(price)
            * ct_val
        )
    )

    qty = min(
        qty_by_risk,
        qty_by_margin,
    )

    qty = round_down(
        qty,
        lot_sz,
    )

    if qty < min_sz:
        return None

    return qty


# ============================================================
# 市价开仓
# ============================================================

def place_market_order(
    inst_id,
    side,
    sz,
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
        "ordType": "market",
        "sz": fmt_decimal(sz),
    }

    data = okx_request(
        "POST",
        "/api/v5/trade/order",
        body=body,
        private=True,
    )

    return data["data"][0]


# ============================================================
# 查询订单
# ============================================================

def get_order(
    inst_id,
    ord_id,
):

    data = okx_request(
        "GET",
        "/api/v5/trade/order",
        params={
            "instId": inst_id,
            "ordId": ord_id,
        },
        private=True,
    )

    rows = data.get(
        "data",
        [],
    )

    return rows[0] if rows else None


def wait_order_filled(
    inst_id,
    ord_id,
    timeout=15,
):

    start = time.time()

    while time.time() - start < timeout:

        order = get_order(
            inst_id,
            ord_id,
        )

        if not order:
            time.sleep(0.8)
            continue

        state = order.get(
            "state"
        )

        if state == "filled":
            return order

        if state in (
            "canceled",
            "mmp_canceled",
        ):
            return order

        time.sleep(0.8)

    return get_order(
        inst_id,
        ord_id,
    )


# ============================================================
# OCO
# ============================================================

def get_pending_algos(
    inst_id,
    ord_type="oco",
):

    data = okx_request(
        "GET",
        "/api/v5/trade/orders-algo-pending",
        params={
            "instType": INST_TYPE,
            "ordType": ord_type,
            "instId": inst_id,
        },
        private=True,
    )

    return data.get(
        "data",
        [],
    )


def cancel_algo(
    inst_id,
    algo_id,
):

    body = [
        {
            "instId": inst_id,
            "algoId": algo_id,
        }
    ]

    return okx_request(
        "POST",
        "/api/v5/trade/cancel-algos",
        body=body,
        private=True,
    )


def place_oco(
    pos,
    plan,
):

    inst_id = pos["instId"]

    side = position_side(pos)

    qty = abs(
        float(
            pos.get("pos", "0")
        )
    )

    if qty <= 0:
        raise RuntimeError(
            f"{inst_id} 持仓数量为0"
        )

    order_side = (
        "sell"
        if side == "LONG"
        else "buy"
    )

    body = {
        "instId": inst_id,
        "tdMode": TD_MODE,
        "side": order_side,
        "posSide": "net",
        "ordType": "oco",
        "sz": fmt_decimal(qty),
        "tpTriggerPx": fmt_decimal(
            plan["tp"]
        ),
        "tpOrdPx": "-1",
        "tpTriggerPxType": "mark",
        "slTriggerPx": fmt_decimal(
            plan["sl"]
        ),
        "slOrdPx": "-1",
        "slTriggerPxType": "mark",
        "closeFraction": "1",
    }

    log(
        f"📌 提交OCO: {inst_id} "
        f"{side} "
        f"sz={qty:g} "
        f"TP={plan['tp']:.12g} "
        f"SL={plan['sl']:.12g} "
        f"Mark={plan['mark']:.12g}"
    )

    data = okx_request(
        "POST",
        "/api/v5/trade/order-algo",
        body=body,
        private=True,
    )

    return data["data"][0]


# ============================================================
# 检查 OCO
# ============================================================

def verify_oco(
    pos,
    plan=None,
):

    inst_id = pos["instId"]

    qty = abs(
        float(
            pos.get("pos", "0")
        )
    )

    if qty <= 0:
        return False

    algos = get_pending_algos(
        inst_id,
        "oco",
    )

    if not algos:
        return False

    side = position_side(pos)

    for a in algos:

        try:

            state = a.get(
                "state",
                "live",
            )

            if state not in (
                "live",
                "effective",
                "partially_effective",
            ):
                continue

            algo_sz = abs(
                float(
                    a.get(
                        "sz",
                        "0",
                    )
                    or 0
                )
            )

            if (
                algo_sz > 0
                and algo_sz < qty * 0.99
            ):
                continue

            tp = float(
                a.get(
                    "tpTriggerPx",
                    "0",
                )
                or 0
            )

            sl = float(
                a.get(
                    "slTriggerPx",
                    "0",
                )
                or 0
            )

            if tp <= 0 or sl <= 0:
                continue

            mark = float(
                get_ticker(
                    inst_id
                ).get(
                    "markPx"
                    ) or 0
            )

            if mark <= 0:
                continue

            if side == "LONG":

                valid_relation = (
                    sl < mark < tp
                )

            else:

                valid_relation = (
                    tp < mark < sl
                )

            if not valid_relation:
                continue

            log(
                f"✅ OCO正常: {inst_id} "
                f"TP={tp:.12g} "
                f"SL={sl:.12g} "
                f"Mark={mark:.12g}"
            )

            return True

        except Exception:
            continue

    return False


# ============================================================
# 删除旧的异常保护
# ============================================================

def cancel_existing_algos(
    inst_id,
):

    for ord_type in (
        "oco",
        "conditional",
    ):

        try:

            algos = get_pending_algos(
                inst_id,
                ord_type,
            )

            for a in algos:

                algo_id = a.get(
                    "algoId"
                )

                if algo_id:

                    log(
                        f"🗑️ 撤销旧{ord_type}: "
                        f"{inst_id} "
                        f"algoId={algo_id}"
                    )

                    try:
                        cancel_algo(
                            inst_id,
                            algo_id,
                        )
                    except Exception as e:
                        log(
                            f"⚠️ 撤销失败: {e}"
                        )

        except Exception as e:

            log(
                f"⚠️ 查询{ord_type}失败: {e}"
            )


# ============================================================
# 自动建立保护
# ============================================================

def ensure_protection(
    pos,
    adopted=False,
    new_position=False,
):

    inst_id = pos["instId"]

    # 先实时打印当前持仓状态
    try:
        print_live_position(pos)
    except Exception as e:
        log(
            f"⚠️ {inst_id} 实时持仓显示失败: {e}"
        )

    # 已有正常 OCO
    try:

        if verify_oco(pos):

            return True

    except Exception as e:

        log(
            f"⚠️ {inst_id} OCO检查异常: {e}"
        )

    # 当前保护不完整，删除旧保护
    cancel_existing_algos(
        inst_id
    )

    last_error = None

    for attempt in range(
        1,
        OCO_RETRIES + 1,
    ):

        try:

            # 每次重试重新读取最新仓位
            current_pos = get_position(
                inst_id
            )

            if not current_pos:
                log(
                    f"ℹ️ {inst_id} 仓位已经结束"
                )
                return True

            # 每次重新读取实时价格和 ATR
            ticker = get_ticker(
                inst_id
            )

            mark = float(
                ticker.get(
                    "markPx"
                )
                or ticker.get("last")
                or 0
            )

            if mark <= 0:
                raise RuntimeError(
                    "Mark Price无效"
                )

            df = get_candles(
                inst_id,
                BAR,
                80,
            )

            df = add_indicators(df)

            if df.empty:
                raise RuntimeError(
                    "ATR数据为空"
                )

            atr = float(
                df.iloc[-1]["atr"]
            )

            plan = calculate_protection(
                current_pos,
                atr=atr,
                adopted=adopted,
            )

            log(
                f"🛡️ OCO第{attempt}/{OCO_RETRIES}次 "
                f"{inst_id}: "
                f"Mark={plan['mark']:.12g} "
                f"ATR={plan['atr']:.12g} "
                f"TP={plan['tp']:.12g} "
                f"SL={plan['sl']:.12g}"
            )

            place_oco(
                current_pos,
                plan,
            )

            time.sleep(
                OCO_VERIFY_WAIT
            )

            verify_pos = get_position(
                inst_id
            )

            if not verify_pos:
                return True

            if verify_oco(
                verify_pos,
                plan,
            ):
                return True

            raise RuntimeError(
                "OCO提交后验证失败"
            )

        except Exception as e:

            last_error = e

            log(
                f"⚠️ {inst_id} OCO第{attempt}次失败: "
                f"{e}"
            )

            # 下一次重新取实时价格
            time.sleep(1.2)

    # ========================================================
    # 新仓最终保护失败
    # ========================================================

    if new_position:

        log(
            f"🚨 {inst_id} 新仓OCO连续失败 "
            f"{OCO_RETRIES}次"
        )

        if EMERGENCY_CLOSE_IF_NEW_OCO_FAIL:

            try:

                emergency_close_position(
                    get_position(inst_id)
                )

                return True

            except Exception as e:

                log(
                    f"🚨 紧急平仓也失败: {e}"
                )

        return False

    # ========================================================
    # 已有仓位不因一次保护失败强平
    # 下一轮继续自动修复
    # ========================================================

    log(
        f"⚠️ {inst_id} 已有仓位保护暂未建立，"
        f"不会因为本轮OCO失败直接强平。"
    )

    return False


# ============================================================
# 紧急平仓
# ============================================================

def emergency_close_position(
    pos,
):

    if not pos:
        return

    inst_id = pos["instId"]

    qty = abs(
        float(
            pos.get("pos", "0")
        )
    )

    if qty <= 0:
        return

    side = position_side(pos)

    close_side = (
        "sell"
        if side == "LONG"
        else "buy"
    )

    log(
        f"🚨 紧急平仓: {inst_id} "
        f"{side} qty={qty:g}"
    )

    body = {
        "instId": inst_id,
        "tdMode": TD_MODE,
        "side": close_side,
        "posSide": "net",
        "ordType": "market",
        "sz": fmt_decimal(qty),
        "reduceOnly": "true",
    }

    okx_request(
        "POST",
        "/api/v5/trade/order",
        body=body,
        private=True,
    )


# ============================================================
# 历史平仓结果
# ============================================================

def get_position_history(
    inst_id,
    limit=20,
):

    data = okx_request(
        "GET",
        "/api/v5/account/positions-history",
        params={
            "instType": INST_TYPE,
            "instId": inst_id,
            "limit": limit,
        },
        private=True,
    )

    return data.get(
        "data",
        [],
    )


def print_closed_position_result(
    inst_id,
    old_pos=None,
):

    try:

        rows = get_position_history(
            inst_id,
            20,
        )

        if not rows:
            log(
                f"ℹ️ {inst_id} 仓位结束，但暂未取得历史结果"
            )
            return

        row = rows[0]

        direction = row.get(
            "direction",
            row.get(
                "posSide",
                "?",
            ),
        )

        open_px = float(
            row.get(
                "openAvgPx",
                "0",
            )
            or 0
        )

        close_px = float(
            row.get(
                "closeAvgPx",
                "0",
            )
            or 0
        )

        pnl = float(
            row.get(
                "pnl",
                "0",
            )
            or 0
        )

        fee = float(
            row.get(
                "fee",
                "0",
            )
            or 0
        )

        funding = float(
            row.get(
                "fundingFee",
                "0",
            )
            or 0
        )

        realized = float(
            row.get(
                "realizedPnl",
                "0",
            )
            or 0
        )

        roi = float(
            row.get(
                "pnlRatio",
                "0",
            )
            or 0
        ) * 100

        if realized > 0:

            result = "🟢 盈利"

        elif realized < 0:

            result = "🔴 亏损"

        else:

            result = "⚪ 持平"

        log(
            f"🏁 {inst_id} 仓位结束"
        )

        log(
            f"   方向: {direction} | "
            f"开仓均价: {open_px:.12g} | "
            f"平仓均价: {close_px:.12g}"
        )

        log(
            f"   {result} | "
            f"毛PnL: {pnl:+.6f} USDT | "
            f"手续费: {fee:+.6f} USDT | "
            f"资金费: {funding:+.6f} USDT"
        )

        log(
            f"   已实现PnL: "
            f"{realized:+.6f} USDT | "
            f"ROI: {roi:+.2f}%"
        )

    except Exception as e:

        log(
            f"⚠️ {inst_id} 获取平仓结果失败: {e}"
        )


# ============================================================
# 仓位快照
# ============================================================

previous_positions = {}


def position_key(pos):

    return pos.get(
        "instId"
    )


def detect_closed_positions(
    current_positions,
):

    global previous_positions

    current_map = {
        position_key(x): x
        for x in current_positions
    }

    # 之前有，现在没有
    for inst_id, old_pos in list(
        previous_positions.items()
    ):

        if inst_id not in current_map:

            print_closed_position_result(
                inst_id,
                old_pos,
            )

    previous_positions = current_map


# ============================================================
# 管理所有已有仓位
# ============================================================

def manage_existing_positions():

    positions = get_positions()

    detect_closed_positions(
        positions
    )

    if not positions:
        return positions

    top_symbols, _ = get_top30_market_caps()

    for pos in positions:

        inst_id = pos["instId"]

        base = inst_id.split(
            "-USDT-SWAP"
        )[0]

        if base not in top_symbols:

            log(
                f"ℹ️ {inst_id} 当前不在市值Top30"
                f"：禁止新开，但继续管理已有仓位"
            )

        try:

            # 实时显示
            print_live_position(
                pos
            )

        except Exception as e:

            log(
                f"⚠️ {inst_id} 实时价格管理失败: {e}"
            )

        try:

            ensure_protection(
                pos,
                adopted=True,
                new_position=False,
            )

        except Exception as e:

            log(
                f"⚠️ {inst_id} TP/SL修复失败: {e}"
            )

    return positions


# ============================================================
# 判断是否可以新开仓
# ============================================================

def can_open_new_trade():

    positions = get_positions()

    if positions:
        return False

    return True


# ============================================================
# 新仓执行
# ============================================================

def execute_candidate(
    candidate,
    equity,
):

    inst_id = candidate["instId"]

    side = candidate["side"]

    score = candidate["score"]

    # 再次检查 Top30
    top_symbols, _ = get_top30_market_caps()

    if not top_symbols:
        log(
            "⛔ Top30数据获取失败，禁止新开仓"
        )
        return False

    base = inst_id.split(
        "-USDT-SWAP"
    )[0]

    if base not in top_symbols:

        log(
            f"⛔ {inst_id} 已不在Top30，"
            f"禁止新开仓"
        )

        return False

    # 防止扫描过程中已经出现仓位
    if get_positions():
        return False

    leverage = leverage_from_score(
        score
    )

    log(
        f"🎯 新信号 {inst_id} "
        f"{side} "
        f"score={score} "
        f"leverage={leverage}x"
    )

    set_leverage(
        inst_id,
        leverage,
    )

    # 先根据当前价格和 ATR 估算止损
    price = float(
        candidate["price"]
    )

    atr = float(
        candidate["atr"]
    )

    atr_pct = atr / price

    sl_pct = max(
        MIN_SL_PRICE_PCT,
        atr_pct * ATR_SL_FRACTION,
        TARGET_SL_ROI_PCT
        / leverage
        / 100,
    )

    sl_pct = min(
        sl_pct,
        MAX_SL_PRICE_PCT,
    )

    qty = calculate_size(
        inst_id,
        price,
        leverage,
        sl_pct,
        equity,
    )

    if not qty:

        log(
            f"⛔ {inst_id} 计算出的下单数量低于最小数量"
        )

        return False

    log(
        f"📥 开仓: {inst_id} "
        f"{side} "
        f"qty={qty} "
        f"lev={leverage}x"
    )

    order = place_market_order(
        inst_id,
        side,
        qty,
    )

    ord_id = order.get(
        "ordId"
    )

    if not ord_id:
        raise RuntimeError(
            "下单没有返回ordId"
        )

    filled = wait_order_filled(
        inst_id,
        ord_id,
    )

    if not filled:
        raise RuntimeError(
            "无法取得成交订单"
        )

    if filled.get(
        "state"
    ) != "filled":

        log(
            f"⚠️ {inst_id} 下单状态: "
            f"{filled.get('state')}"
        )

        return False

    # 等待仓位建立
    time.sleep(1)

    pos = get_position(
        inst_id
    )

    if not pos:

        raise RuntimeError(
            f"{inst_id} 成交后没有发现持仓"
        )

    log(
        f"✅ {inst_id} 开仓成功"
    )

    try:
        print_live_position(
            pos
        )
    except Exception:
        pass

    # 新仓建立 OCO
    protected = ensure_protection(
        pos,
        adopted=False,
        new_position=True,
    )

    if not protected:

        log(
            f"🚨 {inst_id} 新仓保护失败"
        )

        return False

    log(
        f"🛡️ {inst_id} TP/SL保护建立完成"
    )

    return True


# ============================================================
# 选择候选
# ============================================================

def scan_candidates():

    instruments = get_top30_okx_instruments()

    if not instruments:

        log(
            "⛔ 当前无法取得有效Top30 OKX合约"
        )

        return []

    # 获取全市场 ticker，只从 Top30 里筛
    tickers = get_tickers()

    ticker_map = {
        x.get("instId"): x
        for x in tickers
    }

    candidates = []

    for item in instruments[:SCAN_LIMIT]:

        inst_id = item["instId"]

        ticker = ticker_map.get(
            inst_id
        )

        if not ticker:
            continue

        try:

            vol_usd = float(
                ticker.get(
                    "volCcy24h",
                    "0",
                )
                or 0
            )

            if vol_usd < MIN_24H_VOLUME_USD:
                continue

        except Exception:
            continue

        signal = calculate_signal(
            inst_id
        )

        if not signal:
            continue

        signal["volume24h"] = vol_usd

        candidates.append(
            signal
        )

    candidates.sort(
        key=lambda x: (
            x["score"],
            x["volume24h"],
        ),
        reverse=True,
    )

    return candidates


# ============================================================
# 主循环
# ============================================================

def main():

    log("=" * 60)

    log(
        f"🚀 OKX BTC/Top30 永续自动交易模拟盘 {VERSION}"
    )

    log(
        "🕐 日志时区: UTC+8"
    )

    log(
        f"📌 模式: {'模拟盘' if SIMULATED_TRADING == '1' else '实盘'}"
    )

    log(
        f"📌 杠杆范围: {MIN_LEVERAGE}x - {MAX_LEVERAGE}x"
    )

    log(
        f"📌 市值限制: Top {TOP_N}"
    )

    log(
        "📌 已有仓位：无论是否Top30都继续管理"
    )

    log("=" * 60)

    consecutive_losses = 0

    last_loss_time = 0

    while True:

        try:

            # =================================================
            # 第一优先级：管理已有仓位
            # =================================================

            positions = manage_existing_positions()

            if positions:

                time.sleep(
                    LOOP_SECONDS
                )

                continue

            # =================================================
            # 无仓位
            # =================================================

            if (
                consecutive_losses
                >= MAX_CONSECUTIVE_LOSSES
            ):

                if (
                    time.time()
                    - last_loss_time
                    < LOSS_COOLDOWN_SECONDS
                ):

                    remain = int(
                        LOSS_COOLDOWN_SECONDS
                        - (
                            time.time()
                            - last_loss_time
                        )
                    )

                    log(
                        f"⏸️ 连续亏损保护 "
                        f"剩余{remain}s"
                    )

                    time.sleep(
                        LOOP_SECONDS
                    )

                    continue

                consecutive_losses = 0

            equity = get_equity()

            if equity <= 0:

                log(
                    "⚠️ USDT权益无效"
                )

                time.sleep(
                    LOOP_SECONDS
                )

                continue

            log(
                f"💰 当前账户权益: "
                f"{equity:.4f} USDT"
            )

            # =================================================
            # 扫描 Top30
            # =================================================

            candidates = scan_candidates()

            if not candidates:

                log(
                    "🔍 Top30当前没有满足条件的信号"
                )

                time.sleep(
                    LOOP_SECONDS
                )

                continue

            log(
                f"🔎 找到{len(candidates)}个候选"
            )

            # =================================================
            # 只尝试最强信号
            # =================================================

            candidate = candidates[0]

            log(
                f"⭐ 候选: "
                f"{candidate['instId']} "
                f"{candidate['side']} "
                f"score={candidate['score']} "
                f"RSI={candidate['rsi']:.2f} "
                f"ATR%={candidate['atr_pct'] * 100:.3f}%"
            )

            success = execute_candidate(
                candidate,
                equity,
            )

            if success:

                log(
                    "✅ 本轮开仓及保护完成"
                )

            else:

                log(
                    "⚠️ 本轮没有建立新仓"
                )

            time.sleep(
                LOOP_SECONDS
            )

        except KeyboardInterrupt:

            log(
                "🛑 手动停止机器人"
            )

            break

        except Exception as e:

            log(
                f"❌ 主循环异常: {e}"
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
