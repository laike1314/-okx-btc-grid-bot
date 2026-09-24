import os
import json
import hmac
import base64
import hashlib
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError

BASE_URL = "https://openapi.okx.com"


def iso_time():
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def main():
    api_key = os.environ["OKX_API_KEY"]
    secret_key = os.environ["OKX_SECRET_KEY"]
    passphrase = os.environ["OKX_PASSPHRASE"]

    path = "/api/v5/account/balance"
    method = "GET"
    timestamp = iso_time()

    message = timestamp + method + path

    sign = base64.b64encode(
        hmac.new(
            secret_key.encode(),
            message.encode(),
            hashlib.sha256
        ).digest()
    ).decode()

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "x-simulated-trading": "1",
        "OK-ACCESS-KEY": api_key,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": passphrase,
    }

    req = Request(
        BASE_URL + path,
        headers=headers,
        method="GET"
    )

    try:
        with urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode())

        print("HTTP:", response.status)
        print("OKX 返回:")
        print(json.dumps(data, ensure_ascii=False, indent=2))

    except HTTPError as e:
        print("HTTP:", e.code)
        print("OKX 返回:")
        print(e.read().decode(errors="replace"))


if __name__ == "__main__":
    main()
