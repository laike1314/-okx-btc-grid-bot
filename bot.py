import os
import json
import hmac
import base64
import hashlib
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import urlencode

BASE_URL = "https://openapi.okx.com"
INST_ID = "BTC-USDT-SWAP"


def iso_time():
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def request(method, path, params=None, private=False):
    params = params or {}

    query = ""
    if params:
        query = "?" + urlencode(params)

    body = ""

    timestamp = iso_time()
    message = timestamp + method + path + query + body

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "x-simulated-trading": "1",
    }

    if private:
        api_key = os.environ["OKX_API_KEY"]
        secret_key = os.environ["OKX_SECRET_KEY"]
        passphrase = os.environ["OKX_PASSPHRASE"]

        sign = base64.b64encode(
            hmac.new(
                secret_key.encode(),
                message.encode(),
                hashlib.sha256
            ).digest()
        ).decode()

        headers.update({
            "OK-ACCESS-KEY": api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": passphrase,
        })

    req = Request(
        BASE_URL + path + query,
        headers=headers,
        method=method
    )

    try:
        with urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode())

    except HTTPError as e:
        print("HTTP:", e.code)
        print(e.read().decode(errors="replace"))
        return None


def main():
    print("================================")
    print(" OKX Demo BTC 交易前检查")
    print(" 不下单 / 只读取")
    print("================================")

    # 1. 合约参数
    print("\n[1] BTC-USDT-SWAP 合约参数")

    instruments = request(
        "GET",
        "/api/v5/public/instruments",
        {
            "instType": "SWAP",
            "instId": INST_ID
        }
    )

    if instruments:
        print(json.dumps(
            instruments,
            ensure_ascii=False,
            indent=2
        ))

    # 2. 当前价格
    print("\n[2] BTC 当前行情")

    ticker = request(
        "GET",
        "/api/v5/market/ticker",
        {
            "instId": INST_ID
        }
    )

    if ticker:
        print(json.dumps(
            ticker,
            ensure_ascii=False,
            indent=2
        ))

    # 3. 当前持仓
    print("\n[3] BTC 永续当前持仓")

    positions = request(
        "GET",
        "/api/v5/account/positions",
        {
            "instType": "SWAP",
            "instId": INST_ID
        },
        private=True
    )

    if positions:
        print(json.dumps(
            positions,
            ensure_ascii=False,
            indent=2
        ))

    # 4. 当前挂单
    print("\n[4] BTC 永续当前挂单")

    orders = request(
        "GET",
        "/api/v5/trade/orders-pending",
        {
            "instType": "SWAP",
            "instId": INST_ID
        },
        private=True
    )

    if orders:
        print(json.dumps(
            orders,
            ensure_ascii=False,
            indent=2
        ))

    print("\n================================")
    print(" 检查完成")
    print(" 本程序没有下单功能")
    print("================================")


if __name__ == "__main__":
    main()
