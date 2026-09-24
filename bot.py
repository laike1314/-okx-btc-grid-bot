import os
import json
import time
import hmac
import base64
import hashlib
import urllib.request
import urllib.parse
from decimal import Decimal, ROUND_DOWN

BASE_URL = "https://openapi.okx.com"
INST_ID = "BTC-USDT-SWAP"

API_KEY = os.environ["OKX_API_KEY"]
SECRET_KEY = os.environ["OKX_SECRET_KEY"]
PASSPHRASE = os.environ["OKX_PASSPHRASE"]


def timestamp():
    return time.strftime(
        "%Y-%m-%dT%H:%M:%S.000Z",
        time.gmtime()
    )


def sign(ts, method, request_path, body=""):
    message = ts + method + request_path + body

    mac = hmac.new(
        SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256
    )

    return base64.b64encode(mac.digest()).decode()


def request(method, path, body=None):
    ts = timestamp()

    body_text = json.dumps(
        body,
        separators=(",", ":")
    ) if body else ""

    headers = {
        "Content-Type": "application/json",
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": sign(
            ts,
            method,
            path,
            body_text
        ),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,

        # 关键：模拟盘
        "x-simulated-trading": "1",

        # 避免 403
        "User-Agent": "Mozilla/5.0"
    }

    req = urllib.request.Request(
        BASE_URL + path,
        data=body_text.encode() if body else None,
        headers=headers,
        method=method
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read().decode()
            return json.loads(text)

    except Exception as e:
        print("REQUEST ERROR:", e)
        raise


def get_ticker():
    path = (
        "/api/v5/market/ticker?"
        "instId=" + urllib.parse.quote(INST_ID)
    )

    return request("GET", path)


def get_instrument():
    path = (
        "/api/v5/public/instruments?"
        "instType=SWAP&"
        "instId=" + urllib.parse.quote(INST_ID)
    )

    return request("GET", path)


def get_account_config():
    return request(
        "GET",
        "/api/v5/account/config"
    )


def place_order(price, side):
    body = {
        "instId": INST_ID,

        # 逐仓
        "tdMode": "isolated",

        # Maker 限价单
        "ordType": "post_only",

        "side": side,

        # 0.01 张 = 0.0001 BTC
        "sz": "0.01",

        "px": str(price),

        "clOrdId": (
            "demo_grid_" +
            side +
            "_" +
            str(int(time.time()))
        )
    }

    return request(
        "POST",
        "/api/v5/trade/order",
        body
    )


print("=" * 60)
print("OKX BTC DEMO TEST ORDER")
print("=" * 60)

# --------------------------------------------------
# 1. 获取行情
# --------------------------------------------------

ticker = get_ticker()

print("TICKER:")
print(json.dumps(ticker, indent=2, ensure_ascii=False))

if ticker.get("code") != "0":
    raise RuntimeError("获取行情失败")

last_price = Decimal(
    ticker["data"][0]["last"]
)

print()
print("当前 BTC:", last_price)


# --------------------------------------------------
# 2. 获取合约规格
# --------------------------------------------------

instrument = get_instrument()

print()
print("INSTRUMENT:")
print(json.dumps(
    instrument,
    indent=2,
    ensure_ascii=False
))

if instrument.get("code") != "0":
    raise RuntimeError("获取合约信息失败")

info = instrument["data"][0]

ct_val = Decimal(info["ctVal"])
lot_sz = Decimal(info["lotSz"])
min_sz = Decimal(info["minSz"])

print()
print("ctVal =", ct_val)
print("lotSz =", lot_sz)
print("minSz =", min_sz)

# --------------------------------------------------
# 3. 检查最小下单量
# --------------------------------------------------

btc_qty = ct_val * min_sz
notional = last_price * btc_qty

print()
print("最小 BTC 数量:", btc_qty)
print("最小名义价值:", notional, "USDT")


# --------------------------------------------------
# 4. 查询账户模式
# --------------------------------------------------

config = get_account_config()

print()
print("ACCOUNT CONFIG:")
print(json.dumps(
    config,
    indent=2,
    ensure_ascii=False
))

if config.get("code") != "0":
    raise RuntimeError("获取账户配置失败")

pos_mode = config["data"][0]["posMode"]

print()
print("当前持仓模式:", pos_mode)


# --------------------------------------------------
# 5. 计算测试价格
# --------------------------------------------------

# 买单放在现价下方约 0.4%
buy_price = (
    last_price *
    (Decimal("1") - Decimal("0.004"))
)

# 根据 BTC-USDT-SWAP 的价格精度向下处理
tick_sz = Decimal(info["tickSz"])

buy_price = (
    buy_price / tick_sz
).quantize(
    Decimal("1"),
    rounding=ROUND_DOWN
) * tick_sz

print()
print("测试买入价格:", buy_price)
print("测试数量: 0.0001 BTC")
print("API 数量: 0.01 张")
print("订单类型: post_only")
print("保证金模式: isolated")
print()


# --------------------------------------------------
# 6. 下模拟盘测试单
# --------------------------------------------------

order_body = {
    "instId": INST_ID,
    "tdMode": "isolated",
    "side": "buy",
    "ordType": "post_only",
    "px": str(buy_price),
    "sz": str(min_sz),
    "clOrdId": "demo_test_" + str(int(time.time()))
}

# long/short 模式必须带 posSide
if pos_mode == "long_short_mode":
    order_body["posSide"] = "long"

print("准备发送模拟订单:")
print(json.dumps(
    order_body,
    indent=2,
    ensure_ascii=False
))

result = request(
    "POST",
    "/api/v5/trade/order",
    order_body
)

print()
print("=" * 60)
print("下单结果")
print("=" * 60)

print(json.dumps(
    result,
    indent=2,
    ensure_ascii=False
))

if result.get("code") != "0":
    raise RuntimeError(
        "模拟下单失败: " +
        json.dumps(result, ensure_ascii=False)
    )

print()
print("✅ 模拟盘订单已成功提交")
print("订单 ID:", result["data"][0].get("ordId"))
print("客户端订单 ID:", result["data"][0].get("clOrdId"))
print()
print("注意：post_only 是挂单，不保证立即成交。")
print("请到 OKX 模拟盘 → 当前委托 查看。")
