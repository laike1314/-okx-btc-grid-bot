"""Executor 代理：开仓（附交易所止损/止盈）、改止损、撤单、平仓。

- 开仓：市价单 + attachAlgoOrds（SL 用标记价格触发，TP 用最新价触发）
- 开仓后按成交价重挂保护单：双边用 oco。单向持仓的 conditional 同时带止盈和止损时，
  OKX 只留下止损并返回成功，所以补挂后要回读 tpTriggerPx，空的不算挂上。
- 止损挂不出 -> 立即平仓（裸仓不留）。止盈反复挂不上则拉长重试间隔，不每分钟刷日志。
- 平仓：先撤算法单，IOC 限价吃对手价，最多 N 次；剩余用 close-position 市价兜底
- 已知错误：51023 仓位不存在（视为已平），51304 止损价已越过标记价（不改，记录），
            51088 仅在回读确认止盈已在时视为成功，限流在 okx_client 里自动退避重试
"""
import time
import config
from okx_client import OkxError, fmt_num
from .position import bracket_from_fill, round_px, stop_is_valid
from .protection import (
    ack_error, ack_is_gone, coverage_problem, describe_algo, describe_order,
    is_full_protection, leg_problem, tighter_sl, trigger_set,
)
from .risk import chase_blocked

AGENT = "executor"
_TP_RETRY_BASE = 120
_TP_RETRY_CAP = 1800


