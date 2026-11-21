"""波段部分成交、重启对账和 OCO 回读的离线回归。不联网、不下单。"""
import csv
import tempfile

import config
from okx_client import OkxError


def _journal():
    from agents.journal import Journal
    return Journal(tempfile.mkdtemp(), echo=False)


def _events(j):
    return open(j.jsonl.name, encoding="utf-8").read()


def _event_count(text, name):
    import json
    return sum(1 for line in text.splitlines() if line.strip() and json.loads(line).get("event") == name)


def _await_cases():
    from agents.executor import Executor

    class _Partial:
        is_mock = True

        def __init__(self, after):
            self.after = after
            self.cancelled = False
            self.cancel_error = None

        def get_order(self, inst, oid):
            if self.cancelled:
                return self.after
            return {"state": "partially_filled", "avgPx": "109.29", "accFillSz": "45.4"}

        def cancel_order(self, inst, oid):
            if self.cancel_error:
                raise self.cancel_error
            self.cancelled = True
            return {}

        def positions(self):
            return []

    saved = config.STRATEGY
    j = _journal()
    try:
        config.STRATEGY = "scalp"
        scalp = _Partial({"state": "canceled", "avgPx": "109.29", "accFillSz": "229.9"})
        ex = Executor(scalp, j)
        px, qty = ex._await_fill("AAVE-USDT-SWAP", "oid")
        assert (px, qty) == (109.29, 45.4), (px, qty)
        assert scalp.cancelled is False

        config.STRATEGY = "swing_combo"
        swing = _Partial({"state": "canceled", "avgPx": "109.29", "accFillSz": "229.9"})
        ex = Executor(swing, j)
        px, qty = ex._await_fill("AAVE-USDT-SWAP", "oid")
        assert swing.cancelled is True
        assert qty == 229.9 and abs(px - 109.29) < 1e-9, (px, qty)

        leftover = _Partial({"state": "canceled", "avgPx": "109.29", "accFillSz": "45.4"})
        ex = Executor(leftover, j)
        px, qty = ex._await_fill("AAVE-USDT-SWAP", "oid")
        assert leftover.cancelled is True
        assert qty == 45.4

        class _Zero(_Partial):
            def get_order(self, inst, oid):
                return {"state": "canceled" if self.cancelled else "live", "avgPx": "", "accFillSz": "0"}

        zero = _Zero({})
        ex = Executor(zero, j)
        assert ex._await_fill("AAVE-USDT-SWAP", "oid") == (None, 0.0)
        assert zero.cancelled is True

        stuck = _Partial({"state": "live", "avgPx": "109.29", "accFillSz": "45.4"})
        stuck.cancel_error = OkxError("50011", "busy", smsg="Too Many Requests")
        ex = Executor(stuck, j)
        px, qty = ex._await_fill("AAVE-USDT-SWAP", "oid")
        assert qty == 45.4 and stuck.cancelled is False
        text = _events(j)
        assert "residual_live" in text and "不另下新仓" in text
        assert "sCode=50011" in text or "Too Many Requests" in text
    finally:
        config.STRATEGY = saved
        j.close()


