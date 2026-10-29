"""Executor 代理：开仓（附交易所止损/止盈）、改止损、撤单、平仓。

- 开仓：市价单 + attachAlgoOrds（SL 用标记价格触发，TP 用最新价触发）
- 开仓后核验交易所止损是否存在；没有则单独补挂；补挂也失败 -> 立即平仓（裸仓不留）
- 平仓：先撤算法单，IOC 限价吃对手价，最多 N 次；剩余用 close-position 市价兜底
- 已知错误：51023 仓位不存在（视为已平），51304 止损价已越过标记价（不改，记录），
            51088 TP/SL 已存在（视为成功），限流在 okx_client 里自动退避重试
"""
import time
import config
from okx_client import OkxError, fmt_num
from .position import bracket_from_fill, round_px, stop_is_valid
from .risk import chase_blocked

AGENT = "executor"


def _algo_set(algo, key):
    raw = (algo or {}).get(key)
    if raw in (None, "", "0", 0):
        return False
    try:
        return float(raw) > 0
    except (TypeError, ValueError):
        return False


def parse_fill_sz(raw):
    """成交张数。空值返回 None（未知）；字符串 \"0\" 是 0.0，不能当成已成交。"""
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip() == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class Executor:
    def __init__(self, client, journal):
        self.c, self.j = client, journal
        self.long_short = False

    def init_mode(self):
        try:
            self.long_short = self.c.pos_mode() == "long_short_mode"
        except Exception as e:
            self.j.log(AGENT, "pos_mode_err", f"读取持仓模式失败，按单向持仓处理: {e}")
        self.j.log(AGENT, "pos_mode", "双向持仓 long_short_mode" if self.long_short else "单向持仓 net_mode")

    def _pos_side(self, direction):
        return direction if self.long_short else "net"

    # ---------------- 开仓 ----------------
    def open(self, plan):
        inst, d = plan["instId"], plan["direction"]
        spec = self.c.instrument(inst)
        try:
            self.c.set_leverage(inst, plan["lever"], self._pos_side(d) if self.long_short else None)
        except OkxError as e:
            self.j.log(AGENT, "lever_warn", f"{inst} 设杠杆失败（继续用当前杠杆）: {e}", inst=inst, code=e.code)
        # 用最新盘口复核止损没越过当前价（防 51304）
        t = self.c.ticker(inst)
        px = t["ask"] if d == "long" else t["bid"]
        sig = float(plan.get("signal_px") or plan.get("entry_ref") or 0)
        if chase_blocked(d, sig, t["bid"], t["ask"], config.CHASE_LIMIT_PCT):
            self.j.log(AGENT, "no_chase", f"{inst} 下单前价格已偏离信号 {sig} 超过 "
                       f"{config.CHASE_LIMIT_PCT:.2%}，不追", inst=inst)
            return None
        if (d == "long" and plan["sl"] >= px) or (d == "short" and plan["sl"] <= px):
            self.j.log(AGENT, "abort", f"{inst} 行情已越过止损价 {plan['sl']}，放弃开仓", inst=inst)
            return None
        # 贴着己方盘口挂 post-only，超时撤单，不用市价去追。
        limit = t["bid"] if d == "long" else t["ask"]
        body = {"instId": inst, "tdMode": config.TD_MODE, "side": "buy" if d == "long" else "sell",
                "posSide": self._pos_side(d), "ordType": "post_only",
                "px": fmt_num(limit, spec["tickSz"]), "sz": fmt_num(plan["qty"], spec["lotSz"]),
                "attachAlgoOrds": [{"slTriggerPx": str(plan["sl"]), "slOrdPx": "-1", "slTriggerPxType": "mark",
                                    "tpTriggerPx": str(plan["tp"]), "tpOrdPx": "-1", "tpTriggerPxType": "last"}]}
        try:
            r = self.c.place_order(body)
        except OkxError as e:
            if e.code == "51088":
                self.j.log(AGENT, "order_51088", f"{inst} TP/SL 已存在，视为成功", inst=inst)
            else:
                self.j.log(AGENT, "order_fail", f"{inst} 下单失败: {e}", inst=inst, code=e.code)
                return None
            r = {}
        ord_id = r.get("ordId")
        fill_px, filled = self._await_fill(inst, ord_id)
        if not filled or filled <= 0 or not fill_px or fill_px <= 0:
            self.j.log(AGENT, "no_fill", f"{inst} 没有真实成交（accFillSz={filled}），不记账、不当成持仓",
                       inst=inst, ordId=ord_id, qty=filled or 0)
            return None
        ref = float(limit or plan.get("entry_ref") or fill_px)
        slip = abs(fill_px - ref) / ref if ref else 0.0
        if slip > config.MAX_FILL_SLIP_PCT:
            self.j.log(AGENT, "slip_cancel", f"{inst} 成交价 {fill_px} 偏离计划 {ref} 达 {slip:.3%}，"
                       f"超过 {config.MAX_FILL_SLIP_PCT:.3%}，平掉并记账", inst=inst, slip=slip)
            closed = self.close({"instId": inst, "side": d, "posSide": self._pos_side(d)}, "成交滑点过大")
            if not closed:
                self.j.log(AGENT, "slip_still_open", f"{inst} 滑点平仓未完成，改按持仓接管", inst=inst)
            return {"ordId": ord_id, "fill_px": fill_px, "qty": filled,
                    "flattened": bool(closed), "close_reason": "滑点撤单"}
        sl, tp = bracket_from_fill(d, fill_px, plan["sl_pct"], plan["tp_pct"], spec["tickSz"])
        mark = self._mark(inst, fill_px)
        if not stop_is_valid(d, sl, mark):
            self.j.log(AGENT, "stop_invalid", f"{inst} 按成交价算出的止损 {sl} 在标记价 {mark} 错误一侧，平掉",
                       inst=inst, sl=sl, mark=mark)
            self.close({"instId": inst, "side": d, "posSide": self._pos_side(d)}, "止损无法放在正确一侧")
            return None
        plan["sl"], plan["tp"] = sl, tp
        self._replace_bracket(inst, d, sl, tp)
        self.ensure_stop(inst, d, sl, tp)
        self.j.log(AGENT, "opened", f"{inst} 开{('多' if d == 'long' else '空')} {filled}张 @ {fill_px} "
                   f"SL {sl} TP {tp}", inst=inst, ordId=ord_id, fill_px=fill_px, qty=filled,
                   sl=sl, tp=tp)
        return {"ordId": ord_id, "fill_px": fill_px, "qty": filled}

    def _mark(self, inst, fallback):
        try:
            px = float(self.c.mark_price(inst))
            return px if px > 0 else fallback
        except Exception:
            return fallback

    def _read_order(self, inst, ord_id):
        o = self.c.get_order(inst, ord_id)
        state = str(o.get("state") or "")
        sz = parse_fill_sz(o.get("accFillSz"))
        px = parse_fill_sz(o.get("avgPx"))
        if sz is not None and sz > 0 and px is not None and px > 0:
            return px, sz, state
        return None, 0.0 if sz is None else sz, state

    def _await_fill(self, inst, ord_id):
        """post-only 等到真实成交。字符串 \"0\" 不是成交。超时撤单，撤完再看一眼避免漏记部分成交。"""
        if not ord_id:
            return None, 0.0
        deadline = time.time() + max(0.0, float(config.MAKER_ENTRY_TIMEOUT_SEC))
        last_sz = 0.0
        while True:
            try:
                px, sz, state = self._read_order(inst, ord_id)
            except OkxError as e:
                self.j.log(AGENT, "order_query", f"{inst} 查询订单失败: {e}", inst=inst)
                px, sz, state = None, last_sz, ""
            last_sz = sz
            if sz and sz > 0 and px:
                return px, sz
            if state in ("canceled", "cancelled", "mmp_canceled"):
                break
            if self.c.is_mock or time.time() >= deadline:
                self.j.log(AGENT, "maker_timeout", f"{inst} post-only 未在 {config.MAKER_ENTRY_TIMEOUT_SEC:g}s 内成交，撤单",
                           inst=inst)
                try:
                    self.c.cancel_order(inst, ord_id)
                except Exception as e:
                    self.j.log(AGENT, "cancel_warn", f"{inst} 撤挂单失败: {e}", inst=inst)
                try:
                    px, sz, _state = self._read_order(inst, ord_id)
                except OkxError:
                    px, sz = None, 0.0
                if sz and sz > 0 and px:
                    return px, sz
                break
            time.sleep(config.ORDER_FILL_WAIT_SEC)
        live = self._still_open(inst)
        if live and float(live.get("qty") or 0) > 0 and float(live.get("avgPx") or 0) > 0:
            return float(live["avgPx"]), float(live["qty"])
        return None, last_sz or 0.0

    def _replace_bracket(self, inst, direction, sl, tp):
        try:
            ids = [a.get("algoId") for a in self.c.pending_algos(inst) if a.get("algoId")]
            if ids:
                self.c.cancel_algos(inst, ids)
        except OkxError as e:
            self.j.log(AGENT, "cancel_warn", f"{inst} 撤掉原止盈止损失败（继续重挂）: {e}", inst=inst)
        try:
            self.c.place_sl_algo(inst, "sell" if direction == "long" else "buy",
                                 self._pos_side(direction), sl, tp)
            self.j.log(AGENT, "bracket", f"{inst} 按成交价重挂止损 {sl} 止盈 {tp}", inst=inst, sl=sl, tp=tp)
            return True
        except OkxError as e:
            if e.code == "51088":
                return True
            self.j.log(AGENT, "bracket_fail", f"{inst} 重挂止盈止损失败: {e}", inst=inst, code=e.code)
            return False

    def protect_adopted(self, p):
        """接管或孤儿仓：一定补上止损和止盈。止损放不进正确一侧就市价平掉。"""
        inst, side = p["instId"], p.get("side") or "long"
        entry = float(p.get("avgPx") or 0) or float(p.get("markPx") or 0)
        mark = float(p.get("markPx") or 0) or self._mark(inst, entry)
        if entry <= 0 or mark <= 0:
            self.j.log(AGENT, "adopt_flat", f"{inst} 没有有效均价，无法保护，市价平", inst=inst)
            self.close(p, "接管仓缺少价格")
            return None
        spec = self.c.instrument(inst)
        sl_pct = config.MIN_SL_PCT
        tp_pct = sl_pct * config.RISK_REWARD
        sl, tp = bracket_from_fill(side, entry, sl_pct, tp_pct, spec["tickSz"])
        if not stop_is_valid(side, sl, mark):
            buf = max(float(spec["tickSz"]) * 2, mark * 0.002)
            sl = round_px(mark - buf if side == "long" else mark + buf, spec["tickSz"],
                          "down" if side == "long" else "up")
        if side == "long" and tp <= mark:
            tp = round_px(mark * (1 + tp_pct), spec["tickSz"], "up")
        if side == "short" and tp >= mark:
            tp = round_px(mark * (1 - tp_pct), spec["tickSz"], "down")
        if not stop_is_valid(side, sl, mark):
            self.j.log(AGENT, "adopt_bad_stop", f"{inst} 接管止损 {sl} 相对标记价 {mark} 无效，市价平",
                       inst=inst, sl=sl, mark=mark)
            self.close(p, "接管后无法挂出有效止损")
            return None
        self._replace_bracket(inst, side, sl, tp)
        self.ensure_stop(inst, side, sl, tp)
        self.j.log(AGENT, "adopt_protect", f"{inst} 接管仓已挂 SL {sl} TP {tp}", inst=inst, sl=sl, tp=tp)
        return sl, tp

    def audit_bracket(self, p, meta=None):
        """每个持仓都要同时有止损和止盈。缺了就按记录重挂，挂不出有效止损就平仓。"""
        inst = p["instId"]
        meta = meta or {}
        side = p.get("side") or meta.get("direction") or "long"
        try:
            algos = self.c.pending_algos(inst)
        except Exception as e:
            self.j.log(AGENT, "protect_check_err", f"{inst} 检查止盈止损失败: {e}", inst=inst)
            return False
        has_sl = any(_algo_set(a, "slTriggerPx") for a in algos)
        has_tp = any(_algo_set(a, "tpTriggerPx") for a in algos)
        if has_sl and has_tp:
            return True
        self.j.log(AGENT, "protect_missing", f"{inst} 保护单不完整 sl={has_sl} tp={has_tp}，补挂", inst=inst)
        mark = float(p.get("markPx") or 0) or self._mark(inst, float(p.get("avgPx") or meta.get("entry") or 0))
        sl, tp = meta.get("sl"), meta.get("tp")
        if sl and tp and stop_is_valid(side, sl, mark):
            self._replace_bracket(inst, side, sl, tp)
            self.ensure_stop(inst, side, sl, tp)
            return True
        protected = self.protect_adopted(p)
        return "closed" if protected is None else protected

    def ensure_stop(self, inst, d, sl, tp=None):
        try:
            algos = self.c.pending_algos(inst)
            if any(a.get("slTriggerPx") for a in algos):
                self.j.log(AGENT, "sl_ok", f"{inst} 交易所止损已在位", inst=inst)
                return True
            self.c.place_sl_algo(inst, "sell" if d == "long" else "buy", self._pos_side(d), sl, tp)
            self.j.log(AGENT, "sl_added", f"{inst} 附带止损缺失，已补挂 SL {sl}", inst=inst, sl=sl)
            return True
        except OkxError as e:
            if e.code == "51088":
                return True
            if e.code == "51023":
                self.j.log(AGENT, "gone", f"{inst} 仓位已不存在 (51023)", inst=inst)
                return True
            self.j.log(AGENT, "sl_fail", f"{inst} 无法挂止损 ({e})，为安全立即平仓", inst=inst, code=e.code)
            self.close({"instId": inst, "side": d, "posSide": self._pos_side(d)}, "止损挂单失败-紧急平仓")
            return False

    # ---------------- 改止损 ----------------
    def amend_sl(self, inst, new_sl):
        try:
            algos = [a for a in self.c.pending_algos(inst) if a.get("slTriggerPx")]
            if not algos:
                return False
            self.c.amend_sl(inst, algos[0]["algoId"], new_sl)
            self.j.log(AGENT, "sl_amended", f"{inst} 止损上移到 {new_sl}", inst=inst, sl=new_sl)
            return True
        except OkxError as e:
            if e.code in ("51302", "51304"):
                self.j.log(AGENT, "sl_reject", f"{inst} 新止损 {new_sl} 在标记价错误一侧 ({e.code})，本次不改",
                           inst=inst, code=e.code)
            elif e.code == "51023":
                self.j.log(AGENT, "gone", f"{inst} 仓位已不存在 (51023)", inst=inst)
            else:
                self.j.log(AGENT, "sl_amend_fail", f"{inst} 改止损失败: {e}", inst=inst, code=e.code)
            return False

    # ---------------- 平仓 ----------------
    def _still_open(self, inst):
        return next((p for p in self.c.positions() if p["instId"] == inst), None)

    def close(self, pos, reason):
        inst = pos["instId"]
        long = pos["side"] == "long"
        try:
            ids = [a["algoId"] for a in self.c.pending_algos(inst)]
            self.c.cancel_algos(inst, ids)
        except OkxError as e:
            self.j.log(AGENT, "cancel_warn", f"{inst} 撤算法单失败（继续平仓）: {e}", inst=inst)
        spec = self.c.instrument(inst)
        for i in range(config.CLOSE_IOC_TRIES):
            cur = self._still_open(inst)
            if not cur:
                break
            t = self.c.ticker(inst)
            px = t["bid"] if long else t["ask"]
            try:
                self.c.place_order({"instId": inst, "tdMode": config.TD_MODE, "side": "sell" if long else "buy",
                                    "posSide": pos.get("posSide", "net"), "ordType": "ioc", "px": str(px),
                                    "sz": fmt_num(cur["qty"], spec["lotSz"]), "reduceOnly": True})
                self.j.log(AGENT, "close_ioc", f"{inst} IOC#{i + 1} @ {px} ({reason})", inst=inst, px=px)
            except OkxError as e:
                if e.code == "51023":
                    break
                self.j.log(AGENT, "close_ioc_fail", f"{inst} IOC 失败: {e}", inst=inst, code=e.code)
            time.sleep(0 if self.c.is_mock else 0.5)
        if self._still_open(inst):
            try:
                self.c.close_position(inst, pos.get("posSide", "net"))
                self.j.log(AGENT, "close_market", f"{inst} IOC 未全部成交，市价 close-position 兜底", inst=inst)
            except OkxError as e:
                if e.code != "51023":
                    self.j.log(AGENT, "close_fail", f"{inst} 市价平仓失败: {e}", inst=inst, code=e.code)
                    return False
        self.j.log(AGENT, "closed", f"{inst} 已平仓：{reason}", inst=inst, reason=reason)
        return True


