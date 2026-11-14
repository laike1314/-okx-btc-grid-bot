"""Net-TP 监控：每 2 秒算一次「扣掉双边手续费和资金费后的净利」，够门槛就落袋。

净利 = 未实现盈亏 - 开仓手续费(实际成交) - 平仓手续费(按 taker 预估) - 已付资金费 - (滑点缓冲 + 半个点差)
门槛 = max(NET_TP_MIN_USDT, 名义价值 x NET_TP_MIN_PCT)，需连续 N 次满足，且点差正常。

另外：浮盈达到止损距离的一小段后，把止损抬到锁定大部分利润（必须在标记价正确一侧）。
持仓超过 MAX_HOLD_SEC 就市价平，不再干等远处的 2R。

波段每个监控周期还会看止盈：标记价或最新价已经穿过记录里的止盈，
而交易所没有有效止盈单时，立刻市价平。这条不受最短持仓限制。
"""
import time
import config
from .position import round_px, stop_is_valid

AGENT = "net_tp"
TP_CROSS_REASON = "止盈价已穿过且无止盈单，市价平仓"


def crossed_tp(side, tp, mark=None, last=None):
    """多：标记价或最新价 >= 止盈。空：任一价格 <= 止盈。返回穿过的那个价格。"""
    try:
        tp_v = float(tp)
    except (TypeError, ValueError):
        return None
    if tp_v <= 0:
        return None
    short = side == "short"
    for raw in (mark, last):
        try:
            px = float(raw)
        except (TypeError, ValueError):
            continue
        if px <= 0:
            continue
        if (short and px <= tp_v) or ((not short) and px >= tp_v):
            return px
    return None


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

    def _has_live_tp(self, inst):
        """查不到算法单时当作止盈还在，避免误市价平。"""
        try:
            algos = self.c.pending_algos(inst) or []
        except Exception as e:
            self.j.log(AGENT, "tp_check_err", f"{inst} 查询止盈单失败，本轮不市价抢平: {e}", inst=inst)
            return True
        from .executor import _algo_set
        return any(_algo_set(a, "tpTriggerPx") for a in algos)

    def _tp_crossed_close(self, p):
        """止盈价已被穿过且没有有效止盈单：立刻 reduce-only 市价平。不看最短持仓。"""
        inst = p.get("instId")
        if not inst:
            return False
        meta = (self.s.d.get("open_meta") or {}).get(inst) or {}
        tp = meta.get("tp")
        if not tp:
            return False
        side = p.get("side") or meta.get("direction") or "long"
        hit = crossed_tp(side, tp, p.get("markPx"), p.get("last"))
        if hit is None and not p.get("last"):
            try:
                t = self.c.ticker(inst)
                hit = crossed_tp(side, tp, None, (t or {}).get("last"))
            except Exception:
                hit = None
        if hit is None:
            return False
        if self._has_live_tp(inst):
            return False
        reason = TP_CROSS_REASON
        self.j.log(AGENT, "tp_cross_close",
                   f"{inst} {side} 价格 {hit} 已穿过止盈 {tp}，交易所没有止盈单，"
                   f"不受最短持仓限制，立刻市价平",
                   inst=inst, side=side, tp=float(tp), px=float(hit))
        closed = False
        try:
            closed = bool(self.ex.close(p, reason))
        except Exception as e:
            self.j.log(AGENT, "tp_cross_fail", f"{inst} 止盈价穿过后市价平失败: {e}", inst=inst)
            return False
        if not closed:
            self.j.log(AGENT, "tp_cross_fail", f"{inst} 止盈价穿过后市价平未完成，下个监控周期再试", inst=inst)
            return False
        book = self.s.d.setdefault("open_meta", {}).setdefault(inst, {})
        book["close_reason"] = reason
        self.s.save()
        return True

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
        if self._tp_crossed_close(p):
            return
        if config.is_swing():
            return self._check_swing(p)
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

    def _check_swing(self, p):
        """波段：交易所止损止盈一直有效。最短持仓内不做信号平仓（信号平仓在扫描轮）。
        到期才市价平。动量单浮盈后跟踪止损。不走剥头皮的净利立刻落袋。"""
        from .swing import time_exit_due
        inst = p["instId"]
        meta = (self.s.d.get("open_meta") or {}).get(inst)
        if not meta:
            return
        if self._audit_protection(p):
            return
        if time_exit_due(meta, self.s.now()):
            meta["close_reason"] = "到期平仓"
            age = self.s.now() - float(meta.get("opened_ts") or self.s.now())
            self.j.log(AGENT, "max_hold",
                       f"{inst} {meta.get('family')} 已持有 {int(age)}s，到达最长持仓，市价平",
                       inst=inst, age=age, family=meta.get("family"))
            self.ex.close(p, "到期平仓")
            self.s.save()
            return
        self._swing_trail(p)
        key = self._pkey(p)
        if time.time() - self.last_report.get(key, 0) > 60:
            try:
                e = self.estimate(p)
            except Exception as err:
                self.j.log(AGENT, "net_pnl_err", f"{inst} 净利估算失败: {err}", inst=inst)
                return
            self.last_report[key] = time.time()
            self.j.log(AGENT, "net_pnl",
                       f"{inst} {p.get('side')} {meta.get('family')} 浮盈 {e['upl']:+.2f} 净 {e['net']:+.2f}",
                       inst=inst, side=p.get("side"), family=meta.get("family"), **e)

    def _swing_trail(self, p):
        """动量单：价格已经盖过来回成本之后，按峰值回撤 ATR 上移止损。只收紧，不放宽。"""
        inst = p["instId"]
        meta = (self.s.d.get("open_meta") or {}).get(inst)
        if not meta or meta.get("family") != "momentum":
            return
        entry = float(p.get("avgPx") or meta.get("entry") or 0)
        mark = float(p.get("markPx") or 0)
        atr_px = float(meta.get("atr") or 0)
        if entry <= 0 or mark <= 0 or atr_px <= 0:
            return
        long = p["side"] == "long"
        rt = self.fees.maker + self.fees.taker + 2 * config.SLIPPAGE_BPS / 1e4
        in_profit = mark > entry * (1 + rt) if long else mark < entry * (1 - rt)
        if not in_profit:
            return
        if meta.get("peak_px"):
            peak = float(meta["peak_px"])
            peak = max(peak, mark) if long else min(peak, mark)
        else:
            peak = mark
        meta["peak_px"] = peak
        dist = atr_px * float(config.SWING_MOM_TRAIL_ATR)
        raw = (peak - dist) if long else (peak + dist)
        tick = self.c.instrument(inst)["tickSz"]
        buf = max(float(tick) * 2, mark * 0.0003)
        if long:
            raw = min(raw, mark - buf)
            new_sl = round_px(raw, tick, "down")
        else:
            raw = max(raw, mark + buf)
            new_sl = round_px(raw, tick, "up")
        if not stop_is_valid(p["side"], new_sl, mark):
            self.s.save()
            return
        prev = float(meta.get("sl") or 0)
        if prev and ((long and new_sl <= prev + float(tick) / 2) or (not long and new_sl >= prev - float(tick) / 2)):
            self.s.save()
            return
        if self.ex.amend_sl(inst, new_sl):
            meta["sl"] = new_sl
            self.j.log(AGENT, "trail", f"{inst} 动量单浮盈后止损上移到 {new_sl}", inst=inst, sl=new_sl)
        self.s.save()

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
        _tp_cross_close_checks()
    finally:
        j.close()


