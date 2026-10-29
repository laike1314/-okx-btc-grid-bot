"""Net-TP 监控：每 2 秒算一次「扣掉双边手续费和资金费后的净利」，够门槛就落袋。

净利 = 未实现盈亏 - 开仓手续费(实际成交) - 平仓手续费(按 taker 预估) - 已付资金费 - (滑点缓冲 + 半个点差)
门槛 = max(NET_TP_MIN_USDT, 名义价值 x NET_TP_MIN_PCT)，需连续 N 次满足，且点差正常。

另外：浮盈达到止损距离的一小段后，把止损抬到锁定大部分利润（必须在标记价正确一侧）。
持仓超过 MAX_HOLD_SEC 就市价平，不再干等远处的 2R。
"""
import time
import config
from .position import round_px, stop_is_valid

AGENT = "net_tp"


class NetTpMonitor:
    def __init__(self, client, journal, fees, executor, state):
        self.c, self.j, self.fees, self.ex, self.s = client, journal, fees, executor, state
        self.confirm = {}
        self.cost_cache = {}   # (inst, side, cTime) -> (ts, fee_in, funding)
        self.last_report = {}
        self._last_summary = None
        self._last_summary_at = 0.0
        self._audit_at = {}

    def _pkey(self, p):
        return f"{p.get('instId')}:{p.get('side') or ''}"

    def _costs(self, p):
        key = (p["instId"], p.get("side"), p["cTime"])
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
        """每个监控周期走完交易所返回的全部持仓。某一仓失败只记日志，不跳过后面的仓。"""
        positions = list(positions or [])
        summary = ",".join(self._pkey(p) for p in positions)
        now = time.time()
        if positions and (summary != self._last_summary or now - self._last_summary_at >= 60):
            self._last_summary = summary
            self._last_summary_at = now
            self.j.log(AGENT, "monitor_all",
                       f"本轮监控全部 {len(positions)} 个持仓: {summary}")
        for p in positions:
            try:
                self._check_one(p)
            except Exception as e:
                self.j.log(AGENT, "monitor_error",
                           f"{p.get('instId')} 本轮监控失败，其余仓位继续，下轮重试该仓: "
                           f"{type(e).__name__}: {e}", inst=p.get("instId"))

    def _audit_protection(self, p):
        if config.PROTECT_AUDIT_SEC <= 0:
            return False
        inst = p["instId"]
        now = self.s.now()
        if now - self._audit_at.get(inst, 0) < config.PROTECT_AUDIT_SEC:
            return False
        self._audit_at[inst] = now
        meta = self.s.d.get("open_meta", {}).get(inst) or {}
        result = self.ex.audit_bracket(p, meta)
        if isinstance(result, tuple) and len(result) == 2:
            meta = self.s.d["open_meta"].setdefault(inst, {})
            meta["sl"], meta["tp"] = result
            self.s.save()
        return result == "closed"

    def _check_one(self, p):
        inst = p["instId"]
        key = self._pkey(p)
        if self._audit_protection(p):
            return
        if self._maybe_max_hold(p):
            self.confirm[key] = 0
            return
        e = self.estimate(p)
        thr = max(config.NET_TP_MIN_USDT, e["notional"] * config.NET_TP_MIN_PCT)
        if time.time() - self.last_report.get(key, 0) > 60:
            self.last_report[key] = time.time()
            self.j.log(AGENT, "net_pnl", f"{inst} {p.get('side')} 浮盈 {e['upl']:+.2f} 开仓费 {e['fee_in']:.2f} "
                       f"平仓费 {e['fee_out']:.2f} 资金费 {e['funding']:+.2f} 滑点 {e['slip']:.2f} → 净 {e['net']:+.2f}"
                       f"（门槛 {thr:.2f}）", inst=inst, side=p.get("side"), **e, threshold=thr)
        self._maybe_trail(p, e)
        if not config.ENABLE_NET_TP or e["net"] <= thr:
            self.confirm[key] = 0
            return
        self.confirm[key] = self.confirm.get(key, 0) + 1
        if self.confirm[key] < config.NET_TP_CONFIRM_CHECKS:
            return
        t = self.c.ticker(inst)
        mid = (t["bid"] + t["ask"]) / 2
        spread = (t["ask"] - t["bid"]) / mid * 1e4 if mid else 1e9
        if spread > config.MAX_CLOSE_SPREAD_BPS:
            self.j.log(AGENT, "wait_spread", f"{inst} 净利够了但点差 {spread:.2f}bps 太大，本轮不平", inst=inst)
            return
        self.j.log(AGENT, "trigger", f"{inst} 净利 {e['net']:+.2f} > 门槛 {thr:.2f}，连续 "
                   f"{self.confirm[key]} 次确认 → 平仓落袋", inst=inst, est_net=e["net"])
        meta = self.s.d["open_meta"].setdefault(inst, {})
        meta["close_reason"], meta["est_net"] = "NET-TP 净利止盈", e["net"]
        self.ex.close(p, "NET-TP 净利止盈")
        self.confirm[key] = 0

    def _opened_ts(self, p):
        meta = self.s.d.get("open_meta", {}).get(p["instId"]) or {}
        if meta.get("opened_ts"):
            return float(meta["opened_ts"])
        ms = meta.get("open_ms") or p.get("cTime") or 0
        if ms and float(ms) > 1e11:
            return float(ms) / 1000.0
        return None

    def _maybe_max_hold(self, p):
        if config.MAX_HOLD_SEC <= 0:
            return False
        opened = self._opened_ts(p)
        if opened is None:
            return False
        age = self.s.now() - opened
        if age < config.MAX_HOLD_SEC:
            return False
        meta = self.s.d["open_meta"].setdefault(p["instId"], {})
        meta["close_reason"] = "超时平仓"
        self.j.log(AGENT, "max_hold", f"{p['instId']} 已持有 {int(age)}s，超过 {config.MAX_HOLD_SEC}s，市价平",
                   inst=p["instId"], age=age)
        self.ex.close(p, "超时平仓")
        self.s.save()
        return True

    def _maybe_trail(self, p, e):
        """浮盈够一段就把止损抬到锁定大部分利润。新止损必须在标记价的保护一侧。"""
        meta = self.s.d.get("open_meta", {}).get(p["instId"])
        if not meta:
            return
        entry, mark = float(p.get("avgPx") or 0), float(p.get("markPx") or 0)
        if entry <= 0 or mark <= 0:
            return
        long = p["side"] == "long"
        moved = (mark - entry) if long else (entry - mark)
        risk = abs(entry - float(meta.get("sl") or entry)) or entry * config.MIN_SL_PCT
        if moved <= 0 or moved < risk * config.TRAIL_ARM_R:
            return
        cost = entry * (2 * self.fees.taker + 2 * config.SLIPPAGE_BPS / 1e4)
        locked = max(cost, moved * config.TRAIL_LOCK_FRAC)
        raw = entry + locked if long else entry - locked
        tick = self.c.instrument(p["instId"])["tickSz"]
        buf = max(float(tick) * 2, mark * 0.0003)
        if long:
            raw = min(raw, mark - buf)
            new_sl = round_px(raw, tick, "down")
        else:
            raw = max(raw, mark + buf)
            new_sl = round_px(raw, tick, "up")
        if not stop_is_valid(p["side"], new_sl, mark):
            self.j.log(AGENT, "trail_skip", f"{p['instId']} 跟踪止损 {new_sl} 在标记价 {mark} 错误一侧，不改",
                       inst=p["instId"], sl=new_sl, mark=mark)
            return
        prev = float(meta.get("sl") or 0)
        if prev and ((long and new_sl <= prev) or (not long and new_sl >= prev)):
            return
        if self.ex.amend_sl(p["instId"], new_sl):
            meta["sl"] = new_sl
            meta["be_done"] = True
            self.j.log(AGENT, "trail", f"{p['instId']} 浮盈 {moved:.4f}，止损上移锁定利润到 {new_sl}",
                       inst=p["instId"], sl=new_sl)
            self.s.save()


