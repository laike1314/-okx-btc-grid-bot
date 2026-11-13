"""Position 代理：按权益百分比定仓位，限制杠杆和保证金占用。

张数 = (权益 x 单笔风险%) / (每张在止损时的总亏损)
每张总亏损 = 止损距离 + 双边手续费 + 双边滑点（都按名义价值算）
再用「保证金 <= 权益 x MAX_MARGIN_PCT」封顶，最后按 lotSz 向下取整。
"""
import math
import config
from okx_client import fmt_num

AGENT = "position"


def round_px(px, tick, mode="nearest"):
    t = float(tick)
    n = px / t
    n = math.floor(n) if mode == "down" else math.ceil(n) if mode == "up" else round(n)
    return float(fmt_num(n * t, tick))


def stop_is_valid(side, sl, mark):
    """止损必须在标记价的保护一侧，否则 OKX 会回 51302/51304。"""
    try:
        sl, mark = float(sl), float(mark)
    except (TypeError, ValueError):
        return False
    if sl <= 0 or mark <= 0:
        return False
    if side == "long":
        return sl < mark
    return sl > mark


def round_trip_cost_pct(maker, taker, spread_bps):
    """开仓按 maker、平仓按 taker，再加一次点差和配置里的双边滑点。"""
    return (float(maker) + float(taker) + float(spread_bps or 0) / 1e4
            + 2 * config.SLIPPAGE_BPS / 1e4)


def planned_sl_pct(atr_pct, round_trip):
    """止损至少 max(ATR 倍数, 最小止损, 来回成本 × 倍数)。不超过 MAX_SL_PCT。"""
    need = float(round_trip) * config.MIN_EDGE_FEE_MULT / max(float(config.RISK_REWARD), 1e-9)
    sl = max(float(atr_pct) * config.ATR_SL_MULT, config.MIN_SL_PCT, need)
    return min(sl, config.MAX_SL_PCT), need


def bracket_from_fill(direction, fill_px, sl_pct, tp_pct, tick):
    if direction == "long":
        sl = round_px(fill_px * (1 - sl_pct), tick, "down")
        tp = round_px(fill_px * (1 + tp_pct), tick, "up")
    else:
        sl = round_px(fill_px * (1 + sl_pct), tick, "up")
        tp = round_px(fill_px * (1 - tp_pct), tick, "down")
    return sl, tp