def _reconcile_cases():
    from agents.executor import Executor
    from agents.state import SharedState
    from agents.swing import reconcile

    class _Ex:
        def __init__(self):
            self.pos = [{"instId": "AAVE-USDT-SWAP", "side": "short", "qty": 45.4, "avgPx": 109.29,
                         "upl": 0.05, "cTime": 1795012049759, "posSide": "net", "markPx": 109.2}]
            self.orders = [{"instId": "AAVE-USDT-SWAP", "ordId": "rest-1", "ordType": "post_only",
                            "side": "sell", "posSide": "net", "sz": "184.5", "px": "109.29",
                            "state": "live", "reduceOnly": "false", "accFillSz": "45.4", "tag": "swmom"}]
            self.algos = [{"algoId": "sl-only", "instId": "AAVE-USDT-SWAP", "ordType": "conditional",
                           "posSide": "net", "slTriggerPx": "116.79", "tpTriggerPx": "",
                           "closeFraction": "", "sz": "45.4"}]
            self.placed = []
            self.cancelled = []
            self.entry_orders = []

        def positions(self):
            return [dict(p) for p in self.pos]

        def equity(self):
            return 102000.0

        def open_orders(self, inst):
            return [dict(o) for o in self.orders if o.get("instId") == inst]

        def pending_orders(self):
            return [dict(o) for o in self.orders]

        def cancel_order(self, inst, oid):
            self.cancelled.append(oid)
            self.orders = [o for o in self.orders if o.get("ordId") != oid]
            self.pos[0]["qty"] = 229.9
            self.pos[0]["avgPx"] = 109.29
            return {}

        def pending_algos(self, inst):
            return [dict(a) for a in self.algos]

        def cancel_algos(self, inst, ids):
            gone = {str(i) for i in ids or []}
            self.algos = [a for a in self.algos if str(a.get("algoId")) not in gone]
            return []

        def place_sl_algo(self, inst, side, pos_side, sl, tp=None):
            self.placed.append(None if tp is None else float(tp))
            self.algos.append({"algoId": "oco-1", "instId": inst, "ordType": "oco" if tp else "conditional",
                               "posSide": pos_side, "side": side, "slTriggerPx": str(sl),
                               "tpTriggerPx": str(tp or ""), "closeFraction": "1", "sz": ""})
            return {"algoId": "oco-1", "sCode": "0"}

        def place_order(self, body):
            self.entry_orders.append(body)
            raise AssertionError("对账不能开新仓或反向仓")

        def recent_closed(self, limit=100):
            return []

    saved = config.STRATEGY
    j = _journal()
    try:
        config.STRATEGY = "swing_combo"
        d = tempfile.mkdtemp()
        st = SharedState(d, clock=lambda: 1_700_000_000.0)
        st.d["open_meta"] = {"AAVE-USDT-SWAP": {
            "direction": "short", "qty": 45.4, "entry": 109.29, "sl": 116.79, "tp": 104.29,
            "family": "momentum", "opened_ts": 10, "open_ms": 10000}}
        book = _Ex()
        ex = Executor(book, j)
        reconcile(book, st, j, ex)
        meta = st.d["open_meta"]["AAVE-USDT-SWAP"]
        assert meta["qty"] == 229.9, meta
        assert abs(float(meta["entry"]) - 109.29) < 1e-9
        assert book.cancelled == ["rest-1"]
        assert book.placed == [104.29], book.placed
        assert book.entry_orders == []
        text = _events(j)
        assert _event_count(text, "qty_sync") == 1
        assert "算法单" in text and "普通挂单" in text
        assert "algoId=oco-1" in text and "tpTriggerPx=104.29" in text
        assert "slTriggerPx=116.79" in text and "closeFraction=1 全平" in text
        assert "ordId=rest-1" in text and "post_only" in text

        reconcile(book, st, j, ex)
        assert st.d["open_meta"]["AAVE-USDT-SWAP"]["qty"] == 229.9
        assert book.placed == [104.29]
        assert _event_count(_events(j), "qty_sync") == 1
        assert book.entry_orders == []

        # 残单撤掉后持仓不变：账本保持已成交张数，不记一笔新开仓。
        d2 = tempfile.mkdtemp()
        st2 = SharedState(d2, clock=lambda: 1_700_000_100.0)
        st2.d["open_meta"] = {"ALGO-USDT-SWAP": {
            "direction": "long", "qty": 866.0, "entry": 0.0773, "sl": 0.0744, "tp": 0.0793,
            "family": "momentum", "opened_ts": 10, "open_ms": 10000}}
        quiet = _Ex()
        quiet.pos = [{"instId": "ALGO-USDT-SWAP", "side": "long", "qty": 866.0, "avgPx": 0.0773,
                      "upl": 0.0, "cTime": 1000, "posSide": "net"}]
        quiet.orders = [{"instId": "ALGO-USDT-SWAP", "ordId": "rest-0", "ordType": "post_only",
                         "side": "buy", "posSide": "net", "sz": "2397", "px": "0.0773",
                         "state": "live", "reduceOnly": "false", "accFillSz": "0", "tag": "swmom"}]
        quiet.algos = [{"algoId": "oco-keep", "instId": "ALGO-USDT-SWAP", "ordType": "oco",
                        "posSide": "net", "slTriggerPx": "0.0744", "tpTriggerPx": "0.0793",
                        "closeFraction": "1", "sz": ""}]

        def cancel_without_fill(inst, oid):
            quiet.cancelled.append(oid)
            quiet.orders = []
            return {}

        quiet.cancel_order = cancel_without_fill
        reconcile(quiet, st2, j, Executor(quiet, j))
        assert st2.d["open_meta"]["ALGO-USDT-SWAP"]["qty"] == 866.0
        assert quiet.cancelled == ["rest-0"]
        assert quiet.placed == []
    finally:
        config.STRATEGY = saved
        j.close()


