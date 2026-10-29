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

    def plan(self, cand, equity):
        inst = cand["instId"]
        spec = self.c.instrument(inst)
        lever = int(min(config.LEVERAGE, config.MAX_LEVERAGE, spec["maxLever"]))
        entry = cand["ask"] if cand["direction"] == "long" else cand["bid"]
        sl_pct = min(max(cand["atr_pct"] * config.ATR_SL_MULT, config.MIN_SL_PCT), config.MAX_SL_PCT)
        tp_pct = sl_pct * config.RISK_REWARD
        cost_pct = 2 * self.fees.taker + 2 * config.SLIPPAGE_BPS / 1e4
        per_ct_notional = entry * spec["ctVal"]
        per_ct_loss = per_ct_notional * (sl_pct + cost_pct)
        risk_usdt = equity * config.RISK_PER_TRADE_PCT
        qty_risk = risk_usdt / per_ct_loss
        qty_margin = equity * config.MAX_MARGIN_PCT * lever / per_ct_notional
        qty = min(qty_risk, qty_margin)
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
                "signal_px": cand.get("price")}
        self.j.log(AGENT, "plan", f"{inst} {cand['direction']} {plan['qty']}张 名义 {notional:.1f}U "
                   f"保证金 {plan['margin']:.1f}U {lever}x 止损 {sl}({sl_pct:.2%}) 止盈 {tp}({tp_pct:.2%}) "
                   f"最大亏损≈{plan['risk_usdt']:.2f}U", **plan)
        return plan