class PositionManager:
    def __init__(self, client, journal, fees):
        self.c, self.j, self.fees = client, journal, fees

    def _available(self, available):
        """调用方没传入时自己读。0 是真的没有保证金，None 才是没读到。"""
        if available is not None:
            return available, ""
        fn = getattr(self.c, "margin_view", None)
        if not fn:
            return None, ""
        try:
            view = fn()
        except Exception as e:
            self.j.log(AGENT, "margin_view_err", f"读可用保证金失败，仓位按总权益: {e}")
            return None, ""
        if not isinstance(view, dict) or view.get("available") is None:
            self.j.log(AGENT, "margin_view_empty", "没有可用保证金字段，仓位按总权益")
            return None, ""
        return view.get("available"), view.get("available_field") or ""

    def plan(self, cand, equity, available=None, size_scale=1.0):
        if config.is_swing():
            return self._plan_swing(cand, equity, available=available, size_scale=size_scale)
        inst = cand["instId"]
        spec = self.c.instrument(inst)
        lever = int(min(config.LEVERAGE, config.MAX_LEVERAGE, spec["maxLever"]))
        # maker 进场：多单挂买一，空单挂卖一，不吃对手价。
        entry = cand["bid"] if cand["direction"] == "long" else cand["ask"]
        rt = round_trip_cost_pct(self.fees.maker, self.fees.taker, cand.get("spread_bps") or 0)
        sl_pct, need = planned_sl_pct(cand["atr_pct"], rt)
        tp_pct = sl_pct * config.RISK_REWARD
        if tp_pct + 1e-12 < rt * config.MIN_EDGE_FEE_MULT:
            self.j.log(AGENT, "cost_too_wide",
                       f"{inst} 来回成本 {rt:.3%}×{config.MIN_EDGE_FEE_MULT:g} 需要止损 {need:.2%}，"
                       f"超过上限 {config.MAX_SL_PCT:.2%}，放弃", inst=inst)
            return None
        cost_pct = rt
        per_ct_notional = entry * spec["ctVal"]
        per_ct_loss = per_ct_notional * (sl_pct + cost_pct)
        from agents.margin import sizing_base, use_frac
        available, avail_field = self._available(available)
        base, _how = sizing_base(equity, available)
        risk_usdt = equity * config.RISK_PER_TRADE_PCT
        qty_risk = risk_usdt / per_ct_loss
        qty_margin = base * config.MAX_MARGIN_PCT * lever / per_ct_notional
        if available is not None and per_ct_notional > 0:
            cap = max(0.0, float(available)) * use_frac() * lever / per_ct_notional
            qty_margin = min(qty_margin, cap)
        try:
            scale = float(size_scale if size_scale is not None else 1.0)
        except (TypeError, ValueError):
            scale = 1.0
        if scale <= 0:
            self.j.log(AGENT, "too_small", f"{inst} 保证金缩放比例 {size_scale} 无效，放弃", inst=inst)
            return None
        qty = min(qty_risk, qty_margin) * scale
        lot = float(spec["lotSz"])
        qty = math.floor(qty / lot) * lot
        if qty < spec["minSz"]:
            self.j.log(AGENT, "too_small", f"{inst} 计算张数 {qty} < 最小 {spec['minSz']}，放弃", inst=inst)
            return None
        if cand["direction"] == "long":
            sl = round_px(entry * (1 - sl_pct), spec["tickSz"], "down")
            tp = round_px(entry * (1 + tp_pct), spec["tickSz"], "up")
        else:
            sl = round_px(entry * (1 + sl_pct), spec["tickSz"], "up")
            tp = round_px(entry * (1 - tp_pct), spec["tickSz"], "down")
        notional = qty * per_ct_notional
        plan = {"instId": inst, "direction": cand["direction"], "qty": float(fmt_num(qty, spec["lotSz"])),
                "lever": lever, "entry_ref": entry, "sl": sl, "tp": tp, "sl_pct": sl_pct, "tp_pct": tp_pct,
                "notional": notional, "margin": notional / lever,
                "risk_usdt": qty * per_ct_loss, "score": cand["score"],
                "signal_px": cand.get("price"), "available": available,
                "available_field": avail_field, "size_scale": scale}
        base_txt = (f"按{avail_field or '可用保证金'} {float(available):.1f}"
                    if available is not None else "按总权益（没有读到可用保证金）")
        self.j.log(AGENT, "plan", f"{inst} {cand['direction']} {plan['qty']}张 名义 {notional:.1f}U "
                   f"保证金 {plan['margin']:.1f}U {lever}x {base_txt} 止损 {sl}({sl_pct:.2%}) "
                   f"止盈 {tp}({tp_pct:.2%}) 最大亏损≈{plan['risk_usdt']:.2f}U", **plan)
        return plan

    def _plan_swing(self, cand, equity, available=None, size_scale=1.0):
        """每笔名义约等于分配额的 1/N。分配额是 min(总权益, 可用保证金)，不是虚高的 eq。"""
        from .swing import profile_for, size_qty, fee_allows
        inst = cand["instId"]
        spec = self.c.instrument(inst)
        lever = int(min(max(1, int(config.SWING_LEVERAGE)), config.MAX_LEVERAGE, spec["maxLever"]))
        entry = cand["bid"] if cand["direction"] == "long" else cand["ask"]
        prof = profile_for(cand.get("family") or "oi")
        atr_pct = float(cand.get("atr_pct") or 0)
        sl_pct = atr_pct * float(prof["sl_atr"])
        tp_pct = atr_pct * float(prof["tp_atr"])
        rt = round_trip_cost_pct(self.fees.maker, self.fees.taker, cand.get("spread_bps") or 0)
        if atr_pct <= 0 or not fee_allows(tp_pct, rt):
            self.j.log(AGENT, "cost_too_wide",
                       f"{inst} 止盈 {tp_pct:.3%} 不够来回成本 {rt:.3%}×{config.SWING_MIN_EDGE_FEE_MULT:g}，放弃",
                       inst=inst)
            return None
        available, avail_field = self._available(available)
        try:
            scale = float(size_scale if size_scale is not None else 1.0)
        except (TypeError, ValueError):
            scale = 1.0
        qty = size_qty(equity, entry, spec["ctVal"], spec["lotSz"], spec["minSz"],
                       available=available, fraction=scale, lever=lever)
        if not qty:
            shown = f"{float(available):.2f}" if available is not None else f"权益 {float(equity):.2f}"
            self.j.log(AGENT, "too_small",
                       f"{inst} 按 {shown} 的 1/{config.SWING_MAX_POSITIONS} × {scale:g} 算不出最小张数，放弃",
                       inst=inst, available=available, size_scale=scale)
            return None
        sl, tp = bracket_from_fill(cand["direction"], entry, sl_pct, tp_pct, spec["tickSz"])
        per = entry * spec["ctVal"]
        notional = qty * per
        plan = {"instId": inst, "direction": cand["direction"], "qty": float(fmt_num(qty, spec["lotSz"])),
                "lever": lever, "entry_ref": entry, "sl": sl, "tp": tp, "sl_pct": sl_pct, "tp_pct": tp_pct,
                "notional": notional, "margin": notional / lever,
                "risk_usdt": notional * sl_pct, "score": cand.get("combined", cand.get("score")),
                "signal_px": cand.get("price"), "atr": cand.get("atr"), "atr_pct": atr_pct,
                "family": prof["family"] if prof["family"] in ("momentum", "oi") else cand.get("family"),
                "regime": cand.get("regime"), "mom_score": cand.get("mom_score"),
                "oi_score": cand.get("oi_score"), "combined": cand.get("combined"),
                "min_hold_sec": prof["min_hold"], "max_hold_sec": prof["max_hold"],
                "tag": cand.get("tag") or prof["tag"], "available": available,
                "available_field": avail_field, "size_scale": scale}
        base_txt = (f"按{avail_field or '可用保证金'} {float(available):.1f} × {scale:g}"
                    if available is not None else f"按总权益 {float(equity):.1f}（没有读到可用保证金）")
        self.j.log(AGENT, "plan",
                   f"{inst} {cand['direction']} {plan['family']} {plan['qty']}张 名义 {notional:.1f}U "
                   f"{lever}x {base_txt} 止损 {sl}({sl_pct:.2%}) 止盈 {tp}({tp_pct:.2%})", inst=inst, **{
                       k: plan[k] for k in ("qty", "lever", "sl", "tp", "family", "combined", "notional")})
        return plan
