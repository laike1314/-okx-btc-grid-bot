"""Risk 代理（对应推文的 Rug 检测）：任何一项不过就拒绝开仓，并写明原因。

检查项：当日亏损上限、连亏冷却、单币冷却（秒数 <=0 则关闭，含已落盘的到期时间）、
        持仓个数上限（MAX_POSITIONS<=0 则不限制）/同币已有仓位、点差、资金费率极端、
        波动率区间、预期利润是否覆盖双边手续费。
"""
import config

AGENT = "risk"


def position_cap_reached(n):
    """同时持仓数是否已经顶到上限。MAX_POSITIONS<=0 表示不限制个数。"""
    cap = config.MAX_POSITIONS
    return cap > 0 and n >= cap


def bar_seconds(bar):
    text = str(bar or "").strip()
    try:
        if text.endswith("m"):
            return int(text[:-1]) * 60
        if text.endswith("H"):
            return int(text[:-1]) * 3600
        if text.endswith("D"):
            return int(text[:-1]) * 86400
    except ValueError:
        return 0
    return 0


def reentry_bar_name():
    """同向再入场等待的 K 线。短于 5 分钟的配置抬到 5m。"""
    name = config.REENTRY_BAR or "5m"
    if bar_seconds(name) < 300:
        return "5m"
    return name


def stock_ids():
    return set((config.UNIVERSE_META or {}).get("stocks") or [])


def reentry_blocked(state, inst, direction, bar_ts):
    last = (state.d.get("last_entry_bar") or {}).get(inst)
    if not last or last.get("direction") != direction:
        return False
    return int(bar_ts or 0) <= int(last.get("bar_ts") or 0)


def same_dir_crypto_count(positions, direction):
    stocks = stock_ids()
    return sum(1 for p in positions
               if p.get("side") == direction and p.get("instId") not in stocks)


def chase_blocked(direction, ref, bid, ask, limit):
    """价格已经顺着方向跑过信号收盘，就不要追。"""
    if not ref or limit <= 0:
        return False
    if direction == "long":
        return float(ask) > float(ref) * (1 + limit)
    return float(bid) < float(ref) * (1 - limit)


def exit_bar_blocked(stored_ts, latest_ts):
    """平仓之后，要等一根更新的趋势 K 线收盘。没有记录则不挡。"""
    if not stored_ts:
        return False
    return int(latest_ts or 0) <= int(stored_ts)