def _close_once_cases():
    from agents.equity import EquityTracker, choose_close_rows
    from agents.state import SharedState

    class _Hist:
        is_mock = True

        def __init__(self, rows):
            self.rows = rows

        def closed_history(self, inst, limit=100):
            return list(self.rows)

        def last_closed(self, inst):
            return self.rows[-1] if self.rows else None

        def equity(self):
            return 10000.0

    class _Risk:
        def __init__(self):
            self.nets = []

        def on_trade_closed(self, inst, net):
            self.nets.append(net)

    saved = config.STRATEGY
    j = _journal()
    try:
        config.STRATEGY = "swing_combo"
        open_ms = 1_000_000
        fragments = [
            {"uTime": open_ms + 1000, "pnl": 1.0, "realizedPnl": 0.8, "fee": -0.3, "fundingFee": 0.1,
             "closeAvgPx": 109.2, "openAvgPx": 109.29, "closeTotalPos": "100"},
            {"uTime": open_ms + 2000, "pnl": 2.3086, "realizedPnl": 2.0, "fee": -0.4, "fundingFee": 0.0914,
             "closeAvgPx": 109.1, "openAvgPx": 109.29, "closeTotalPos": "129.9"},
        ]
        chosen, how = choose_close_rows(fragments, open_ms, 45.4)
        assert how == "latest_unverified", how
        assert len(chosen) == 1
        chosen, how = choose_close_rows(fragments, open_ms, 229.9)
        assert how == "summed" and len(chosen) == 2

        d = tempfile.mkdtemp()
        st = SharedState(d, clock=lambda: 2_000.0)
        st.d["open_meta"]["AAVE-USDT-SWAP"] = {
            "direction": "short", "qty": 229.9, "entry": 109.29, "open_ms": open_ms, "family": "momentum"}
        risk = _Risk()
        eq = EquityTracker(_Hist(fragments), j, st, risk, d)
        eq.known.add("AAVE-USDT-SWAP")
        eq.sync([])
        eq.sync([])
        rows = list(csv.DictReader(open(eq.trades_csv, encoding="utf-8")))
        assert len(rows) == 1, rows
        assert abs(float(rows[0]["qty"]) - 229.9) < 1e-6
        assert abs(float(rows[0]["net_pnl"]) - 2.8) < 1e-6, rows[0]
        assert risk.nets == [2.8]
        assert _events(j).count("trade_closed") == 1

        bare = [
            {"uTime": open_ms + 1000, "pnl": 10, "realizedPnl": 10, "fee": 0, "fundingFee": 0,
             "closeAvgPx": 1, "openAvgPx": 1},
            {"uTime": open_ms + 3000, "pnl": 4, "realizedPnl": 4, "fee": 0, "fundingFee": 0,
             "closeAvgPx": 1.1, "openAvgPx": 1},
        ]
        chosen, how = choose_close_rows(bare, open_ms, 10)
        assert how == "latest_unverified"
        assert float(chosen[0]["realizedPnl"]) == 4
        d3 = tempfile.mkdtemp()
        st3 = SharedState(d3, clock=lambda: 3_000.0)
        st3.d["open_meta"]["ALGO-USDT-SWAP"] = {
            "direction": "long", "qty": 3263, "entry": 0.0773, "open_ms": open_ms}
        risk3 = _Risk()
        eq3 = EquityTracker(_Hist(bare), j, st3, risk3, d3)
        eq3.known.add("ALGO-USDT-SWAP")
        eq3.sync([])
        eq3.sync([])
        rows3 = list(csv.DictReader(open(eq3.trades_csv, encoding="utf-8")))
        assert len(rows3) == 1
        assert rows3[0]["net_pnl"] == ""
        assert "未验证" in rows3[0]["reason"]
        assert risk3.nets == [None]
        text = _events(j)
        assert "pnl_unverified" in text and "candidate_net" in text
        assert "不计入虚拟权益" in text
        day = st3.d.get("daily") or {}
        assert all(abs(float(v) - 4) > 1e-6 for v in day.values())
    finally:
        config.STRATEGY = saved
        j.close()


