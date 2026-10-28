"""Risk 代理（对应推文的 Rug 检测）：任何一项不过就拒绝开仓，并写明原因。

检查项：当日亏损上限、连亏冷却、单币冷却、最大持仓/同币已有仓位、
        点差、资金费率极端、波动率区间、预期利润是否覆盖双边手续费。
"""
import config

AGENT = "risk"


class RiskGate:
    def __init__(self, client, journal, state, fees):
        self.c, self.j, self.s, self.fees = client, journal, state, fees

    def global_block(self, equity):
        """不针对具体币种的闸门。返回拒绝原因或 None。"""
        start = self.s.day_start_equity(equity)
        if self.s.daily_pnl() <= -abs(start) * config.DAILY_LOSS_CAP_PCT:
            return f"当日已亏 {self.s.daily_pnl():.2f} USDT，达到上限 {config.DAILY_LOSS_CAP_PCT:.1%}"
        if self.s.now() < self.s.d["global_cooldown_until"]:
            left = int(self.s.d["global_cooldown_until"] - self.s.now())
            return f"连亏冷却中，还剩 {left}s"
        return None

    def check(self, cand, positions, equity):
        inst = cand["instId"]
        g = self.global_block(equity)
        if g:
            return False, g
        if len(positions) >= config.MAX_POSITIONS:
            return False, f"持仓数 {len(positions)} 已达上限 {config.MAX_POSITIONS}"
        if any(p["instId"] == inst for p in positions):
            return False, "该币已有持仓"
        until = self.s.d["symbol_cooldown"].get(inst, 0)
        if self.s.now() < until:
            return False, f"该币止损冷却中，还剩 {int(until - self.s.now())}s"
        t = self.c.ticker(inst)
        mid = (t["bid"] + t["ask"]) / 2
        spread_bps = (t["ask"] - t["bid"]) / mid * 1e4 if mid else 1e9
        if spread_bps > config.MAX_SPREAD_BPS:
            return False, f"点差 {spread_bps:.2f}bps > {config.MAX_SPREAD_BPS}"
        fr = self.c.funding_rate(inst)
        if abs(fr) > config.MAX_ABS_FUNDING_RATE:
            return False, f"资金费率极端 {fr:.4%}"
        # 顺着资金费方向付钱的一侧，费率过半上限也拒绝
        if (cand["direction"] == "long" and fr > config.MAX_ABS_FUNDING_RATE / 2) or \
           (cand["direction"] == "short" and fr < -config.MAX_ABS_FUNDING_RATE / 2):
            return False, f"开{cand['direction']}要付较高资金费 {fr:.4%}"
        ap = cand["atr_pct"]
        if ap > config.MAX_ATR_PCT:
            return False, f"波动过猛 ATR {ap:.3%} > {config.MAX_ATR_PCT:.3%}"
        if ap < config.MIN_ATR_PCT:
            return False, f"波动太小 ATR {ap:.3%}，不够覆盖手续费"
        taker = self.fees.taker
        round_trip = 2 * taker + 2 * config.SLIPPAGE_BPS / 1e4      # 双边手续费 + 双边滑点
        sl_pct = min(max(ap * config.ATR_SL_MULT, config.MIN_SL_PCT), config.MAX_SL_PCT)
        tp_pct = sl_pct * config.RISK_REWARD
        if tp_pct < round_trip * config.MIN_EDGE_FEE_MULT:
            return False, f"止盈距离 {tp_pct:.3%} < 双边成本 {round_trip:.3%} x{config.MIN_EDGE_FEE_MULT}"
        cand.update(spread_bps=spread_bps, funding=fr, bid=t["bid"], ask=t["ask"])
        return True, f"通过：点差 {spread_bps:.2f}bps 资金费 {fr:.4%} ATR {ap:.3%}"

    # ---- 平仓后由 equity 代理回调 ----
    def on_trade_closed(self, inst, net):
        self.s.add_daily_pnl(net)
        if net < 0:
            self.s.d["consecutive_losses"] += 1
            self.s.d["symbol_cooldown"][inst] = self.s.now() + config.SYMBOL_COOLDOWN_SEC
            if self.s.d["consecutive_losses"] >= config.MAX_CONSECUTIVE_LOSSES:
                self.s.d["global_cooldown_until"] = self.s.now() + config.LOSS_COOLDOWN_SEC
                self.j.log(AGENT, "cooldown", f"连亏 {self.s.d['consecutive_losses']} 次，全局冷却 "
                           f"{config.LOSS_COOLDOWN_SEC}s", inst=inst)
                self.s.d["consecutive_losses"] = 0
            else:
                self.j.log(AGENT, "symbol_cooldown", f"{inst} 亏损，单币冷却 {config.SYMBOL_COOLDOWN_SEC}s", inst=inst)
        else:
            self.s.d["consecutive_losses"] = 0
        self.s.save()