def _err_text(e):
    """把 OKX 的 sCode / sMsg 写进一行，方便从 Actions 日志里直接看到。"""
    if not isinstance(e, OkxError):
        return str(e)
    smsg = (e.smsg or "").strip()
    if smsg:
        return f"sCode={e.code} sMsg={smsg}"
    return f"sCode={e.code} sMsg={e}"


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
        self._protect_after = {}
        self._protect_wait = {}
        self._last_protect_err = {}
        self._unknown_sig = {}
        self.residual_ord_id = None
        self.last_protect_sl = None

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
        if config.is_swing():
            atr_px = float(plan.get("atr") or 0)
            if atr_px > 0 and sig > 0 and config.SWING_CHASE_ATR > 0:
                room = config.SWING_CHASE_ATR * atr_px
                chased = (d == "long" and px > sig + room) or (d == "short" and px < sig - room)
                if chased:
                    self.j.log(AGENT, "no_chase", f"{inst} 下单前价格已偏离信号 {sig} 超过 "
                               f"{config.SWING_CHASE_ATR:g} ATR，不追", inst=inst)
                    return None
        elif chase_blocked(d, sig, t["bid"], t["ask"], config.CHASE_LIMIT_PCT):
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
        if plan.get("tag"):
            body["tag"] = str(plan["tag"])[:16]
        if not self._margin_allows(inst, plan):
            return None
        if config.is_swing():
            self.residual_ord_id = None
            try:
                resting = list(self.c.open_orders(inst) or [])
            except Exception as e:
                self.j.log(AGENT, "orders_read_fail",
                           f"{inst} 开仓前回读普通挂单失败，本轮不下新单: {_err_text(e)}",
                           inst=inst)
                return {"rejected": "orders_unread"}
            for order in resting:
                if str(order.get("reduceOnly") or "").lower() in ("true", "1", "yes"):
                    continue
                if str(order.get("state") or "live") not in ("live", "partially_filled", ""):
                    continue
                self.residual_ord_id = order.get("ordId")
                self.j.log(AGENT, "residual_live",
                           f"{inst} 已有开仓残单 {self.residual_ord_id}，不再开新仓",
                           inst=inst, ordId=self.residual_ord_id)
                return None
        try:
            r = self.c.place_order(body)
        except OkxError as e:
            if e.code == "51088":
                self.j.log(AGENT, "order_51088", f"{inst} TP/SL 已存在，视为成功", inst=inst)
            elif str(e.code) == "51008":
                # 业务拒绝，不是网络空响应。这里不再 POST，交给调用方冷却并缩小下次张数。
                self.j.log(AGENT, "margin_reject",
                           f"{inst} 51008 保证金不足，本单 POST 不重试。{e}",
                           inst=inst, code=e.code)
                return {"rejected": "51008", "instId": inst}
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
        if config.is_swing():
            # 波段以回读到的止盈、止损和覆盖为准。只看到止损不算保护完成。
            status = self.confirm_protection(
                inst, d, sl, tp, filled, self._pos_side(d), reason="open")
            self.j.log(AGENT, "opened", f"{inst} 开{('多' if d == 'long' else '空')} {filled}张 @ {fill_px} "
                       f"SL {sl} TP {tp}", inst=inst, ordId=ord_id, fill_px=fill_px, qty=filled,
                       sl=sl, tp=tp)
            if status == "closed":
                return {"ordId": ord_id, "fill_px": fill_px, "qty": filled, "flattened": True,
                        "close_reason": "止损挂单失败-紧急平仓"}
            return {"ordId": ord_id, "fill_px": fill_px, "qty": filled}
        self._replace_bracket(inst, d, sl, tp)
        self.ensure_stop(inst, d, sl, tp)
        self.j.log(AGENT, "opened", f"{inst} 开{('多' if d == 'long' else '空')} {filled}张 @ {fill_px} "
                   f"SL {sl} TP {tp}", inst=inst, ordId=ord_id, fill_px=fill_px, qty=filled,
                   sl=sl, tp=tp)
        return {"ordId": ord_id, "fill_px": fill_px, "qty": filled}

    def _margin_allows(self, inst, plan):
        """下单前记下全账户，保证金明显不够就不发 POST。"""
        view = None
        try:
            from agents.margin import log_account_snapshot, use_frac
            view = log_account_snapshot(self.j, self.c, "pre_order", inst=inst,
                                        margin=plan.get("margin"), notional=plan.get("notional"))
        except Exception as e:
            self.j.log(AGENT, "margin_snapshot_err", f"{inst} 下单前账户明细失败: {e}", inst=inst)
            return True
        if not isinstance(view, dict) or view.get("available") is None or plan.get("margin") is None:
            return True
        try:
            avail = float(view["available"])
            need = float(plan["margin"])
        except (TypeError, ValueError):
            return True
        limit = avail * use_frac()
        if avail <= 0 or need > limit + 0.05:
            self.j.log(AGENT, "margin_skip",
                       f"{inst} 计划保证金 {need:.2f} 超过可用 {avail:.2f} 的 {use_frac():.0%}，不发 POST",
                       inst=inst, margin=need, available=avail)
            return False
        return True

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
        """post-only 等到真实成交。字符串 \"0\" 不是成交。

        剥头皮保持原行为：第一次看到成交就返回，不等剩余张数。
        波段要等到全部成交，或到 maker 超时后撤掉残单。撤单完成后再读一次
        accFillSz，把撤单竞态里的成交算进去。撤不掉就记下残单，不另开反向仓。
        """
        if not ord_id:
            return None, 0.0
        self.residual_ord_id = None
        deadline = time.time() + max(0.0, float(config.MAKER_ENTRY_TIMEOUT_SEC))
        last_px, last_sz, last_state = None, 0.0, ""
        while True:
            try:
                px, sz, state = self._read_order(inst, ord_id)
            except OkxError as e:
                self.j.log(AGENT, "order_query", f"{inst} 查询订单失败: {e}", inst=inst)
                px, sz, state = None, last_sz, last_state
            if sz and sz > 0:
                last_sz = sz
                if px:
                    last_px = px
            if state:
                last_state = state
            if last_state == "filled" and last_sz > 0 and last_px:
                return last_px, last_sz
            if not config.is_swing() and last_sz > 0 and last_px:
                return last_px, last_sz
            if last_state in ("canceled", "cancelled", "mmp_canceled"):
                break
            if self.c.is_mock or time.time() >= deadline:
                self.j.log(AGENT, "maker_timeout",
                           f"{inst} post-only 未在 {config.MAKER_ENTRY_TIMEOUT_SEC:g}s 内成交，撤单",
                           inst=inst)
                cancel_ok = True
                read_failed = False
                try:
                    ack = self.c.cancel_order(inst, ord_id)
                    bad = ack_error(ack)
                    if bad and not ack_is_gone(ack):
                        cancel_ok = False
                        self.j.log(AGENT, "cancel_warn",
                                   f"{inst} 撤挂单未成功 sCode={bad[0]} sMsg={bad[1]}",
                                   inst=inst, code=bad[0], sMsg=bad[1])
                except Exception as e:
                    cancel_ok = False
                    self.j.log(AGENT, "cancel_warn", f"{inst} 撤挂单失败: {_err_text(e)}",
                               inst=inst, code=getattr(e, "code", ""), sMsg=getattr(e, "smsg", ""))
                try:
                    px, sz, state = self._read_order(inst, ord_id)
                except OkxError as e:
                    read_failed = True
                    px, sz, state = None, last_sz, last_state
                    self.j.log(AGENT, "order_query",
                               f"{inst} 撤单后回读失败，不能当成已经撤掉: {_err_text(e)}",
                               inst=inst, code=e.code, sMsg=e.smsg)
                if sz and sz > 0:
                    last_sz = sz
                    if px:
                        last_px = px
                if state:
                    last_state = state
                terminal = last_state in ("canceled", "cancelled", "mmp_canceled", "filled")
                if config.is_swing() and (not cancel_ok or read_failed or not terminal):
                    self.residual_ord_id = ord_id
                    self.j.log(AGENT, "residual_live",
                               f"{inst} 开仓残单 {ord_id} 仍可能留在场上"
                               f"（撤单={'成功' if cancel_ok else '失败'}，"
                               f"回读={'失败' if read_failed else (last_state or '未知')}）。"
                               f"不另下新仓，下一轮继续撤并按持仓改账本",
                               inst=inst, ordId=ord_id, qty=last_sz, state=last_state or "")
                break
            time.sleep(config.ORDER_FILL_WAIT_SEC)
        if last_sz > 0 and last_px:
            return last_px, last_sz
        live = self._still_open(inst)
        if live and float(live.get("qty") or 0) > 0 and float(live.get("avgPx") or 0) > 0:
            return float(live["avgPx"]), float(live["qty"])
        return None, last_sz or 0.0

    def _legs(self, inst):
        """(has_sl, has_tp) 或查询失败时 (None, OkxError)。"""
        try:
            algos = self.c.pending_algos(inst) or []
        except OkxError as e:
            return None, e
        except Exception as e:
            return None, OkxError("NET", str(e))
        has_sl = any(_algo_set(a, "slTriggerPx") for a in algos)
        has_tp = any(_algo_set(a, "tpTriggerPx") for a in algos)
        return (has_sl, has_tp), None

    def _cancel_pending(self, inst):
        try:
            ids = [a.get("algoId") for a in (self.c.pending_algos(inst) or []) if a.get("algoId")]
            if ids:
                self.c.cancel_algos(inst, ids)
        except OkxError as e:
            self.j.log(AGENT, "cancel_warn", f"{inst} 撤掉原止盈止损失败（继续重挂）: {_err_text(e)}",
                       inst=inst, code=e.code, sMsg=e.smsg)

    def _ensure_sl_only(self, inst, direction, sl):
        """止盈没挂上时，把已经撤掉的止损补回去。已有止损就不动。"""
        legs, err = self._legs(inst)
        if err:
            self.j.log(AGENT, "sl_kept_err", f"{inst} 止盈失败后回读止损失败: {_err_text(err)}",
                       inst=inst, code=err.code, sMsg=err.smsg)
            return False
        if legs[0]:
            return True
        try:
            self.c.place_sl_algo(inst, "sell" if direction == "long" else "buy",
                                 self._pos_side(direction), sl, None)
            self.j.log(AGENT, "sl_kept", f"{inst} 止盈未挂上，已单独补上止损 {sl}", inst=inst, sl=sl)
            return True
        except OkxError as e:
            if e.code == "51088":
                return True
            self.j.log(AGENT, "sl_fail", f"{inst} 止盈失败后补止损也失败 {_err_text(e)}",
                       inst=inst, code=e.code, sMsg=e.smsg)
            return False

    def _remember_protect_err(self, inst, text):
        self._last_protect_err[inst] = text

    def _replace_bracket(self, inst, direction, sl, tp):
        """撤掉旧保护单再挂。两边都有时交易所必须是 oco，挂完要回读到止盈价。"""
        self._cancel_pending(inst)
        close_side = "sell" if direction == "long" else "buy"
        pos_side = self._pos_side(direction)
        if not tp:
            try:
                self.c.place_sl_algo(inst, close_side, pos_side, sl, None)
                self.j.log(AGENT, "bracket", f"{inst} 按成交价重挂止损 {sl}", inst=inst, sl=sl)
                return True
            except OkxError as e:
                if e.code == "51088":
                    return True
                text = _err_text(e)
                self._remember_protect_err(inst, text)
                self.j.log(AGENT, "bracket_fail", f"{inst} 重挂止损失败 {text}",
                           inst=inst, code=e.code, sMsg=e.smsg)
                return False
        try:
            self.c.place_sl_algo(inst, close_side, pos_side, sl, tp)
        except OkxError as e:
            if e.code == "51088":
                legs, _err = self._legs(inst)
                if legs and legs[1]:
                    return True
            text = _err_text(e)
            self._remember_protect_err(inst, text)
            self.j.log(AGENT, "bracket_fail", f"{inst} 重挂止盈止损失败 {text}",
                       inst=inst, code=e.code, sMsg=e.smsg)
            self._ensure_sl_only(inst, direction, sl)
            return False
        legs, err = self._legs(inst)
        if err:
            text = f"已提交 oco，回读失败 {_err_text(err)}"
            self._remember_protect_err(inst, text)
            self.j.log(AGENT, "tp_missing", f"{inst} {text}", inst=inst, code=err.code, sMsg=err.smsg)
            return False
        if legs[1]:
            self.j.log(AGENT, "bracket", f"{inst} 按成交价重挂止损 {sl} 止盈 {tp}（oco 全平）",
                       inst=inst, sl=sl, tp=tp)
            return True
        text = "OKX 返回成功但挂单没有止盈价 tpTriggerPx"
        self._remember_protect_err(inst, text)
        self.j.log(AGENT, "tp_missing",
                   f"{inst} {text}（本单已按 oco 全平提交，回读 sl={legs[0]} tp={legs[1]}）",
                   inst=inst, sl=sl, tp=tp)
        self._ensure_sl_only(inst, direction, sl)
        return False

    def _backoff_active(self, inst):
        return time.time() < self._protect_after.get(inst, 0)

    def _schedule_backoff(self, inst):
        wait = self._protect_wait.get(inst, _TP_RETRY_BASE)
        self._protect_after[inst] = time.time() + wait
        self._protect_wait[inst] = min(max(wait * 2, _TP_RETRY_BASE), _TP_RETRY_CAP)
        err = self._last_protect_err.get(inst) or ""
        self.j.log(AGENT, "protect_backoff",
                   f"{inst} 止盈仍未挂上（{err}），{wait}s 内不再重复补挂。"
                   f"价格穿过止盈且没有止盈单时会立刻市价平，不受最短持仓限制",
                   inst=inst, wait=wait)
        return wait

    def _clear_backoff(self, inst):
        self._protect_after.pop(inst, None)
        self._protect_wait.pop(inst, None)
        self._last_protect_err.pop(inst, None)

    def protect_adopted(self, p, meta=None):
        """接管或孤儿仓：一定补上止损和止盈。止损放不进正确一侧就市价平掉。"""
        if config.is_swing():
            return self._protect_swing(p, meta or {})
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
        ok = self._replace_bracket(inst, side, sl, tp)
        self.ensure_stop(inst, side, sl, None if not ok else tp)
        if ok:
            self.j.log(AGENT, "adopt_protect", f"{inst} 接管仓已挂 SL {sl} TP {tp}", inst=inst, sl=sl, tp=tp)
        else:
            self.j.log(AGENT, "adopt_protect", f"{inst} 接管仓止盈未挂上，止损 {sl}，目标止盈仍是 {tp}",
                       inst=inst, sl=sl, tp=tp)
        return sl, tp

    def _protect_swing(self, p, meta):
        """波段接管：交易所已经有有效止损和止盈就沿用，不改成剥头皮的窄止损。缺了再按家族 ATR 补。"""
        from .swing import profile_for, candle_features
        inst, side = p["instId"], p.get("side") or meta.get("direction") or "long"
        entry = float(p.get("avgPx") or 0) or float(meta.get("entry") or 0) or float(p.get("markPx") or 0)
        mark = float(p.get("markPx") or 0) or self._mark(inst, entry)
        if entry <= 0 or mark <= 0:
            self.j.log(AGENT, "adopt_flat", f"{inst} 没有有效均价，无法保护，市价平", inst=inst)
            self.close(p, "接管仓缺少价格")
            return None
        try:
            algos = self.c.pending_algos(inst)
        except Exception as e:
            self.j.log(AGENT, "protect_read_fail",
                       f"{inst} 回读算法单失败，不能当成没有保护，本次不撤不补: {e}", inst=inst)
            return "unconfirmed"
        sl = tp = None
        for algo in algos or []:
            if _algo_set(algo, "slTriggerPx"):
                sl = float(algo.get("slTriggerPx"))
            if _algo_set(algo, "tpTriggerPx"):
                tp = float(algo.get("tpTriggerPx"))
        if sl and tp and stop_is_valid(side, sl, mark):
            if meta is not None:
                meta["sl"], meta["tp"] = sl, tp
            status = self.confirm_protection(
                inst, side, sl, tp, p.get("qty"), p.get("posSide"), reason="startup")
            if status == "closed":
                return None
            if status == "unconfirmed":
                return "unconfirmed"
            if status != "ok":
                self.j.log(AGENT, "adopt_protect",
                           f"{inst} 交易所已有 SL {sl} TP {tp}，但覆盖或止盈回读未通过",
                           inst=inst, sl=sl, tp=tp)
                return False
            self.j.log(AGENT, "adopt_protect", f"{inst} 沿用交易所 SL {sl} TP {tp}", inst=inst, sl=sl, tp=tp)
            return sl, tp
        prof = profile_for(meta.get("family") or "unknown")
        atr_px = float(meta.get("atr") or 0)
        try:
            bars = self.c.candles(inst, config.SWING_REGIME_BAR, 120)
            feat = candle_features(bars)
        except Exception as e:
            feat = None
            self.j.log(AGENT, "adopt_atr_err", f"{inst} 补保护单时 K 线失败: {e}", inst=inst)
        if feat:
            atr_px = float(feat["atr"])
            if meta is not None:
                meta["atr"] = atr_px
                meta["atr_pct"] = feat["atr_pct"]
        spec = self.c.instrument(inst)
        if atr_px > 0 and entry > 0:
            sl_pct = atr_px / entry * float(prof["sl_atr"])
            tp_pct = atr_px / entry * float(prof["tp_atr"])
        else:
            sl_pct = max(config.MIN_SL_PCT, 0.01) * float(prof["sl_atr"])
            tp_pct = max(config.MIN_SL_PCT, 0.01) * float(prof["tp_atr"])
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
        if meta is not None:
            meta["sl"], meta["tp"] = sl, tp
        status = self.confirm_protection(
            inst, side, sl, tp, p.get("qty"), p.get("posSide"), reason="startup")
        if status == "closed":
            return None
        if status == "unconfirmed":
            return "unconfirmed"
        if status != "ok":
            self.j.log(AGENT, "adopt_protect",
                       f"{inst} 波段接管止盈或覆盖未确认，止损目标 {sl}，止盈目标 {tp}",
                       inst=inst, sl=sl, tp=tp)
            return False
        self.j.log(AGENT, "adopt_protect", f"{inst} 波段接管已挂 SL {sl} TP {tp}", inst=inst, sl=sl, tp=tp)
        return sl, tp

    def _audit_swing(self, p, meta):
        """波段巡检：回读止盈、止损和覆盖。读失败只跳过本轮，不补第二张单。"""
        inst = p["instId"]
        meta = meta or {}
        side = p.get("side") or meta.get("direction") or "long"
        sl, tp = meta.get("sl"), meta.get("tp")
        if self._backoff_active(inst):
            status = self.confirm_protection(
                inst, side, sl, tp, p.get("qty"), p.get("posSide"), reason="audit", repair=False)
            if status == "ok":
                self._clear_backoff(inst)
                return True
            return False
        if not sl or not tp:
            protected = self.protect_adopted(p, meta)
            if protected is None:
                return "closed"
            if protected == "unconfirmed":
                return False
            if protected is False:
                self._schedule_backoff(inst)
                return False
            if isinstance(protected, tuple) and len(protected) == 2:
                meta["sl"], meta["tp"] = protected
                self._clear_backoff(inst)
                return True
            return False
        status = self.confirm_protection(
            inst, side, sl, tp, p.get("qty"), p.get("posSide"), reason="audit")
        if status == "ok":
            self._clear_backoff(inst)
            return True
        if status == "closed":
            return "closed"
        if status == "unconfirmed":
            return False
        self._schedule_backoff(inst)
        return False

    def audit_bracket(self, p, meta=None):
        """每个持仓都要同时有止损和止盈。缺了就按记录重挂，挂不出有效止损就平仓。

        波段还核对覆盖张数。回读失败不当成「没有保护单」。
        止损还在、只是止盈反复失败时拉长重试间隔，避免每分钟撤掉有效止损再补一次。
        """
        if config.is_swing():
            return self._audit_swing(p, meta or {})
        return self._audit_scalp(p, meta)

    def _audit_scalp(self, p, meta=None):
        """剥头皮巡检，保持原有撤旧再挂的行为。

        止损还在、只是止盈反复失败时拉长重试间隔，避免每分钟撤掉有效止损再补一次。
        """
        inst = p["instId"]
        meta = meta or {}
        side = p.get("side") or meta.get("direction") or "long"
        legs, err = self._legs(inst)
        if err:
            self.j.log(AGENT, "protect_check_err", f"{inst} 检查止盈止损失败: {_err_text(err)}",
                       inst=inst, code=err.code, sMsg=err.smsg)
            return False
        has_sl, has_tp = legs
        if has_sl and has_tp:
            self._clear_backoff(inst)
            return True
        if has_sl and not has_tp and self._backoff_active(inst):
            return False
        self.j.log(AGENT, "protect_missing", f"{inst} 保护单不完整 sl={has_sl} tp={has_tp}，补挂", inst=inst)
        mark = float(p.get("markPx") or 0) or self._mark(inst, float(p.get("avgPx") or meta.get("entry") or 0))
        sl, tp = meta.get("sl"), meta.get("tp")
        if sl and tp and stop_is_valid(side, sl, mark):
            ok = self._replace_bracket(inst, side, sl, tp)
            if ok:
                self._clear_backoff(inst)
                self.ensure_stop(inst, side, sl, tp)
                return True
            self._schedule_backoff(inst)
            legs2, _err2 = self._legs(inst)
            if not legs2 or not legs2[0]:
                self.ensure_stop(inst, side, sl, None)
            return False
        protected = self.protect_adopted(p, meta)
        if protected is None:
            return "closed"
        legs2, _err2 = self._legs(inst)
        if legs2 and legs2[0] and legs2[1]:
            self._clear_backoff(inst)
        elif legs2 and legs2[0] and not legs2[1]:
            self._schedule_backoff(inst)
        return protected

    def _read_algos(self, inst):
        try:
            return list(self.c.pending_algos(inst) or []), None
        except OkxError as e:
            return None, e
        except Exception as e:
            return None, OkxError("NET", str(e))

    def _read_orders(self, inst):
        fn = getattr(self.c, "open_orders", None)
        if not fn:
            return None, OkxError("NOMETHOD", "客户端没有 open_orders")
        try:
            return list(fn(inst) or []), None
        except OkxError as e:
            return None, e
        except Exception as e:
            return None, OkxError("NET", str(e))

    def _log_protection_book(self, inst, pos_side, algos, orders, algo_err, order_err, reason):
        """回读日志把算法单和普通挂单分开写，字段固定带上，空的也留着。"""
        if algo_err:
            self.j.log(AGENT, "protect_read_fail",
                       f"{inst} 回读算法单失败，不能当成保护单不存在，本次不撤不补。"
                       f"原因={reason} {_err_text(algo_err)}",
                       inst=inst, code=algo_err.code, sMsg=algo_err.smsg, reason=reason)
        elif not algos:
            self.j.log(AGENT, "protect_read", f"{inst} 算法单 无。原因={reason}",
                       inst=inst, reason=reason, posSide=pos_side)
        else:
            for algo in algos:
                self.j.log(AGENT, "protect_read",
                           f"{inst} {describe_algo(algo, inst, pos_side)} 原因={reason}",
                           inst=inst, reason=reason, algoId=algo.get("algoId"),
                           ordType=algo.get("ordType"), posSide=algo.get("posSide") or pos_side,
                           tpTriggerPx=algo.get("tpTriggerPx") or "",
                           slTriggerPx=algo.get("slTriggerPx") or "",
                           closeFraction=algo.get("closeFraction") or "",
                           sz=algo.get("sz") or "")
        if order_err:
            self.j.log(AGENT, "orders_read_fail",
                       f"{inst} 回读普通挂单失败，不能当成没有残单。{_err_text(order_err)}",
                       inst=inst, code=order_err.code, sMsg=order_err.smsg)
        elif orders:
            for order in orders:
                self.j.log(AGENT, "open_order", f"{inst} {describe_order(order, inst)}",
                           inst=inst, ordId=order.get("ordId"), ordType=order.get("ordType"))
        elif orders is not None and reason in ("open", "startup", "amend", "qty_sync"):
            self.j.log(AGENT, "open_order", f"{inst} 普通挂单 无", inst=inst)

    def _cancel_algo_ids(self, inst, ids):
        ids = [i for i in ids if i]
        if not ids:
            return True
        try:
            ack = self.c.cancel_algos(inst, ids)
        except OkxError as e:
            self.j.log(AGENT, "protect_cancel_fail",
                       f"{inst} 撤销旧算法单失败 {_err_text(e)}。合格保护还在，本轮不补第三张，下轮只重试撤销",
                       inst=inst, code=e.code, sMsg=e.smsg, algoIds=",".join(str(i) for i in ids))
            return False
        if ack_is_gone(ack):
            return True
        bad = ack_error(ack)
        if bad:
            self.j.log(AGENT, "protect_cancel_fail",
                       f"{inst} 撤销旧算法单未成功 sCode={bad[0]} sMsg={bad[1]}。"
                       f"两张全平条件单可能先后触发，下轮只重试撤销、不再补挂",
                       inst=inst, code=bad[0], sMsg=bad[1], algoIds=",".join(str(i) for i in ids))
            return False
        return True

    def _place_protection(self, inst, direction, sl, tp, pos_side):
        try:
            self.c.place_sl_algo(inst, "sell" if direction == "long" else "buy", pos_side, sl, tp)
            return True, None
        except OkxError as e:
            return False, e

    def _finish_good(self, inst, pos_side, algos, goods, old_ids, reason):
        keeper = goods[0]
        keep_id = keeper.get("algoId")
        extras = [a.get("algoId") for a in algos if a.get("algoId") and a.get("algoId") != keep_id]
        extras += [i for i in old_ids if i and i != keep_id and i not in extras]
        if extras and not self._cancel_algo_ids(inst, extras):
            self.j.log(AGENT, "protect_duplicate",
                       f"{inst} 合格保护 algoId={keep_id} 已在，重复条件单没撤干净，不再补挂",
                       inst=inst, algoId=keep_id)
        self._clear_backoff(inst)
        live_sl = trigger_set(keeper, "slTriggerPx") and keeper.get("slTriggerPx")
        if live_sl:
            try:
                self.last_protect_sl = float(live_sl)
            except (TypeError, ValueError):
                self.last_protect_sl = None
        self.j.log(AGENT, "protect_ok",
                   f"{inst} {describe_algo(keeper, inst, pos_side)} 止盈腿、止损腿和覆盖已确认。原因={reason}",
                   inst=inst, reason=reason, algoId=keep_id,
                   ordType=keeper.get("ordType") or "",
                   posSide=keeper.get("posSide") or pos_side,
                   tpTriggerPx=keeper.get("tpTriggerPx") or "",
                   slTriggerPx=keeper.get("slTriggerPx") or "",
                   closeFraction=keeper.get("closeFraction") or "",
                   sz=keeper.get("sz") or "")
        return "ok"

    def _restore_sl_or_flatten(self, inst, direction, sl, pos_side, algos):
        if any(trigger_set(a, "slTriggerPx") for a in algos or []):
            return "mismatch"
        if self._ensure_sl_only(inst, direction, sl):
            return "mismatch"
        self.j.log(AGENT, "sl_fail", f"{inst} 止损也补不上，立即平仓", inst=inst)
        self.close({"instId": inst, "side": direction, "posSide": pos_side}, "止损挂单失败-紧急平仓")
        return "closed"

    def confirm_protection(self, inst, direction, sl, tp, pos_qty=None, pos_side=None,
                           reason="audit", repair=True):
        """回读算法单。合格才算保护完成；读失败不等于没有。

        先挂新的全平 OCO，回读确认后再撤旧单，避免撤完补不上留下裸仓。
        交易所报已存在时再读一次；仍然不合格才撤掉旧单补一次。
        """
        pos_side = pos_side or self._pos_side(direction)
        need_tp = bool(tp)
        algos, algo_err = self._read_algos(inst)
        orders, order_err = self._read_orders(inst)
        if reason in ("open", "startup", "amend", "qty_sync") or algo_err or order_err:
            self._log_protection_book(inst, pos_side, algos, orders, algo_err, order_err, reason)
        if algo_err:
            return "unconfirmed"
        goods = [a for a in algos
                 if is_full_protection(a, pos_qty, need_tp, direction, pos_side)]
        if goods:
            kept = tighter_sl(direction, sl, goods[0].get("slTriggerPx"))
            if kept and sl and abs(float(kept) - float(sl)) > max(1e-8, abs(float(kept)) * 1e-8):
                self.j.log(AGENT, "sl_keep_tighter",
                           f"{inst} 交易所止损 {goods[0].get('slTriggerPx')} 比账本 {sl} 更紧，"
                           f"重启不把止损放回更差的价格",
                           inst=inst, sl=kept, book_sl=sl)
            return self._finish_good(inst, pos_side, algos, goods, [], reason)
        problems = []
        for algo in algos:
            if not is_full_protection(algo, pos_qty, need_tp, direction, pos_side):
                problems.append(leg_problem(algo, need_tp) or coverage_problem(algo, pos_qty) or "inactive")
        unknown_only = bool(algos) and all(p in ("unknown", "qty_unknown") for p in problems)
        if unknown_only:
            ocos = [a for a in algos if str(a.get("ordType") or "") == "oco" and not leg_problem(a, need_tp)]
            # 单向 conditional 即使回读里带着止盈价，交易所也只执行止损。开仓/对账时换成 oco 一次。
            # 已经有 oco 但覆盖字段读不到时，不再补第二张，只尝试撤掉旁边的旧单。
            if ocos or reason not in ("open", "startup", "qty_sync"):
                if ocos:
                    keeper = ocos[0].get("algoId")
                    extras = [a.get("algoId") for a in algos if a.get("algoId") and a.get("algoId") != keeper]
                    self._cancel_algo_ids(inst, extras)
                sig = tuple(sorted(str(a.get("algoId") or "") for a in algos))
                if self._unknown_sig.get(inst) != sig:
                    self._unknown_sig[inst] = sig
                    self._log_protection_book(inst, pos_side, algos, orders, None, order_err, reason)
                    self.j.log(AGENT, "protect_coverage_unknown",
                               f"{inst} 止盈和止损触发价在，但没有 closeFraction 或 sz，"
                               f"不能确认覆盖张数。不撤掉这张 oco，也不再补第二张",
                               inst=inst, reason=reason)
                if trigger_set((ocos or algos)[0], "slTriggerPx"):
                    try:
                        self.last_protect_sl = float((ocos or algos)[0].get("slTriggerPx"))
                    except (TypeError, ValueError):
                        pass
                return "ok"
        if not repair:
            return "mismatch"
        if not sl:
            self.j.log(AGENT, "protect_missing", f"{inst} 没有可用止损价，无法补保护", inst=inst)
            return "mismatch"
        better = tighter_sl(direction, sl, *[a.get("slTriggerPx") for a in algos])
        if better and sl and abs(float(better) - float(sl)) > max(1e-8, abs(float(better)) * 1e-8):
            self.j.log(AGENT, "sl_keep_tighter",
                       f"{inst} 补保护使用更紧的止损 {better}，不用账本里更差的 {sl}",
                       inst=inst, sl=better, book_sl=sl)
            sl = better
        self.j.log(AGENT, "protect_missing",
                   f"{inst} 保护缺失或覆盖不够（{','.join(problems) or 'none'}），"
                   f"补 closeFraction=1 的{'双边 oco' if tp else '止损'}",
                   inst=inst, reason=reason, sl=sl, tp=tp, qty=pos_qty)
        old_ids = [a.get("algoId") for a in algos if a.get("algoId")]
        _placed, err = self._place_protection(inst, direction, sl, tp, pos_side)
        if err and str(err.code) == "51088":
            algos2, err2 = self._read_algos(inst)
            if err2:
                self.j.log(AGENT, "protect_read_fail",
                           f"{inst} sCode=51088 表示保护单已存在，但回读失败，不撤旧单。"
                           f"{_err_text(err2)}",
                           inst=inst, code=err2.code, sMsg=err2.smsg, reason=reason)
                return "unconfirmed"
            goods = [a for a in algos2
                     if is_full_protection(a, pos_qty, need_tp, direction, pos_side)]
            if goods:
                return self._finish_good(inst, pos_side, algos2, goods, old_ids, reason)
            self.j.log(AGENT, "protect_blocked",
                       f"{inst} sCode=51088 sMsg={err.smsg or err}，已有条件单但止盈或覆盖不合格，撤掉后补一次",
                       inst=inst, code="51088", sMsg=err.smsg, reason=reason)
            self._cancel_algo_ids(inst, old_ids)
            _placed, err = self._place_protection(inst, direction, sl, tp, pos_side)
        if err:
            self._remember_protect_err(inst, _err_text(err))
            self.j.log(AGENT, "bracket_fail", f"{inst} 补保护失败 {_err_text(err)}",
                       inst=inst, code=err.code, sMsg=err.smsg, reason=reason)
            return self._restore_sl_or_flatten(inst, direction, sl, pos_side, algos)
        algos2, err2 = self._read_algos(inst)
        if err2:
            self.j.log(AGENT, "protect_read_fail",
                       f"{inst} 保护单已提交，回读失败，不能当成没挂上，也不撤旧单。{_err_text(err2)}",
                       inst=inst, code=err2.code, sMsg=err2.smsg, reason=reason)
            return "unconfirmed"
        self._log_protection_book(inst, pos_side, algos2, None, None, None, reason)
        goods = [a for a in algos2
                 if is_full_protection(a, pos_qty, need_tp, direction, pos_side)]
        if not goods:
            leg_ok = [a for a in algos2 if not leg_problem(a, need_tp)]
            unknown = [a for a in leg_ok if coverage_problem(a, pos_qty) in ("unknown", "qty_unknown")]
            if unknown and len(unknown) == len(leg_ok):
                keeper = unknown[-1]
                self._cancel_algo_ids(
                    inst, [i for i in old_ids if i and i != keeper.get("algoId")])
                self._unknown_sig[inst] = (str(keeper.get("algoId") or ""),)
                self.j.log(AGENT, "protect_coverage_unknown",
                           f"{inst} {describe_algo(keeper, inst, pos_side)} "
                           f"触发价已回读，但没有 closeFraction 或 sz，不能确认覆盖。"
                           f"保留这一张，不再补第二张",
                           inst=inst, reason=reason, algoId=keeper.get("algoId"))
                return "ok"
            text = "回读仍没有同时具备止盈、止损和足够覆盖的保护单"
            self._remember_protect_err(inst, text)
            self.j.log(AGENT, "tp_missing", f"{inst} {text}", inst=inst, sl=sl, tp=tp, qty=pos_qty)
            return self._restore_sl_or_flatten(inst, direction, sl, pos_side, algos2)
        return self._finish_good(inst, pos_side, algos2, goods, old_ids, reason)

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
            self.j.log(AGENT, "sl_fail", f"{inst} 无法挂止损 ({_err_text(e)})，为安全立即平仓",
                       inst=inst, code=e.code, sMsg=e.smsg)
            self.close({"instId": inst, "side": d, "posSide": self._pos_side(d)}, "止损挂单失败-紧急平仓")
            return False

    # ---------------- 改止损 ----------------
    def amend_sl(self, inst, new_sl):
        try:
            algos = [a for a in self.c.pending_algos(inst) if a.get("slTriggerPx")]
            if not algos:
                return False
            self.c.amend_sl(inst, algos[0]["algoId"], new_sl)
        except OkxError as e:
            if e.code in ("51302", "51304"):
                self.j.log(AGENT, "sl_reject", f"{inst} 新止损 {new_sl} 在标记价错误一侧 ({e.code})，本次不改",
                           inst=inst, code=e.code)
            elif e.code == "51023":
                self.j.log(AGENT, "gone", f"{inst} 仓位已不存在 (51023)", inst=inst)
            else:
                self.j.log(AGENT, "sl_amend_fail", f"{inst} 改止损失败: {_err_text(e)}",
                           inst=inst, code=e.code, sMsg=e.smsg)
            return False
        if not config.is_swing():
            self.j.log(AGENT, "sl_amended", f"{inst} 止损上移到 {new_sl}", inst=inst, sl=new_sl)
            return True
        pos = None
        try:
            pos = self._still_open(inst)
        except Exception:
            pos = None
        direction = (pos or {}).get("side") or "long"
        tp = None
        for algo in algos:
            if trigger_set(algo, "tpTriggerPx"):
                tp = algo.get("tpTriggerPx")
                break
        status = self.confirm_protection(
            inst, direction, new_sl, tp, (pos or {}).get("qty"), (pos or {}).get("posSide"),
            reason="amend")
        if status == "unconfirmed":
            self.j.log(AGENT, "sl_amended",
                       f"{inst} 止损修改已提交 {new_sl}，回读未确认，不据此再补一单",
                       inst=inst, sl=new_sl)
            return True
        self.j.log(AGENT, "sl_amended", f"{inst} 止损上移到 {new_sl}，已回读算法单",
                   inst=inst, sl=new_sl)
        return status == "ok"

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
        _protective_order_checks()
    finally:
        j.close()


