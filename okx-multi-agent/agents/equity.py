"""Equity 代理：发现仓位消失（被止盈/止损/净利平仓）后，取交易所实际已实现盈亏，
更新 trades.csv / equity.csv / equity.json（胜率、盈亏比 PF、最大回撤），并通知风控。
"""
import csv
import json
import os
import time
from datetime import datetime, timezone, timedelta

import config

BJ = timezone(timedelta(hours=8))
AGENT = "equity"


def _close_sz(row):
    raw = (row or {}).get("closeTotalPos")
    if raw in (None, ""):
        return None
    try:
        qty = float(raw)
    except (TypeError, ValueError):
        return None
    return qty if qty > 0 else None


def _dedupe_rows(rows):
    seen, out = set(), []
    for row in rows or []:
        key = (str(row.get("posId") or ""), str(row.get("uTime") or ""), str(row.get("pnl") or ""),
               str(row.get("closeTotalPos") or ""), str(row.get("type") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def choose_close_rows(rows, open_ms, qty, pos_id=None):
    """从平仓历史里选出能对上这一笔持仓的记录。

    同一 posId 的全平记录优先，部分平仓只在没有全平、且张数相加等于持仓时才合并。
    不同 posId 是另一段持仓，不能加进这一笔。对不上时返回 latest_unverified，
    调用方不得把这条盈亏当成最终准确值。
    """
    window = []
    for row in _dedupe_rows(rows):
        try:
            ut = int(float(row.get("uTime") or 0))
        except (TypeError, ValueError):
            ut = 0
        if open_ms and ut and ut < int(open_ms) - 30000:
            continue
        rid = str(row.get("posId") or "")
        if pos_id and rid and rid != str(pos_id):
            continue
        window.append(row)
    if not window:
        return None, "none"
    ids = {str(r.get("posId")) for r in window if r.get("posId")}
    if not pos_id and len(ids) > 1:
        latest = max(window, key=lambda r: int(float(r.get("uTime") or 0)))
        return [latest], "latest_unverified"
    try:
        qty_f = float(qty or 0)
    except (TypeError, ValueError):
        qty_f = 0.0
    sized = [(sz, row) for row in window if (sz := _close_sz(row))]
    tol = max(qty_f * 1e-4, 1e-6) if qty_f > 0 else 1e-6

    def _matches(sz):
        return qty_f > 0 and abs(sz - qty_f) <= tol

    full = [row for sz, row in sized if str(row.get("type") or "") in ("2", "3", "6") and _matches(sz)]
    if full:
        full.sort(key=lambda r: int(float(r.get("uTime") or 0)))
        return [full[-1]], "matched"
    if qty_f > 0 and sized and not any(str(r.get("type") or "") in ("2", "3", "6") for _sz, r in sized):
        matched = [row for sz, row in sized if _matches(sz)]
        if matched:
            matched.sort(key=lambda r: int(float(r.get("uTime") or 0)))
            return [matched[-1]], "matched"
        total = sum(sz for sz, _row in sized)
        if abs(total - qty_f) <= max(qty_f * 0.02, 1e-6):
            return [row for _sz, row in sized], "summed"
    if len(window) == 1 and (not qty_f or not sized or _matches(sized[0][0])):
        return window, "single"
    latest = max(window, key=lambda r: int(float(r.get("uTime") or 0)))
    return [latest], "latest_unverified"


def combine_close_rows(rows):
    def num(row, key):
        try:
            return float(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    pnl = sum(num(r, "pnl") for r in rows)
    fee = sum(num(r, "fee") for r in rows)
    funding = sum(num(r, "fundingFee") for r in rows)
    realized = sum(num(r, "realizedPnl") for r in rows)
    if abs(realized) < 1e-12:
        realized = pnl + fee + funding
    weighted = 0.0
    size = 0.0
    for row in rows:
        sz = _close_sz(row)
        px = num(row, "closeAvgPx")
        if sz and px > 0:
            weighted += sz * px
            size += sz
    if size > 0:
        exit_px = weighted / size
    else:
        exit_px = num(rows[-1], "closeAvgPx")
    open_px = 0.0
    for row in rows:
        px = num(row, "openAvgPx")
        if px > 0:
            open_px = px
            break
    return {"realizedPnl": realized, "pnl": pnl, "fee": fee, "fundingFee": funding,
            "openAvgPx": open_px, "closeAvgPx": exit_px, "closeQty": size,
            "uTime": max(int(float(r.get("uTime") or 0)) for r in rows),
            "posId": rows[-1].get("posId") or "",
            "why": rows[-1].get("why") or ""}


def _pnl_row_trusted(res, meta):
    """成交价明显不同却回报 0 盈亏时，当成没读到，改走估算。"""
    if not res:
        return False
    try:
        net = float(res.get("realizedPnl") or 0)
        exit_px = float(res.get("closeAvgPx") or 0)
        entry = float(meta.get("entry") or 0)
    except (TypeError, ValueError):
        return False
    if exit_px <= 0:
        return abs(net) > 1e-8
    if entry > 0 and abs(exit_px - entry) / entry > 0.0005 and abs(net) < 1e-6:
        return False
    return True


class EquityTracker:
    def __init__(self, client, journal, state, risk, data_dir, executor=None):
        self.c, self.j, self.s, self.risk = client, journal, state, risk
        self.ex = executor
        self.dir = data_dir
        self.known = set(self.s.d["open_meta"].keys())
        self._last_live = None
        self._synced_once = False
        self.run_opens = []
        self.run_closes = []
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
        meta = {
            "direction": plan["direction"], "qty": qty, "entry": px, "sl": plan["sl"],
            "tp": plan["tp"], "opened_at": datetime.now(BJ).isoformat(timespec="seconds"),
            "opened_ts": now, "open_ms": int(now * 1000), "score": plan.get("score")}
        for key in ("family", "regime", "mom_score", "oi_score", "combined", "atr", "atr_pct",
                    "min_hold_sec", "max_hold_sec", "sl_pct", "tp_pct", "tag"):
            if plan.get(key) is not None:
                meta[key] = plan[key]
        self.s.d["open_meta"][inst] = meta
        if plan.get("reentry_bar_ts"):
            self.s.d.setdefault("last_entry_bar", {})[inst] = {
                "direction": plan["direction"], "bar_ts": int(plan["reentry_bar_ts"])}
        if fill.get("close_reason"):
            self.s.d["open_meta"][inst]["close_reason"] = fill["close_reason"]
        self.known.add(inst)
        self.run_opens.append({
            "inst": inst, "direction": plan.get("direction"), "score": plan.get("score")})
        self.s.save()
        return True

    def sync(self, positions):
        # 这一进程第一次对账时，账本里已经不在交易所的仓是上一轮留下的，不是本轮新平的。
        startup = not self._synced_once
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
                self._on_closed(inst, historical="cleanup" if startup else None)
            except Exception as e:
                self.j.log(AGENT, "close_sync_error", f"{inst} 平仓记账失败，下轮重试: {e}", inst=inst)
        for p in positions:                       # 程序重启后接管已有仓位；逐个做，一笔失败不挡后面的
            try:
                self._adopt_if_needed(p)
            except Exception as e:
                self.j.log(AGENT, "adopt_error", f"{p.get('instId')} 接管失败，下轮重试: {e}",
                           inst=p.get("instId"))
        if config.is_swing():
            book = self.s.d.get("open_meta") or {}
            upl = 0.0
            for p in positions:
                if p.get("instId") not in book:
                    continue
                try:
                    upl += float(p.get("upl") or 0)
                except (TypeError, ValueError):
                    pass
            self.s.d["swing_last_our_upl"] = upl
        self._synced_once = True

    def _adopt_if_needed(self, p):
        if p["instId"] in self.known:
            return
        # 波段只接管账本里已经记下的仓，或对账时按标签写进 open_meta 的仓。
        # 同一模拟盘上的剥头皮仓没有 swmom/swoi 标签，不能按 15 分钟规则去管，也不能被波段提前平掉。
        if config.is_swing() and p["instId"] not in (self.s.d.get("open_meta") or {}):
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
        if isinstance(protected, tuple) and len(protected) == 2:
            meta["sl"], meta["tp"] = protected
        self.j.log(AGENT, "adopt", f"发现已有仓位 {p['instId']} {p['side']} {p['qty']}张，纳入监控",
                   inst=p["instId"])
        self.s.save()

    def _on_closed(self, inst, historical=None):
        def log(agent, event, msg, **fields):
            if historical:
                fields["historical"] = historical
            self.j.log(agent, event, msg, **fields)

        meta = self.s.d["open_meta"].pop(inst, {})
        self.known.discard(inst)
        if float(meta.get("qty") or 0) <= 0:
            log(AGENT, "skip_zero_close", f"{inst} 记录张数为 0，不写 trade_closed", inst=inst)
            self.s.save()
            return
        from .risk import note_exit_trend_bar
        note_exit_trend_bar(self.s, inst)
        res = None
        how = ""
        read_failed = False
        open_ms = int(meta.get("open_ms") or 0)
        history_fn = getattr(self.c, "closed_history", None)
        for _ in range(1 if self.c.is_mock else 8):
            rows = None
            if history_fn:
                try:
                    rows = history_fn(inst) or []
                    read_failed = False
                except Exception as e:
                    read_failed = True
                    rows = None
                    log(AGENT, "pnl_read_fail",
                        f"{inst} 平仓历史读取失败，不能把失败或其中一条当成完整盈亏: {e}",
                        inst=inst)
            if read_failed:
                how = "read_failed"
                res = None
                break
            if rows:
                chosen, how = choose_close_rows(
                    rows, open_ms, meta.get("qty"), meta.get("pos_id"))
                res = combine_close_rows(chosen) if chosen else None
            elif history_fn:
                how = "none"
                res = None
            else:
                how = "last_closed"
                try:
                    res = self.c.last_closed(inst)
                except Exception:
                    res = None
            if how == "latest_unverified":
                break
            if res and int(res.get("uTime") or 0) >= open_ms - 30000 and _pnl_row_trusted(res, meta):
                break
            res = None
            if not self.c.is_mock:
                time.sleep(1)
        unverified = how in ("latest_unverified", "read_failed")
        candidate = None
        if unverified and res:
            candidate = res.get("realizedPnl")
            trace = {
                "uTime": res.get("uTime"), "posId": (res.get("posId") if isinstance(res, dict) else ""),
                "closeQty": res.get("closeQty"), "pnl_source": how,
            }
            log(AGENT, "pnl_unverified",
                f"{inst} 平仓历史不能唯一对上本次持仓（{how}）。"
                f"候选净盈亏 {candidate} 不计入虚拟权益，也不能当成最终准确值",
                inst=inst, pnl_unverified=True, candidate_net=candidate, **trace)
            res = None
        elif how == "latest_unverified":
            log(AGENT, "pnl_unverified",
                f"{inst} 平仓历史不能唯一对上本次持仓，不计入虚拟权益",
                inst=inst, pnl_unverified=True, pnl_source=how, candidate_net=candidate)
        if how in ("matched", "summed") and res and float(res.get("closeQty") or 0) > 0:
            book_qty = float(meta.get("qty") or 0)
            exch_qty = float(res["closeQty"])
            if book_qty <= 0 or abs(book_qty - exch_qty) > max(1e-6, exch_qty * 1e-4):
                log(AGENT, "close_qty",
                    f"{inst} 平仓张数以交易所 closeTotalPos {exch_qty} 为准，账本原为 {book_qty}",
                    inst=inst, book_qty=book_qty, exch_qty=exch_qty, pnl_source=how)
                meta["qty"] = exch_qty
            open_px = float(res.get("openAvgPx") or 0)
            if open_px > 0:
                meta["entry"] = open_px
        est = None if unverified else self._estimate_net(inst, meta)
        if unverified:
            net = None
            reason = "盈亏未验证，不计入虚拟权益"
        elif res and _pnl_row_trusted(res, meta):
            net = float(res.get("realizedPnl") or 0)
            reason = meta.get("close_reason") or (res.get("why") if res and res.get("why") else "交易所止盈/止损")
        elif est is not None:
            net, gross, fee, exit_px = est
            res = {"realizedPnl": net, "pnl": gross, "fee": fee, "fundingFee": 0.0,
                   "closeAvgPx": exit_px, "why": "估算"}
            reason = (meta.get("close_reason") or "平仓") + "（交易所盈亏未对齐，按成交价估算）"
            log(AGENT, "pnl_estimated", f"{inst} 交易所盈亏不可用或为 0，按价格估算净盈亏 {net:+.2f}",
                inst=inst, est_net=net)
        else:
            net = 0.0
            reason = meta.get("close_reason") or "交易所止盈/止损"
            log(AGENT, "pnl_unknown", f"{inst} 无法取得平仓盈亏，记 0 并保留原因 {reason}", inst=inst)
        if meta.get("family"):
            reason = f"{reason} [{meta.get('family')}/{meta.get('regime') or '-'}]"
        try:
            eq = self.c.equity()
        except Exception:
            eq = None
        row = {"time": datetime.now(BJ).isoformat(timespec="seconds"), "instId": inst,
               "direction": meta.get("direction"), "qty": meta.get("qty"), "entry": meta.get("entry"),
               "exit": res.get("closeAvgPx") if res else None, "gross_pnl": res.get("pnl") if res else None,
               "fee": res.get("fee") if res else None, "funding": res.get("fundingFee") if res else None,
               "net_pnl": "" if net is None else round(net, 4),
               "est_net": meta.get("est_net"), "reason": reason,
               "equity_after": round(eq, 4) if eq is not None else None}
        if meta.get("family"):
            row.update(family=meta.get("family"), regime=meta.get("regime"),
                       combined=meta.get("combined"))
        self._append_trade(row)
        shown = "未验证" if net is None else f"{net:+.2f}"
        if not historical:
            self.run_closes.append({
                "inst": inst, "direction": meta.get("direction"),
                "score": meta.get("score"), "net": net})
        log(AGENT, "trade_closed", f"{inst} 平仓（{reason}）净盈亏 {shown} USDT，权益 "
            f"{eq if eq is None else round(eq, 2)}", **row, pnl_source=how or "",
            pnl_unverified=bool(unverified), candidate_net=candidate)
        self.risk.on_trade_closed(inst, net)
        self.snapshot(eq)

    def _append_trade(self, row):
        """已有表头时按旧列写入，避免给剥头皮账本硬加列；新文件带上波段列。"""
        new = not os.path.exists(self.trades_csv)
        if not new:
            with open(self.trades_csv, encoding="utf-8") as f:
                header = next(csv.reader(f), [])
            if header:
                with open(self.trades_csv, "a", newline="", encoding="utf-8") as f:
                    csv.DictWriter(f, fieldnames=header, extrasaction="ignore").writerow(
                        {k: row.get(k) for k in header})
                return
        with open(self.trades_csv, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row))
            w.writeheader()
            w.writerow(row)

    def _estimate_net(self, inst, meta):
        entry = float(meta.get("entry") or 0)
        qty = float(meta.get("qty") or 0)
        if entry <= 0 or qty <= 0:
            return None
        try:
            t = self.c.ticker(inst)
            exit_px = t["bid"] if meta.get("direction") == "long" else t["ask"]
            ct = float(self.c.instrument(inst)["ctVal"])
        except Exception:
            return None
        if not exit_px or ct <= 0:
            return None
        sign = 1.0 if meta.get("direction") == "long" else -1.0
        gross = (float(exit_px) - entry) * qty * ct * sign
        fee = (entry + float(exit_px)) * qty * ct * config.DEFAULT_TAKER_FEE
        return gross - fee, gross, -fee, float(exit_px)

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


def offline_self_check():
    """交易所把一笔有价差的平仓记成 0 时，不能照抄 +0.00。"""
    import tempfile
    from agents.journal import Journal
    from agents.state import SharedState

    class _C:
        is_mock = True

        def last_closed(self, inst):
            return {"realizedPnl": 0, "pnl": 0, "fee": 0, "fundingFee": 0,
                    "closeAvgPx": 110, "uTime": 10**13, "why": "SL"}

        def ticker(self, inst):
            return {"bid": 110.0, "ask": 110.1, "last": 110.0}

        def instrument(self, inst):
            return {"ctVal": 1.0}

        def equity(self):
            return 9900.0

    class _Risk:
        def __init__(self):
            self.net = None

        def on_trade_closed(self, inst, net):
            self.net = net

    d = tempfile.mkdtemp()
    j = Journal(d, echo=False)
    try:
        st = SharedState(d, clock=lambda: 1000)
        risk = _Risk()
        eq = EquityTracker(_C(), j, st, risk, d)
        st.d["open_meta"]["BTC-USDT-SWAP"] = {
            "direction": "long", "qty": 1, "entry": 100, "open_ms": 1000}
        eq.known.add("BTC-USDT-SWAP")
        eq._on_closed("BTC-USDT-SWAP")
        assert risk.net is not None and abs(risk.net) > 1, risk.net
        rows = list(csv.DictReader(open(eq.trades_csv, encoding="utf-8")))
        assert len(rows) == 1
        assert abs(float(rows[0]["net_pnl"])) > 1
        assert float(rows[0]["qty"]) > 0
    finally:
        j.close()
    _startup_cleanup_self_check()


def _startup_cleanup_self_check():
    """进程第一次对账补记的旧仓不算本轮平仓，日志要标成历史清理。之后本轮开平才计入。"""
    import tempfile
    from agents.journal import Journal
    from agents.state import SharedState

    d = tempfile.mkdtemp(prefix="ma-eq-startup-")
    j = Journal(d, echo=False)

    class C:
        is_mock = True

        def equity(self):
            return 10000.0

        def last_closed(self, inst):
            return {"realizedPnl": 1.5, "pnl": 2, "fee": -0.5, "fundingFee": 0,
                    "closeAvgPx": 101, "uTime": 10**13, "why": "超时平仓"}

        def ticker(self, inst):
            return {"bid": 101.0, "ask": 101.1, "last": 101.0}

        def instrument(self, inst):
            return {"ctVal": 0.01}

    class Risk:
        def on_trade_closed(self, inst, net):
            return None

    try:
        st = SharedState(d, clock=lambda: 1000)
        st.d["open_meta"]["ETH-USDT-SWAP"] = {
            "direction": "long", "qty": 1, "entry": 100, "score": 0.7,
            "open_ms": 1000, "opened_ts": 1}
        eq = EquityTracker(C(), j, st, Risk(), d)
        eq.sync([])
        assert eq.run_opens == [] and eq.run_closes == []
        text = open(j.jsonl.name, encoding="utf-8").read()
        assert "【历史清理】" in text and "ETH-USDT-SWAP" in text and "超时平仓" in text
        eq.register_open(
            {"instId": "SOL-USDT-SWAP", "direction": "short", "score": 0.8, "sl": 1, "tp": 2},
            {"qty": 2, "fill_px": 100})
        assert eq.run_opens == [{"inst": "SOL-USDT-SWAP", "direction": "short", "score": 0.8}]
        eq.sync([])
        assert len(eq.run_closes) == 1
        assert eq.run_closes[0]["inst"] == "SOL-USDT-SWAP"
        assert eq.run_closes[0]["direction"] == "short"
        assert eq.run_closes[0]["score"] == 0.8
        assert abs(eq.run_closes[0]["net"] - 1.5) < 1e-6
        rows = [json.loads(line) for line in open(j.jsonl.name, encoding="utf-8") if line.strip()]
        sol = [r for r in rows if r.get("event") == "trade_closed" and "SOL-USDT-SWAP" in r.get("msg", "")]
        assert sol and not sol[-1]["msg"].startswith("【历史"), sol[-1]["msg"]
    finally:
        j.close()
