"""交易池：市值前 N 的 USDT 永续 + OKX 股票类永续。

加密货币
  CoinGecko ``/api/v3/coins/markets?order=market_cap_desc`` 的符号，
  映射到 OKX ``GET /api/v5/public/instruments?instType=SWAP`` 里真实存在的
  ``{BASE}-USDT-SWAP``。只收 state=live、settleCcy=USDT、ctType=linear、
  instCategory=1（官方文档：Crypto）。稳定币跳过，避免占掉名额。
  市值接口失败时用 FALLBACK_USDT_SWAPS，并与当前 live 集合取交集。

股票 / 美股相关
  OKX 没有单独的「美股」instType，instruments 里也没有国家字段。
  官方 ``instCategory``：1 Crypto，3 Stocks，4 Commodities，5 Forex，6 Bonds。
  股票永续一律是 SWAP，instId 形如 ``AAPL-USDT-SWAP``。
  帮助中心说明该产品线同时包含美股、韩股、日股、港股和少量 pre-market。
  ENABLE_US_STOCKS=1 时纳入 **全部** instCategory=3 且 live 的 USDT 线性永续。
  默认关闭：模拟盘股票永续点差宽，价格也容易失真。
  不手写代码表，也不把商品（XAU 等，category 4）算进去。

SYMBOLS 环境变量若非空，则整表替换自动交易池（调试用）。
``--smoke`` 不访问 CoinGecko / OKX。
"""
import json
import os
import urllib.request

import config

AGENT = "universe"

CRYPTO_CATEGORY = "1"
STOCK_CATEGORY = "3"
STOCK_RULE = (
    "SWAP instCategory=3 (OKX Stocks) & state=live & settleCcy=USDT "
    "& ctType=linear & instId endswith -USDT-SWAP"
)

# 2026-10-28 CoinGecko 市值序（去掉稳定币）里，当时 OKX 上确有 live USDT 永续的前 30 个。
# 只在排名接口失败时使用；instruments 成功时还会再滤掉已经下架的。
FALLBACK_USDT_SWAPS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP", "SOL-USDT-SWAP",
    "TRX-USDT-SWAP", "ZEC-USDT-SWAP", "HYPE-USDT-SWAP", "DOGE-USDT-SWAP", "LINK-USDT-SWAP",
    "ADA-USDT-SWAP", "NEAR-USDT-SWAP", "XLM-USDT-SWAP", "BCH-USDT-SWAP", "LTC-USDT-SWAP",
    "UNI-USDT-SWAP", "CC-USDT-SWAP", "AVAX-USDT-SWAP", "SUI-USDT-SWAP", "GRAM-USDT-SWAP",
    "HBAR-USDT-SWAP", "TAO-USDT-SWAP", "SHIB-USDT-SWAP", "ENA-USDT-SWAP", "CRO-USDT-SWAP",
    "BTW-USDT-SWAP", "OKB-USDT-SWAP", "AAVE-USDT-SWAP", "PEPE-USDT-SWAP", "ONDO-USDT-SWAP",
]

# 仅当主 instId 不在 live 集合、而别名 instId 在时才用。不凭空发明合约。
SYMBOL_ALIASES = {
    "MATIC": ("POL",),
    "RNDR": ("RENDER",),
    "TON": ("GRAM",),
}

STABLE_SYMBOLS = frozenset({
    "USDT", "USDC", "USDS", "USDE", "DAI", "USD1", "USDG", "PYUSD", "FDUSD",
    "TUSD", "USDD", "FRAX", "LUSD", "GUSD", "USDP", "EURC", "RLUSD", "USDY",
    "USDB", "BUSD", "GHO", "CRVUSD", "MIM", "USDN", "USTC", "SUSDS", "SUSDE",
    "SUSD", "EURS", "EURT", "USDX", "USDF", "USD0", "USDTB", "USDR", "USDQ",
})

MARKETCAP_URL = "https://api.coingecko.com/api/v3/coins/markets"


def is_stable(symbol, name=""):
    if str(symbol or "").upper() in STABLE_SYMBOLS:
        return True
    return "stablecoin" in str(name or "").lower()