def _protection_cases():
    from agents.executor import Executor
    from agents.protection import is_full_protection

    class _Algo:
        def __init__(self, algos):
            self.algos = algos
            self.places = []
            self.cancels = []
            self.fail_read = 0
            self.fail_after_place = False
            self.block = False

        def pending_algos(self, inst):
            if self.fail_read:
                self.fail_read -= 1
                raise OkxError("50011", "limited", smsg="Too Many Requests")
            if self.fail_after_place and self.places:
                raise OkxError("50011", "limited", smsg="Too Many Requests")
            return [dict(a) for a in self.algos]

        def open_orders(self, inst):
            return []

        def cancel_algos(self, inst, ids):
            self.cancels.append(list(ids))
            gone = {str(i) for i in ids or []}
            self.algos = [a for a in self.algos if str(a.get("algoId")) not in gone]
            return []

        def place_sl_algo(self, inst, side, pos_side, sl, tp=None):
            if self.block:
                raise OkxError("51088", "exists", smsg="Algo order already exists")
            self.places.append((sl, tp))
            self.algos.append({"algoId": f"oco-{len(self.places)}", "instId": inst, "ordType": "oco" if tp else "conditional",
                               "posSide": pos_side, "side": side, "slTriggerPx": str(sl),
                               "tpTriggerPx": str(tp or ""), "closeFraction": "1", "sz": ""})
            return {"algoId": self.algos[-1]["algoId"], "sCode": "0"}

        def positions(self):
            return [{"instId": "AAVE-USDT-SWAP", "side": "short", "qty": 229.9, "posSide": "net"}]

        def close(self, *a, **k):
            raise AssertionError("合格止损还在时不能平仓")

    saved = config.STRATEGY
    j = _journal()
    try:
        config.STRATEGY = "swing_combo"
        missing_tp = _Algo([{"algoId": "sl-only", "instId": "AAVE-USDT-SWAP", "ordType": "conditional",
                             "posSide": "net", "slTriggerPx": "116.79", "tpTriggerPx": "",
                             "closeFraction": "", "sz": "45.4"}])
        ex = Executor(missing_tp, j)
        assert ex.confirm_protection("AAVE-USDT-SWAP", "short", 116.79, 104.29, 229.9, "net",
                                     reason="open") == "ok"
        assert missing_tp.places == [(116.79, 104.29)]
        assert ex.confirm_protection("AAVE-USDT-SWAP", "short", 116.79, 104.29, 229.9, "net",
                                     reason="audit") == "ok"
        assert missing_tp.places == [(116.79, 104.29)]
        text = _events(j)
        assert "algoId=oco-1" in text and "tpTriggerPx=104.29" in text
        assert "slTriggerPx=116.79" in text and "closeFraction=1 全平" in text
        assert "posSide=net" in text

        short = _Algo([{"algoId": "small", "instId": "AAVE-USDT-SWAP", "ordType": "conditional",
                        "posSide": "net", "slTriggerPx": "116.79", "tpTriggerPx": "104.29",
                        "closeFraction": "", "sz": "45.4"}])
        ex = Executor(short, j)
        assert ex.confirm_protection("AAVE-USDT-SWAP", "short", 116.79, 104.29, 229.9, "net",
                                     reason="qty_sync") == "ok"
        assert len(short.places) == 1
        assert any(str(a.get("closeFraction")) == "1" for a in short.algos)
        assert ex.confirm_protection("AAVE-USDT-SWAP", "short", 116.79, 104.29, 229.9, "net",
                                     reason="audit") == "ok"
        assert len(short.places) == 1

        full = _Algo([{"algoId": "full-1", "instId": "AAVE-USDT-SWAP", "ordType": "oco",
                       "posSide": "net", "slTriggerPx": "116.79", "tpTriggerPx": "104.29",
                       "closeFraction": "1", "sz": ""}])
        ex = Executor(full, j)
        assert ex.confirm_protection("AAVE-USDT-SWAP", "short", 116.79, 104.29, 229.9, "net",
                                     reason="startup") == "ok"
        assert full.places == [] and full.cancels == []

        down = _Algo([])
        down.fail_read = 1
        ex = Executor(down, j)
        assert ex.confirm_protection("AAVE-USDT-SWAP", "short", 116.79, 104.29, 229.9, "net",
                                     reason="startup") == "unconfirmed"
        assert down.places == [] and down.cancels == []
        assert "不能当成保护单不存在" in _events(j)
        assert "sCode=50011" in _events(j) and "Too Many Requests" in _events(j)

        raced = _Algo([])
        raced.fail_after_place = True
        ex = Executor(raced, j)
        assert ex.confirm_protection("AAVE-USDT-SWAP", "short", 116.79, 104.29, 229.9, "net",
                                     reason="open") == "unconfirmed"
        assert len(raced.places) == 1 and raced.cancels == []
        assert "不能当成没挂上" in _events(j)

        # 成交价接近止盈，不能代替回读到 tpTriggerPx。
        assert is_full_protection(
            {"slTriggerPx": "0.6902", "tpTriggerPx": "", "closeFraction": "1"}, 3566, True) is False
    finally:
        config.STRATEGY = saved
        j.close()


