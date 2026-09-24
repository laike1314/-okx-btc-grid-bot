import os
import json
import time
import hmac
import base64
import hashlib
import urllib.request
import urllib.error
import urllib.parse

from decimal import Decimal, ROUND_DOWN


# ============================================================
# OKX DEMO CONFIG
# ============================================================

BASE_URL = "https://openapi.okx.com"

INST_ID = "BTC-USDT-SWAP"

# 本次只测试最小订单
BTC_QTY = Decimal("0.0001")

# 测试挂单距离现价
GRID_DISTANCE = Decimal("0.004")   # 0.4%


# ============================================================
# API SECRETS
# ============================================================

API_KEY = os.environ["OKX_API_KEY"]
SECRET_KEY = os.environ["OKX_SECRET_KEY"]
PASSPHRASE = os.environ["OKX_PASSPHRASE"]


# ============================================================
# JSON
# ============================================================

def json_body(data):
    return json.dumps(
        data,
        separators=(",", ":"),
        ensure_ascii=False
    )


# ============================================================
# OKX SERVER TIME
# ============================================================

def get_server_time_ms():

    path = "/api/v5/public/time"

    req = urllib.request.Request(
        BASE_URL + path,
        headers={
            "User-Agent": "Mozilla/5.0"
        },
        method="GET"
    )

    try:

        with urllib.request.urlopen(
            req,
            timeout=20
        ) as response:

            text = response.read().decode()

            data = json.loads(text)

            if data.get("code") != "0":
                raise RuntimeError(
                    "OKX服务器时间获取失败: " + text
                )

            return int(
                data["data"][0]["ts"]
            )

    except Exception as e:

        print()
        print("获取OKX服务器时间失败:")
        print(repr(e))
        raise


# ============================================================
# ISO TIMESTAMP
# ============================================================

def get_timestamp():

    server_ms = get_server_time_ms()

    seconds = server_ms // 1000
    milliseconds = server_ms % 1000

    return time.strftime(
        "%Y-%m-%dT%H:%M:%S",
        time.gmtime(seconds)
    ) + ".%03dZ" % milliseconds


# ============================================================
# SIGN
# ============================================================

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
        SECRET_KEY.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).digest()

    return base64.b64encode(
        digest
    ).decode("utf-8")


# ============================================================
# OKX REQUEST
# ============================================================