def _is_live_usdt_swap(row):
    inst = str(row.get("instId") or "")
    if not inst.endswith("-USDT-SWAP"):
        return False
    if str(row.get("state") or "") != "live":
        return False
    if str(row.get("settleCcy") or "") != "USDT":
        return False
    ct = str(row.get("ctType") or "")
    return ct in ("linear", "")


def split_instruments(rows):
    """返回 (crypto_ids set, stocks list)。stocks 为 instCategory=3 的全部 live USDT 线性永续。"""
    rows = rows or []
    cats = {str(r.get("instCategory") or "") for r in rows}
    has_cat = any(c in (CRYPTO_CATEGORY, STOCK_CATEGORY, "4", "5", "6") for c in cats)
    crypto, stocks = [], []
    for row in rows:
        if not _is_live_usdt_swap(row):
            continue
        cat = str(row.get("instCategory") or "")
        inst = row["instId"]
        if cat == STOCK_CATEGORY:
            stocks.append(inst)
        elif cat == CRYPTO_CATEGORY or not has_cat:
            crypto.append(inst)
    return set(crypto), sorted(set(stocks))


def resolve_inst(symbol, live_ids):
    sym = str(symbol or "").strip().upper()
    if not sym.isascii() or not sym.isalnum():
        return None
    for base in (sym,) + tuple(SYMBOL_ALIASES.get(sym, ())):
        inst = f"{base}-USDT-SWAP"
        if inst in live_ids:
            return inst
    return None


def select_crypto(ranked, live_ids, top_n):
    """按市值顺序取前 top_n 个能映射到 live USDT 永续、且不是稳定币的合约。"""
    if top_n <= 0:
        return []
    out, seen = [], set()
    for item in ranked or []:
        if isinstance(item, (tuple, list)):
            sym = item[0]
            name = item[1] if len(item) > 1 else ""
        else:
            sym, name = item, ""
        if is_stable(sym, name):
            continue
        inst = resolve_inst(sym, live_ids)
        if not inst or inst in seen:
            continue
        seen.add(inst)
        out.append(inst)
        if len(out) >= top_n:
            break
    return out


def select_stocks(rows, enabled=True):
    if not enabled:
        return []
    _crypto, stocks = split_instruments(rows)
    return stocks


def fallback_crypto(live_ids, top_n):
    out = []
    for inst in FALLBACK_USDT_SWAPS:
        if live_ids is not None and inst not in live_ids:
            continue
        out.append(inst)
        if len(out) >= top_n:
            break
    return out


