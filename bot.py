# -*- coding: utf-8 -*-
"""
OKX 全市场永续强行情自动交易机器人 V2.6
------------------------------------------------
核心修复：
1. TP/SL 使用 OKX OCO，而不是 conditional 双向 TP/SL。
2. 下单后严格确认：ordType=oco + TP + SL + live/effective。
3. OCO 未确认 -> 紧急平仓。
4. 记录 active_trade，重启后可恢复本机器人自己建立的保护单。
5. 对旧版“只有 SL”的持仓：如果能找到旧 SL，则保留旧 SL，并按 RR 推导 TP，再建立 OCO。
6. 全部 API 密钥从环境变量读取，不写进代码。
7. 默认 OKX 模拟盘。

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
from decimal import Decimal, ROUND_DOWN, ROUND_UP, InvalidOperation
from datetime import datetime, timezone

import requests
import pandas as pd


# =========================
# 基础配置
# =========================
BASE_URL = "https://www.okx.com"
SIMULATED_TRADING = "1"

INST_TYPE = "SWAP"
SETTLE_CCY = "USDT"
TD_MODE = "cross"
POS_MODE = "net"

BAR = "5m"
LOOP_SECONDS = 20

# 多周期确认
CONFIRM_BAR_15M = "15m"
CONFIRM_BAR_1H = "1H"

# 指标
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
ATR_PERIOD = 14

# 信号
MIN_SIGNAL_SCORE = 4
RSI_LONG_MIN = 52
RSI_LONG_MAX = 72
RSI_SHORT_MIN = 28
RSI_SHORT_MAX = 48

# 风险
RISK_PER_TRADE_PCT = 0.005       # 单笔风险 = 权益 0.5%
MAX_MARGIN_PCT = 0.08            # 单笔最大保证金 = 权益 8%
MIN_LEVERAGE = 5
MAX_LEVERAGE = 15

ATR_SL_MULTIPLIER = 1.5
RISK_REWARD = 1.8

# 手续费：按吃单估算，买入和卖出都计入
TAKER_FEE_RATE = 0.0005
FEE_BUFFER_MULTIPLIER = 1.0

# 连续亏损限制
MAX_CONSECUTIVE_LOSSES = 3
LOSS_COOLDOWN_SECONDS = 1800

# 每日限制
MAX_DAILY_LOSS_PCT = 0.03
MAX_DAILY_PROFIT_PCT = 0.05
MAX_DAILY_TRADES = 12

# 扫描
SCAN_LIMIT = 80
MIN_24H_VOL_USDT = 3_000_000
MIN_ATR_PCT = 0.0015

# 请求
REQUEST_TIMEOUT = 12
ORDER_FILL_TIMEOUT = 15
PROTECTION_TIMEOUT = 12

# 安全
EMERGENCY_CLOSE_IF_OCO_FAIL = True
REPAIR_UNKNOWN_OLD_SL = True
ALLOW_NEW_TRADE_WHEN_UNKNOWN_POSITION = False

# 状态文件
STATE_FILE = "bot_state_v26.json"


# =========================
# 日志
# =========================
def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# =========================
# 环境 / 状态
# =========================
API_KEY = os.getenv("OKX_API_KEY", "")
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

if not API_KEY or not SECRET_KEY or not PASSPHRASE:
    raise RuntimeError(
        "缺少 OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE 环境变量。"
    )


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
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        for k, v in default.items():
            state.setdefault(k, v)
        return state
    except Exception as e:
        log(f"读取状态文件失败，使用新状态：{e}")
        return default


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


state = load_state()


# =========================
# OKX 签名
# =========================
def iso_timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sign_okx(timestamp, method, request_path, body=""):
    message = timestamp + method.upper() + request_path + body
    mac = hmac.new(
        SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256
    ).digest()
    return base64.b64encode(mac).decode()


def okx_request(method, path, params=None, body=None, private=False):
    method = method.upper()
    params = params or {}

    if method == "GET" and params:
        from urllib.parse import urlencode
        query = urlencode(params)
        request_path = path + "?" + query
    else:
        request_path = path

    body_text = ""
    if body is not None:
        body_text = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    if SIMULATED_TRADING == "1":
        headers["x-simulated-trading"] = "1"

    if private:
        ts = iso_timestamp()
        headers.update({
            "OK-ACCESS-KEY": API_KEY,
            "OK-ACCESS-SIGN": sign_okx(ts, method, request_path, body_text),
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
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")

            data = r.json()

            if str(data.get("code")) != "0":
                raise RuntimeError(
                    f"OKX API错误 code={data.get('code')} msg={data.get('msg')} data={data.get('data')}"
                )

            return data

        except Exception as e:
            last_error = e
            if attempt < 2:
                time.sleep(0.7 * (attempt + 1))
            else:
                raise

    raise last_error


# =========================
# Decimal / 数量价格工具
# =========================
def D(x):
    return Decimal(str(x))


def fmt_decimal(x):
    d = D(x)
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def floor_step(value, step):
    value = D(value)
    step = D(step)
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_step(value, step):
    value = D(value)
    step = D(step)
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


def round_price(value, tick, direction=None):
    """
    direction:
      UP   -> 向上取 tick
      DOWN -> 向下取 tick
      None -> 普通向下
    """
    if direction == "UP":
        return ceil_step(value, tick)
    return floor_step(value, tick)


# =========================
# 市场数据
# =========================
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
        raise RuntimeError(f"找不到合约信息：{inst_id}")

    item = data[0]
    _instrument_cache[inst_id] = item
    return item


def get_all_swap_instruments():
    data = okx_request(
        "GET",
        "/api/v5/public/instruments",
        params={"instType": INST_TYPE},
        private=False,
    )["data"]

    result = []
    for x in data:
        if x.get("settleCcy") != SETTLE_CCY:
            continue
        if x.get("state") != "live":
            continue
        if x.get("ctType") not in ("linear", ""):
            continue
        result.append(x)

    return result


def get_tickers():
    data = okx_request(
        "GET",
        "/api/v5/market/tickers",
        params={"instType": INST_TYPE},
        private=False,
    )["data"]

    return {x["instId"]: x for x in data}


def get_candles(inst_id, bar=BAR, limit=120):
    data = okx_request(
        "GET",
        "/api/v5/market/candles",
        params={
            "instId": inst_id,
            "bar": bar,
            "limit": str(min(limit, 300)),
        },
        private=False,
    )["data"]

    if not data:
        return pd.DataFrame()

    # OKX: ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm, ...
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


def get_ticker(inst_id):
    data = okx_request(
        "GET",
        "/api/v5/market/ticker",
        params={"instId": inst_id},
        private=False,
    )["data"]

    if not data:
        raise RuntimeError(f"ticker为空：{inst_id}")

    return data[0]


# =========================
# 指标
# =========================
def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, float("nan"))
    result = 100 - (100 / (1 + rs))
    return result.fillna(50)


def macd(series, fast=12, slow=26, signal=9):
    fast_ema = ema(series, fast)
    slow_ema = ema(series, slow)
    line = fast_ema - slow_ema
    sig = ema(line, signal)
    hist = line - sig
    return line, sig, hist


def atr(df, period=14):
    prev_close = df["close"].shift(1)
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"] - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def prepare_indicators(df):
    if len(df) < 60:
        return df

    df = df.copy()
    df["ema_fast"] = ema(df["close"], EMA_FAST)
    df["ema_slow"] = ema(df["close"], EMA_SLOW)
    df["rsi"] = rsi(df["close"], RSI_PERIOD)
    df["macd"], df["macd_signal"], df["macd_hist"] = macd(
        df["close"],
        MACD_FAST,
        MACD_SLOW,
        MACD_SIGNAL,
    )
    df["atr"] = atr(df, ATR_PERIOD)
    return df


# =========================
# 信号
# =========================
def closed_df(df):
    if df.empty:
        return df

    # 只使用已经确认的 K 线
    if str(df.iloc[-1].get("confirm", "1")) == "1":
        return df

    return df.iloc[:-1].copy()


def timeframe_trend(inst_id, bar):
    df = get_candles(inst_id, bar, 80)
    df = closed_df(df)
    if len(df) < 40:
        return 0

    df = prepare_indicators(df)
    x = df.iloc[-1]

    if x["ema_fast"] > x["ema_slow"]:
        return 1
    if x["ema_fast"] < x["ema_slow"]:
        return -1
    return 0


def calculate_signal(inst_id, ticker):
    df = get_candles(inst_id, BAR, 120)
    df = closed_df(df)

    if len(df) < 60:
        return None

    df = prepare_indicators(df)
    x = df.iloc[-1]

    price = float(ticker["last"])
    atr_value = float(x["atr"])

    if price <= 0 or atr_value <= 0:
        return None

    atr_pct = atr_value / price
    if atr_pct < MIN_ATR_PCT:
        return None

    long_score = 0
    short_score = 0

    # EMA
    if x["ema_fast"] > x["ema_slow"]:
        long_score += 2
    elif x["ema_fast"] < x["ema_slow"]:
        short_score += 2

    # 价格相对 EMA
    if price > x["ema_fast"]:
        long_score += 1
    elif price < x["ema_fast"]:
        short_score += 1

    # RSI
    if RSI_LONG_MIN <= x["rsi"] <= RSI_LONG_MAX:
        long_score += 1
    if RSI_SHORT_MIN <= x["rsi"] <= RSI_SHORT_MAX:
        short_score += 1

    # MACD
    if x["macd"] > x["macd_signal"] and x["macd"] > 0:
        long_score += 2
    elif x["macd"] < x["macd_signal"] and x["macd"] < 0:
        short_score += 2

    # 15m / 1H 趋势确认
    trend15 = timeframe_trend(inst_id, CONFIRM_BAR_15M)
    trend1h = timeframe_trend(inst_id, CONFIRM_BAR_1H)

    if trend15 == 1:
        long_score += 1
    elif trend15 == -1:
        short_score += 1

    if trend1h == 1:
        long_score += 1
    elif trend1h == -1:
        short_score += 1

    if long_score >= MIN_SIGNAL_SCORE and long_score > short_score:
        direction = "LONG"
        score = long_score
    elif short_score >= MIN_SIGNAL_SCORE and short_score > long_score:
        direction = "SHORT"
        score = short_score
    else:
        return None

    # 动态杠杆
    if score <= 4:
        leverage = 5
    elif score == 5:
        leverage = 8
    elif score == 6:
        leverage = 10
    else:
        leverage = 15

    return {
        "instId": inst_id,
        "direction": direction,
        "score": score,
        "leverage": leverage,
        "price": price,
        "atr": atr_value,
        "atr_pct": atr_pct,
        "rsi": float(x["rsi"]),
        "trend15": trend15,
        "trend1h": trend1h,
    }


# =========================
# 账户 / 持仓
# =========================
def get_equity():
    data = okx_request(
        "GET",
        "/api/v5/account/balance",
        private=True,
    )["data"]

    if not data:
        raise RuntimeError("账户余额返回为空")

    details = data[0].get("details", [])
    for x in details:
        if x.get("ccy") == SETTLE_CCY:
            eq = x.get("eq") or x.get("cashBal") or "0"
            return float(eq)

    # fallback
    total = data[0].get("totalEq")
    if total:
        return float(total)

    raise RuntimeError("没有找到 USDT 权益")


def get_positions():
    data = okx_request(
        "GET",
        "/api/v5/account/positions",
        params={
            "instType": INST_TYPE,
        },
        private=True,
    )["data"]

    result = []
    for p in data:
        pos = float(p.get("pos", "0") or "0")
        if abs(pos) <= 0:
            continue
        result.append(p)

    return result


def get_bot_position():
    positions = get_positions()

    if not positions:
        return None

    # 本机器人设计为单仓。
    # 如果账户本身还有其它人工仓位，也会被视为已有持仓，避免机器人误开新仓。
    return positions[0]


def position_direction(pos):
    pos_side = pos.get("posSide", "net")
    pos_qty = float(pos.get("pos", "0") or "0")

    if pos_side == "long":
        return "LONG"
    if pos_side == "short":
        return "SHORT"

    # net 模式下，OKX pos 正负通常代表方向
    if pos_qty > 0:
        return "LONG"
    return "SHORT"


def get_position_avg_px(pos):
    for k in ("avgPx", "openAvgPx"):
        if pos.get(k):
            try:
                px = float(pos[k])
                if px > 0:
                    return px
            except Exception:
                pass
    return 0.0


# =========================
# 杠杆
# =========================
def set_leverage(inst_id, leverage):
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
    )["data"]

    if data and str(data[0].get("sCode", "0")) != "0":
        raise RuntimeError(f"设置杠杆失败：{data[0]}")

    return True


# =========================
# 仓位 / TP / SL 计划
# =========================
def build_protection_plan(candidate, avg_px=None, sl_override=None):
    inst_id = candidate["instId"]
    info = get_instrument(inst_id)

    tick = D(info["tickSz"])
    lot = D(info["lotSz"])
    min_sz = D(info["minSz"])
    ct_val = D(info["ctVal"])

    avg_px = float(avg_px or candidate["price"])
    atr_value = float(candidate["atr"])

    if avg_px <= 0 or atr_value <= 0:
        raise RuntimeError(f"{inst_id} 无法计算 TP/SL")

    direction = candidate["direction"]

    if sl_override is not None and float(sl_override) > 0:
        sl = float(sl_override)
        risk_distance = abs(avg_px - sl)
    else:
        risk_distance = atr_value * ATR_SL_MULTIPLIER
        if direction == "LONG":
            sl = avg_px - risk_distance
        else:
            sl = avg_px + risk_distance

    if risk_distance <= 0:
        raise RuntimeError(f"{inst_id} TP/SL 距离无效")

    if direction == "LONG":
        tp = avg_px + risk_distance * RISK_REWARD
        sl = float(round_price(sl, tick, "DOWN"))
        tp = float(round_price(tp, tick, "UP"))

        if sl >= avg_px:
            sl = float(round_price(avg_px - tick, tick, "DOWN"))
        if tp <= avg_px:
            tp = float(round_price(avg_px + tick, tick, "UP"))

    else:
        tp = avg_px - risk_distance * RISK_REWARD
        sl = float(round_price(sl, tick, "UP"))
        tp = float(round_price(tp, tick, "DOWN"))

        if sl <= avg_px:
            sl = float(round_price(avg_px + tick, tick, "UP"))
        if tp >= avg_px:
            tp = float(round_price(avg_px - tick, tick, "DOWN"))

    return {
        "instId": inst_id,
        "direction": direction,
        "avgPx": avg_px,
        "tp": tp,
        "sl": sl,
        "tickSz": str(tick),
        "lotSz": str(lot),
        "minSz": str(min_sz),
        "ctVal": str(ct_val),
    }


def calculate_size(candidate, equity):
    """
    线性 USDT 永续：
    名义价值 ≈ contracts * ctVal * price
    风险金额 ≈ contracts * ctVal * |entry-sl|
    """
    info = get_instrument(candidate["instId"])

    lot = D(info["lotSz"])
    min_sz = D(info["minSz"])
    ct_val = D(info["ctVal"])

    price = D(candidate["price"])
    atr_value = D(candidate["atr"])
    leverage = D(candidate["leverage"])

    stop_distance = atr_value * D(str(ATR_SL_MULTIPLIER))
    if stop_distance <= 0 or ct_val <= 0:
        return 0.0

    risk_budget = D(str(equity)) * D(str(RISK_PER_TRADE_PCT))

    # 加入双向手续费的风险缓冲
    # 开仓费 + 止损平仓费，粗略按两边名义价值估算
    fee_per_contract = price * ct_val * D(str(TAKER_FEE_RATE)) * D("2") * D(str(FEE_BUFFER_MULTIPLIER))
    denominator = stop_distance * ct_val + fee_per_contract

    if denominator <= 0:
        return 0.0

    raw_sz = risk_budget / denominator

    # 保证金上限
    max_margin = D(str(equity)) * D(str(MAX_MARGIN_PCT))
    max_contracts = max_margin * leverage / (price * ct_val)

    sz = min(raw_sz, max_contracts)
    sz = floor_step(sz, lot)

    if sz < min_sz:
        return 0.0

    return float(sz)


# =========================
# 普通下单
# =========================
def place_market_order(candidate, sz):
    inst_id = candidate["instId"]
    direction = candidate["direction"]

    side = "buy" if direction == "LONG" else "sell"

    cl_ord_id = "MCV26" + str(int(time.time() * 1000))[-20:]

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

    item = resp["data"][0]
    if str(item.get("sCode", "0")) != "0":
        raise RuntimeError(f"开仓被OKX拒绝：{item}")

    ord_id = item.get("ordId")
    if not ord_id:
        raise RuntimeError(f"OKX未返回ordId：{resp}")

    return ord_id


def get_order(order_id):
    data = okx_request(
        "GET",
        "/api/v5/trade/order",
        params={
            "instId": "",
            "ordId": order_id,
        },
        private=True,
    )["data"]

    return data[0] if data else None


def wait_order_filled(inst_id, ord_id, timeout=ORDER_FILL_TIMEOUT):
    end = time.time() + timeout
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
                state_now = str(row.get("state", ""))

                if state_now == "filled":
                    avg_px = float(row.get("avgPx") or row.get("fillPx") or 0)
                    fill_sz = float(row.get("accFillSz") or row.get("fillSz") or row.get("sz") or 0)
                    if avg_px > 0 and fill_sz > 0:
                        return row

                if state_now in ("canceled", "mmp_canceled"):
                    raise RuntimeError(f"开仓订单已取消：{row}")

        except Exception as e:
            log(f"查询成交状态异常：{e}")

        time.sleep(0.7)

    # 最后再检查实际持仓
    pos = get_bot_position()
    if pos and pos.get("instId") == inst_id:
        return {
            "avgPx": pos.get("avgPx") or pos.get("openAvgPx"),
            "accFillSz": pos.get("pos"),
            "state": "filled_by_position_check",
        }

    raise RuntimeError(f"订单 {ord_id} 超时未确认成交，last={last}")


# =========================
# OCO TP/SL
# =========================
def place_protection_order(candidate, plan, algo_cl_ord_id=None):
    """
    V2.6：
    使用 OKX ordType=oco。
    TP 和 SL 同时存在，一个触发后另一侧取消。
    """
    inst_id = candidate["instId"]
    direction = candidate["direction"]
    exit_side = "sell" if direction == "LONG" else "buy"

    algo_cl_ord_id = algo_cl_ord_id or (
        "OCO26" + str(int(time.time() * 1000))[-20:]
    )

    body = {
        "instId": inst_id,
        "tdMode": TD_MODE,
        "side": exit_side,
        "posSide": "net",
        "ordType": "oco",
        "sz": fmt_decimal(plan["sz"]),
        "tpTriggerPx": fmt_decimal(plan["tp"]),
        "tpOrdPx": "-1",
        "tpTriggerPxType": "mark",
        "slTriggerPx": fmt_decimal(plan["sl"]),
        "slOrdPx": "-1",
        "slTriggerPxType": "mark",
        "algoClOrdId": algo_cl_ord_id,
    }

    resp = okx_request(
        "POST",
        "/api/v5/trade/order-algo",
        body=body,
        private=True,
    )

    item = resp["data"][0]

    if str(item.get("sCode", "0")) != "0":
        raise RuntimeError(f"OCO TP/SL 被 OKX 拒绝：{item}")

    algo_id = item.get("algoId")
    if not algo_id:
        raise RuntimeError(f"OKX 未返回 OCO algoId：{resp}")

    return algo_id, algo_cl_ord_id


def verify_protection_by_algo(inst_id, algo_id, algo_cl_ord_id, timeout_sec=PROTECTION_TIMEOUT):
    """
    严格验证：
      ordType = oco
      state = live/effective
      tpTriggerPx > 0
      slTriggerPx > 0
    """
    end = time.time() + timeout_sec
    last = None

    while time.time() < end:
        try:
            params = {"algoId": str(algo_id)}
            if not algo_id and algo_cl_ord_id:
                params = {"algoClOrdId": algo_cl_ord_id}

            data = okx_request(
                "GET",
                "/api/v5/trade/order-algo",
                params=params,
                private=True,
            )["data"]

            if data:
                row = data[0]
                last = row

                state_now = str(row.get("state") or "")
                ord_type = str(row.get("ordType") or "")
                tp_px = row.get("tpTriggerPx")
                sl_px = row.get("slTriggerPx")

                log(
                    f"OCO检查：state={state_now} "
                    f"ordType={ord_type} TP={tp_px} SL={sl_px}"
                )

                if ord_type != "oco":
                    time.sleep(0.6)
                    continue

                tp_ok = False
                sl_ok = False

                try:
                    tp_ok = tp_px is not None and float(tp_px) > 0
                except Exception:
                    pass

                try:
                    sl_ok = sl_px is not None and float(sl_px) > 0
                except Exception:
                    pass

                if state_now in ("live", "effective") and tp_ok and sl_ok:
                    return True, row

                if state_now in ("order_failed", "partially_failed"):
                    return False, row

        except Exception as e:
            log(f"OCO 策略单查询异常：{e}")

        time.sleep(0.7)

    return False, last


def get_pending_algos(inst_id):
    result = []

    for ord_type in ("oco", "conditional"):
        try:
            data = okx_request(
                "GET",
                "/api/v5/trade/orders-algo-pending",
                params={
                    "ordType": ord_type,
                    "instType": INST_TYPE,
                    "instId": inst_id,
                },
                private=True,
            )["data"]

            result.extend(data)
        except Exception as e:
            log(f"查询 {ord_type} 策略单失败：{e}")

    return result


def find_valid_oco(inst_id, position_sz, direction, algo_id=None):
    """
    在当前挂起策略单中寻找与当前持仓匹配的 OCO。
    """
    try:
        algos = get_pending_algos(inst_id)
        for a in algos:
            if str(a.get("ordType")) != "oco":
                continue

            state_now = str(a.get("state") or "")
            if state_now not in ("live", "effective"):
                continue

            tp = a.get("tpTriggerPx")
            sl = a.get("slTriggerPx")

            try:
                if float(tp) <= 0 or float(sl) <= 0:
                    continue
            except Exception:
                continue

            if algo_id and str(a.get("algoId")) == str(algo_id):
                return a

            # 如果没有指定 algoId，优先匹配数量
            try:
                a_sz = float(a.get("sz") or 0)
                if position_sz > 0 and abs(a_sz - position_sz) <= max(position_sz * 0.0001, 1e-12):
                    return a
            except Exception:
                pass

        return None

    except Exception as e:
        log(f"寻找有效 OCO 失败：{e}")
        return None


# =========================
# 取消策略单 / 紧急平仓
# =========================
def cancel_algo(inst_id, algo_id):
    if not algo_id:
        return False

    body = [{
        "instId": inst_id,
        "algoId": str(algo_id),
    }]

    try:
        resp = okx_request(
            "POST",
            "/api/v5/trade/cancel-algos",
            body=body,
            private=True,
        )

        item = resp.get("data", [{}])[0]
        if str(item.get("sCode", "0")) != "0":
            log(f"取消策略单失败：{item}")
            return False

        return True
    except Exception as e:
        log(f"取消策略单异常：{e}")
        return False


def emergency_close_position(pos):
    inst_id = pos["instId"]
    direction = position_direction(pos)
    qty = abs(float(pos.get("pos", "0") or "0"))

    if qty <= 0:
        return True

    side = "sell" if direction == "LONG" else "buy"

    log(f"⚠️ 紧急平仓：{inst_id} {direction} qty={qty}")

    body = {
        "instId": inst_id,
        "tdMode": TD_MODE,
        "side": side,
        "ordType": "market",
        "sz": fmt_decimal(qty),
        "posSide": "net",
        "reduceOnly": True,
        "clOrdId": "EMC26" + str(int(time.time() * 1000))[-20:],
    }

    resp = okx_request(
        "POST",
        "/api/v5/trade/order",
        body=body,
        private=True,
    )

    item = resp["data"][0]
    if str(item.get("sCode", "0")) != "0":
        raise RuntimeError(f"紧急平仓被拒绝：{item}")

    return True


# =========================
# 已有持仓保护恢复
# =========================
def repair_existing_position(pos):
    """
    V2.6 启动/循环保护：
    1. 如果 active_trade 有记录，优先使用原 TP/SL。
    2. 如果没有 active_trade，但能找到旧 conditional SL：
       保留原 SL，根据 RR 推导 TP。
    3. 如果什么都找不到，不猜 SL；默认不自动平仓，避免误判人工仓位。
    """
    inst_id = pos["instId"]
    direction = position_direction(pos)
    qty = abs(float(pos.get("pos", "0") or "0"))
    avg_px = get_position_avg_px(pos)

    if qty <= 0 or avg_px <= 0:
        return False

    active = state.get("active_trade")

    # ---------- 方案 A：有本机器人状态 ----------
    if (
        active
        and active.get("instId") == inst_id
        and active.get("direction") == direction
        and float(active.get("avgPx", 0) or 0) > 0
        and float(active.get("tp", 0) or 0) > 0
        and float(active.get("sl", 0) or 0) > 0
    ):
        algo_id = active.get("algoId")
        algo_cl_ord_id = active.get("algoClOrdId")

        valid = find_valid_oco(
            inst_id,
            qty,
            direction,
            algo_id=algo_id,
        )

        if valid:
            log(
                f"已有持仓保护正常：{inst_id} "
                f"TP={valid.get('tpTriggerPx')} "
                f"SL={valid.get('slTriggerPx')}"
            )
            return True

        log(f"发现持仓但原 OCO 不存在，准备恢复：{inst_id}")

        candidate = {
            "instId": inst_id,
            "direction": direction,
            "price": avg_px,
            "atr": abs(avg_px - float(active["sl"])) / ATR_SL_MULTIPLIER,
        }

        plan = {
            "instId": inst_id,
            "direction": direction,
            "avgPx": avg_px,
            "tp": float(active["tp"]),
            "sl": float(active["sl"]),
            "sz": qty,
        }

        try:
            algo_id_new, clid = place_protection_order(
                candidate,
                plan,
                algo_cl_ord_id=algo_cl_ord_id,
            )

            ok, row = verify_protection_by_algo(
                inst_id,
                algo_id_new,
                clid,
                PROTECTION_TIMEOUT,
            )

            if not ok:
                raise RuntimeError(f"OCO恢复失败：{row}")

            active["algoId"] = algo_id_new
            active["algoClOrdId"] = clid
            active["sz"] = qty
            active["avgPx"] = avg_px
            state["active_trade"] = active
            save_state(state)

            log(
                f"✅ 原 TP/SL 已恢复："
                f"TP={plan['tp']} SL={plan['sl']}"
            )
            return True

        except Exception as e:
            log(f"恢复原 OCO 失败：{e}")
            if EMERGENCY_CLOSE_IF_OCO_FAIL:
                emergency_close_position(pos)
            return False

    # ---------- 方案 B：旧版 V2.5 可能只有 conditional SL ----------
    if REPAIR_UNKNOWN_OLD_SL:
        try:
            algos = get_pending_algos(inst_id)

            old_sl = None
            old_algo_id = None

            for a in algos:
                if str(a.get("ordType")) != "conditional":
                    continue

                sl_px = a.get("slTriggerPx")
                if not sl_px:
                    continue

                try:
                    sl_val = float(sl_px)
                    if sl_val <= 0:
                        continue
                except Exception:
                    continue

                old_sl = sl_val
                old_algo_id = a.get("algoId")
                break

            if old_sl:
                log(
                    f"检测到旧版 conditional SL："
                    f"{inst_id} SL={old_sl}"
                )

                # 取消旧 conditional，避免新旧两套保护同时存在
                if old_algo_id:
                    cancel_algo(inst_id, old_algo_id)

                # 计算 ATR 等效值，让 build_protection_plan 保留旧 SL
                atr_equiv = abs(avg_px - old_sl) / ATR_SL_MULTIPLIER
                candidate = {
                    "instId": inst_id,
                    "direction": direction,
                    "price": avg_px,
                    "atr": max(atr_equiv, avg_px * 0.001),
                }

                plan = build_protection_plan(
                    candidate,
                    avg_px=avg_px,
                    sl_override=old_sl,
                )
                plan["sz"] = qty

                algo_id_new, clid = place_protection_order(
                    candidate,
                    plan,
                )

                ok, row = verify_protection_by_algo(
                    inst_id,
                    algo_id_new,
                    clid,
                    PROTECTION_TIMEOUT,
                )

                if not ok:
                    raise RuntimeError(f"旧 SL 转 OCO 失败：{row}")

                state["active_trade"] = {
                    "instId": inst_id,
                    "direction": direction,
                    "sz": qty,
                    "avgPx": avg_px,
                    "tp": plan["tp"],
                    "sl": plan["sl"],
                    "algoId": algo_id_new,
                    "algoClOrdId": clid,
                }
                save_state(state)

                log(
                    f"✅ 旧 SL 已转换为 OCO："
                    f"TP={plan['tp']} SL={plan['sl']}"
                )
                return True

        except Exception as e:
            log(f"旧 SL 自动转换 OCO 失败：{e}")

    log(
        f"⚠️ {inst_id} 存在持仓，但无法确定原始 TP/SL。"
        f"为避免猜价格，机器人不会主动修改该仓位。"
    )
    return False


# =========================
# 每日状态
# =========================
def reset_daily_if_needed(equity):
    today = datetime.now().strftime("%Y-%m-%d")

    if state.get("date") != today:
        state["date"] = today
        state["start_equity"] = float(equity)
        state["trades"] = 0
        state["consecutive_losses"] = 0
        state["cooldown_until"] = 0
        state["last_trade_inst"] = ""
        save_state(state)
        log(f"新交易日，起始权益={equity:.4f}")

    if not state.get("start_equity"):
        state["start_equity"] = float(equity)
        save_state(state)


def daily_allowed(equity):
    reset_daily_if_needed(equity)

    if state["trades"] >= MAX_DAILY_TRADES:
        log("达到今日最大交易次数，暂停开新仓")
        return False

    if time.time() < float(state.get("cooldown_until", 0) or 0):
        remain = int(state["cooldown_until"] - time.time())
        log(f"连续亏损冷却中，剩余 {max(remain, 0)} 秒")
        return False

    start = float(state["start_equity"])
    if start > 0:
        change_pct = (equity - start) / start

        if change_pct <= -MAX_DAILY_LOSS_PCT:
            log(f"达到每日最大亏损限制：{change_pct:.2%}")
            return False

        if change_pct >= MAX_DAILY_PROFIT_PCT:
            log(f"达到每日盈利限制：{change_pct:.2%}")
            return False

    return True


# =========================
# 交易结果处理
# =========================
def get_realized_pnl_for_position(inst_id):
    """
    尝试从最近成交记录中估算平仓结果。
    不作为安全逻辑，只用于统计。
    """
    try:
        data = okx_request(
            "GET",
            "/api/v5/trade/fills-history",
            params={
                "instType": INST_TYPE,
                "instId": inst_id,
                "limit": "20",
            },
            private=True,
        )["data"]

        if not data:
            return None

        # 返回最近若干成交，尽量找到与本次仓位相关的成交。
        # OKX fill history 的 pnl 字段如果有值直接使用。
        pnl_values = []
        for x in data:
            if x.get("pnl") not in (None, ""):
                try:
                    pnl_values.append(float(x["pnl"]))
                except Exception:
                    pass

        if pnl_values:
            return sum(pnl_values)

    except Exception as e:
        log(f"读取成交盈亏失败：{e}")

    return None


def check_position_closed():
    active = state.get("active_trade")
    if not active:
        return

    inst_id = active.get("instId")
    if not inst_id:
        return

    pos = None
    try:
        positions = get_positions()
        for p in positions:
            if p.get("instId") == inst_id:
                pos = p
                break
    except Exception as e:
        log(f"检查持仓结束状态失败：{e}")
        return

    if pos:
        return

    # 仓位已经不存在
    pnl = get_realized_pnl_for_position(inst_id)

    if pnl is not None:
        log(f"检测到仓位结束：{inst_id}，近期成交统计 PnL≈{pnl:.4f} USDT")

        if pnl < 0:
            state["consecutive_losses"] += 1
            if state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
                state["cooldown_until"] = time.time() + LOSS_COOLDOWN_SECONDS
                log(
                    f"连续亏损达到 {MAX_CONSECUTIVE_LOSSES} 次，"
                    f"冷却 {LOSS_COOLDOWN_SECONDS // 60} 分钟"
                )
        elif pnl > 0:
            state["consecutive_losses"] = 0

    state["active_trade"] = None
    save_state(state)


# =========================
# 全市场扫描
# =========================
def scan_candidates():
    instruments = get_all_swap_instruments()
    tickers = get_tickers()

    rows = []

    for info in instruments:
        inst_id = info["instId"]
        ticker = tickers.get(inst_id)

        if not ticker:
            continue

        try:
            vol24 = float(ticker.get("volCcy24h") or 0)
            last = float(ticker.get("last") or 0)

            if last <= 0:
                continue
            if vol24 < MIN_24H_VOL_USDT:
                continue

            candidate = calculate_signal(inst_id, ticker)
            if not candidate:
                continue

            # 综合评分：信号分数 + 波动/成交活跃度
            volume_score = min(vol24 / 50_000_000, 3.0)
            candidate["rank_score"] = candidate["score"] + volume_score + candidate["atr_pct"] * 100

            rows.append(candidate)

        except Exception:
            continue

    rows.sort(key=lambda x: x["rank_score"], reverse=True)
    return rows[:SCAN_LIMIT]


# =========================
# 执行候选
# =========================
def execute_candidate(candidate, equity):
    inst_id = candidate["instId"]

    # 防止重复交易同一个标的
    if state.get("last_trade_inst") == inst_id:
        log(f"{inst_id} 为最近交易标的，跳过一次")
        return False

    # 设置杠杆
    set_leverage(inst_id, candidate["leverage"])

    sz = calculate_size(candidate, equity)

    if sz <= 0:
        log(
            f"{inst_id} 计算出的合约数量低于最小下单量，"
            f"跳过。价格={candidate['price']}"
        )
        return False

    info = get_instrument(inst_id)
    min_sz = float(info["minSz"])

    if sz < min_sz:
        log(f"{inst_id} sz={sz} < minSz={min_sz}，跳过")
        return False

    log(
        f"准备开仓：{inst_id} "
        f"{candidate['direction']} "
        f"score={candidate['score']} "
        f"lev={candidate['leverage']}x "
        f"sz={sz} "
        f"price={candidate['price']:.8f}"
    )

    # 1. 市价开仓
    ord_id = place_market_order(candidate, sz)
    log(f"开仓订单已提交：ordId={ord_id}")

    # 2. 等待真实成交
    fill = wait_order_filled(inst_id, ord_id)

    avg_px = float(fill.get("avgPx") or fill.get("fillPx") or 0)
    actual_sz = float(
        fill.get("accFillSz")
        or fill.get("fillSz")
        or fill.get("pos")
        or sz
    )

    if avg_px <= 0:
        pos = get_bot_position()
        if not pos or pos.get("instId") != inst_id:
            raise RuntimeError("开仓后无法取得真实成交价")
        avg_px = get_position_avg_px(pos)

    if actual_sz <= 0:
        actual_sz = sz

    log(
        f"真实成交：{inst_id} "
        f"avgPx={avg_px} sz={actual_sz}"
    )

    # 3. 按真实成交价重新计算 TP/SL
    actual_plan = build_protection_plan(
        candidate,
        avg_px=avg_px,
    )
    actual_plan["sz"] = actual_sz

    log(
        f"准备 OCO："
        f"TP={actual_plan['tp']} "
        f"SL={actual_plan['sl']} "
        f"RR={RISK_REWARD}"
    )

    # 4. 建立 OCO
    algo_id, algo_cl_id = place_protection_order(
        candidate,
        actual_plan,
    )

    log(
        f"OCO 策略单已提交："
        f"algoId={algo_id}，客户ID={algo_cl_id}"
    )

    # 5. 严格验证
    ok, algo = verify_protection_by_algo(
        inst_id,
        algo_id,
        algo_cl_id,
        PROTECTION_TIMEOUT,
    )

    if not ok:
        log(f"❌ OCO TP/SL 未确认：{algo}")

        if EMERGENCY_CLOSE_IF_OCO_FAIL:
            pos = get_bot_position()
            if pos and pos.get("instId") == inst_id:
                emergency_close_position(pos)

        raise RuntimeError("OCO TP/SL 未确认，已执行安全处理")

    log(
        f"✅ OCO TP/SL 已确认："
        f"algoId={algo_id} "
        f"TP={algo.get('tpTriggerPx')} "
        f"SL={algo.get('slTriggerPx')}"
    )

    # 6. 保存本次交易保护参数
    state["active_trade"] = {
        "instId": inst_id,
        "direction": candidate["direction"],
        "sz": actual_sz,
        "avgPx": avg_px,
        "tp": actual_plan["tp"],
        "sl": actual_plan["sl"],
        "algoId": algo_id,
        "algoClOrdId": algo_cl_id,
        "openedAt": int(time.time()),
    }
    state["trades"] += 1
    state["last_trade_inst"] = inst_id
    save_state(state)

    log(
        f"🎯 交易建立完成：{inst_id} "
        f"{candidate['direction']} "
        f"TP={actual_plan['tp']} "
        f"SL={actual_plan['sl']}"
    )

    return True


# =========================
# 主循环
# =========================
def main():
    log("=" * 70)
    log("OKX 全市场永续强行情自动交易机器人 V2.6 启动")
    log(f"模拟盘={SIMULATED_TRADING} | BAR={BAR} | TD_MODE={TD_MODE}")
    log("TP/SL 模式：OCO")
    log("=" * 70)

    # 启动账户检查
    equity = get_equity()
    reset_daily_if_needed(equity)
    log(f"当前 USDT 权益：{equity:.4f}")

    # 启动时先处理已有持仓
    try:
        pos = get_bot_position()
        if pos:
            log(
                f"启动发现已有持仓："
                f"{pos.get('instId')} "
                f"{position_direction(pos)} "
                f"qty={pos.get('pos')}"
            )
            repair_existing_position(pos)
        else:
            state["active_trade"] = None
            save_state(state)
    except Exception as e:
        log(f"启动持仓保护检查失败：{e}")

    while True:
        try:
            # 更新权益
            equity = get_equity()
            reset_daily_if_needed(equity)

            # 先检查之前的交易是否已经结束
            check_position_closed()

            # 当前是否已有仓位
            pos = get_bot_position()

            if pos:
                # 有仓位时，绝不继续开新仓。
                # 同时检查 OCO 是否还存在。
                repair_existing_position(pos)
                log(
                    f"当前已有持仓："
                    f"{pos.get('instId')} "
                    f"{position_direction(pos)} "
                    f"qty={pos.get('pos')}，等待 OCO TP/SL 结束"
                )
                time.sleep(LOOP_SECONDS)
                continue

            # 没有仓位时清理 active_trade
            if state.get("active_trade"):
                state["active_trade"] = None
                save_state(state)

            # 每日限制
            if not daily_allowed(equity):
                time.sleep(LOOP_SECONDS)
                continue

            # 扫描市场
            candidates = scan_candidates()

            if not candidates:
                log("本轮没有满足条件的强信号")
                time.sleep(LOOP_SECONDS)
                continue

            # 输出前几名
            preview = []
            for c in candidates[:5]:
                preview.append(
                    f"{c['instId']} {c['direction']} "
                    f"S{c['score']} rank={c['rank_score']:.2f}"
                )
            log("候选：" + " | ".join(preview))

            # 依次尝试最强候选
            traded = False

            for candidate in candidates:
                # 再确认一次没有仓位
                if get_bot_position():
                    break

                try:
                    if execute_candidate(candidate, equity):
                        traded = True
                        break
                except Exception as e:
                    log(
                        f"执行 {candidate['instId']} 失败：{e}"
                    )
                    traceback.print_exc()

                    # 如果已经形成仓位，立即停止尝试其它币
                    if get_bot_position():
                        break

            if not traded:
                log("本轮没有成功建立交易")

        except KeyboardInterrupt:
            log("收到停止指令，机器人退出")
            break

        except Exception as e:
            log(f"主循环异常：{e}")
            traceback.print_exc()

        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
