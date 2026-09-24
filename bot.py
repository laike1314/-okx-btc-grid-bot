from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

urls = [
    "https://openapi.okx.com/api/v5/market/ticker?instId=BTC-USDT",
    "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT",
    "https://us.okx.com/api/v5/market/ticker?instId=BTC-USDT",
    "https://eea.okx.com/api/v5/market/ticker?instId=BTC-USDT",
]

for url in urls:
    print("\n==============================")
    print("测试:", url)

    try:
        req = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0"
            }
        )

        with urlopen(req, timeout=15) as response:
            data = response.read().decode(
                "utf-8",
                errors="replace"
            )

            print("HTTP:", response.status)
            print(data[:500])

    except HTTPError as e:
        print("HTTP:", e.code)

        try:
            body = e.read().decode(
                "utf-8",
                errors="replace"
            )
            print(body[:500])
        except Exception:
            pass

    except URLError as e:
        print("网络错误:", e)

    except Exception as e:
        print("错误:", type(e).__name__)
        print(str(e))

print("\n==============================")
print("域名诊断完成")
print("==============================")
