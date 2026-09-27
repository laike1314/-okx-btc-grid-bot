import os
import time
import json
import math
import hmac
import base64
import hashlib
import threading
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode

import requests

# ============================================================
# OKX 全市场永续强行情自动交易机器人 V2.2
# - 仅模拟盘：SIMULATED_TRADING=1
# - USDT 本位永续：SWAP / -USDT-SWAP
# - 单向持仓 net_mode
# - 全仓 cross
# - 5m 信号 + 15m / 1H 趋势确认
# - 动态杠杆 5~15x，但自动受每个合约最大杠杆限制
# - 风险仓位：单笔最多亏损约账户权益 0.5%
# - ATR 止损 + RR 止盈
# - 单次只允许一个持仓
# - 每日达到目标或达到日亏损上限后停止开新仓
# - 主订单必须 fully filled；TP/SL 未确认则紧急平仓
# ============================================================

BASE_URL = os.getenv("OKX_BASE_URL", "https://openapi.okx.com")
API_KEY = os.getenv("OKX_API_KEY", "")
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

SIMULATED_TRADING = os.getenv("SIMULATED_TRADING", "1")
INST_TYPE = "SWAP"
TD_MODE = "cross"

# ---------- 扫描 ----------
BAR = "5m"
LOOP_SECONDS = 20
LIQUIDITY_MIN_USDT = float(os.getenv("LIQUIDITY_MIN_USDT", "5000000"))
MAX_SCAN_SYMBOLS = int(os.getenv("MAX_SCAN_SYMBOLS", "150"))
SCAN_WORKERS = int(os.getenv("SCAN_WORKERS", "8"))
SCAN_REQUEST_INTERVAL = float(os.getenv("SCAN_REQUEST_INTERVAL", "0.055"))

# ---------- 信号 ----------
MIN_SIGNAL_SCORE = int(os.getenv("MIN_SIGNAL_SCORE", "8"))
RSI_LONG_MIN = 52.0
RSI_LONG_MAX = 72.0
RSI_SHORT_MIN = 28.0
RSI_SHORT_MAX = 48.0
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
ATR_PERIOD = 14
VOLUME_LOOKBACK = 20
BREAKOUT_LOOKBACK = 20

# ---------- 风控 ----------
RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", "0.005"))
MAX_MARGIN_PCT = float(os.getenv("MAX_MARGIN_PCT", "0.05"))
MIN_LEVERAGE = int(os.getenv("MIN_LEVERAGE", "5"))
MAX_LEVERAGE = int(os.getenv("MAX_LEVERAGE", "15"))
ATR_SL_MULTIPLIER = float(os.getenv("ATR_SL_MULTIPLIER", "1.5"))
RISK_REWARD = float(os.getenv("RISK_REWARD", "2.0"))
MIN_RR = float(os.getenv("MIN_RR", "1.8"))
DAILY_PROFIT_TARGET_PCT = float(os.getenv("DAILY_PROFIT_TARGET_PCT", "0.03"))
DAILY_MAX_LOSS_PCT = float(os.getenv("DAILY_MAX_LOSS_PCT", "0.02"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
LOSS_COOLDOWN_SECONDS = int(os.getenv("LOSS_COOLDOWN_SECONDS", "1800"))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "8"))

# ---------- 文件 ----------
STATE_FILE = os.getenv("STATE_FILE", "multi_coin_bot_state_v22.json")
REQUEST_TIMEOUT = 12

session = requests.Session()
session.headers.update({"User-Agent": "OKX-MultiCoin-Bot-V2.2"})

_print_lock = threading.Lock()
_rate_lock = threading.Lock()
_last_market_request = 0.0


def log(msg):
    with _print_lock:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def require_env():
    missing = []
    if not API_KEY:
        missing.append("OKX_API_KEY")
    if not SECRET_KEY:
        missing.append("OKX_SECRET_KEY")
    if not PASSPHRASE:
        missing.append("OKX_PASSPHRASE")
    if missing:
        raise RuntimeError("缺少环境变量：" + ", ".join(missing))


