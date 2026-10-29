"""Equity 代理：发现仓位消失（被止盈/止损/净利平仓）后，取交易所实际已实现盈亏，
更新 trades.csv / equity.csv / equity.json（胜率、盈亏比 PF、最大回撤），并通知风控。
"""
import csv
import json
import os
import time
from datetime import datetime, timezone, timedelta

BJ = timezone(timedelta(hours=8))
AGENT = "equity"


class EquityTracker:
    def __init__(self, client, journal, state, risk, data_dir, executor=None):
        self.c, self.j, self.s, self.risk = client, journal, state, risk
        self.ex = executor
        self.dir = data_dir
        self.known = set(self.s.d["open_meta"].keys())
        self._last_live = None
        self.trades_csv = os.path.join(data_dir, "trades.csv")
        self.equity_csv = os.path.join(data_dir, "equity.csv")
        self.equity_json = os.path.join(data_dir, "equity.json")

    def register_open(self, plan, fill):
        qty = float((fill or {}).get("qty") or 0)
        px = float((fill or {}).get("fill_px") or 0)
        if qty <= 0 or px <= 0:
            self.j.log(AGENT, "skip_zero_fill",
                       f"{plan.get('instId')} 成交张数 {qty} 或价格 {px} 无效，不写开仓记录",
                       inst=plan.get("instId"), qty=qty)
            return False
        now = self.s.now()
        inst = plan["instId"]
        self.s.d["open_meta"][inst] = {
            "direction": plan["direction"], "qty": qty, "entry": px, "sl": plan["sl"],
            "tp": plan["tp"], "opened_at": datetime.now(BJ).isoformat(timespec="seconds"),
            "opened_ts": now, "open_ms": int(now * 1000), "score": plan.get("score")}
        if plan.get("reentry_bar_ts"):
            self.s.d.setdefault("last_entry_bar", {})[inst] = {
                "direction": plan["direction"], "bar_ts": int(plan["reentry_bar_ts"])}
        self.known.add(inst)
        self.s.save()
        return True

    def sync(self, positions):
        positions = list(positions or [])
        live_ids, seen = [], set()
        for p in positions:
            inst = p.get("instId")
            if inst and inst not in seen:
                seen.add(inst)
                live_ids.append(inst)
        live = set(live_ids)
        if live != self._last_live:
            self._last_live = set(live)
            shown = ",".join(live_ids) if live_ids else "无"
            self.j.log(AGENT, "positions", f"交易所当前持仓 {len(live)} 个: {shown}")
        for inst in list(self.known - live):
            try:
                self._on_closed(inst)
            except Exception as e:
                self.j.log(AGENT, "close_sync_error", f"{inst} 平仓记账失败，下轮重试: {e}", inst=inst)
        for p in positions:                       # 程序重启后接管已有仓位；逐个做，一笔失败不挡后面的
            try:
                self._adopt_if_needed(p)
            except Exception as e:
                self.j.log(AGENT, "adopt_error", f"{p.get('instId')} 接管失败，下轮重试: {e}",
                           inst=p.get("instId"))

    def _adopt_if_needed(self, p):
        if p["instId"] in self.known:
            return
        if float(p.get("qty") or 0) <= 0:
            self.j.log(AGENT, "skip_zero_pos", f"{p.get('instId')} 张数为 0，不接管、不平仓记账",
                       inst=p.get("instId"))
            return
        now = self.s.now()
        open_ms = int(p.get("cTime") or now * 1000)
        self.known.add(p["instId"])
        meta = self.s.d["open_meta"].setdefault(p["instId"], {
            "direction": p["side"], "qty": p["qty"], "entry": p["avgPx"],
            "opened_at": "接管", "opened_ts": open_ms / 1000.0, "open_ms": open_ms})
        protected = None
        if self.ex is not None:
            try:
                protected = self.ex.protect_adopted(p)
            except Exception as e:
                self.j.log(AGENT, "adopt_protect_err", f"{p['instId']} 补挂止盈止损失败: {e}", inst=p["instId"])
        if protected:
            meta["sl"], meta["tp"] = protected
        self.j.log(AGENT, "adopt", f"发现已有仓位 {p['instId']} {p['side']} {p['qty']}张，纳入监控",
                   inst=p["instId"])
        self.s.save()

    def _on_closed(self, inst):
        meta = self.s.d["open_meta"].pop(inst, {})
        self.known.discard(inst)
        if float(meta.get("qty") or 0) <= 0:
            self.j.log(AGENT, "skip_zero_close", f"{inst} 记录张数为 0，不写 trade_closed", inst=inst)
            self.s.save()
            return
        from .risk import note_exit_trend_bar
        note_exit_trend_bar(self.s, inst)
        res = None
        for _ in range(1 if self.c.is_mock else 6):
            try:
                res = self.c.last_closed(inst)
            except Exception:
                res = None
            if res and res.get("uTime", 0) >= (meta.get("open_ms") or 0):
                break
            res = None
            time.sleep(1)
        net = res["realizedPnl"] if res else 0.0
        reason = meta.get("close_reason") or (res.get("why") if res and res.get("why") else "交易所止盈/止损")
        try:
            eq = self.c.equity()
        except Exception:
            eq = None
        row = {"time": datetime.now(BJ).isoformat(timespec="seconds"), "instId": inst,
               "direction": meta.get("direction"), "qty": meta.get("qty"), "entry": meta.get("entry"),
               "exit": res.get("closeAvgPx") if res else None, "gross_pnl": res.get("pnl") if res else None,
               "fee": res.get("fee") if res else None, "funding": res.get("fundingFee") if res else None,
               "net_pnl": round(net, 4), "est_net": meta.get("est_net"), "reason": reason,
               "equity_after": round(eq, 4) if eq is not None else None}
        new = not os.path.exists(self.trades_csv)
        with open(self.trades_csv, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row))
            if new:
                w.writeheader()
            w.writerow(row)
        self.j.log(AGENT, "trade_closed", f"{inst} 平仓（{reason}）净盈亏 {net:+.2f} USDT，权益 "
                   f"{eq if eq is None else round(eq, 2)}", **row)
        self.risk.on_trade_closed(inst, net)
        self.snapshot(eq)

    def snapshot(self, eq=None):
        if eq is None:
            try:
                eq = self.c.equity()
            except Exception:
                return
        new = not os.path.exists(self.equity_csv)
        with open(self.equity_csv, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "equity"])
            w.writerow([datetime.now(BJ).isoformat(timespec="seconds"), round(eq, 4)])
        self._write_stats(eq)

    def _write_stats(self, eq):
        pnls = []
        if os.path.exists(self.trades_csv):
            with open(self.trades_csv, encoding="utf-8") as f:
                pnls = [float(r["net_pnl"]) for r in csv.DictReader(f) if r.get("net_pnl")]
        curve = []
        if os.path.exists(self.equity_csv):
            with open(self.equity_csv, encoding="utf-8") as f:
                curve = [float(r["equity"]) for r in csv.DictReader(f)]
        peak, mdd = (curve[0] if curve else eq), 0.0
        for v in curve:
            peak = max(peak, v)
            mdd = max(mdd, (peak - v) / peak if peak else 0)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        stats = {"updated": datetime.now(BJ).isoformat(timespec="seconds"), "equity": round(eq, 4),
                 "start_equity": curve[0] if curve else eq, "trades": len(pnls), "wins": len(wins),
                 "losses": len(losses), "win_rate": round(len(wins) / len(pnls), 4) if pnls else None,
                 "net_pnl": round(sum(pnls), 4),
                 "profit_factor": round(sum(wins) / abs(sum(losses)), 3) if losses and sum(losses) else None,
                 "avg_win": round(sum(wins) / len(wins), 4) if wins else None,
                 "avg_loss": round(sum(losses) / len(losses), 4) if losses else None,
                 "max_drawdown_pct": round(mdd * 100, 3)}
        json.dump(stats, open(self.equity_json, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        return stats
