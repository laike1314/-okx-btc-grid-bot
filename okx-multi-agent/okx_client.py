"""OKX v5 精简封装 + 离线 Mock 客户端。

- OkxClient: 真实 REST 调用，每个请求都带 `x-simulated-trading: 1`（只走模拟盘）。
- MockOkxClient: 无密钥时的冒烟测试用，本地随机行情 + 撮合，不联网。

两个类方法名一致，代理只依赖这些方法。
"""
import base64
import hashlib
import hmac
import json
import math
import random
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode

import config

RATE_LIMIT_CODES = {"50011", "50061", "50013", "50026"}   # 限流 / 系统繁忙，可安全重试


class OkxError(RuntimeError):
    def __init__(self, code, msg, data=None):
        super().__init__(f"[{code}] {msg}")
        self.code = str(code)
        self.data = data


def fmt_num(x, step=None):
    """按步长向下取整并转成不带科学计数的字符串。"""
    d = Decimal(str(x))
    if step:
        s = Decimal(str(step))
        d = (d / s).to_integral_value(rounding=ROUND_DOWN) * s
    return format(d.normalize(), "f")


def _f(v, default=0.0):
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


# =====================================================================
# 真实客户端（模拟盘）
# =====================================================================
class OkxClient:
    is_mock = False

    def __init__(self, api_key, secret, passphrase, base_url=config.BASE_URL):
        import requests
        if config.SIMULATED_TRADING != "1":
            raise SystemExit("拒绝启动：本项目只允许模拟盘 (SIMULATED_TRADING=1)")
        self.key, self.secret, self.passphrase = api_key, secret, passphrase
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self._inst_cache = {}
        self._next_get_at = 0.0

    def _pace_get(self):
        """公开/私有 GET 之间留一点间隔，持仓多、交易池大时不把 OKX 限速打满。"""
        gap = float(getattr(config, "PUBLIC_REQ_GAP_SEC", 0) or 0)
        if gap <= 0:
            return
        now = time.monotonic()
        wait = self._next_get_at - now
        if wait > 0:
            time.sleep(wait)
        self._next_get_at = time.monotonic() + gap

    # ---------- 底层 ----------
    def _sign(self, ts, method, path, body):
        mac = hmac.new(self.secret.encode(), (ts + method + path + body).encode(), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    def request(self, method, path, params=None, body=None, private=False, retries=4):
        method = method.upper()
        q = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        req_path = path + ("?" + urlencode(q) if q else "")
        body_text = json.dumps(body, separators=(",", ":")) if method == "POST" else ""
        last = None
        for attempt in range(1, retries + 1):
            if method == "GET":
                self._pace_get()
            headers = {"Content-Type": "application/json", "x-simulated-trading": config.SIMULATED_TRADING}
            if private:
                ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                headers.update({
                    "OK-ACCESS-KEY": self.key,
                    "OK-ACCESS-SIGN": self._sign(ts, method, req_path, body_text),
                    "OK-ACCESS-TIMESTAMP": ts,
                    "OK-ACCESS-PASSPHRASE": self.passphrase,
                })
            try:
                r = self.s.request(method, self.base + req_path, headers=headers,
                                   data=body_text or None, timeout=15)
                if r.status_code == 429:
                    raise OkxError("429", "HTTP 429 限流")
                data = r.json()
            except OkxError as e:
                last = e
            except Exception as e:
                # 网络异常：GET 可重试；POST 不重试，避免重复下单
                if method != "GET":
                    raise OkxError("NET", f"网络错误(未重试以免重复下单): {e}")
                last = e
            else:
                code = str(data.get("code"))
                if code == "0":
                    return data.get("data", [])
                rows = data.get("data") or []
                scode = str(rows[0].get("sCode", "")) if rows and isinstance(rows[0], dict) else ""
                eff = scode if scode and scode != "0" else code
                smsg = rows[0].get("sMsg", "") if rows and isinstance(rows[0], dict) else ""
                if eff in RATE_LIMIT_CODES or code in RATE_LIMIT_CODES:
                    last = OkxError(eff, f"限流/繁忙: {data.get('msg')} {smsg}")
                else:
                    raise OkxError(eff, f"{data.get('msg')} {smsg}".strip(), data)
            if attempt < retries:
                time.sleep(min(2 ** attempt * 0.5, 8))
        raise OkxError(getattr(last, "code", "RETRY"), f"重试 {retries} 次仍失败 {method} {path}: {last}")

    # ---------- 行情 ----------
    def instrument(self, inst_id):
        if inst_id not in self._inst_cache:
            rows = self.request("GET", "/api/v5/public/instruments", {"instType": "SWAP", "instId": inst_id})
            r = rows[0]
            self._inst_cache[inst_id] = {
                "ctVal": _f(r["ctVal"]), "lotSz": r["lotSz"], "minSz": _f(r["minSz"]),
                "tickSz": r["tickSz"], "maxLever": _f(r.get("lever"), 10),
            }
        return self._inst_cache[inst_id]

    def ticker(self, inst_id):
        r = self.request("GET", "/api/v5/market/ticker", {"instId": inst_id})[0]
        last = _f(r["last"])
        # 永续的 volCcy24h 是基础币数量，乘最新价得到 USDT 成交额。
        vol_usdt = _f(r.get("volCcy24h")) * last
        return {"last": last, "bid": _f(r["bidPx"]), "ask": _f(r["askPx"]), "vol_usdt_24h": vol_usdt}

    def mark_price(self, inst_id):
        r = self.request("GET", "/api/v5/public/mark-price", {"instType": "SWAP", "instId": inst_id})[0]
        return _f(r["markPx"])

    def index_price(self, inst_id):
        idx = inst_id[:-5] if str(inst_id).endswith("-SWAP") else inst_id
        rows = self.request("GET", "/api/v5/market/index-tickers", {"instId": idx})
        if not rows:
            return 0.0
        return _f(rows[0].get("idxPx"))

    def candles(self, inst_id, bar, limit=120):
        rows = self.request("GET", "/api/v5/market/candles", {"instId": inst_id, "bar": bar, "limit": str(limit)})
        out = [{"ts": int(r[0]), "o": _f(r[1]), "h": _f(r[2]), "l": _f(r[3]), "c": _f(r[4]),
                "vol": _f(r[5]), "confirm": r[8] == "1" if len(r) > 8 else True} for r in rows]
        out.reverse()                                   # 旧 -> 新
        return [c for c in out if c["confirm"]]         # 只用已收盘 K 线

    def funding_rate(self, inst_id):
        r = self.request("GET", "/api/v5/public/funding-rate", {"instId": inst_id})[0]
        return _f(r.get("fundingRate"))

    # ---------- 账户 ----------
    def pos_mode(self):
        return self.request("GET", "/api/v5/account/config", private=True)[0].get("posMode", "net_mode")

    def equity(self):
        r = self.request("GET", "/api/v5/account/balance", {"ccy": "USDT"}, private=True)[0]
        for d in r.get("details", []):
            if d.get("ccy") == "USDT":
                return _f(d.get("eq"), _f(r.get("totalEq")))
        return _f(r.get("totalEq"))

    def positions(self):
        rows = self.request("GET", "/api/v5/account/positions", {"instType": "SWAP"}, private=True)
        return [self._norm_pos(p) for p in rows if _f(p.get("pos")) != 0]

    def _norm_pos(self, p):
        qty = _f(p.get("pos"))
        side = p.get("posSide")
        if side not in ("long", "short"):
            side = "long" if qty > 0 else "short"
        return {"instId": p["instId"], "side": side, "posSide": p.get("posSide", "net"),
                "qty": abs(qty), "avgPx": _f(p.get("avgPx")), "markPx": _f(p.get("markPx")),
                "upl": _f(p.get("upl")), "notional": abs(_f(p.get("notionalUsd"))),
                "lever": _f(p.get("lever")), "cTime": int(p.get("cTime") or 0)}

    def fee_rates(self):
        r = self.request("GET", "/api/v5/account/trade-fee", {"instType": "SWAP"}, private=True)[0]
        taker = abs(_f(r.get("takerU") or r.get("taker")))
        maker = abs(_f(r.get("makerU") or r.get("maker")))
        return taker or config.DEFAULT_TAKER_FEE, maker or config.DEFAULT_MAKER_FEE

    def entry_fee(self, inst_id, since_ms):
        rows = self.request("GET", "/api/v5/trade/fills",
                            {"instType": "SWAP", "instId": inst_id, "begin": str(since_ms - 1000), "limit": "100"},
                            private=True)
        return sum(abs(_f(r.get("fee"))) for r in rows if int(r.get("ts") or 0) >= since_ms - 1000)

    def funding_paid(self, inst_id, since_ms):
        """开仓以来资金费，正数 = 支出。"""
        rows = self.request("GET", "/api/v5/account/bills",
                            {"instType": "SWAP", "type": "8", "begin": str(since_ms), "limit": "100"}, private=True)
        return -sum(_f(r.get("balChg")) for r in rows
                    if r.get("instId") == inst_id and int(r.get("ts") or 0) >= since_ms)

    def last_closed(self, inst_id):
        rows = self.request("GET", "/api/v5/account/positions-history",
                            {"instType": "SWAP", "instId": inst_id, "limit": "1"}, private=True)
        if not rows:
            return None
        r = rows[0]
        return {"realizedPnl": _f(r.get("realizedPnl")), "pnl": _f(r.get("pnl")), "fee": _f(r.get("fee")),
                "fundingFee": _f(r.get("fundingFee")), "openAvgPx": _f(r.get("openAvgPx")),
                "closeAvgPx": _f(r.get("closeAvgPx")), "uTime": int(r.get("uTime") or 0)}

    # ---------- 交易 ----------
    def set_leverage(self, inst_id, lever, pos_side=None):
        body = {"instId": inst_id, "lever": str(lever), "mgnMode": config.TD_MODE}
        if pos_side:
            body["posSide"] = pos_side
        return self.request("POST", "/api/v5/account/set-leverage", body=body, private=True)

    def place_order(self, body):
        body.setdefault("clOrdId", "ma" + uuid.uuid4().hex[:20])   # 幂等标识
        return self.request("POST", "/api/v5/trade/order", body=body, private=True)[0]

    def get_order(self, inst_id, ord_id):
        return self.request("GET", "/api/v5/trade/order", {"instId": inst_id, "ordId": ord_id}, private=True)[0]

    def cancel_order(self, inst_id, ord_id):
        return self.request("POST", "/api/v5/trade/cancel-order",
                            body={"instId": inst_id, "ordId": ord_id}, private=True)

    def close_position(self, inst_id, pos_side="net"):
        return self.request("POST", "/api/v5/trade/close-position",
                            body={"instId": inst_id, "mgnMode": config.TD_MODE, "posSide": pos_side,
                                  "autoCxl": True}, private=True)

    def pending_algos(self, inst_id):
        out = []
        for t in ("conditional", "oco"):
            out += self.request("GET", "/api/v5/trade/orders-algo-pending",
                                {"instType": "SWAP", "instId": inst_id, "ordType": t}, private=True)
        return out

    def cancel_algos(self, inst_id, algo_ids):
        if not algo_ids:
            return []
        return self.request("POST", "/api/v5/trade/cancel-algos",
                            body=[{"instId": inst_id, "algoId": a} for a in algo_ids], private=True)

    def place_sl_algo(self, inst_id, close_side, pos_side, sl, tp=None):
        """单独挂交易所止损，可选同时带止盈。closeFraction=1 全平。"""
        body = {"instId": inst_id, "tdMode": config.TD_MODE, "side": close_side, "posSide": pos_side,
                "ordType": "conditional", "closeFraction": "1", "reduceOnly": True,
                "slTriggerPx": str(sl), "slOrdPx": "-1", "slTriggerPxType": "mark"}
        if tp:
            body.update({"tpTriggerPx": str(tp), "tpOrdPx": "-1", "tpTriggerPxType": "last"})
        return self.request("POST", "/api/v5/trade/order-algo", body=body, private=True)[0]

    def amend_sl(self, inst_id, algo_id, new_sl):
        return self.request("POST", "/api/v5/trade/amend-algos",
                            body={"instId": inst_id, "algoId": algo_id, "newSlTriggerPx": str(new_sl),
                                  "newSlTriggerPxType": "mark"}, private=True)

    def step(self):
        pass


# =====================================================================
# Mock 客户端：离线冒烟测试
# =====================================================================
class MockOkxClient:
    """每调用一次 step() 生成一根新的 5m K 线（带趋势 regime），并检查 SL/TP 触发。"""
    is_mock = True
    SPECS = {"BTC-USDT-SWAP": (65000, 0.01, "0.01", 0.01, "0.1"),
             "ETH-USDT-SWAP": (3200, 0.1, "0.01", 0.01, "0.01"),
             "SOL-USDT-SWAP": (150, 1, "0.01", 0.01, "0.001")}
    TAKER = 0.0005

    def __init__(self, seed=7, start_equity=10000.0):
        self.rng = random.Random(seed)
        self.cash = start_equity
        self.now_ms = int(time.time() * 1000)
        self.bars = {}
        self.drift = {}
        self.pos = {}        # instId -> dict
        self.algos = {}      # instId -> {"algoId","sl","tp"}
        self.history = {}
        self.fills_fee = {}
        for inst, (px, *_r) in self.SPECS.items():
            self.bars[inst] = []
            self.drift[inst] = 0.0
            p = float(px)
            for _ in range(1500):
                p = self._new_bar(inst, p)

    def _new_bar(self, inst, p):
        if self.rng.random() < 0.03:     # 切换趋势 regime
            self.drift[inst] = self.rng.choice([-1, 0, 0, 1]) * 0.0012
        o = p
        c = o * (1 + self.drift[inst] + self.rng.gauss(0, 0.0025))
        h = max(o, c) * (1 + abs(self.rng.gauss(0, 0.0008)))
        l = min(o, c) * (1 - abs(self.rng.gauss(0, 0.0008)))
        vol = abs(self.rng.gauss(1000, 300)) * (2.5 if abs(c / o - 1) > 0.004 else 1)
        ts = (self.bars[inst][-1]["ts"] + 300000) if self.bars[inst] else self.now_ms - 1500 * 300000
        self.bars[inst].append({"ts": ts, "o": o, "h": h, "l": l, "c": c, "vol": vol, "confirm": True})
        if len(self.bars[inst]) > 2000:
            self.bars[inst] = self.bars[inst][-2000:]
        return c

    def step(self):
        self.now_ms += 300000
        for inst in self.SPECS:
            self._new_bar(inst, self.bars[inst][-1]["c"])
            bar = self.bars[inst][-1]
            p, a = self.pos.get(inst), self.algos.get(inst)
            if p and a:
                long = p["side"] == "long"
                if a.get("sl") and ((long and bar["l"] <= a["sl"]) or (not long and bar["h"] >= a["sl"])):
                    self._close(inst, a["sl"], "SL")
                elif a.get("tp") and ((long and bar["h"] >= a["tp"]) or (not long and bar["l"] <= a["tp"])):
                    self._close(inst, a["tp"], "TP")

    # ---------- 行情 ----------
    def instrument(self, inst_id):
        _px, ct, lot, mn, tick = self.SPECS[inst_id]
        return {"ctVal": ct, "lotSz": lot, "minSz": mn, "tickSz": tick, "maxLever": 50}

    def _last(self, inst):
        return self.bars[inst][-1]["c"]

    def ticker(self, inst_id):
        last = self._last(inst_id)
        return {"last": last, "bid": last * (1 - 0.00005), "ask": last * (1 + 0.00005),
                "vol_usdt_24h": 50_000_000.0}

    def mark_price(self, inst_id):
        return self._last(inst_id)

    def index_price(self, inst_id):
        return self._last(inst_id)

    def candles(self, inst_id, bar, limit=120):
        bars = self.bars[inst_id]
        # 内部 K 线是 5 分钟。15m / 1H 按根数聚合，趋势过滤在冒烟里仍然和信号周期不同。
        factor = {"15m": 3, "30m": 6, "1H": 12, "2H": 24, "4H": 48}.get(bar, 1)
        if factor > 1:
            agg = []
            start = len(bars) - factor * limit
            for i in range(start, len(bars), factor):
                chunk = bars[max(i, 0):i + factor]
                if len(chunk) < factor:
                    continue
                agg.append({"ts": chunk[0]["ts"], "o": chunk[0]["o"], "h": max(b["h"] for b in chunk),
                            "l": min(b["l"] for b in chunk), "c": chunk[-1]["c"],
                            "vol": sum(b["vol"] for b in chunk), "confirm": True})
            return agg[-limit:]
        return [dict(b) for b in bars[-limit:]]

    def funding_rate(self, inst_id):
        return 0.0001

    # ---------- 账户 ----------
    def pos_mode(self):
        return "net_mode"

    def _upl(self, p):
        sign = 1 if p["side"] == "long" else -1
        return (self._last(p["instId"]) - p["avgPx"]) * p["qty"] * p["ctVal"] * sign

    def equity(self):
        return self.cash + sum(self._upl(p) for p in self.pos.values())

    def positions(self):
        out = []
        for p in self.pos.values():
            q = dict(p)
            q["markPx"] = self._last(p["instId"])
            q["upl"] = self._upl(p)
            q["notional"] = p["qty"] * p["ctVal"] * q["markPx"]
            out.append(q)
        return out

    def fee_rates(self):
        return self.TAKER, 0.0002

    def entry_fee(self, inst_id, since_ms):
        return self.fills_fee.get(inst_id, 0.0)

    def funding_paid(self, inst_id, since_ms):
        return 0.0

    def last_closed(self, inst_id):
        return self.history.get(inst_id)

    # ---------- 交易 ----------
    def set_leverage(self, inst_id, lever, pos_side=None):
        self.lever = lever
        return [{}]

    def _close(self, inst, px, why):
        p = self.pos.pop(inst)
        self.algos.pop(inst, None)
        sign = 1 if p["side"] == "long" else -1
        pnl = (px - p["avgPx"]) * p["qty"] * p["ctVal"] * sign
        fee_out = px * p["qty"] * p["ctVal"] * self.TAKER
        fee_in = self.fills_fee.pop(inst, 0.0)
        self.cash += pnl - fee_out
        self.history[inst] = {"realizedPnl": pnl - fee_in - fee_out, "pnl": pnl, "fee": -(fee_in + fee_out),
                              "fundingFee": 0.0, "openAvgPx": p["avgPx"], "closeAvgPx": px,
                              "uTime": self.now_ms, "why": why}

    def place_order(self, body):
        inst = body["instId"]
        t = self.ticker(inst)
        px = t["ask"] if body["side"] == "buy" else t["bid"]
        maker = body.get("ordType") == "post_only"
        if maker:
            px = float(body["px"])
            # 会吃单的价格直接拒，和交易所 post-only 一样。
            if (body["side"] == "buy" and px >= t["ask"]) or (body["side"] == "sell" and px <= t["bid"]):
                raise OkxError("51016", "post-only order would take")
        if body.get("ordType") == "ioc":
            lim = float(body["px"])
            if (body["side"] == "buy" and px > lim) or (body["side"] == "sell" and px < lim):
                return {"ordId": "mock-cancel", "sCode": "0"}
        qty = float(body["sz"])
        if body.get("reduceOnly"):
            if inst not in self.pos:
                raise OkxError("51023", "Position does not exist")
            self._close(inst, px, "IOC")
            return {"ordId": "mock-" + uuid.uuid4().hex[:8], "sCode": "0"}
        ct = self.instrument(inst)["ctVal"]
        fee = px * qty * ct * (0.0002 if maker else self.TAKER)
        self.cash -= fee
        self.fills_fee[inst] = fee
        side = "long" if body["side"] == "buy" else "short"
        self.pos[inst] = {"instId": inst, "side": side, "posSide": "net", "qty": qty, "avgPx": px,
                          "ctVal": ct, "lever": float(getattr(self, "lever", 5)), "cTime": self.now_ms}
        algo = None
        for a in body.get("attachAlgoOrds", []):
            algo = {"algoId": "algo-" + uuid.uuid4().hex[:8],
                    "sl": float(a["slTriggerPx"]) if a.get("slTriggerPx") else None,
                    "tp": float(a["tpTriggerPx"]) if a.get("tpTriggerPx") else None}
        if algo:
            self.algos[inst] = algo
        return {"ordId": "mock-" + uuid.uuid4().hex[:8], "sCode": "0"}

    def get_order(self, inst_id, ord_id):
        p = self.pos.get(inst_id)
        return {"state": "filled", "avgPx": str(p["avgPx"] if p else self._last(inst_id)),
                "accFillSz": str(p["qty"] if p else 0)}

    def cancel_order(self, inst_id, ord_id):
        return {"ordId": ord_id, "sCode": "0"}

    def close_position(self, inst_id, pos_side="net"):
        if inst_id not in self.pos:
            raise OkxError("51023", "Position does not exist")
        t = self.ticker(inst_id)
        self._close(inst_id, t["bid"] if self.pos[inst_id]["side"] == "long" else t["ask"], "MARKET")
        return [{}]

    def pending_algos(self, inst_id):
        a = self.algos.get(inst_id)
        return [{"algoId": a["algoId"], "slTriggerPx": str(a["sl"] or ""), "tpTriggerPx": str(a["tp"] or "")}] if a else []

    def cancel_algos(self, inst_id, algo_ids):
        self.algos.pop(inst_id, None)
        return [{}]

    def place_sl_algo(self, inst_id, close_side, pos_side, sl, tp=None):
        if inst_id not in self.pos:
            raise OkxError("51023", "Position does not exist")
        self.algos[inst_id] = {"algoId": "algo-" + uuid.uuid4().hex[:8], "sl": float(sl),
                               "tp": float(tp) if tp else None}
        return {"algoId": self.algos[inst_id]["algoId"], "sCode": "0"}

    def amend_sl(self, inst_id, algo_id, new_sl):
        p, a = self.pos.get(inst_id), self.algos.get(inst_id)
        if not a:
            raise OkxError("51023", "algo not found")
        mark = self._last(inst_id)
        if p and ((p["side"] == "long" and new_sl >= mark) or (p["side"] == "short" and new_sl <= mark)):
            raise OkxError("51304", "SL trigger price is past mark price")
        a["sl"] = float(new_sl)
        return [{}]