def _tp_cross_close_checks():
    """止盈价被穿过且没有止盈单时市价平。多空都测，最短持仓挡不住，并记入 trades / 已实现盈亏。"""
    import tempfile
    from agents.journal import Journal
    from agents.state import SharedState
    from agents.swing import signal_exit_reason

    saved = config.STRATEGY
    config.STRATEGY = "swing_combo"
    d = tempfile.mkdtemp(prefix="ma-tp-")
    j = Journal(d, echo=False)
    try:
        assert crossed_tp("short", 2.196, 2.127, 2.130) == 2.127
        assert crossed_tp("short", 2.196, 2.20, 2.10) == 2.10
        assert crossed_tp("short", 2.196, 2.25, 2.24) is None
        assert crossed_tp("long", 2.20, 2.19, 2.21) == 2.21
        assert crossed_tp("long", 2.20, 2.19, 2.195) is None

        class Ex:
            def __init__(self):
                self.reasons = []
                self.fail = False

            def close(self, pos, reason):
                if self.fail:
                    return False
                self.reasons.append((pos["instId"], pos["side"], reason))
                return True

            def audit_bracket(self, *a, **k):
                return True

            def amend_sl(self, *a, **k):
                raise AssertionError("止盈穿过后的监控不该再改止损")

        class C:
            def __init__(self, positions, algos):
                self.is_mock = True
                self._positions = positions
                self._algos = algos
                self.ticker_calls = 0

            def pending_algos(self, inst):
                return list(self._algos)

            def ticker(self, inst):
                self.ticker_calls += 1
                return {"last": 2.10, "bid": 2.099, "ask": 2.101}

            def instrument(self, inst):
                return {"ctVal": 0.01, "lotSz": "1", "minSz": 1, "tickSz": "0.001"}

            def entry_fee(self, *a):
                return 0.0

            def funding_paid(self, *a):
                return 0.0

        st = SharedState(d, clock=lambda: 1_700_000_000.0)
        now = st.now()
        hold = {"family": "oi", "direction": "short", "opened_ts": now,
                "min_hold_sec": 12 * 3600, "max_hold_sec": 24 * 3600,
                "sl": 2.388, "tp": 2.196, "entry": 2.273}
        st.d["open_meta"]["ICP-USDT-SWAP"] = dict(hold)
        assert signal_exit_reason(hold, {"direction": "long", "combined": 0.1, "blocked": True}, now) is None

        class Fees:
            taker = 0.0005
            maker = 0.0002

        ex = Ex()
        no_tp = [{"algoId": "sl1", "slTriggerPx": "2.388", "tpTriggerPx": ""}]
        live_tp = [{"algoId": "oco1", "ordType": "oco", "slTriggerPx": "2.388", "tpTriggerPx": "2.196"}]
        short_pos = {"instId": "ICP-USDT-SWAP", "side": "short", "posSide": "net",
                     "qty": 10, "avgPx": 2.273, "markPx": 2.25, "last": 2.24,
                     "upl": 1.0, "notional": 25, "cTime": int(now * 1000)}
        mon = NetTpMonitor(C([short_pos], no_tp), j, Fees(), ex, st)
        mon._check_one(dict(short_pos))
        assert ex.reasons == []

        hit = dict(short_pos, markPx=2.127, last=2.130)
        mon.c = C([hit], live_tp)
        mon._check_one(dict(hit))
        assert ex.reasons == []

        mon.c = C([hit], no_tp)
        mon._check_one(dict(hit))
        assert ex.reasons == [("ICP-USDT-SWAP", "short", TP_CROSS_REASON)]
        assert st.d["open_meta"]["ICP-USDT-SWAP"]["close_reason"] == TP_CROSS_REASON

        long_meta = dict(hold, direction="long", tp=2.20, sl=2.10, entry=2.15)
        st.d["open_meta"]["ETH-USDT-SWAP"] = long_meta
        long_pos = {"instId": "ETH-USDT-SWAP", "side": "long", "posSide": "net",
                    "qty": 1, "avgPx": 2.15, "markPx": 2.19, "last": 2.21,
                    "upl": 1.0, "notional": 20, "cTime": int(now * 1000)}
        mon.c = C([long_pos], no_tp)
        mon._check_one(dict(long_pos))
        assert ex.reasons[-1] == ("ETH-USDT-SWAP", "long", TP_CROSS_REASON)
        assert signal_exit_reason(long_meta, {"direction": "short", "combined": 0.9, "blocked": False}, now) is None

        quiet = dict(long_pos, markPx=2.16, last=2.17)
        before = len(ex.reasons)
        mon._check_one(dict(quiet))
        assert len(ex.reasons) == before

        bare = dict(short_pos, markPx=2.20, last=None)
        mon.c = C([bare], no_tp)
        mon._check_one(dict(bare))
        assert ex.reasons[-1][0] == "ICP-USDT-SWAP" and ex.reasons[-1][1] == "short"
        assert mon.c.ticker_calls == 1

        ex.fail = True
        st.d["open_meta"]["ICP-USDT-SWAP"].pop("close_reason", None)
        mon._check_one(dict(hit))
        assert st.d["open_meta"]["ICP-USDT-SWAP"].get("close_reason") in (None, "")
        _book_tp_cross_trade()
    finally:
        config.STRATEGY = saved
        j.close()