def _amend_cases():
    from agents.executor import Executor

    class _Amend:
        def __init__(self, drop_tp=False):
            self.drop_tp = drop_tp
            self.algo = {"algoId": "sui-1", "instId": "SUI-USDT-SWAP", "ordType": "oco",
                         "posSide": "net", "slTriggerPx": "0.63146", "tpTriggerPx": "0.76169",
                         "closeFraction": "1", "sz": ""}
            self.places = []
            self.amended = []

        def pending_algos(self, inst):
            return [dict(self.algo)]

        def amend_sl(self, inst, algo_id, new_sl):
            self.amended.append(new_sl)
            self.algo["slTriggerPx"] = str(new_sl)
            if self.drop_tp:
                self.algo["tpTriggerPx"] = ""
                self.algo["closeFraction"] = ""
                self.algo["sz"] = "3566"
            return {}

        def positions(self):
            return [{"instId": "SUI-USDT-SWAP", "side": "long", "qty": 3566, "posSide": "net",
                     "avgPx": 0.7096}]

        def open_orders(self, inst):
            return []

        def cancel_algos(self, inst, ids):
            gone = {str(i) for i in ids or []}
            if str(self.algo.get("algoId")) in gone:
                self.algo = {}
            return []

        def place_sl_algo(self, inst, side, pos_side, sl, tp=None):
            self.places.append((sl, tp))
            self.algo = {"algoId": "sui-2", "instId": inst, "ordType": "oco", "posSide": pos_side,
                         "slTriggerPx": str(sl), "tpTriggerPx": str(tp or ""),
                         "closeFraction": "1", "sz": ""}
            return {"algoId": "sui-2"}

    saved = config.STRATEGY
    j = _journal()
    try:
        config.STRATEGY = "swing_combo"
        book = _Amend()
        ex = Executor(book, j)
        assert ex.amend_sl("SUI-USDT-SWAP", 0.6902) is True
        assert book.amended == [0.6902]
        assert book.places == []
        text = _events(j)
        assert "algoId=sui-1" in text and "tpTriggerPx=0.76169" in text
        assert "slTriggerPx=0.6902" in text and "closeFraction=1 全平" in text

        dropped = _Amend(drop_tp=True)
        ex = Executor(dropped, j)
        assert ex.amend_sl("SUI-USDT-SWAP", 0.6902) is True
        assert dropped.places == [(0.6902, "0.76169")] or dropped.places == [(0.6902, 0.76169)]
        assert any(p[1] not in (None, "", "None") for p in dropped.places)

        config.STRATEGY = "scalp"
        scalp = _Amend(drop_tp=True)
        ex = Executor(scalp, j)
        assert ex.amend_sl("SUI-USDT-SWAP", 0.6902) is True
        assert scalp.places == []
    finally:
        config.STRATEGY = saved
        j.close()