def _protective_order_checks():
    """双边保护用 oco；失败要记下 sCode/sMsg，并在冷却期内不再每分钟重挂。"""
    import tempfile
    from agents.journal import Journal
    from okx_client import OkxClient, OkxError, protective_algo_body

    body = protective_algo_body("ICP-USDT-SWAP", "buy", "net", 2.388, 2.196, "cross")
    assert body["ordType"] == "oco" and body["closeFraction"] == "1" and body["reduceOnly"] is True
    assert body["tpTriggerPx"] == "2.196" and body["slTriggerPx"] == "2.388"
    assert body["tpOrdPx"] == "-1" and body["slOrdPx"] == "-1" and "sz" not in body
    sl_only = protective_algo_body("ICP-USDT-SWAP", "buy", "net", 2.388, None, "cross")
    assert sl_only["ordType"] == "conditional" and "tpTriggerPx" not in sl_only

    sent = {}
    client = OkxClient.__new__(OkxClient)

    def request(method, path, params=None, body=None, private=False, retries=4, simulated=True):
        sent["body"] = body
        sent["path"] = path
        return [{"algoId": "1", "sCode": "0", "sMsg": ""}]

    client.request = request
    client.place_sl_algo("ICP-USDT-SWAP", "buy", "net", 2.388, 2.196)
    assert sent["path"] == "/api/v5/trade/order-algo"
    assert sent["body"]["ordType"] == "oco" and sent["body"]["tpTriggerPx"] == "2.196"
    client.place_sl_algo("ICP-USDT-SWAP", "buy", "net", 2.388, None)
    assert sent["body"]["ordType"] == "conditional" and "tpTriggerPx" not in sent["body"]

    class Book:
        def __init__(self):
            self.mode = "error"
            self.places = []
            self.algos = [{"algoId": "sl1", "ordType": "conditional",
                           "slTriggerPx": "2.388", "tpTriggerPx": ""}]

        def pending_algos(self, inst):
            return list(self.algos)

        def cancel_algos(self, inst, ids):
            return []

        def place_sl_algo(self, inst, side, pos_side, sl, tp=None):
            self.places.append(None if tp is None else float(tp))
            if tp and self.mode == "error":
                raise OkxError("51327", "TP trigger price error",
                               smsg="Take-profit trigger price should be lower than the last price")
            if tp and self.mode == "ok":
                self.algos = [{"algoId": "oco1", "ordType": "oco",
                               "slTriggerPx": str(sl), "tpTriggerPx": str(tp)}]
            return {"algoId": "x", "sCode": "0"}

    j = Journal(tempfile.mkdtemp(), echo=False)
    try:
        book = Book()
        ex = Executor(book, j)
        ex.long_short = False
        pos = {"instId": "ICP-USDT-SWAP", "side": "short", "markPx": 2.25,
               "avgPx": 2.273, "posSide": "net"}
        meta = {"direction": "short", "sl": 2.388, "tp": 2.196}
        assert ex.audit_bracket(pos, meta) is False
        assert book.places == [2.196], book.places
        log = open(j.jsonl.name, encoding="utf-8").read()
        assert "sCode=51327" in log
        assert "Take-profit trigger price should be lower than the last price" in log
        assert log.count("protect_missing") == 1
        assert ex.audit_bracket(pos, meta) is False
        assert book.places == [2.196]
        assert open(j.jsonl.name, encoding="utf-8").read().count("protect_missing") == 1

        book.mode = "silent"
        ex._protect_after["ICP-USDT-SWAP"] = time.time() - 1
        assert ex.audit_bracket(pos, meta) is False
        assert book.places == [2.196, 2.196]
        log = open(j.jsonl.name, encoding="utf-8").read()
        assert "tpTriggerPx" in log and "oco" in log
        assert ex.audit_bracket(pos, meta) is False
        assert book.places == [2.196, 2.196]

        book.mode = "ok"
        ex._protect_after["ICP-USDT-SWAP"] = time.time() - 1
        assert ex.audit_bracket(pos, meta) is True
        assert any(_algo_set(a, "tpTriggerPx") for a in book.algos)
        placed = len(book.places)
        assert ex.audit_bracket(pos, meta) is True
        assert len(book.places) == placed
    finally:
        j.close()
