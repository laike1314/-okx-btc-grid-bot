import os
import json
import hmac
import base64
import hashlib
import math
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import urlencode

# ============================================================
# OKX Demo BTC 永续交易前检查
#
# 重要：
# 1. 本程序只读取数据
# 2. 本程序不会下单
# 3. 本程序不会撤单
# 4. 本程序不会修改杠杆
# 5. 本程序不会修改账户设置
#
# 当前策略参数：
# 策略资金：300 USDT
# 预留资金：60 USDT
# 网格可用资金：240 USDT
# 目标杠杆：5x
# 网格数量：50
# ============================================================

BASE_URL = "https://openapi.okx.com"
INST_ID = "BTC-USDT-SWAP"

# -------- 策略参数 --------
STRATEGY_CAPITAL = Decimal("300")
RESERVE_CAPITAL = Decimal("60")
GRID_CAPITAL = STRATEGY_CAPITAL - RESERVE_CAPITAL

TARGET_LEVERAGE = Decimal("5")
GRID_COUNT = 50

# 初步网格范围
GRID_RANGE_PERCENT = Decimal("10")

# -------- OKX API 基础函数 --------

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

    # OKX 签名内容：
    # timestamp + method + requestPath + body
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
            raw = response.read().decode()
            return json.loads(raw)

    except HTTPError as e:
        print("")
        print("HTTP:", e.code)

        try:
            error_body = e.read().decode(errors="replace")
            print("OKX 返回:")
            print(error_body)
        except Exception:
            pass

        return None

    except Exception as e:
        print("")
        print("请求异常:", str(e))
        return None


# ============================================================
# 工具函数
# ============================================================

def print_json(title, data):
    print("")
    print(title)

    if data is None:
        print("请求失败")
        return

    print(json.dumps(
        data,
        ensure_ascii=False,
        indent=2
    ))


def get_first(data):
    if not data:
        return None

    if data.get("code") != "0":
        return None

    items = data.get("data", [])

    if not items:
        return None

    return items[0]


def decimal(value, default="0"):
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def floor_decimal(value, step):
    if step <= 0:
        return Decimal("0")

    return (
        value / step
    ).to_integral_value(
        rounding=ROUND_DOWN
    ) * step


# ============================================================
# 主程序
# ============================================================