def _round2_cases():
    """第二轮：残单留存、未知覆盖不补单、未验证盈亏、止损不被放宽、参数不变。"""
    from agents.executor import Executor
    from agents.protection import algo_active, is_full_protection, tighter_sl
    from agents.state import SharedState
    from agents.swing import note_residual, read_positions, reconcile, residual_blocks, settle_entry_residuals
    from okx_client import page_all, protective_algo_body
    import main as main_mod

    saved = config.STRATEGY
    j = _journal()
    try:
        config.STRATEGY = "swing_combo"
        assert config.MAKER_ENTRY_TIMEOUT_SEC == 25
        assert config.SWING_ENTRY_THRESHOLD == 0.65
        assert float(config.SWING_CAPITAL_USDT) == 10000
        assert config.SWING_MAX_SPREAD_BPS == 20
        assert config.SWING_MIN_VOL_USDT_24H == 2000000
        assert config.SWING_MAX_MARK_INDEX_BPS == 100

        net = protective_algo_body("AAVE-USDT-SWAP", "buy", "net", 116.79, 104.29, "cross")
        assert net["ordType"] == "oco" and net["closeFraction"] == "1" and net["reduceOnly"] is True
        assert net["posSide"] == "net" and net["side"] == "buy" and "sz" not in net
        hedge = protective_algo_body("AAVE-USDT-SWAP", "sell", "long", 100, 110, "cross")
        assert hedge["posSide"] == "long" and hedge["side"] == "sell" and hedge["reduceOnly"] is True
        assert tighter_sl("short", 116.79, 112.14) == 112.14
        assert tighter_sl("long", 0.63, 0.69) == 0.69
        assert algo_active({"state": "pause", "slTriggerPx": "1"}) is False
        assert is_full_protection(
            {"state": "live", "side": "buy", "posSide": "net", "slTriggerPx": "1",
             "tpTriggerPx": "2", "closeFraction": "1"}, 10, True, "short", "net")
        assert is_full_protection(
            {"state": "live", "side": "sell", "posSide": "net", "slTriggerPx": "1",
             "tpTriggerPx": "2", "closeFraction": "1"}, 10, True, "short", "net") is False

        pages = {"n": 0}

        def fetch(after):
            pages["n"] += 1
            if pages["n"] == 1:
                return [{"algoId": str(i)} for i in range(100)]
            raise OkxError("50011", "page", smsg="Too Many Requests")

        try:
            page_all(fetch, lambda r: r["algoId"])
            raise AssertionError("后一页失败不能把第一页当成全部")
        except OkxError as e:
            assert e.code == "50011"

        def fetch_ok(after):
            if after:
                return [{"algoId": "b"}]
            return [{"algoId": str(i)} for i in range(100)]

        got = page_all(fetch_ok, lambda r: r["algoId"])
        assert len(got) == 101 and got[-1]["algoId"] == "b"

        class _Gate:
            def __init__(self, algos):
                self.algos = algos
                self.places = []
                self.cancels = []

            def pending_algos(self, inst):
                return [dict(a) for a in self.algos if a]

            def open_orders(self, inst):
                return []

            def cancel_algos(self, inst, ids):
                self.cancels.append(list(ids))
                gone = {str(i) for i in ids or []}
                self.algos = [a for a in self.algos if str(a.get("algoId")) not in gone]
                return [{"sCode": "0", "sMsg": ""}]

            def place_sl_algo(self, inst, side, pos_side, sl, tp=None):
                self.places.append((sl, tp, side, pos_side))
                self.algos.append({"algoId": f"oco-{len(self.places)}", "instId": inst,
                                   "ordType": "oco" if tp else "conditional", "posSide": pos_side,
                                   "side": side, "state": "live", "slTriggerPx": str(sl),
                                   "tpTriggerPx": str(tp or ""), "closeFraction": "1", "sz": ""})
                return {"algoId": self.algos[-1]["algoId"], "sCode": "0"}

            def positions(self):
                return [{"instId": "AAVE-USDT-SWAP", "side": "short", "qty": 229.9, "posSide": "net"}]

        unknown = _Gate([{"algoId": "u1", "instId": "AAVE-USDT-SWAP", "ordType": "oco",
                          "posSide": "net", "side": "buy", "state": "live",
                          "slTriggerPx": "112.14", "tpTriggerPx": "104.29",
                          "closeFraction": "", "sz": ""}])
        ex = Executor(unknown, j)
        assert ex.confirm_protection("AAVE-USDT-SWAP", "short", 116.79, 104.29, 229.9, "net",
                                     reason="startup") == "ok"
        assert ex.confirm_protection("AAVE-USDT-SWAP", "short", 116.79, 104.29, 229.9, "net",
                                     reason="open") == "ok"
        assert unknown.places == []

        short = _Gate([{"algoId": "tight", "instId": "AAVE-USDT-SWAP", "ordType": "conditional",
                        "posSide": "net", "side": "buy", "state": "live",
                        "slTriggerPx": "112.14", "tpTriggerPx": "",
                        "closeFraction": "", "sz": "45.4"}])
        ex = Executor(short, j)
        assert ex.confirm_protection("AAVE-USDT-SWAP", "short", 116.79, 104.29, 229.9, "net",
                                     reason="startup") == "ok"
        assert short.places and short.places[0][0] == 112.14

        class _Dup(_Gate):
            def __init__(self):
                super().__init__([
                    {"algoId": "a", "instId": "AAVE-USDT-SWAP", "ordType": "oco", "posSide": "net",
                     "side": "buy", "state": "live", "slTriggerPx": "112", "tpTriggerPx": "104",
                     "closeFraction": "1", "sz": ""},
                    {"algoId": "b", "instId": "AAVE-USDT-SWAP", "ordType": "oco", "posSide": "net",
                     "side": "buy", "state": "live", "slTriggerPx": "112", "tpTriggerPx": "104",
                     "closeFraction": "1", "sz": ""},
                ])
                self.fail_cancel = True

            def cancel_algos(self, inst, ids):
                self.cancels.append(list(ids))
                if self.fail_cancel:
                    return [{"sCode": "50001", "sMsg": "cancel rejected"}]
                return [{"sCode": "0", "sMsg": ""}]

        dup = _Dup()
        ex = Executor(dup, j)
        assert ex.confirm_protection("AAVE-USDT-SWAP", "short", 112, 104, 229.9, "net",
                                     reason="audit") == "ok"
        assert dup.places == []
        assert dup.cancels
        assert ex.confirm_protection("AAVE-USDT-SWAP", "short", 112, 104, 229.9, "net",
                                     reason="audit") == "ok"
        assert dup.places == [] and len(dup.cancels) == 2
        assert "不再补挂" in _events(j) or "不再补第三张" in _events(j) or "下轮只重试撤销" in _events(j)

        d = tempfile.mkdtemp()
        st = SharedState(d, clock=lambda: 50.0)
        st.d["swing_last_our_upl"] = 31.5
        st.d["open_meta"] = {"AAVE-USDT-SWAP": {
            "direction": "short", "qty": 229.9, "entry": 109.29, "sl": 112.14, "tp": 104.29,
            "family": "momentum", "opened_ts": 10}}

        class _Down:
            def positions(self):
                raise OkxError("EMPTY", "timeout", smsg="read timeout")

            def equity(self):
                return 10000

        reconcile(_Down(), st, j, None)
        assert st.d["swing_last_our_upl"] == 31.5
        assert st.d["open_meta"]["AAVE-USDT-SWAP"]["qty"] == 229.9
        assert "不把失败当成空仓" in _events(j)

        class _Agents:
            def __init__(self):
                self.synced = False
                self.client = _Down()
                self.journal = j
                self.state = st

            class equity:
                @staticmethod
                def sync(*a, **k):
                    raise AssertionError("持仓回读失败不能去记账或平仓")

        assert main_mod.monitor_positions({"client": _Down(), "journal": j, "state": st,
                                           "equity": _Agents.equity, "executor": None,
                                           "net_tp": None}) is None

        note_residual(st, "AAVE-USDT-SWAP", "rest-9")
        assert residual_blocks(st, "AAVE-USDT-SWAP")

        class _Still:
            def __init__(self):
                self.pos = [{"instId": "AAVE-USDT-SWAP", "side": "short", "qty": 45.4,
                             "avgPx": 109.29, "upl": 1, "posSide": "net"}]
                self.orders = [{"instId": "AAVE-USDT-SWAP", "ordId": "rest-9", "ordType": "post_only",
                                "side": "sell", "posSide": "net", "sz": "184.5", "state": "live",
                                "reduceOnly": "false"}]
                self.cancels = []

            def positions(self):
                return list(self.pos)

            def open_orders(self, inst):
                return list(self.orders)

            def pending_orders(self):
                return list(self.orders)

            def cancel_order(self, inst, oid):
                self.cancels.append(oid)
                return {"sCode": "50001", "sMsg": "still live"}

        book = _Still()
        settle_entry_residuals(book, st, j, book.positions())
        assert residual_blocks(st, "AAVE-USDT-SWAP")
        assert book.cancels == ["rest-9"]
        settle_entry_residuals(book, st, j, book.positions())
        assert book.cancels == ["rest-9", "rest-9"]
        assert book.pos[0]["qty"] == 45.4

        class _Late(_Still):
            def cancel_order(self, inst, oid):
                self.cancels.append(oid)
                self.orders = []
                self.pos[0]["qty"] = 229.9
                self.pos[0]["avgPx"] = 109.1
                return {"sCode": "0", "sMsg": ""}

        late = _Late()
        note_residual(st, "AAVE-USDT-SWAP", "rest-9")
        out = settle_entry_residuals(late, st, j, late.positions())
        assert out[0]["qty"] == 229.9
        assert residual_blocks(st, "AAVE-USDT-SWAP") is False

        rows, err = read_positions(_Down())
        assert rows is None and err is not None
    finally:
        config.STRATEGY = saved
        j.close()


def _aave_evidence():
    """#34/#35 的快照和 #36 的平仓行对不上。不能从现有记录复原 AAVE 真实盈亏。"""
    ct = 0.1  # 公开 instruments：AAVE-USDT-SWAP ctVal=0.1
    entry = 109.29
    exit_px = 109.14608695652174
    gross = 0.0331
    full = (entry - exit_px) * 229.9 * ct
    partial = (entry - exit_px) * 45.4 * ct
    assert full > 3 and abs(full - gross) > 1
    assert abs(partial - gross) > 0.5
    implied = gross / ((entry - exit_px) * ct)
    assert abs(implied - 2.3) < 0.02
    # 账本没有逐笔成交，也没有 closeTotalPos。2.3 只是倒推，不能当成真实平仓张数。
    snapshots = (45.4, 229.9, 229.9)
    assert 229.9 in snapshots and 45.4 in snapshots
    assert "fills" not in {}


def offline_self_check():
    _await_cases()
    _reconcile_cases()
    _close_once_cases()
    _protection_cases()
    _amend_cases()
    _round2_cases()
    _aave_evidence()
