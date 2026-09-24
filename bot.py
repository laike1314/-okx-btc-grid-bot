import os
import json
import hmac
import base64
import hashlib
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError

BASE_URL = "https://openapi.okx.com"

API_KEY = os.environ["OKX_API_KEY"]
SECRET_KEY = os.environ["OKX_SECRET_KEY"]
PASSPHRASE = os.environ["OKX_PASSPHRASE"]


def iso_time():
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


timestamp = iso_time()
path = "/api/v5/account/balance"

message = timestamp + "GET" + path

sign = base64.b64encode(
    hmac.new(
        SECRET_KEY.encode(),
        message.encode(),
        hashlib.sha256,
    ).digest()
).decode()

headers = {headers = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0",
    "x-simulated-trading": "1",
}
    "OK-ACCESS-KEY": API_KEY,
    "OK-ACCESS-SIGN": sign,
    "OK-ACCESS-TIMESTAMP": timestamp,
    "OK-ACCESS-PASSPHRASE": PASSPHRASE,
}

req = Request(
    BASE_URL + path,
    headers=headers,
    method="GET",
)

print("================================")
print(" OKX Demo 私有 API 测试")
print("================================")

try:
    with urlopen(req, timeout=15) as response:
        body = response.read().decode(
            "utf-8",
            errors="replace"
        )

        print("HTTP:", response.status)
        print(body)

except HTTPError as e:
    print("HTTP:", e.code)

    try:
        body = e.read().decode(
            "utf-8",
            errors="replace"
        )
        print("OKX 返回:")
        print(body)
    except Exception:
        pass

except Exception as e:
    print(type(e).__name__)
    print(str(e))

print("================================")
print("测试结束")
print("================================")