def _fetch_page(page):
    url = (f"{MARKETCAP_URL}?vs_currency=usd&order=market_cap_desc"
           f"&per_page=100&page={page}&sparkline=false")
    req = urllib.request.Request(url, headers={
        "User-Agent": f"okx-multi-agent/{config.VERSION}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=12) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, list):
        raise RuntimeError("市值接口返回的不是列表: " + str(data)[:180])
    out = []
    for coin in data:
        if not isinstance(coin, dict):
            continue
        sym = str(coin.get("symbol") or "").strip().upper()
        if sym:
            out.append((sym, str(coin.get("name") or "")))
    return out


def fetch_ranked_symbols(top_n, live_ids):
    ranked = []
    for page in (1, 2, 3):
        batch = _fetch_page(page)
        if not batch:
            break
        ranked.extend(batch)
        if len(select_crypto(ranked, live_ids, top_n)) >= top_n:
            break
        if len(batch) < 100:
            break
    if not ranked:
        raise RuntimeError("市值接口没有返回币种")
    return ranked


def _write(data_dir, payload):
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "universe.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _commit(crypto, stocks, source, journal, data_dir, note, extra=None):
    crypto = list(dict.fromkeys(crypto))
    stock_set = []
    seen = set(crypto)
    for inst in stocks:
        if inst not in seen:
            seen.add(inst)
            stock_set.append(inst)
    symbols = crypto + stock_set
    config.SYMBOLS = symbols
    meta = {
        "version": config.VERSION,
        "source": source,
        "note": note,
        "stock_rule": STOCK_RULE,
        "crypto_top_n": config.CRYPTO_TOP_N,
        "enable_us_stocks": bool(config.ENABLE_US_STOCKS),
        "max_positions": config.MAX_POSITIONS,
        "crypto": crypto,
        "stocks": stock_set,
        "symbols": symbols,
    }
    if extra:
        meta.update(extra)
    config.UNIVERSE_META = meta
    _write(data_dir, meta)
    journal.log(AGENT, "ready",
                f"{note}。共 {len(symbols)} 个（加密 {len(crypto)}，股票永续 {len(stock_set)}），来源 {source}",
                source=source, crypto=len(crypto), stocks=len(stock_set), symbols=len(symbols))
    return symbols


def _apply_smoke(client, journal, data_dir):
    known = set(getattr(client, "SPECS", {}))
    if config.SYMBOLS_OVERRIDE:
        chosen = [s for s in config.SYMBOLS_OVERRIDE if s in known]
        dropped = [s for s in config.SYMBOLS_OVERRIDE if s not in known]
        source = "SYMBOLS-smoke"
        if dropped:
            journal.log(AGENT, "smoke_drop",
                        "冒烟 Mock 没有这些合约，已忽略: " + ",".join(dropped))
        if not chosen:
            chosen = fallback_crypto(known, config.CRYPTO_TOP_N) or sorted(known)
            source = "smoke-fallback"
    else:
        chosen = fallback_crypto(known, config.CRYPTO_TOP_N) or sorted(known)
        source = "smoke-offline"
    _commit(chosen, [], source, journal, data_dir,
            "离线冒烟交易池，不访问 CoinGecko / OKX")


def _apply_auto(client, journal, data_dir):
    top_n = config.CRYPTO_TOP_N
    try:
        rows = client.request("GET", "/api/v5/public/instruments", {"instType": "SWAP"})
    except Exception as e:
        journal.log(AGENT, "instruments_fail",
                    f"拉取 OKX instruments 失败，股票永续本轮留空，加密用离线回退: {e}")
        _commit(fallback_crypto(None, top_n), [], "fallback-no-instruments", journal, data_dir,
                "instruments 不可用，仅离线加密回退（未逐个向交易所核对）",
                extra={"instruments_error": str(e)})
        return

    live_crypto, stocks = split_instruments(rows)
    if not config.ENABLE_US_STOCKS:
        stocks = []
    counts = {}
    for row in rows:
        cat = str(row.get("instCategory") or "")
        counts[cat] = counts.get(cat, 0) + 1
    journal.log(AGENT, "instruments",
                f"OKX SWAP {len(rows)} 条，其中可映射加密 {len(live_crypto)}、"
                f"股票类 live USDT 永续 {len(stocks)}，instCategory 计数 {counts}")

    ranked, rank_error = None, ""
    try:
        ranked = fetch_ranked_symbols(top_n, live_crypto)
        crypto = select_crypto(ranked, live_crypto, top_n)
        source = "coingecko+okx"
    except Exception as e:
        ranked, crypto, source = None, [], "fallback"
        rank_error = str(e)
        journal.log(AGENT, "rank_fail", f"市值排名获取失败，改用离线回退列表: {e}")

    filled = len(crypto)
    if len(crypto) < top_n:
        for inst in fallback_crypto(live_crypto, top_n):
            if inst not in crypto:
                crypto.append(inst)
            if len(crypto) >= top_n:
                break
        if filled == 0:
            source = "fallback"
        elif len(crypto) > filled:
            source = source + "+fallback"
    if not crypto:
        crypto = fallback_crypto(None, top_n)
        source = "fallback-unverified"
        journal.log(AGENT, "fallback_raw",
                    "live 加密集合与回退列表没有交集，退回未经过滤的离线列表")

    note = "自动交易池：市值前排 USDT 永续 + 股票类永续" if config.ENABLE_US_STOCKS \
        else "自动交易池：仅市值前排 USDT 永续（ENABLE_US_STOCKS=0）"
    extra = {"instruments": len(rows), "rank_error": rank_error}
    _commit(crypto, stocks, source, journal, data_dir, note, extra=extra)


def apply(client, journal, data_dir):
    """按当前模式写入 config.SYMBOLS / config.UNIVERSE_META，并落盘 universe.json。"""
    os.makedirs(data_dir, exist_ok=True)
    if getattr(client, "is_mock", False):
        _apply_smoke(client, journal, data_dir)
        return
    if config.SYMBOLS_OVERRIDE:
        _commit(list(config.SYMBOLS_OVERRIDE), [], "SYMBOLS", journal, data_dir,
                "SYMBOLS 强制列表，跳过市值排名和股票发现")
        return
    _apply_auto(client, journal, data_dir)


def offline_self_check():
    """不联网。校验市值映射、稳定币过滤、别名和股票类别筛选。"""
    live = {
        "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP",
        "USDC-USDT-SWAP", "POL-USDT-SWAP", "GRAM-USDT-SWAP", "TON-USDT-SWAP",
    }
    ranked = [
        ("USDT", "Tether"),
        ("BTC", "Bitcoin"),
        ("USDC", "USDC"),
        ("ETH", "Ethereum"),
        ("MATIC", "Polygon"),
        ("SOL", "Solana"),
        ("FIGR_HELOC", "Figure Heloc"),
    ]
    got = select_crypto(ranked, live, 3)
    # MATIC 在 SOL 前面，且能映射到已上市的 POL，所以占住第 3 个名额。
    assert got == ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "POL-USDT-SWAP"], got
    got5 = select_crypto(ranked, live, 5)
    assert got5 == ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "POL-USDT-SWAP", "SOL-USDT-SWAP"], got5
    assert select_crypto([("MATIC", "Polygon")], live, 5) == ["POL-USDT-SWAP"]
    assert select_crypto([("MATIC", "Polygon")], {"BTC-USDT-SWAP"}, 5) == []
    assert select_crypto([("TON", "Toncoin")], live, 5) == ["TON-USDT-SWAP"]
    assert select_crypto([("TON", "Toncoin")], {"GRAM-USDT-SWAP"}, 5) == ["GRAM-USDT-SWAP"]
    assert select_crypto([("DAI", "Dai Stablecoin")], live, 5) == []
    assert select_crypto(ranked, live, 0) == []

    rows = [
        {"instId": "AAPL-USDT-SWAP", "instCategory": "3", "state": "live",
         "settleCcy": "USDT", "ctType": "linear", "ruleType": "normal"},
        {"instId": "SAMSUNG-USDT-SWAP", "instCategory": "3", "state": "live",
         "settleCcy": "USDT", "ctType": "linear", "ruleType": "normal"},
        {"instId": "OPENAI-USDT-SWAP", "instCategory": "3", "state": "live",
         "settleCcy": "USDT", "ctType": "linear", "ruleType": "pre_market"},
        {"instId": "BTC-USDT-SWAP", "instCategory": "1", "state": "live",
         "settleCcy": "USDT", "ctType": "linear"},
        {"instId": "XAU-USDT-SWAP", "instCategory": "4", "state": "live",
         "settleCcy": "USDT", "ctType": "linear"},
        {"instId": "DEAD-USDT-SWAP", "instCategory": "3", "state": "suspend",
         "settleCcy": "USDT", "ctType": "linear"},
        {"instId": "AAPL-USD-SWAP", "instCategory": "3", "state": "live",
         "settleCcy": "USD", "ctType": "linear"},
        {"instId": "BTC-USD-SWAP", "instCategory": "1", "state": "live",
         "settleCcy": "BTC", "ctType": "inverse"},
    ]
    stocks = select_stocks(rows, enabled=True)
    assert stocks == ["AAPL-USDT-SWAP", "OPENAI-USDT-SWAP", "SAMSUNG-USDT-SWAP"], stocks
    assert select_stocks(rows, enabled=False) == []
    crypto_ids, _stocks = split_instruments(rows)
    assert crypto_ids == {"BTC-USDT-SWAP"}
    assert fallback_crypto(live, 2) == ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
    assert "USDC-USDT-SWAP" not in select_crypto([("USDC", "USDC"), ("BTC", "Bitcoin")], live, 5)


if __name__ == "__main__":
    offline_self_check()
    print("universe offline self-check ok")