def main():

    print("")
    print("==============================================")
    print("       OKX Demo BTC 永续交易前检查")
    print("==============================================")
    print("")
    print("⚠️ 本程序只读取，不下单")
    print("⚠️ 本程序不会修改账户")
    print("⚠️ 本程序不会修改杠杆")
    print("")
    print("策略资金:", STRATEGY_CAPITAL, "USDT")
    print("预留资金:", RESERVE_CAPITAL, "USDT")
    print("网格资金:", GRID_CAPITAL, "USDT")
    print("目标杠杆:", TARGET_LEVERAGE, "倍")
    print("网格数量:", GRID_COUNT)
    print("")

    # ========================================================
    # 1. 合约参数
    # ========================================================

    print("==============================================")
    print("[1] BTC-USDT-SWAP 合约参数")
    print("==============================================")

    instruments = request(
        "GET",
        "/api/v5/public/instruments",
        {
            "instType": "SWAP",
            "instId": INST_ID
        }
    )

    if not instruments or instruments.get("code") != "0":
        print("❌ 无法获取合约参数")
        return

    inst = get_first(instruments)

    if not inst:
        print("❌ 合约参数为空")
        return

    print("合约:", inst.get("instId"))
    print("状态:", inst.get("state"))
    print("合约类型:", inst.get("ctType"))
    print("合约面值 ctVal:", inst.get("ctVal"), "BTC")
    print("合约单位 ctValCcy:", inst.get("ctValCcy"))
    print("最小下单数量 minSz:", inst.get("minSz"), "张")
    print("下单步长 lotSz:", inst.get("lotSz"), "张")
    print("价格最小变动 tickSz:", inst.get("tickSz"))

    ct_val = decimal(inst.get("ctVal"))
    min_sz = decimal(inst.get("minSz"))
    lot_sz = decimal(inst.get("lotSz"))
    tick_sz = decimal(inst.get("tickSz"))

    if ct_val <= 0:
        print("❌ ctVal 无效")
        return

    if lot_sz <= 0:
        print("❌ lotSz 无效")
        return

    if inst.get("state") != "live":
        print("⚠️ 当前合约不是 live 状态")
        print("状态:", inst.get("state"))

    # ========================================================
    # 2. 当前行情
    # ========================================================

    print("")
    print("==============================================")
    print("[2] BTC 当前行情")
    print("==============================================")

    ticker = request(
        "GET",
        "/api/v5/market/ticker",
        {
            "instId": INST_ID
        }
    )

    if not ticker or ticker.get("code") != "0":
        print("❌ 无法获取行情")
        return

    ticker_data = get_first(ticker)

    if not ticker_data:
        print("❌ 行情数据为空")
        return

    last_price = decimal(ticker_data.get("last"))
    bid_price = decimal(ticker_data.get("bidPx"))
    ask_price = decimal(ticker_data.get("askPx"))

    print("最新价:", last_price, "USDT")
    print("买一价:", bid_price, "USDT")
    print("卖一价:", ask_price, "USDT")
    print("24H最高:", ticker_data.get("high24h"))
    print("24H最低:", ticker_data.get("low24h"))

    if last_price <= 0:
        print("❌ 当前价格无效")
        return

    # ========================================================
    # 3. 账户配置
    # ========================================================

    print("")
    print("==============================================")
    print("[3] 账户配置")
    print("==============================================")

    config = request(
        "GET",
        "/api/v5/account/config",
        private=True
    )

    print_json("账户配置:", config)

    config_data = get_first(config)

    if config_data:
        acct_lv = config_data.get("acctLv", "")
        pos_mode = config_data.get("posMode", "")

        print("")
        print("账户模式:", acct_lv)
        print("持仓模式:", pos_mode)

        if pos_mode == "net_mode":
            print("说明：买卖模式 / 净持仓模式")

        elif pos_mode == "long_short_mode":
            print("说明：双向持仓模式")

    # ========================================================
    # 4. USDT 账户余额
    # ========================================================

    print("")
    print("==============================================")
    print("[4] USDT 账户余额")
    print("==============================================")

    balance = request(
        "GET",
        "/api/v5/account/balance",
        private=True
    )

    balance_data = get_first(balance)

    if not balance_data:
        print("❌ 无法获取账户余额")
        return

    total_eq = decimal(balance_data.get("totalEq"))

    print("账户总权益:", total_eq, "USDT")

    usdt = None

    for item in balance_data.get("details", []):
        if item.get("ccy") == "USDT":
            usdt = item
            break

    if usdt:
        cash_bal = decimal(usdt.get("cashBal"))
        avail_bal = decimal(usdt.get("availBal"))
        avail_eq = decimal(usdt.get("availEq"))

        print("USDT cashBal:", cash_bal)
        print("USDT availBal:", avail_bal)
        print("USDT availEq:", avail_eq)

    else:
        cash_bal = Decimal("0")
        avail_bal = Decimal("0")
        avail_eq = Decimal("0")

        print("⚠️ 没有找到 USDT 余额")

    # ========================================================
    # 5. 当前杠杆
    # ========================================================

    print("")
    print("==============================================")
    print("[5] BTC 永续当前杠杆")
    print("==============================================")

    # 先检查 isolated
    leverage_isolated = request(
        "GET",
        "/api/v5/account/leverage-info",
        {
            "instId": INST_ID,
            "mgnMode": "isolated"
        },
        private=True
    )

    print_json("逐仓杠杆:", leverage_isolated)

    isolated_items = []

    if leverage_isolated and leverage_isolated.get("code") == "0":
        isolated_items = leverage_isolated.get("data", [])

    # 再检查 cross
    leverage_cross = request(
        "GET",
        "/api/v5/account/leverage-info",
        {
            "instId": INST_ID,
            "mgnMode": "cross"
        },
        private=True
    )

    print_json("全仓杠杆:", leverage_cross)

    cross_items = []

    if leverage_cross and leverage_cross.get("code") == "0":
        cross_items = leverage_cross.get("data", [])

    print("")
    print("逐仓杠杆记录:")

    if isolated_items:
        for item in isolated_items:
            print(
                "  方向:",
                item.get("posSide"),
                "杠杆:",
                item.get("lever")
            )
    else:
        print("  没有逐仓杠杆记录")

    print("")
    print("全仓杠杆记录:")

    if cross_items:
        for item in cross_items:
            print(
                "  方向:",
                item.get("posSide"),
                "杠杆:",
                item.get("lever")
            )
    else:
        print("  没有全仓杠杆记录")

    # ========================================================
    # 6. 当前 BTC 持仓
    # ========================================================

    print("")
    print("==============================================")
    print("[6] BTC 永续当前持仓")
    print("==============================================")

    positions = request(
        "GET",
        "/api/v5/account/positions",
        {
            "instType": "SWAP",
            "instId": INST_ID
        },
        private=True
    )

    print_json("当前持仓:", positions)

    position_items = []

    if positions and positions.get("code") == "0":
        position_items = positions.get("data", [])

    if not position_items:
        print("当前没有 BTC 永续持仓")
    else:
        print("")
        print("发现当前持仓:")
        for p in position_items:
            print(
                "方向:",
                p.get("posSide"),
                "| 数量:",
                p.get("pos"),
                "| 开仓均价:",
                p.get("avgPx"),
                "| 保证金模式:",
                p.get("mgnMode"),
                "| 杠杆:",
                p.get("lever")
            )

    # ========================================================
    # 7. 当前 BTC 挂单
    # ========================================================

    print("")
    print("==============================================")
    print("[7] BTC 永续当前挂单")
    print("==============================================")

    orders = request(
        "GET",
        "/api/v5/trade/orders-pending",
        {
            "instType": "SWAP",
            "instId": INST_ID
        },
        private=True
    )

    print_json("当前挂单:", orders)

    order_items = []

    if orders and orders.get("code") == "0":
        order_items = orders.get("data", [])

    if not order_items:
        print("当前没有 BTC 永续挂单")
    else:
        print("")
        print("发现当前挂单:", len(order_items), "笔")

        for order in order_items:
            print(
                "订单:",
                order.get("ordId"),
                "| 方向:",
                order.get("side"),
                "| 价格:",
                order.get("px"),
                "| 数量:",
                order.get("sz"),
                "| 状态:",
                order.get("state")
            )

    # ========================================================
    # 8. 资金计算
    # ========================================================

    print("")
    print("==============================================")
    print("[8] 300U 网格资金测算")
    print("==============================================")

    print("策略资金:", STRATEGY_CAPITAL, "USDT")
    print("预留资金:", RESERVE_CAPITAL, "USDT")
    print("实际网格资金:", GRID_CAPITAL, "USDT")

    # 一张合约的 BTC 数量
    one_contract_btc = ct_val

    # 一张合约名义价值
    one_contract_value = last_price * one_contract_btc

    # 按目标 5x 计算理论保证金
    one_contract_margin = (
        one_contract_value / TARGET_LEVERAGE
    )

    print("")
    print("BTC 当前价格:", last_price, "USDT")
    print("1 张合约:", one_contract_btc, "BTC")
    print(
        "1 张合约名义价值:",
        one_contract_value.quantize(
            Decimal("0.01")
        ),
        "USDT"
    )

    print(
        "1 张合约按",
        TARGET_LEVERAGE,
        "倍杠杆理论保证金:",
        one_contract_margin.quantize(
            Decimal("0.01")
        ),
        "USDT"
    )

    # ========================================================
    # 9. 最大合约数量估算
    # ========================================================

    max_contracts_raw = (
        GRID_CAPITAL / one_contract_margin
    )

    max_contracts = floor_decimal(
        max_contracts_raw,
        lot_sz
    )

    print("")
    print("理论最大合约数量:", max_contracts, "张")

    if max_contracts < min_sz:
        print("")
        print("⚠️ 结论：")
        print("300U 网格资金不足以按照当前合约最小下单单位建立 1 张仓位。")
        print("暂时不要进入真实下单程序。")

    else:
        used_margin = (
            max_contracts * one_contract_margin
        )

        remaining = (
            GRID_CAPITAL - used_margin
        )

        print("预计最大使用保证金:", used_margin.quantize(
            Decimal("0.01")
        ), "USDT")

        print("预计剩余网格资金:", remaining.quantize(
            Decimal("0.01")
        ), "USDT")

        print("")
        print("可以满足至少 1 张合约的最低数量要求。")

    # ========================================================
    # 10. 50 格理论价格区间
    # ========================================================

    print("")
    print("==============================================")
    print("[9] 50 格价格区间测算")
    print("==============================================")

    lower_price = (
        last_price *
        (Decimal("1") - GRID_RANGE_PERCENT / Decimal("100"))
    )

    upper_price = (
        last_price *
        (Decimal("1") + GRID_RANGE_PERCENT / Decimal("100"))
    )

    grid_step_percent = (
        GRID_RANGE_PERCENT * Decimal("2")
    ) / Decimal(GRID_COUNT)

    print(
        "下限:",
        lower_price.quantize(Decimal("0.01")),
        "USDT"
    )

    print(
        "上限:",
        upper_price.quantize(Decimal("0.01")),
        "USDT"
    )

    print(
        "总区间:",
        GRID_RANGE_PERCENT * Decimal("2"),
        "%"
    )

    print(
        "网格数量:",
        GRID_COUNT
    )

    print(
        "平均每格价格变化:",
        grid_step_percent.quantize(Decimal("0.001")),
        "%"
    )

    # ========================================================
    # 11. 最终安全检查
    # ========================================================

    print("")
    print("==============================================")
    print("[10] 安全检查结果")
    print("==============================================")

    print("")

    if position_items:
        print("⚠️ 当前已有 BTC 永续持仓")
    else:
        print("✅ 当前没有 BTC 永续持仓")

    if order_items:
        print("⚠️ 当前已有 BTC 永续挂单")
    else:
        print("✅ 当前没有 BTC 永续挂单")

    if inst.get("state") == "live":
        print("✅ BTC 永续合约状态正常")
    else:
        print("⚠️ BTC 永续合约状态异常")

    if last_price > 0:
        print("✅ BTC 当前行情正常")
    else:
        print("❌ BTC 当前行情异常")

    if usdt:
        print("✅ USDT 账户余额读取正常")
    else:
        print("⚠️ USDT 账户余额读取异常")

    print("")
    print("==============================================")
    print("检查完成")
    print("==============================================")
    print("")
    print("⚠️ 本程序没有下单功能")
    print("⚠️ 本程序没有撤单功能")
    print("⚠️ 本程序没有修改杠杆功能")
    print("⚠️ 本程序没有修改账户设置功能")
    print("")
    print("下一步需要根据这个检查结果设计真正的")
    print("300U / 50格 / 5倍 Demo 网格策略。")
    print("")


if __name__ == "__main__":
    main()
