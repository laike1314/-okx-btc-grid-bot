# -*- coding: utf-8 -*-
"""
临时紧急修复版（已修复语法错误）

问题：仓位仍然过大（用了约20%保证金）
修复：
1. MAX_MARGIN_PCT = 0.01（1%）
2. RISK_PER_TRADE_PCT = 0.001（0.1%）
3. MAX_LEVERAGE = 10

注意：完整策略逻辑已丢失，当前只能做基础测试。
请尽快恢复完整 bot.py。
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

VERSION = "V2.9.5-hotfix"

BASE_URL = "https://www.okx.com"
INST_TYPE = "SWAP"
SETTLE_CCY = "USDT"
TD_MODE = "cross"
POS_MODE = "net"
SIMULATED_TRADING = "1"

TARGET_INST_ID = ""

SIGNAL_BAR = "5m"
TREND_BAR_15M = "15m"
TREND_BAR_1H = "1H"
LOOP_SECONDS = 20

TOP30_CACHE_SECONDS = 600
SCAN_LIMIT = 30
MIN_24H_VOLUME_USD = Decimal("3000000")

EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
ATR_PERIOD = 14

RSI_LONG_MIN = Decimal("52")
RSI_LONG_MAX = Decimal("72")
RSI_SHORT_MIN = Decimal("28")
RSI_SHORT_MAX = Decimal("48")

MIN_SIGNAL_SCORE = 4

# ========== 紧急收紧仓位限制 ==========
MAX_MARGIN_PCT = Decimal("0.01")        # 单笔最大保证金 = 权益的 1%
RISK_PER_TRADE_PCT = Decimal("0.001")   # 单笔最大风险 = 权益的 0.1%
MIN_LEVERAGE = Decimal("5")
MAX_LEVERAGE = Decimal("10")            # 最大杠杆 10x

MAX_CONSECUTIVE_LOSSES = 3
LOSS_COOLDOWN_SECONDS = 1800

TARGET_SL_ROI_PCT = Decimal("1.5")
TARGET_TP_ROI_PCT = Decimal("3.5")
RISK_REWARD = Decimal("1.8")
ATR_SL_FRACTION = Decimal("0.60")
MIN_SL_PRICE_PCT = Decimal("0.0025")
MAX_SL_PRICE_PCT = Decimal("0.025")
MAX_TP_PRICE_PCT = Decimal("0.045")
MIN_TRIGGER_GAP_PCT = Decimal("0.0010")

OCO_RETRY_COUNT = 3
OCO_VERIFY_WAIT = 1.2
EMERGENCY_CLOSE_ON_NEW_POSITION_PROTECTION_FAILURE = True

UTC8 = timezone(timedelta(hours=8))

def now_str():
    return datetime.now(UTC8).strftime("%Y-%m-%d %H:%M:%S")

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logging.Formatter.converter = lambda *args: datetime.now(UTC8).timetuple()
logger = logging.getLogger("okx-bot")

def log(msg):
    logger.info(msg)

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
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

def round_up(value, step):
    value = D(value)
    step = D(step)
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_UP) * step

def clamp(value, low, high):
    value = D(value)
    if value < low: return low
    if value > high: return high
    return value

API_KEY = os.getenv("OKX_API_KEY")
SECRET_KEY = os.getenv("OKX_SECRET_KEY")
PASSPHRASE = os.getenv("OKX_PASSPHRASE")

if not API_KEY or not SECRET_KEY or not PASSPHRASE:
    raise RuntimeError("缺少 OKX API 环境变量：OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE")

session = requests.Session()
session.headers.update({"Content-Type": "application/json", "x-simulated-trading": SIMULATED_TRADING})

def get_timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

def make_signature(timestamp, method, request_path, body=""):
    message = timestamp + method.upper() + request_path + body
    mac = hmac.new(SECRET_KEY.encode("utf-8"), message.encode("utf-8"), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def okx_request(method, path, params=None, body=None, private=False, timeout=15, max_retries=3):
    method = method.upper()
    params = params or {}
    body = body or {}
    query = ""
    if params:
        clean_params = {k: v for k, v in params.items() if v is not None and v != ""}
        if clean_params:
            query = "?" + urlencode(clean_params)
    request_path = path + query
    body_text = ""
    if method in ("POST", "PUT"):
        body_text = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    last_error = None
    for attempt in range(1, max_retries + 1):
        headers = {"Content-Type": "application/json", "x-simulated-trading": SIMULATED_TRADING}
        if private:
            timestamp = get_timestamp()
            sign = make_signature(timestamp, method, request_path, body_text)
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
                response = session.post(url, headers=headers, data=body_text, timeout=timeout)
            elif method == "DELETE":
                response = session.delete(url, headers=headers, timeout=timeout)
            else:
                raise RuntimeError(f"不支持 HTTP 方法: {method}")
            response.raise_for_status()
            data = response.json()
            if str(data.get("code")) != "0":
                err_code = str(data.get("code", ""))
                if err_code in ("50011", "50013", "50026"):
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
    raise RuntimeError(f"OKX 请求失败（已重试{max_retries}次） {method} {request_path}: {last_error}")

print(f"[{now_str()}] OKX Bot {VERSION} 语法错误已修复")
print("MAX_MARGIN_PCT = 0.01 (1%) | RISK_PER_TRADE_PCT = 0.001 (0.1%) | MAX_LEVERAGE = 10")
print("注意：当前是精简版，完整策略逻辑需要恢复完整 bot.py")