def okx_request(
    method,
    path,
    body=None
):

    method = method.upper()

    if body is None:
        body_text = ""
    else:
        body_text = json_body(body)

    timestamp = get_timestamp()

    signature = make_signature(
        timestamp,
        method,
        path,
        body_text
    )

    headers = {
        "Content-Type": "application/json",

        "OK-ACCESS-KEY": API_KEY,

        "OK-ACCESS-SIGN": signature,

        "OK-ACCESS-TIMESTAMP": timestamp,

        "OK-ACCESS-PASSPHRASE": PASSPHRASE,

        # ====================================================
        # 非常重要：模拟盘
        # ====================================================
        "x-simulated-trading": "1",

        # 防止部分环境出现403
        "User-Agent": "Mozilla/5.0"
    }

    request = urllib.request.Request(
        BASE_URL + path,
        data=(
            body_text.encode("utf-8")
            if body is not None
            else None
        ),
        headers=headers,
        method=method
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            response_text = (
                response
                .read()
                .decode("utf-8")
            )

            return json.loads(
                response_text
            )

    except urllib.error.HTTPError as e:

        error_text = (
            e.read()
            .decode(
                "utf-8",
                errors="replace"
            )
        )

        print()
        print("=" * 70)
        print("OKX HTTP ERROR")
        print("=" * 70)
        print("HTTP STATUS:", e.code)
        print("REQUEST:", method, path)
        print("OKX RESPONSE:")
        print(error_text)
        print("=" * 70)

        raise

    except Exception as e:

        print()
        print("=" * 70)
        print("REQUEST ERROR")
        print("=" * 70)
        print(repr(e))
        print("=" * 70)

        raise


# ============================================================
# GET TICKER
# ============================================================

def get_ticker():

    path = (
        "/api/v5/market/ticker"
        "?instId="
        + urllib.parse.quote(INST_ID)
    )

    return okx_request(
        "GET",
        path
    )


# ============================================================
# GET INSTRUMENT
# ============================================================

def get_instrument():

    path = (
        "/api/v5/public/instruments"
        "?instType=SWAP"
        "&instId="
        + urllib.parse.quote(INST_ID)
    )

    return okx_request(
        "GET",
        path
    )


# ============================================================
# GET ACCOUNT CONFIG
# ============================================================

def get_account_config():

    return okx_request(
        "GET",
        "/api/v5/account/config"
    )


# ============================================================
# GET ACCOUNT BALANCE
# ============================================================

def get_balance():

    return okx_request(
        "GET",
        "/api/v5/account/balance"
    )


# ============================================================
# MAIN
# ============================================================

print()
print("=" * 70)
print("OKX BTC-USDT-SWAP 模拟盘测试")
print("=" * 70)

print()
print("模式：模拟盘")
print("合约：BTC-USDT-SWAP")
print("测试数量：0.0001 BTC")
print("订单类型：post_only")
print("保证金模式：isolated")
print()


# ============================================================
# 1. SERVER TIME
# ============================================================

print("① 检查 OKX 服务器时间")

server_ms = get_server_time_ms()

print(
    "OKX Server Time:",
    server_ms
)

print("✅ 服务器时间正常")


# ============================================================
# 2. TICKER
# ============================================================

print()
print("② 获取 BTC 行情")

ticker = get_ticker()

print(
    "Ticker Code:",
    ticker.get("code")
)

if ticker.get("code") != "0":

    print(
        json.dumps(
            ticker,
            indent=2,
            ensure_ascii=False
        )
    )

    raise RuntimeError(
        "获取 BTC 行情失败"
    )

last_price = Decimal(
    ticker["data"][0]["last"]
)

print(
    "当前 BTC:",
    last_price
)


# ============================================================
# 3. INSTRUMENT
# ============================================================

print()
print("③ 获取合约规则")

instrument = get_instrument()

print(
    "Instrument Code:",
    instrument.get("code")
)

if instrument.get("code") != "0":

    print(
        json.dumps(
            instrument,
            indent=2,
            ensure_ascii=False
        )
    )

    raise RuntimeError(
        "获取合约规则失败"
    )

info = instrument["data"][0]

ct_val = Decimal(
    info["ctVal"]
)

lot_sz = Decimal(
    info["lotSz"]
)

min_sz = Decimal(
    info["minSz"]
)

tick_sz = Decimal(
    info["tickSz"]
)

print(
    "ctVal:",
    ct_val
)

print(
    "lotSz:",
    lot_sz
)

print(
    "minSz:",
    min_sz
)

print(
    "tickSz:",
    tick_sz
)


# ============================================================
# 4. CHECK 0.0001 BTC
# ============================================================

print()
print("④ 检查 0.0001 BTC 是否符合最小下单量")

minimum_btc = (
    ct_val * min_sz
)

print(
    "OKX 最小 BTC 数量:",
    minimum_btc
)

if BTC_QTY < minimum_btc:

    raise RuntimeError(
        f"0.0001 BTC 小于 OKX 最小数量 {minimum_btc}"
    )

# BTC -> 合约张数
contract_size = (
    BTC_QTY / ct_val
)

print(
    "0.0001 BTC 对应合约数量:",
    contract_size,
    "张"
)

# 检查是否为 lotSz 的整数倍
multiple = (
    contract_size / lot_sz
)

if multiple != multiple.to_integral_value():

    raise RuntimeError(
        "下单数量不是 lotSz 的整数倍"
    )

print("✅ 数量规则通过")


# ============================================================
# 5. ACCOUNT CONFIG
# ============================================================

print()
print("⑤ 查询账户模式")

config = get_account_config()

print(
    "Account Config Code:",
    config.get("code")
)

if config.get("code") != "0":

    print(
        json.dumps(
            config,
            indent=2,
            ensure_ascii=False
        )
    )

    raise RuntimeError(
        "账户配置查询失败"
    )

config_data = config["data"][0]

pos_mode = config_data.get(
    "posMode"
)

print(
    "持仓模式:",
    pos_mode
)

print("✅ 账户配置正常")


# ============================================================
# 6. BALANCE
# ============================================================

print()
print("⑥ 检查模拟账户")

balance = get_balance()

print(
    "Balance Code:",
    balance.get("code")
)

if balance.get("code") != "0":

    print(
        json.dumps(
            balance,
            indent=2,
            ensure_ascii=False
        )
    )

    raise RuntimeError(
        "模拟账户查询失败"
    )

print("✅ 模拟账户 API 鉴权成功")


# ============================================================
# 7. CALCULATE BUY PRICE
# ============================================================

print()
print("⑦ 计算测试挂单价格")

raw_buy_price = (
    last_price
    * (
        Decimal("1")
        - GRID_DISTANCE
    )
)

# 按 tickSz 向下取整
buy_price = (
    raw_buy_price / tick_sz
).quantize(
    Decimal("1"),
    rounding=ROUND_DOWN
) * tick_sz

print(
    "当前 BTC:",
    last_price
)

print(
    "测试买入价格:",
    buy_price
)

print(
    "距离现价:",
    GRID_DISTANCE * 100,
    "%"
)


# ============================================================
# 8. ORDER BODY
# ============================================================

order_body = {

    "instId":
        INST_ID,

    "tdMode":
        "isolated",

    "side":
        "buy",

    "ordType":
        "post_only",

    "px":
        str(buy_price),

    # 0.01 张 = 0.0001 BTC
    "sz":
        str(contract_size),

    "clOrdId":
        "demo_test_"
        + str(
            int(time.time())
        )
}


# ============================================================
# LONG/SHORT MODE
# ============================================================

if pos_mode == "long_short_mode":

    order_body["posSide"] = "long"


# ============================================================
# 9. SHOW ORDER
# ============================================================

print()
print("=" * 70)
print("⑧ 准备提交模拟订单")
print("=" * 70)

print(
    json.dumps(
        order_body,
        indent=2,
        ensure_ascii=False
    )
)

print()
print("注意：")
print("这是 OKX 模拟盘订单。")
print("数量 = 0.0001 BTC。")
print("订单类型 = post_only。")
print("不会主动吃单。")
print()


# ============================================================
# 10. PLACE ORDER
# ============================================================

print()
print("⑨ 正在提交模拟订单...")

result = okx_request(
    "POST",
    "/api/v5/trade/order",
    order_body
)


# ============================================================
# 11. RESULT
# ============================================================

print()
print("=" * 70)
print("⑩ OKX 下单结果")
print("=" * 70)

print(
    json.dumps(
        result,
        indent=2,
        ensure_ascii=False
    )
)


# ============================================================
# 12. CHECK RESULT
# ============================================================

if result.get("code") != "0":

    raise RuntimeError(
        "OKX API 返回错误："
        + json.dumps(
            result,
            ensure_ascii=False
        )
    )


if not result.get("data"):

    raise RuntimeError(
        "OKX 没有返回订单数据"
    )


order_data = result["data"][0]

ord_id = order_data.get(
    "ordId"
)

cl_ord_id = order_data.get(
    "clOrdId"
)

s_code = order_data.get(
    "sCode"
)

s_msg = order_data.get(
    "sMsg"
)


print()
print("=" * 70)
print("✅ 模拟订单提交成功")
print("=" * 70)

print(
    "订单 ID:",
    ord_id
)

print(
    "客户端订单 ID:",
    cl_ord_id
)

print(
    "订单状态码:",
    s_code
)

print(
    "订单消息:",
    s_msg
)

print()
print("现在打开 OKX 模拟交易")
print("BTCUSDT 永续")
print("→ 当前委托")
print()
print("应该可以看到这笔 0.0001 BTC 的挂单。")
print()