def note_exit_trend_bar(state, inst):
    """记下平仓时上一根已收盘的趋势 K 线。下一笔要等更晚的收盘。"""
    sec = bar_seconds(config.TREND_BAR) or 900
    now_ms = int(state.now() * 1000)
    span = sec * 1000
    current_open = (now_ms // span) * span
    state.d.setdefault("last_exit_trend_bar", {})[inst] = current_open - span


class RiskGate:
    def __init__(self, client, journal, state, fees):
        self.c, self.j, self.s, self.fees = client, journal, state, fees

    def global_block(self, equity):
        """不针对具体币种的闸门。返回拒绝原因或 None。"""
        hit, pnl, ref = self.s.loss_cap_hit(equity)
        if hit:
            hours = config.ROLLING_LOSS_WINDOW_SEC / 3600
            return (f"近 {hours:g} 小时已实现盈亏 {pnl:.2f} USDT，"
                    f"达到参考权益 {ref:.0f} 的 {config.DAILY_LOSS_CAP_PCT:.1%}")
        # 0 = 关闭。旧 state.json 里未过期的 global_cooldown_until 也不再挡开仓。
        if config.LOSS_COOLDOWN_SEC > 0 and self.s.now() < self.s.d["global_cooldown_until"]:
            left = int(self.s.d["global_cooldown_until"] - self.s.now())
            return f"连亏冷却中，还剩 {left}s"
        return None

    def check(self, cand, positions, equity):
        inst = cand["instId"]
        g = self.global_block(equity)
        if g:
            return False, g
        if position_cap_reached(len(positions)):
            return False, f"持仓数 {len(positions)} 已达上限 {config.MAX_POSITIONS}"
        # 同一合约只留一个方向：已有多或空都不再开，避免同向加仓和反向对锁。
        if any(p["instId"] == inst for p in positions):
            return False, "该币已有持仓"
        if config.SYMBOL_COOLDOWN_SEC > 0:
            until = self.s.d["symbol_cooldown"].get(inst, 0)
            if self.s.now() < until:
                return False, f"该币止损冷却中，还剩 {int(until - self.s.now())}s"
        if config.MAX_SAME_DIR_CRYPTO > 0 and inst not in stock_ids():
            n_same = same_dir_crypto_count(positions, cand["direction"])
            if n_same >= config.MAX_SAME_DIR_CRYPTO:
                return False, (f"同向加密持仓已有 {n_same}，上限 {config.MAX_SAME_DIR_CRYPTO}")
        t = self.c.ticker(inst)
        mid = (t["bid"] + t["ask"]) / 2
        spread_bps = (t["ask"] - t["bid"]) / mid * 1e4 if mid else 1e9
        if spread_bps > config.MAX_SPREAD_BPS:
            return False, f"点差 {spread_bps:.2f}bps > {config.MAX_SPREAD_BPS}"
        vol = float(t.get("vol_usdt_24h") or 0)
        if config.MIN_VOL_USDT_24H > 0 and vol < config.MIN_VOL_USDT_24H:
            return False, f"24h成交额 {vol:.0f}U < {config.MIN_VOL_USDT_24H:.0f}U"
        if chase_blocked(cand["direction"], cand.get("price"), t["bid"], t["ask"], config.CHASE_LIMIT_PCT):
            return False, (f"不追价：盘口相对信号收盘 {cand.get('price')} "
                           f"已超过 {config.CHASE_LIMIT_PCT:.2%}")
        try:
            mark = float(self.c.mark_price(inst))
            idx = float(self.c.index_price(inst))
        except Exception as e:
            return False, f"标记/指数价不可用: {e}"
        if mark <= 0 or idx <= 0:
            return False, "标记或指数价为空"
        gap_bps = abs(mark - idx) / idx * 1e4
        if gap_bps > config.MAX_MARK_INDEX_BPS:
            return False, f"标记价偏离指数 {gap_bps:.1f}bps > {config.MAX_MARK_INDEX_BPS:.0f}"
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
        from .position import planned_sl_pct, round_trip_cost_pct
        round_trip = round_trip_cost_pct(self.fees.maker, self.fees.taker, spread_bps)
        sl_pct, need = planned_sl_pct(ap, round_trip)
        tp_pct = sl_pct * config.RISK_REWARD
        if tp_pct + 1e-12 < round_trip * config.MIN_EDGE_FEE_MULT:
            return False, (f"最大止损 {config.MAX_SL_PCT:.2%} 仍盖不住来回成本 {round_trip:.3%}"
                           f"×{config.MIN_EDGE_FEE_MULT:g}（需要 {need:.2%}）")
        if config.MAX_COST_TO_R > 0 and sl_pct > 0 and round_trip / sl_pct > config.MAX_COST_TO_R:
            return False, (f"双边成本 {round_trip:.3%} 占止损 {sl_pct:.3%} 的 "
                           f"{round_trip / sl_pct:.0%}，超过 {config.MAX_COST_TO_R:.0%}")
        cand.update(spread_bps=spread_bps, funding=fr, bid=t["bid"], ask=t["ask"])
        bar_name = reentry_bar_name()
        try:
            bars = self.c.candles(inst, bar_name, 5)
        except Exception as e:
            return False, f"再入场周期 {bar_name} K线失败: {e}"
        if not bars:
            return False, f"再入场周期 {bar_name} K线不足"
        bar_ts = int(bars[-1]["ts"])
        if reentry_blocked(self.s, inst, cand["direction"], bar_ts):
            return False, f"同向再入场需等新的 {bar_name} K线收盘"
        trend_name = config.TREND_BAR or "15m"
        try:
            trend_bars = self.c.candles(inst, trend_name, 3)
        except Exception as e:
            return False, f"趋势周期 {trend_name} K线失败: {e}"
        trend_ts = int(trend_bars[-1]["ts"]) if trend_bars else 0
        stored_exit = (self.s.d.get("last_exit_trend_bar") or {}).get(inst)
        if exit_bar_blocked(stored_exit, trend_ts):
            return False, f"平仓后需等新的 {trend_name} K线收盘"
        cand["reentry_bar_ts"] = bar_ts
        return True, f"通过：点差 {spread_bps:.2f}bps 资金费 {fr:.4%} ATR {ap:.3%}"

    # ---- 平仓后由 equity 代理回调 ----
    def on_trade_closed(self, inst, net):
        self.s.add_daily_pnl(net)
        if net < 0:
            self.s.d["consecutive_losses"] += 1
            armed_global = self.s.d["consecutive_losses"] >= config.MAX_CONSECUTIVE_LOSSES
            if config.SYMBOL_COOLDOWN_SEC > 0:
                self.s.d["symbol_cooldown"][inst] = self.s.now() + config.SYMBOL_COOLDOWN_SEC
            if armed_global and config.LOSS_COOLDOWN_SEC > 0:
                self.s.d["global_cooldown_until"] = self.s.now() + config.LOSS_COOLDOWN_SEC
                self.j.log(AGENT, "cooldown", f"连亏 {self.s.d['consecutive_losses']} 次，全局冷却 "
                           f"{config.LOSS_COOLDOWN_SEC}s", inst=inst)
            elif config.SYMBOL_COOLDOWN_SEC > 0:
                self.j.log(AGENT, "symbol_cooldown", f"{inst} 亏损，单币冷却 {config.SYMBOL_COOLDOWN_SEC}s", inst=inst)
            if armed_global:
                self.s.d["consecutive_losses"] = 0
        else:
            self.s.d["consecutive_losses"] = 0
        self.s.save()


def offline_self_check():
    """不联网。仓位上限，以及冷却秒数 <=0 时不因落盘的到期时间拒绝开仓。"""
    old_cap = config.MAX_POSITIONS
    old_loss = config.LOSS_COOLDOWN_SEC
    old_sym = config.SYMBOL_COOLDOWN_SEC
    old_same = config.MAX_SAME_DIR_CRYPTO
    old_reentry = config.REENTRY_BAR
    try:
        config.MAX_POSITIONS = 0
        assert position_cap_reached(0) is False
        assert position_cap_reached(50) is False
        config.MAX_POSITIONS = 1
        assert position_cap_reached(0) is False
        assert position_cap_reached(1) is True
        config.MAX_POSITIONS = -1
        assert position_cap_reached(3) is False

        class _State:
            d = {"global_cooldown_until": 10**12, "symbol_cooldown": {"BTC-USDT-SWAP": 10**12},
                 "consecutive_losses": 0, "daily": {}, "day_start_equity": {}, "open_meta": {}}

            def now(self):
                return 1_000.0

            def loss_cap_hit(self, equity):
                return False, 0.0, equity

            def save(self):
                pass

        class _Journal:
            def log(self, *a, **k):
                pass

        class _Client:
            def __init__(self):
                self.hit = False

            def ticker(self, inst):
                self.hit = True
                raise RuntimeError("past-cooldown")

        config.LOSS_COOLDOWN_SEC = 0
        config.SYMBOL_COOLDOWN_SEC = 0
        config.MAX_SAME_DIR_CRYPTO = 2
        client = _Client()
        gate = RiskGate(client, _Journal(), _State(), None)
        assert gate.global_block(10_000) is None
        try:
            gate.check({"instId": "BTC-USDT-SWAP", "direction": "long", "atr_pct": 0.01}, [], 10_000)
        except RuntimeError as e:
            assert str(e) == "past-cooldown"
        assert client.hit, "冷却为 0 时不应在询价前拒绝"

        config.LOSS_COOLDOWN_SEC = 1800
        assert gate.global_block(10_000) is not None
        config.LOSS_COOLDOWN_SEC = 0
        config.SYMBOL_COOLDOWN_SEC = 3600
        ok, why = gate.check({"instId": "BTC-USDT-SWAP", "direction": "long", "atr_pct": 0.01}, [], 10_000)
        assert ok is False and "冷却" in why
        config.SYMBOL_COOLDOWN_SEC = 0
        config.MAX_POSITIONS = 0
        held = [{"instId": "BTC-USDT-SWAP", "side": "long"},
                {"instId": "ETH-USDT-SWAP", "side": "long"}]
        ok, why = gate.check({"instId": "SOL-USDT-SWAP", "direction": "long", "atr_pct": 0.01}, held, 10_000)
        assert ok is False and "同向" in why, why
        assert reentry_blocked(_State(), "BTC-USDT-SWAP", "long", 100) is False
        _State.d["last_entry_bar"] = {"BTC-USDT-SWAP": {"direction": "long", "bar_ts": 100}}
        assert reentry_blocked(_State(), "BTC-USDT-SWAP", "long", 100) is True
        assert reentry_blocked(_State(), "BTC-USDT-SWAP", "long", 200) is False
        assert reentry_blocked(_State(), "BTC-USDT-SWAP", "short", 100) is False
        assert reentry_bar_name() == "5m"
        config.REENTRY_BAR = "1m"
        assert reentry_bar_name() == "5m"
        config.REENTRY_BAR = "15m"
        assert reentry_bar_name() == "15m"
        assert chase_blocked("long", 100, 100, 100.2, 0.0015) is True
        assert chase_blocked("long", 100, 100, 100.05, 0.0015) is False
        assert chase_blocked("short", 100, 99.8, 100, 0.0015) is True
        assert exit_bar_blocked(0, 100) is False
        assert exit_bar_blocked(1000, 1000) is True
        assert exit_bar_blocked(1000, 2000) is False
        assert (0.0014 / 0.004) > 0.30
        from .position import planned_sl_pct
        sl, need = planned_sl_pct(0.001, 0.002)
        assert need == 0.002 * config.MIN_EDGE_FEE_MULT / config.RISK_REWARD
        assert abs(sl - min(config.MAX_SL_PCT, max(0.0025, config.MIN_SL_PCT, need))) < 1e-12
        sl_cap, _ = planned_sl_pct(0.05, 0.0001)
        assert sl_cap == config.MAX_SL_PCT

        import tempfile
        from agents.state import SharedState
        clock = {"t": 1_000_000.0}
        st = SharedState(tempfile.mkdtemp(), clock=lambda: clock["t"])
        st.add_daily_pnl(-50)
        clock["t"] = 1_000_100.0
        st.add_daily_pnl(-10)
        assert abs(st.rolling_pnl() + 60) < 1e-6
        clock["t"] = 1_000_100.0 + config.ROLLING_LOSS_WINDOW_SEC
        # 第一条已滑出窗口，第二条仍在窗口起点上（ts >= cutoff）。
        assert abs(st.rolling_pnl() + 10) < 1e-6
        hit, pnl, ref = st.loss_cap_hit(10_000)
        assert hit is False
        st.add_daily_pnl(-500)
        hit, pnl, _ref = st.loss_cap_hit(10_000)
        assert hit is True and pnl < 0
    finally:
        config.MAX_POSITIONS = old_cap
        config.LOSS_COOLDOWN_SEC = old_loss
        config.SYMBOL_COOLDOWN_SEC = old_sym
        config.MAX_SAME_DIR_CRYPTO = old_same
        config.REENTRY_BAR = old_reentry
