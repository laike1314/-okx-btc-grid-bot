import os
import json
import time
import hmac
import base64
import hashlib
from datetime import datetime, timezone

import requests


# ============================================================
# OKX 模拟盘配置
# ============================================================

API_KEY = os.getenv("OKX_API_KEY")
SECRET_KEY = os.getenv("OKX_SECRET_KEY")
PASSPHRASE = os.getenv("OKX_PASSPHRASE")

BASE_URL = "https://openapi.okx.com"

# 先使用 BTC 永续合约做测试
INST_ID = "BTC-USDT-SWAP"

# 模拟盘
SIMULATED_TRADING = "1"

# 保证金模式
TD_MODE = "cross"

# 买入
SIDE = "buy"

# 市价单
ORD_TYPE = "market"

# 测试下 1 张合约
# 如果你的合约最小下单单位不同，后面再调整
SIZE = "1"


# ============================================================
# 基础检查
# ============================================================

def check_config():
    print("========================================")
    print("检查 API 配置")
    print("========================================")

    if not API_KEY:
        raise Exception("缺少 OKX_API_KEY")

    if not SECRET_KEY:
        raise Exception("缺少 OKX_SECRET_KEY")

    if not PASSPHRASE:
        raise Exception("缺少 OKX_PASSPHRASE")

    print("OKX_API_KEY      : 已读取")
    print("OKX_SECRET_KEY   : 已读取")
    print("OKX_PASSPHRASE   : 已读取")
    print("模拟盘模式       : 是")
    print()


# ============================================================
# 获取 OKX 服务器时间
# ============================================================

def get_server_time():
    url = BASE_URL + "/api/v5/public/time"

    response = requests.get(url, timeout=10)

    response.raise_for_status()

    result = response.json()

    if result.get("code") != "0":
        raise Exception(f"获取 OKX 服务器时间失败: {result}")

    timestamp_ms = int(result["data"][0]["ts"])

    dt = datetime.fromtimestamp(
        timestamp_ms / 1000,
        tz=timezone.utc
    )

    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ============================================================
# OKX 签名
# ============================================================

def make_signature(timestamp, method, request_path, body):
    message = timestamp + method + request_path + body

    mac = hmac.new(
        SECRET_KEY.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    )

    return base64.b64encode(mac.digest()).decode()


# ============================================================
# 发送私有 API 请求
# ============================================================

def private_request(method, request_path, body=""):
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

        # 非常重要：模拟盘
        "x-simulated-trading": "1",
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

    print("HTTP 状态:", response.status_code)

    try:
        result = response.json()
    except Exception:
        print(response.text)
        raise Exception("OKX 返回的不是 JSON")

    return result


# ============================================================
# 查询账户余额
# ============================================================

def test_account():
    print("========================================")
    print("① 测试 OKX 模拟盘 API")
    print("========================================")

    result = private_request(
        "GET",
        "/api/v5/account/balance"
    )

    print(json.dumps(
        result,
        indent=2,
        ensure_ascii=False
    ))

    if result.get("code") != "0":
        raise Exception(
            "账户 API 测试失败: " +
            json.dumps(result, ensure_ascii=False)
        )

    print()
    print("✅ OKX API 连接成功")
    print()


# ============================================================
# 获取合约信息
# ============================================================

def get_instrument():
    print("========================================")
    print("② 检查交易品种")
    print("========================================")

    path = (
        "/api/v5/public/instruments"
        "?instType=SWAP"
        f"&instId={INST_ID}"
    )

    response = requests.get(
        BASE_URL + path,
        timeout=10
    )

    response.raise_for_status()

    result = response.json()

    print(json.dumps(
        result,
        indent=2,
        ensure_ascii=False
    ))

    if result.get("code") != "0":
        raise Exception(
            "获取合约信息失败: " +
            json.dumps(result, ensure_ascii=False)
        )

    if not result.get("data"):
        raise Exception(
            f"没有找到交易品种: {INST_ID}"
        )

    instrument = result["data"][0]

    print()
    print("交易品种:", instrument.get("instId"))
    print("合约面值:", instrument.get("ctVal"))
    print("最小下单单位:", instrument.get("lotSz"))
    print("最小价格单位:", instrument.get("tickSz"))
    print()

    return instrument


# ============================================================
# 模拟下单
# ============================================================

def place_demo_order():
    print("========================================")
    print("③ 准备提交模拟订单")
    print("========================================")

    # 重点：
    # 不能使用 demo_test_123
    # 因为 clOrdId 只能包含字母和数字
    #
    # 正确：
    # demotest123456
    #
    # 不正确：
    # demo_test_123456

    cl_ord_id = "demotest" + str(int(time.time()))

    body_data = {
        "instId": INST_ID,
        "tdMode": TD_MODE,
        "clOrdId": cl_ord_id,
        "side": SIDE,
        "ordType": ORD_TYPE,
        "sz": SIZE,
    }

    body = json.dumps(
        body_data,
        separators=(",", ":")
    )

    print("订单参数:")
    print(json.dumps(
        body_data,
        indent=2,
        ensure_ascii=False
    ))

    print()
    print("正在提交模拟订单...")
    print()

    result = private_request(
        "POST",
        "/api/v5/trade/order",
        body
    )

    print("========================================")
    print("④ OKX 下单结果")
    print("========================================")

    print(json.dumps(
        result,
        indent=2,
        ensure_ascii=False
    ))

    if result.get("code") != "0":
        raise Exception(
            "模拟下单失败: " +
            json.dumps(result, ensure_ascii=False)
        )

    data = result.get("data", [])

    if not data:
        raise Exception("OKX 没有返回订单数据")

    order = data[0]

    ord_id = order.get("ordId")
    returned_cl_ord_id = order.get("clOrdId")
    s_code = order.get("sCode")
    s_msg = order.get("sMsg")

    print()
    print("========================================")
    print("⑤ 下单完成")
    print("========================================")

    print("OKX 订单 ID:", ord_id)
    print("客户订单 ID:", returned_cl_ord_id)
    print("状态代码:", s_code)
    print("状态信息:", s_msg)

    if s_code != "0":
        raise Exception(
            f"订单被 OKX 拒绝: {s_code} {s_msg}"
        )

    print()
    print("🎉 模拟订单提交成功！")
    print()


# ============================================================
# 主程序
# ============================================================

def main():
    print()
    print("########################################")
    print("#       OKX 模拟盘 Demo Test")
    print("########################################")
    print()

    try:
        # 1. 检查 Secrets
        check_config()

        # 2. 测试 API
        test_account()

        # 3. 检查 BTC 永续合约
        get_instrument()

        # 4. 提交一笔模拟订单
        place_demo_order()

        print("========================================")
        print("全部测试完成")
        print("========================================")

    except Exception as e:
        print()
        print("========================================")
        print("❌ 程序失败")
        print("========================================")
        print(str(e))
        print()

        raise


if __name__ == "__main__":
    main()
