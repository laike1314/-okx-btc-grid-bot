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

AGENT = "executor"


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
        if (d == "long" and plan["sl"] >= px) or (d == "short" and plan["sl"] <= px):
            self.j.log(AGENT, "abort", f"{inst} 行情已越过止损价 {plan['sl']}，放弃开仓", inst=inst)
            return None
        body = {"instId": inst, "tdMode": config.TD_MODE, "side": "buy" if d == "long" else "sell",
                "posSide": self._pos_side(d), "ordType": "market", "sz": fmt_num(plan["qty"], spec["lotSz"]),
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
        fill_px, filled = px, plan["qty"]
        time.sleep(0 if self.c.is_mock else config.ORDER_FILL_WAIT_SEC)
        if ord_id:
            try:
                o = self.c.get_order(inst, ord_id)
                fill_px = float(o.get("avgPx") or px)
                filled = float(o.get("accFillSz") or filled)
            except OkxError as e:
                self.j.log(AGENT, "order_query", f"{inst} 查询订单失败: {e}", inst=inst)
        self.j.log(AGENT, "opened", f"{inst} 开{('多' if d == 'long' else '空')} {filled}张 @ {fill_px} "
                   f"SL {plan['sl']} TP {plan['tp']}", inst=inst, ordId=ord_id, fill_px=fill_px, qty=filled,
                   sl=plan["sl"], tp=plan["tp"])
        self.ensure_stop(inst, d, plan["sl"])
        return {"ordId": ord_id, "fill_px": fill_px, "qty": filled}

    def ensure_stop(self, inst, d, sl):
        try:
            algos = self.c.pending_algos(inst)
            if any(a.get("slTriggerPx") for a in algos):
                self.j.log(AGENT, "sl_ok", f"{inst} 交易所止损已在位", inst=inst)
                return True
            self.c.place_sl_algo(inst, "sell" if d == "long" else "buy", self._pos_side(d), sl)
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
            if e.code == "51304":
                self.j.log(AGENT, "sl_51304", f"{inst} 新止损 {new_sl} 已越过标记价，本次不改", inst=inst)
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
