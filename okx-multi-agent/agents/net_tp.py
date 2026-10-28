"""Net-TP 监控：每 2 秒算一次「扣掉双边手续费和资金费后的净利」，够门槛就落袋。

净利 = 未实现盈亏 - 开仓手续费(实际成交) - 平仓手续费(按 taker 预估) - 已付资金费 - (滑点缓冲 + 半个点差)
门槛 = max(NET_TP_MIN_USDT, 名义价值 x NET_TP_MIN_PCT)，需连续 N 次满足，且点差正常。

另外做保本保护：浮盈走到止盈距离 50% 时，把交易所止损移到「开仓价 + 双边成本」。
"""
import time
import config

AGENT = "net_tp"


class NetTpMonitor:
    def __init__(self, client, journal, fees, executor, state):
        self.c, self.j, self.fees, self.ex, self.s = client, journal, fees, executor, state
        self.confirm = {}
        self.cost_cache = {}   # inst -> (ts, fee_in, funding)
        self.last_report = {}

    def _costs(self, p):
        key = (p["instId"], p["cTime"])
        hit = self.cost_cache.get(key)
        if hit and time.time() - hit[0] < 30:
            return hit[1], hit[2]
        try:
            fee_in = self.c.entry_fee(p["instId"], p["cTime"]) if p["cTime"] else 0.0
        except Exception:
            fee_in = 0.0
        try:
            funding = self.c.funding_paid(p["instId"], p["cTime"]) if p["cTime"] else 0.0
        except Exception:
            funding = 0.0
        self.cost_cache[key] = (time.time(), fee_in, funding)
        return fee_in, funding

    def estimate(self, p):
        notional = p["notional"] or p["qty"] * self.c.instrument(p["instId"])["ctVal"] * p["markPx"]
        fee_in, funding = self._costs(p)
        if fee_in <= 0:
            fee_in = notional * self.fees.taker
        fee_out = notional * self.fees.taker
        # 平仓吃对手价：滑点缓冲 + 半个点差（模拟盘点差比实盘宽）
        try:
            t = self.c.ticker(p["instId"])
            mid = (t["bid"] + t["ask"]) / 2
            half_spread_bps = (t["ask"] - t["bid"]) / mid * 1e4 / 2 if mid else 0.0
        except Exception:
            half_spread_bps = 0.0
        slip = notional * (config.SLIPPAGE_BPS + half_spread_bps) / 1e4
        net = p["upl"] - fee_in - fee_out - funding - slip
        return {"upl": p["upl"], "notional": notional, "fee_in": fee_in, "fee_out": fee_out,
                "funding": funding, "slip": slip, "net": net}

    def check(self, positions):
        for p in positions:
            inst = p["instId"]
            e = self.estimate(p)
            thr = max(config.NET_TP_MIN_USDT, e["notional"] * config.NET_TP_MIN_PCT)
            if time.time() - self.last_report.get(inst, 0) > 60:
                self.last_report[inst] = time.time()
                self.j.log(AGENT, "net_pnl", f"{inst} 浮盈 {e['upl']:+.2f} 开仓费 {e['fee_in']:.2f} 平仓费 "
                           f"{e['fee_out']:.2f} 资金费 {e['funding']:+.2f} 滑点 {e['slip']:.2f} → 净 {e['net']:+.2f}"
                           f"（门槛 {thr:.2f}）", inst=inst, **e, threshold=thr)
            self._maybe_breakeven(p, e)
            if not config.ENABLE_NET_TP or e["net"] <= thr:
                self.confirm[inst] = 0
                continue
            self.confirm[inst] = self.confirm.get(inst, 0) + 1
            if self.confirm[inst] < config.NET_TP_CONFIRM_CHECKS:
                continue
            t = self.c.ticker(inst)
            mid = (t["bid"] + t["ask"]) / 2
            spread = (t["ask"] - t["bid"]) / mid * 1e4 if mid else 1e9
            if spread > config.MAX_CLOSE_SPREAD_BPS:
                self.j.log(AGENT, "wait_spread", f"{inst} 净利够了但点差 {spread:.2f}bps 太大，本轮不平", inst=inst)
                continue
            self.j.log(AGENT, "trigger", f"{inst} 净利 {e['net']:+.2f} > 门槛 {thr:.2f}，连续 "
                       f"{self.confirm[inst]} 次确认 → 平仓落袋", inst=inst, est_net=e["net"])
            meta = self.s.d["open_meta"].setdefault(inst, {})
            meta["close_reason"], meta["est_net"] = "NET-TP 净利止盈", e["net"]
            self.ex.close(p, "NET-TP 净利止盈")
            self.confirm[inst] = 0

    def _maybe_breakeven(self, p, e):
        meta = self.s.d["open_meta"].get(p["instId"])
        if not meta or meta.get("be_done") or not meta.get("tp"):
            return
        entry, tp, mark = p["avgPx"], meta["tp"], p["markPx"]
        long = p["side"] == "long"
        moved = (mark - entry) if long else (entry - mark)
        if moved < abs(tp - entry) * 0.5:
            return
        cost = entry * (2 * self.fees.taker + 2 * config.SLIPPAGE_BPS / 1e4)
        be = entry + cost if long else entry - cost
        tick = self.c.instrument(p["instId"])["tickSz"]
        from .position import round_px
        be = round_px(be, tick, "up" if long else "down")
        if self.ex.amend_sl(p["instId"], be):
            meta["be_done"] = True
            self.j.log(AGENT, "breakeven", f"{p['instId']} 已走 50% 止盈距离，止损移到保本+成本 {be}",
                       inst=p["instId"], sl=be)
            self.s.save()
