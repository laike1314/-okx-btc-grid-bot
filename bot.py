import os
import time
import json
import hmac
import base64
import hashlib
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.parse import urlencode

BASE_URL = "https://openapi.okx.com"

API_KEY = os.environ["OKX_API_KEY"]
SECRET_KEY = os.environ["OKX_SECRET_KEY"]
PASSPHRASE = os.environ["OKX_PASSPHRASE"]

INST_ID = "BTC-USDT-SWAP"


def iso_time():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def request(method, path, params=None, body=None, private=False):
    params = params or {}
    body_text = json.dumps(body, separators=(",", ":")) if body else ""

    query = ""
    if params:
        query = "?" + urlencode(params)

    timestamp = iso_time()
    message = timestamp + method.upper() + path + query + body_text

    headers = {
        "Content-Type": "application/json",
        "x-simulated-trading": "1",
    }

    if private:
        sign = base64.b64encode(
            hmac.new(
                SECRET_KEY.encode(),
                message.encode(),
                hashlib.sha256
            ).digest()
        ).decode()

        headers.update({
            "OK-ACCESS-KEY": API_KEY,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        })

    req = Request(
        BASE_URL + path + query,
        data=body_text.encode() if body else None,
        headers=headers,
        method=method.upper()
    )

    try:
        with urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode())
except Exception as e:
    print("\n===== OKX API ERROR =====")
    print(type(e).__name__)
    print(str(e))

    if hasattr(e, "read"):
        try:
            print(e.read().decode("utf-8", errors="replace"))
        except Exception:
            pass

    print("=========================\n")
    raise
except Exception as e:
    print("\n===== OKX API ERROR =====")
    print(type(e).__name__)
    print(str(e))

    if hasattr(e, "read"):
        try:
            print(e.read().decode("utf-8", errors="replace"))
        except Exception:
            pass

    print("=========================\n")
    raise


def main():
    print("================================")
    print(" OKX Demo BTC Grid Bot")
    print("================================")

    # 1. 获取 BTC 永续合约信息
    instruments = request(
        "GET",
        "/api/v5/public/instruments",
        {"instType": "SWAP", "instId": INST_ID}
    )

    print("\n[1] BTC 合约信息")
    print(json.dumps(instruments, ensure_ascii=False, indent=2))

    # 2. 获取 BTC 最新行情
    ticker = request(
        "GET",
        "/api/v5/market/ticker",
        {"instId": INST_ID}
    )

    print("\n[2] BTC 最新行情")
    print(json.dumps(ticker, ensure_ascii=False, indent=2))

    # 3. 测试 Demo API 身份
    account = request(
        "GET",
        "/api/v5/account/balance",
        private=True
    )

    print("\n[3] Demo 账户")
    print(json.dumps(account, ensure_ascii=False, indent=2))

    print("\n================================")
    print(" API 连接测试完成")
    print(" 当前版本不会下单")
    print("================================")


if __name__ == "__main__":
    main()