def iso_timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sign(timestamp, method, request_path, body=""):
    message = timestamp + method.upper() + request_path + body
    digest = hmac.new(SECRET_KEY.encode(), message.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def okx_request(method, path, params=None, body=None, private=False, timeout=REQUEST_TIMEOUT):
    method = method.upper()
    params = params or {}
    body = body or {}

    if method == "GET":
        query = urlencode(params)
        request_path = path + ("?" + query if query else "")
        payload = ""
    else:
        request_path = path
        payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

    headers = {"Content-Type": "application/json"}
    if SIMULATED_TRADING == "1":
        headers["x-simulated-trading"] = "1"

    if private:
        ts = iso_timestamp()
        headers.update({
            "OK-ACCESS-KEY": API_KEY,
            "OK-ACCESS-SIGN": sign(ts, method, request_path, payload),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        })

    url = BASE_URL.rstrip("/") + request_path
    r = session.request(method, url, params=None if method == "GET" else params,
                        data=payload if method != "GET" else None,
                        headers=headers, timeout=timeout)
    r.raise_for_status()
    data = r.json()

    if str(data.get("code", "0")) != "0":
        raise RuntimeError(f"OKX {method} {path} ERROR: {data}")

    return data


def market_request(path, params):
    global _last_market_request
    with _rate_lock:
        now = time.monotonic()
        wait = SCAN_REQUEST_INTERVAL - (now - _last_market_request)
        if wait > 0:
            time.sleep(wait)
        _last_market_request = time.monotonic()
    return okx_request("GET", path, params=params, private=False)


# ============================================================
# 数学/指标
# ============================================================

def ema(values, period):
    if len(values) < period:
        return []
    k = 2.0 / (period + 1.0)
    out = [sum(values[:period]) / period]
    for x in values[period:]:
        out.append(x * k + out[-1] * (1.0 - k))
    return out


def rsi(values, period=14):
    if len(values) <= period:
        return []
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result = []
    result.append(100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        result.append(100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
    return result


def atr(highs, lows, closes, period=14):
    if len(closes) <= period:
        return []
    trs = []
    for i in range(len(closes)):
        if i == 0:
            tr = highs[i] - lows[i]
        else:
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    out = [sum(trs[:period]) / period]
    for x in trs[period:]:
        out.append((out[-1] * (period - 1) + x) / period)
    return out


def macd(values, fast=12, slow=26, signal=9):
    if len(values) < slow + signal:
        return [], [], []
    ef = ema(values, fast)
    es = ema(values, slow)
    # Align EMA fast to slow EMA timeline.
    offset = slow - fast
    fast_aligned = ef[offset:]
    macd_line = [a - b for a, b in zip(fast_aligned, es)]
    sig = ema(macd_line, signal)
    if not sig:
        return [], [], []
    macd_aligned = macd_line[signal - 1:]
    hist = [m - s for m, s in zip(macd_aligned, sig)]
    return macd_aligned, sig, hist


def mean(values):
    return sum(values) / len(values) if values else 0.0


def floor_decimal(value, step):
    v = Decimal(str(value))
    s = Decimal(str(step))
    if s <= 0:
        return v
    return (v / s).to_integral_value(rounding=ROUND_DOWN) * s


def ceil_decimal(value, step):
    v = Decimal(str(value))
    s = Decimal(str(step))
    if s <= 0:
        return v
    return (v / s).to_integral_value(rounding=ROUND_UP) * s


def fmt_decimal(value, max_places=18):
    d = Decimal(str(value))
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


# ============================================================
# 状态
# ============================================================

def load_state():
    today = datetime.now().strftime("%Y-%m-%d")
    default = {
        "date": today,
        "start_equity": None,
        "trades": 0,
        "consecutive_losses": 0,
        "cooldown_until": 0,
        "last_trade_inst": "",
    }
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        if state.get("date") != today:
            state = default
        else:
            for k, v in default.items():
                state.setdefault(k, v)
        return state
    except Exception:
        return default


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


# ============================================================
# 账户/持仓
# ============================================================

def get_account_config():
    return okx_request("GET", "/api/v5/account/config", private=True)["data"][0]


def check_account_mode():
    config = get_account_config()
    pos_mode = config.get("posMode", "")
    log(f"账户持仓模式：{pos_mode}")
    if pos_mode != "net_mode":
        raise RuntimeError(
            f"当前账户不是单向持仓。OKX API返回：{pos_mode}；请在OKX切换为单向持仓。"
        )
    log("账户持仓模式确认：单向持仓 OK")
    return config


def get_equity():
    data = okx_request("GET", "/api/v5/account/balance", private=True)["data"][0]
    eq = float(data.get("adjEq") or data.get("totalEq") or 0)
    if eq <= 0:
        for item in data.get("details", []):
            if item.get("ccy") == "USDT":
                eq = float(item.get("eq") or 0)
                break
    return eq


def get_open_positions():
    data = okx_request("GET", "/api/v5/account/positions", params={"instType": INST_TYPE}, private=True)["data"]
    positions = []
    for p in data:
        try:
            pos = float(p.get("pos") or 0)
        except Exception:
            pos = 0.0
        if abs(pos) <= 0:
            continue
        positions.append(p)
    return positions


def get_bot_position():
    positions = get_open_positions()
    if not positions:
        return None
    # 机器人只允许全账户一个持仓。发现任何持仓都停止开新仓。
    return positions[0]


# ============================================================
# 产品/杠杆
# ============================================================

def get_instruments():
    data = okx_request("GET", "/api/v5/public/instruments", params={"instType": INST_TYPE}, private=False)["data"]
    result = {}
    for x in data:
        inst_id = x.get("instId", "")
        if not inst_id.endswith("-USDT-SWAP"):
            continue
        if x.get("state") != "live":
            continue
        if x.get("settleCcy") != "USDT":
            continue
        if x.get("ctType") != "linear":
            continue
        try:
            max_lev = int(float(x.get("lever") or 0))
        except Exception:
            max_lev = 0
        if max_lev <= 0:
            continue
        result[inst_id] = x
    return result


def get_leverage_info(inst_id):
    try:
        data = okx_request(
            "GET", "/api/v5/account/leverage-info",
            params={"instId": inst_id, "mgnMode": TD_MODE}, private=True
        )["data"]
        for x in data:
            if x.get("instId") == inst_id:
                return float(x.get("lever") or 0)
    except Exception as e:
        log(f"读取当前杠杆失败 {inst_id}: {e}")
    return 0.0


def set_leverage(inst_id, leverage, max_allowed):
    # 直接用 public/instruments 返回的原始 instId，避免错误拼接导致 51001。
    leverage = int(min(leverage, max_allowed, MAX_LEVERAGE))
    if leverage < MIN_LEVERAGE:
        return None

    body = {
        "instId": inst_id,
        "lever": str(leverage),
        "mgnMode": TD_MODE,
    }
    try:
        resp = okx_request("POST", "/api/v5/account/set-leverage", body=body, private=True)
        actual = 0.0
        if resp.get("data"):
            actual = float(resp["data"][0].get("lever") or 0)
        if actual <= 0:
            actual = get_leverage_info(inst_id)
        if actual <= 0:
            actual = float(leverage)
        log(f"杠杆设置成功：{inst_id} → {actual:g}x（合约最大 {max_allowed}x）")
        return int(round(actual))
    except Exception as e:
        log(f"设置杠杆失败：{inst_id} → {leverage}x：{e}")
        return None


# ============================================================
# 行情
# ============================================================

def get_tickers():
    return okx_request("GET", "/api/v5/market/tickers", params={"instType": INST_TYPE}, private=False)["data"]


def get_candles(inst_id, bar="5m", limit=100):
    data = market_request(
        "/api/v5/market/candles",
        {"instId": inst_id, "bar": bar, "limit": str(min(limit, 300))}
    )["data"]
    rows = []
    for r in data:
        if len(r) < 9:
            continue
        try:
            rows.append({
                "ts": int(r[0]),
                "o": float(r[1]),
                "h": float(r[2]),
                "l": float(r[3]),
                "c": float(r[4]),
                "vol": float(r[5]),
                "vol_ccy": float(r[6]),
                "vol_quote": float(r[7]),
                "confirm": str(r[8]),
            })
        except Exception:
            continue
    rows = [x for x in rows if x["confirm"] == "1"]
    rows.sort(key=lambda x: x["ts"])
    return rows


def analyze_5m(inst_id, instrument, ticker):
    candles = get_candles(inst_id, BAR, 100)
    if len(candles) < 60:
        return None

    closes = [x["c"] for x in candles]
    highs = [x["h"] for x in candles]
    lows = [x["l"] for x in candles]
    volumes = [x["vol_quote"] for x in candles]

    ef = ema(closes, EMA_FAST)[-1]
    es = ema(closes, EMA_SLOW)[-1]
    rsi_v = rsi(closes, RSI_PERIOD)[-1]
    atr_v = atr(highs, lows, closes, ATR_PERIOD)[-1]
    macd_line, macd_sig, macd_hist = macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    if not macd_line or not macd_sig:
        return None
    m = macd_line[-1]
    ms = macd_sig[-1]

    price = closes[-1]
    atr_pct = atr_v / price * 100 if price else 0
    vol_avg = mean(volumes[-(VOLUME_LOOKBACK + 1):-1])
    vol_ratio = volumes[-1] / vol_avg if vol_avg > 0 else 0

    prev_high = max(highs[-(BREAKOUT_LOOKBACK + 1):-1])
    prev_low = min(lows[-(BREAKOUT_LOOKBACK + 1):-1])
    breakout_long = price > prev_high
    breakout_short = price < prev_low

    score_long = 0
    score_short = 0
    reasons_long = []
    reasons_short = []

    if ef > es:
        score_long += 2; reasons_long.append("EMA多头")
    elif ef < es:
        score_short += 2; reasons_short.append("EMA空头")

    if price > ef:
        score_long += 1; reasons_long.append("价格>EMA9")
    elif price < ef:
        score_short += 1; reasons_short.append("价格<EMA9")

    if RSI_LONG_MIN <= rsi_v <= RSI_LONG_MAX:
        score_long += 1; reasons_long.append("RSI多头区")
    if RSI_SHORT_MIN <= rsi_v <= RSI_SHORT_MAX:
        score_short += 1; reasons_short.append("RSI空头区")

    if m > ms and m > 0:
        score_long += 2; reasons_long.append("MACD强多")
    elif m < ms and m < 0:
        score_short += 2; reasons_short.append("MACD强空")

    if vol_ratio >= 2.0:
        score_long += 2; score_short += 2
        reasons_long.append(f"放量{vol_ratio:.2f}x")
        reasons_short.append(f"放量{vol_ratio:.2f}x")
    elif vol_ratio >= 1.4:
        score_long += 1; score_short += 1
        reasons_long.append(f"放量{vol_ratio:.2f}x")
        reasons_short.append(f"放量{vol_ratio:.2f}x")

    if breakout_long:
        score_long += 2; reasons_long.append("20K突破")
    if breakout_short:
        score_short += 2; reasons_short.append("20K跌破")

    body1 = closes[-1] - candles[-1]["o"]
    body2 = closes[-2] - candles[-2]["o"]
    if body1 > 0 and body2 > 0:
        score_long += 1; reasons_long.append("连续2K上涨")
    if body1 < 0 and body2 < 0:
        score_short += 1; reasons_short.append("连续2K下跌")

    direction = "LONG" if score_long > score_short else "SHORT" if score_short > score_long else "WAIT"
    score = max(score_long, score_short)
    if direction == "WAIT" or score < MIN_SIGNAL_SCORE:
        return None

    # 动量太弱的不做。
    move5 = (closes[-1] / closes[-4] - 1.0) * 100 if closes[-4] else 0
    if direction == "LONG" and move5 < 0:
        return None
    if direction == "SHORT" and move5 > 0:
        return None

    return {
        "instId": inst_id,
        "direction": direction,
        "score": score,
        "price": price,
        "atr": atr_v,
        "atr_pct": atr_pct,
        "rsi": rsi_v,
        "volume_ratio": vol_ratio,
        "move5": move5,
        "max_leverage": int(float(instrument.get("lever") or 0)),
        "ctVal": float(instrument["ctVal"]),
        "lotSz": float(instrument["lotSz"]),
        "minSz": float(instrument["minSz"]),
        "tickSz": float(instrument["tickSz"]),
        "reasons": reasons_long if direction == "LONG" else reasons_short,
        "ticker": ticker,
    }


def get_htf_trend(inst_id):
    c15 = get_candles(inst_id, "15m", 80)
    c1h = get_candles(inst_id, "1H", 80)
    if len(c15) < 55 or len(c1h) < 55:
        return None

    e15f = ema([x["c"] for x in c15], 20)[-1]
    e15s = ema([x["c"] for x in c15], 50)[-1]
    e1f = ema([x["c"] for x in c1h], 20)[-1]
    e1s = ema([x["c"] for x in c1h], 50)[-1]

    return {
        "15m": "LONG" if e15f > e15s else "SHORT",
        "1h": "LONG" if e1f > e1s else "SHORT",
    }


def rank_candidates(instruments):
    tickers = get_tickers()
    ticker_map = {}
    liquidity_map = {}
    for t in tickers:
        inst_id = t.get("instId", "")
        if inst_id not in instruments:
            continue
        try:
            last = float(t.get("last") or 0)
            vol_contracts = float(t.get("vol24h") or 0)
            ct_val = float(instruments[inst_id].get("ctVal") or 0)
        except Exception:
            continue
        if last <= 0 or vol_contracts <= 0 or ct_val <= 0:
            continue
        # 对线性永续，用 24h 成交张数 × 合约面值 × 最新价估算 USDT 名义成交额。
        quote_vol = vol_contracts * ct_val * last
        if quote_vol < LIQUIDITY_MIN_USDT:
            continue
        ticker_map[inst_id] = t
        liquidity_map[inst_id] = quote_vol

    ranked_liquidity = sorted(
        ticker_map.keys(),
        key=lambda x: liquidity_map[x],
        reverse=True
    )
    symbols = ranked_liquidity[:MAX_SCAN_SYMBOLS]
    log(f"流动性筛选：{len(instruments)} → {len(ticker_map)}；本轮扫描K线：{len(symbols)}")

    results = []
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        futures = {
            pool.submit(analyze_5m, s, instruments[s], ticker_map[s]): s
            for s in symbols
        }
        for f in as_completed(futures):
            s = futures[f]
            try:
                x = f.result()
                if x:
                    results.append(x)
            except Exception as e:
                log(f"5m扫描失败 {s}: {e}")

    results.sort(key=lambda x: (x["score"], x["volume_ratio"], x["atr_pct"]), reverse=True)
    log(f"5m强行情候选：{len(results)}")

    confirmed = []
    for x in results[:10]:
        try:
            trend = get_htf_trend(x["instId"])
            if not trend:
                continue
            x["trend15m"] = trend["15m"]
            x["trend1h"] = trend["1h"]
            if trend["15m"] == x["direction"] and trend["1h"] == x["direction"]:
                confirmed.append(x)
        except Exception as e:
            log(f"多周期确认失败 {x['instId']}: {e}")

    confirmed.sort(key=lambda x: (x["score"], x["volume_ratio"], x["atr_pct"]), reverse=True)
    return confirmed


# ============================================================
# 仓位 / 订单
# ============================================================

def theoretical_leverage(score):
    if score >= 14:
        return 15
    if score >= 12:
        return 12
    if score >= 10:
        return 10
    return 7


def build_trade_plan(candidate, equity, actual_leverage):
    price = candidate["price"]
    atr_v = candidate["atr"]
    direction = candidate["direction"]
    ct_val = candidate["ctVal"]
    lot_sz = candidate["lotSz"]
    min_sz = candidate["minSz"]
    tick_sz = candidate["tickSz"]

    stop_distance = atr_v * ATR_SL_MULTIPLIER
    if stop_distance <= 0:
        return None

    if direction == "LONG":
        sl_raw = price - stop_distance
        tp_raw = price + stop_distance * RISK_REWARD
        sl = float(floor_decimal(sl_raw, tick_sz))
        tp = float(floor_decimal(tp_raw, tick_sz))
        if sl <= 0 or tp <= price or sl >= price:
            return None
    else:
        sl_raw = price + stop_distance
        tp_raw = price - stop_distance * RISK_REWARD
        sl = float(ceil_decimal(sl_raw, tick_sz))
        tp = float(ceil_decimal(tp_raw, tick_sz))
        if tp <= 0 or tp >= price or sl <= price:
            return None

    if direction == "LONG":
        rr = (tp - price) / (price - sl)
    else:
        rr = (price - tp) / (sl - price)
    if rr < MIN_RR:
        return None

    risk_usdt = equity * RISK_PER_TRADE_PCT
    risk_per_contract = stop_distance * ct_val
    if risk_per_contract <= 0:
        return None
    raw_sz = risk_usdt / risk_per_contract

    max_margin = equity * MAX_MARGIN_PCT
    max_notional = max_margin * actual_leverage
    max_sz_by_margin = max_notional / (price * ct_val)
    raw_sz = min(raw_sz, max_sz_by_margin)

    sz = floor_decimal(raw_sz, lot_sz)
    if sz < Decimal(str(min_sz)):
        return None

    # 由于是全仓，实际保证金大致为 notional / leverage；这里仅用于日志和二次检查。
    notional = float(sz) * ct_val * price
    margin = notional / actual_leverage if actual_leverage > 0 else float("inf")

    return {
        "sz": fmt_decimal(sz),
        "price": price,
        "sl": sl,
        "tp": tp,
        "rr": rr,
        "risk_usdt": risk_usdt,
        "notional": notional,
        "margin": margin,
        "leverage": actual_leverage,
        "stop_distance": stop_distance,
    }


def get_max_size(inst_id, leverage):
    try:
        data = okx_request(
            "GET", "/api/v5/account/max-size",
            params={"instId": inst_id, "tdMode": TD_MODE, "leverage": str(leverage)},
            private=True,
        )["data"]
        if data:
            # maxBuy/maxSell 在 net mode 下分别对应开多/开空能力。
            return float(data[0].get("maxBuy") or 0), float(data[0].get("maxSell") or 0)
    except Exception as e:
        log(f"读取最大可开仓数量失败 {inst_id}: {e}")
    return 0.0, 0.0


def place_market_order(candidate, plan):
    inst_id = candidate["instId"]
    direction = candidate["direction"]
    side = "buy" if direction == "LONG" else "sell"
    cl_ord_id = "MCV22" + str(int(time.time() * 1000))[-20:]
    algo_cl_id = "ALGOV22" + str(int(time.time() * 1000))[-18:]

    body = {
        "instId": inst_id,
        "tdMode": TD_MODE,
        "clOrdId": cl_ord_id,
        "side": side,
        "ordType": "market",
        "sz": plan["sz"],
        "attachAlgoOrds": [{
            "attachAlgoClOrdId": algo_cl_id,
            "tpTriggerPx": fmt_decimal(plan["tp"]),
            "tpOrdPx": "-1",
            "tpTriggerPxType": "mark",
            "slTriggerPx": fmt_decimal(plan["sl"]),
            "slOrdPx": "-1",
            "slTriggerPxType": "mark",
        }],
    }

    # net_mode 下 posSide 可省略；省略可以兼容更多账户配置。
    log(f"准备下单：{inst_id} {direction} {plan['sz']}张，{plan['leverage']}x，TP={plan['tp']} SL={plan['sl']}")
    resp = okx_request("POST", "/api/v5/trade/order", body=body, private=True)
    item = resp["data"][0]
    if str(item.get("sCode", "0")) != "0":
        raise RuntimeError(f"下单被OKX拒绝：{item}")
    ord_id = item.get("ordId")
    if not ord_id:
        raise RuntimeError(f"OKX未返回ordId：{resp}")
    return ord_id, algo_cl_id


def get_order(inst_id, ord_id):
    data = okx_request(
        "GET", "/api/v5/trade/order",
        params={"instId": inst_id, "ordId": ord_id}, private=True
    )["data"]
    return data[0] if data else None


def cancel_order(inst_id, ord_id):
    try:
        okx_request(
            "POST", "/api/v5/trade/cancel-order",
            body={"instId": inst_id, "ordId": ord_id}, private=True
        )
    except Exception as e:
        log(f"取消订单失败 {inst_id}/{ord_id}: {e}")


def wait_order_filled(inst_id, ord_id, timeout_sec=12):
    end = time.time() + timeout_sec
    last = None
    while time.time() < end:
        try:
            last = get_order(inst_id, ord_id)
            if not last:
                time.sleep(0.5)
                continue
            state = last.get("state")
            if state == "filled":
                return last
            if state in ("canceled", "mmp_canceled"):
                return last
        except Exception as e:
            log(f"查询订单状态失败：{e}")
        time.sleep(0.7)
    return last


def get_pending_algos(inst_id):
    try:
        data = okx_request(
            "GET", "/api/v5/trade/orders-algo-pending",
            params={"ordType": "conditional", "instType": INST_TYPE, "instId": inst_id},
            private=True,
        )["data"]
        return data
    except Exception as e:
        log(f"查询TP/SL策略单失败：{e}")
        return []


def verify_protection(inst_id, algo_cl_id, timeout_sec=6):
    end = time.time() + timeout_sec
    while time.time() < end:
        pending = get_pending_algos(inst_id)
        for a in pending:
            if a.get("algoClOrdId") == algo_cl_id or a.get("attachAlgoClOrdId") == algo_cl_id:
                state = a.get("state", "")
                if state in ("live", "effective"):
                    return True, a
        time.sleep(0.8)
    return False, None


def close_position_market(position):
    inst_id = position.get("instId")
    pos = float(position.get("pos") or 0)
    if not inst_id or pos == 0:
        return
    side = "sell" if pos > 0 else "buy"
    sz = fmt_decimal(abs(pos))
    body = {
        "instId": inst_id,
        "tdMode": TD_MODE,
        "side": side,
        "ordType": "market",
        "sz": sz,
        "reduceOnly": True,
    }
    log(f"紧急平仓：{inst_id} {side} {sz}张")
    try:
        okx_request("POST", "/api/v5/trade/order", body=body, private=True)
    except Exception as e:
        log(f"紧急平仓失败：{e}")


# ============================================================
# 每日风控
# ============================================================

def daily_stats(state, equity):
    if state.get("start_equity") is None:
        state["start_equity"] = equity
        save_state(state)
    start = float(state["start_equity"] or equity)
    pnl_pct = (equity / start - 1.0) if start > 0 else 0.0
    return pnl_pct


def can_trade(state, equity):
    pnl_pct = daily_stats(state, equity)
    if pnl_pct >= DAILY_PROFIT_TARGET_PCT:
        log(f"达到今日目标：{pnl_pct * 100:.2f}% ≥ {DAILY_PROFIT_TARGET_PCT * 100:.2f}%，停止开新仓")
        return False
    if pnl_pct <= -DAILY_MAX_LOSS_PCT:
        log(f"达到今日最大亏损：{pnl_pct * 100:.2f}% ≤ {-DAILY_MAX_LOSS_PCT * 100:.2f}%，停止开新仓")
        return False
    if state["trades"] >= MAX_TRADES_PER_DAY:
        log(f"今日交易次数已达到上限：{state['trades']}")
        return False
    if state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
        if time.time() < state.get("cooldown_until", 0):
            left = int(state["cooldown_until"] - time.time())
            log(f"连续亏损熔断中，剩余 {max(left, 0)} 秒")
            return False
        state["consecutive_losses"] = 0
        save_state(state)
    return True


def refresh_trade_stats(state):
    # 仅使用本机器人本次运行记录的本地交易结果；真正日收益以账户权益变化为准。
    # 这里不猜测其他手工交易。
    return state


# ============================================================
# 一次完整交易机会
# ============================================================

def execute_candidate(candidate, state, equity):
    inst_id = candidate["instId"]
    max_lev = candidate["max_leverage"]
    desired_lev = theoretical_leverage(candidate["score"])

    log("")
    log("================ 候选交易 ================")
    log(f"合约       : {inst_id}")
    log(f"方向       : {candidate['direction']}")
    log(f"评分       : {candidate['score']}")
    log(f"价格       : {candidate['price']}")
    log(f"ATR        : {candidate['atr']}")
    log(f"ATR%       : {candidate['atr_pct']:.3f}%")
    log(f"RSI        : {candidate['rsi']:.2f}")
    log(f"成交量     : {candidate['volume_ratio']:.2f}x")
    log(f"15m趋势    : {candidate.get('trend15m')}")
    log(f"1H趋势     : {candidate.get('trend1h')}")
    log(f"合约最大杠杆: {max_lev}x")
    log(f"理论杠杆   : {desired_lev}x")
    log(f"信号依据   : {' / '.join(candidate['reasons'])}")

    if max_lev < MIN_LEVERAGE:
        log(f"该合约最大杠杆仅 {max_lev}x < 最低要求 {MIN_LEVERAGE}x，放弃交易")
        return False

    actual_target_lev = min(desired_lev, max_lev, MAX_LEVERAGE)
    actual_lev = set_leverage(inst_id, actual_target_lev, max_lev)
    if actual_lev is None or actual_lev < MIN_LEVERAGE:
        log("无法设置可用杠杆，放弃交易")
        return False

    plan = build_trade_plan(candidate, equity, actual_lev)
    if not plan:
        log("仓位/止盈止损计算不满足条件，放弃交易")
        return False

    max_buy, max_sell = get_max_size(inst_id, actual_lev)
    allowed = max_buy if candidate["direction"] == "LONG" else max_sell
    if allowed > 0 and float(plan["sz"]) > allowed:
        lot = Decimal(str(candidate["lotSz"]))
        reduced = floor_decimal(allowed, lot)
        if reduced < Decimal(str(candidate["minSz"])):
            log(f"OKX最大可开仓量不足最小下单量：{allowed}")
            return False
        plan["sz"] = fmt_decimal(reduced)
        plan["notional"] = float(reduced) * candidate["ctVal"] * candidate["price"]
        plan["margin"] = plan["notional"] / actual_lev
        log(f"按OKX最大可开仓量缩小仓位：{plan['sz']}张")

    log(f"实际杠杆   : {actual_lev}x")
    log(f"合约数量   : {plan['sz']} 张")
    log(f"名义价值   : {plan['notional']:.2f} USDT")
    log(f"保证金估算 : {plan['margin']:.2f} USDT")
    log(f"理论风险   : {plan['risk_usdt']:.2f} USDT")
    log(f"止损       : {plan['sl']}")
    log(f"止盈       : {plan['tp']}")
    log(f"RR         : {plan['rr']:.2f}")

    ord_id = None
    try:
        ord_id, algo_cl_id = place_market_order(candidate, plan)
        log(f"主订单已提交：ordId={ord_id}")

        order = wait_order_filled(inst_id, ord_id, 12)
        if not order:
            raise RuntimeError("下单后无法查询订单状态")

        state_order = order.get("state")
        acc_fill = float(order.get("accFillSz") or 0)
        avg_px = order.get("avgPx") or ""
        log(f"订单状态：{state_order}，成交量={acc_fill}，均价={avg_px}")

        if state_order != "filled":
            if state_order in ("live", "partially_filled"):
                cancel_order(inst_id, ord_id)
                time.sleep(1)
            pos = get_bot_position()
            if pos:
                close_position_market(pos)
            log("主订单未完全成交，已取消/清理，放弃本次交易")
            return False

        # 主订单 fully filled 后，OKX 才会生成 attached TP/SL；再检查一次。
        ok, algo = verify_protection(inst_id, algo_cl_id, 7)
        if not ok:
            log("严重警告：主订单已成交，但未确认TP/SL策略单")
            pos = get_bot_position()
            if pos:
                close_position_market(pos)
            log("已紧急平仓，避免裸仓")
            return False

        log(f"TP/SL 已确认：algoId={algo.get('algoId')} state={algo.get('state')}")
        state["trades"] += 1
        state["last_trade_inst"] = inst_id
        save_state(state)
        log("交易进入保护状态，机器人等待TP/SL结束仓位")
        return True

    except Exception as e:
        log(f"交易执行异常：{e}")
        try:
            if ord_id:
                order = get_order(inst_id, ord_id)
                if order and order.get("state") in ("live", "partially_filled"):
                    cancel_order(inst_id, ord_id)
        except Exception:
            pass
        try:
            pos = get_bot_position()
            if pos:
                close_position_market(pos)
        except Exception:
            pass
        return False


# ============================================================
# 主循环
# ============================================================

def print_account_header(state, equity):
    pnl_pct = daily_stats(state, equity)
    log("")
    log("==============================================")
    log(f"账户权益：{equity:.2f} USDT")
    log(f"今日收益：{pnl_pct * 100:.2f}%")
    log(f"今日交易：{state['trades']}")
    log(f"连续亏损：{state['consecutive_losses']}")
    log("==============================================")


def main():
    require_env()
    if SIMULATED_TRADING != "1":
        raise RuntimeError("V2.2 目前只允许模拟盘运行。请保持 SIMULATED_TRADING=1。")

    log("启动 OKX 全市场永续强行情自动交易机器人 V2.2")
    log(f"API地址：{BASE_URL}")
    log("交易模式：模拟盘 / SWAP / USDT / cross / net_mode")

    check_account_mode()
    instruments = get_instruments()
    log(f"加载USDT线性永续合约：{len(instruments)} 个")

    state = load_state()
    equity = get_equity()
    daily_stats(state, equity)
    print_account_header(state, equity)

    while True:
        try:
            equity = get_equity()
            print_account_header(state, equity)

            # 只要账户有任何持仓，就不再开新仓。
            position = get_bot_position()
            if position:
                log(
                    f"当前已有持仓：{position.get('instId')} "
                    f"pos={position.get('pos')} avgPx={position.get('avgPx')} "
                    f"upl={position.get('upl')}，等待TP/SL结束"
                )
                time.sleep(LOOP_SECONDS)
                continue

            if not can_trade(state, equity):
                log("当前风控条件不允许开仓，继续观察市场但不下单")
                time.sleep(max(LOOP_SECONDS, 60))
                continue

            log("开始扫描 OKX 全部 USDT 永续合约")
            candidates = rank_candidates(instruments)
            if not candidates:
                log("本轮没有通过5m + 15m + 1H的强行情")
                time.sleep(LOOP_SECONDS)
                continue

            candidate = candidates[0]
            log(f"最终候选：{candidate['instId']} {candidate['direction']} 评分={candidate['score']}")

            # 再检查一次持仓，防止扫描期间出现人工/其他程序仓位。
            if get_bot_position():
                log("扫描期间出现持仓，取消本次开仓")
                time.sleep(LOOP_SECONDS)
                continue

            execute_candidate(candidate, state, equity)
            time.sleep(LOOP_SECONDS)

        except KeyboardInterrupt:
            log("收到停止信号，机器人退出")
            break
        except Exception as e:
            log(f"主循环异常：{e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