def _book_tp_cross_trade():
    """走和监控循环一样的 equity.sync，把这笔市价止盈记进 trades.csv 和 daily。"""
    import tempfile
    from agents.journal import Journal

    data_dir = tempfile.mkdtemp(prefix="ma-tp-book-")
    journal = Journal(data_dir, echo=False)
    try:
        _book_tp_cross_trade_in(data_dir, journal)
    finally:
        journal.close()


def _book_tp_cross_trade_in(data_dir, journal):
    import csv
    from agents.equity import EquityTracker
    from agents.margin import configured_capital, virtual_equity
    from agents.state import SharedState

    st = SharedState(data_dir, clock=lambda: 1_700_000_100.0)
    now = st.now()
    inst = "ICP-USDT-SWAP"
    st.d["swing_realized_basis"] = 0.0
    st.d["swing_capital_usdt"] = configured_capital()
    st.d["swing_capital_since"] = now
    st.d["open_meta"][inst] = {
        "direction": "short", "qty": 109986.8, "entry": 2.273,
        "sl": 2.388, "tp": 2.196, "opened_ts": now, "open_ms": int(now * 1000),
        "min_hold_sec": 12 * 3600, "max_hold_sec": 24 * 3600,
        "family": "momentum", "regime": "trend"}
    pos = {"instId": inst, "side": "short", "posSide": "net", "qty": 109986.8,
           "avgPx": 2.273, "markPx": 2.127, "last": 2.127, "upl": 160.0,
           "notional": 2500, "cTime": int(now * 1000)}

    class C:
        is_mock = True

        def __init__(self):
            self.open = True

        def positions(self):
            return [] if not self.open else [dict(pos)]

        def pending_algos(self, inst_id):
            return [{"algoId": "sl1", "slTriggerPx": "2.388", "tpTriggerPx": ""}]

        def ticker(self, inst_id):
            return {"last": 2.127, "bid": 2.126, "ask": 2.128}

        def instrument(self, inst_id):
            return {"ctVal": 0.01, "tickSz": "0.001"}

        def last_closed(self, inst_id):
            return {"realizedPnl": 146.5, "pnl": 160.0, "fee": -13.5, "fundingFee": 0.0,
                    "closeAvgPx": 2.127, "openAvgPx": 2.273, "uTime": int(now * 1000) + 1000}

        def equity(self):
            return 10146.5

        def entry_fee(self, *a):
            return 0.0

        def funding_paid(self, *a):
            return 0.0

    class Ex:
        def __init__(self, client):
            self.c = client

        def close(self, position, reason):
            self.c.open = False
            self.reason = reason
            return True

        def audit_bracket(self, *a, **k):
            return True

        def amend_sl(self, *a, **k):
            return False

    class Risk:
        def on_trade_closed(self, inst_id, net):
            st.add_daily_pnl(net)

    c = C()
    ex = Ex(c)
    eq = EquityTracker(c, journal, st, Risk(), data_dir)
    eq.known.add(inst)
    mon = NetTpMonitor(c, journal, None, ex, st)
    live = c.positions()
    eq.sync(live)
    mon.check(live)
    assert ex.reason == TP_CROSS_REASON
    eq.sync(c.positions())
    rows = list(csv.DictReader(open(eq.trades_csv, encoding="utf-8")))
    assert len(rows) == 1, rows
    assert rows[0]["instId"] == inst and rows[0]["direction"] == "short"
    assert TP_CROSS_REASON in rows[0]["reason"]
    assert abs(float(rows[0]["net_pnl"]) - 146.5) < 1e-6
    assert abs(virtual_equity(st, 0) - (configured_capital() + 146.5)) < 1e-6