def offline_self_check():
    """不联网。一个仓位报错时，后面的仓位仍要被检查到。"""
    import os
    import tempfile
    from agents.journal import Journal
    from agents.state import SharedState
    from agents.fees import FeeBook

    class _Fake:
        is_mock = True

        def instrument(self, inst):
            if inst == "BAD-USDT-SWAP":
                raise RuntimeError("down")
            return {"ctVal": 0.01, "lotSz": "0.01", "minSz": 0.01, "tickSz": "0.1", "maxLever": 10}

        def ticker(self, inst):
            return {"last": 100.0, "bid": 99.99, "ask": 100.01}

        def entry_fee(self, inst, since):
            return 0.01

        def funding_paid(self, inst, since):
            return 0.0

    class _NoClose:
        def close(self, *a, **k):
            raise AssertionError("净利不够不该平仓")

        def amend_sl(self, *a, **k):
            return False

        def audit_bracket(self, p, meta=None):
            return True

    d = tempfile.mkdtemp(prefix="ma-mon-")
    j = Journal(d, echo=False)
    try:
        c = _Fake()
        state = SharedState(d)
        fees = FeeBook(c, j)
        mon = NetTpMonitor(c, j, fees, _NoClose(), state)
        positions = [
            {"instId": "BAD-USDT-SWAP", "side": "long", "cTime": 1, "upl": 0.0,
             "notional": 0, "qty": 1, "markPx": 100.0, "avgPx": 100.0},
            {"instId": "ETH-USDT-SWAP", "side": "long", "cTime": 2, "upl": 0.0,
             "notional": 100.0, "qty": 1, "markPx": 100.0, "avgPx": 100.0},
            {"instId": "SOL-USDT-SWAP", "side": "short", "cTime": 3, "upl": 0.0,
             "notional": 50.0, "qty": 1, "markPx": 100.0, "avgPx": 100.0},
        ]
        seen = []
        real = mon._check_one

        def wrap(p):
            seen.append(p["instId"])
            return real(p)

        mon._check_one = wrap
        mon.check(positions)
        assert seen == ["BAD-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"], seen
        assert "ETH-USDT-SWAP:long" in mon.last_report
        assert "SOL-USDT-SWAP:short" in mon.last_report
        text = open(os.path.join(d, "decisions.jsonl"), encoding="utf-8").read()
        assert "monitor_error" in text and "BAD-USDT-SWAP" in text
        assert "monitor_all" in text and "SOL-USDT-SWAP:short" in text

        class _Amend:
            def __init__(self):
                self.calls = []

            def amend_sl(self, inst, sl):
                self.calls.append((inst, sl))
                return True

            def close(self, *a, **k):
                raise AssertionError("不该因跟踪止损去平仓")

        amend = _Amend()
        mon2 = NetTpMonitor(c, j, fees, amend, state)
        state.d["open_meta"]["ETH-USDT-SWAP"] = {"sl": 99.0, "tp": 100.2, "entry": 100.0}
        # 止损会算到标记价外侧：不改
        mon2._maybe_trail({"instId": "ETH-USDT-SWAP", "side": "long", "avgPx": 100.0,
                           "markPx": 100.12, "upl": 0.1, "notional": 100, "qty": 1, "cTime": 0}, {"net": 0})
        assert amend.calls == []
        # 标记价已经走远，止损落在正确一侧，可以改
        mon2._maybe_trail({"instId": "ETH-USDT-SWAP", "side": "long", "avgPx": 100.0,
                           "markPx": 101.0, "upl": 1, "notional": 100, "qty": 1, "cTime": 0}, {"net": 1})
        assert amend.calls and amend.calls[0][0] == "ETH-USDT-SWAP"
        assert amend.calls[0][1] < 101.0
    finally:
        j.close()