def offline_self_check():
    """字符串 \"0\" 不能当成已成交；0 张不能返回开仓结果。"""
    import tempfile
    from agents.journal import Journal

    assert parse_fill_sz("0") == 0.0
    assert parse_fill_sz(0) == 0.0
    assert parse_fill_sz("2.5") == 2.5
    assert parse_fill_sz("") is None
    assert parse_fill_sz(None) is None
    assert ("0" or 3) == "0"  # 这就是旧 bug：非空字符串会盖住真正的张数

    class _C:
        is_mock = True

        def instrument(self, inst):
            return {"ctVal": 0.01, "lotSz": "0.01", "minSz": 0.01, "tickSz": "0.1", "maxLever": 20}

        def set_leverage(self, *a, **k):
            return None

        def ticker(self, inst):
            return {"last": 100.0, "bid": 99.99, "ask": 100.01}

        def mark_price(self, inst):
            return 100.0

        def pos_mode(self):
            return "net_mode"

        def place_order(self, body):
            self.last_type = body.get("ordType")
            return {"ordId": "oid-1", "sCode": "0"}

        def get_order(self, inst, oid):
            return {"state": "live", "avgPx": "", "accFillSz": "0"}

        def cancel_order(self, inst, oid):
            self.cancelled = True
            return {}

        def positions(self):
            return []

        def pending_algos(self, inst):
            return []

        def cancel_algos(self, inst, ids):
            return []

        def place_sl_algo(self, *a, **k):
            raise AssertionError("0 成交不应去挂止损")

    j = Journal(tempfile.mkdtemp(), echo=False)
    try:
        client = _C()
        ex = Executor(client, j)
        ex.long_short = False
        plan = {"instId": "AMD-USDT-SWAP", "direction": "long", "qty": 1.0, "lever": 5,
                "entry_ref": 99.99, "signal_px": 100.0, "sl": 99.0, "tp": 101.0,
                "sl_pct": 0.01, "tp_pct": 0.01, "score": 7}
        assert ex.open(plan) is None
        assert client.cancelled and client.last_type == "post_only"

        class _Slip(_C):
            def get_order(self, inst, oid):
                return {"state": "filled", "avgPx": "102", "accFillSz": "1"}

        slip = _Slip()
        ex2 = Executor(slip, j)
        ex2.long_short = False
        ex2.close = lambda *a, **k: True
        got = ex2.open(dict(plan))
        assert got and got.get("flattened") and got["qty"] == 1 and got["close_reason"] == "滑点撤单"
    finally:
        j.close()
