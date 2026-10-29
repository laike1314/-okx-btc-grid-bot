"""共享状态（连亏、冷却、当日盈亏、持仓元数据），落盘到 data/state.json，重启不丢。"""
import json
import os
import time
from datetime import datetime, timezone, timedelta

import config

BJ = timezone(timedelta(hours=8))


class SharedState:
    def __init__(self, data_dir, clock=None):
        self.clock = clock or time.time   # Mock 模式用模拟时钟，冷却按 K 线推进
        self.path = os.path.join(data_dir, "state.json")
        self.d = {"consecutive_losses": 0, "global_cooldown_until": 0, "symbol_cooldown": {},
                  "daily": {}, "open_meta": {}, "day_start_equity": {}, "pnl_events": [],
                  "last_entry_bar": {}}
        if os.path.exists(self.path):
            try:
                self.d.update(json.load(open(self.path, encoding="utf-8")))
            except Exception:
                pass

    def save(self):
        tmp = self.path + ".tmp"
        json.dump(self.d, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    @staticmethod
    def today():
        return datetime.now(BJ).strftime("%Y-%m-%d")

    def daily_pnl(self):
        """兼容旧调用。开仓闸门用 rolling_pnl，不再按北京时间自然日清零。"""
        return self.rolling_pnl()

    def add_daily_pnl(self, v):
        t = self.today()
        self.d.setdefault("daily", {})
        self.d["daily"][t] = self.d["daily"].get(t, 0.0) + v
        self.d.setdefault("pnl_events", []).append({"ts": self.now(), "net": float(v)})
        self._prune_pnl()

    def _prune_pnl(self):
        window = max(0, int(config.ROLLING_LOSS_WINDOW_SEC))
        cutoff = self.now() - window
        self.d["pnl_events"] = [e for e in self.d.get("pnl_events", [])
                                if float(e.get("ts") or 0) >= cutoff]

    def rolling_pnl(self):
        self._prune_pnl()
        return sum(float(e.get("net") or 0) for e in self.d.get("pnl_events", []))

    def loss_cap_hit(self, equity):
        """滚动窗口已实现盈亏触达参考权益的比例。参考权益 ≈ 当前权益 − 窗口内盈亏。"""
        pnl = self.rolling_pnl()
        ref = equity - pnl
        if ref <= 0:
            ref = abs(equity) or 1.0
        hit = pnl <= -abs(ref) * config.DAILY_LOSS_CAP_PCT
        return hit, pnl, ref

    def day_start_equity(self, eq):
        t = self.today()
        if t not in self.d["day_start_equity"]:
            self.d["day_start_equity"][t] = eq
            self.save()
        return self.d["day_start_equity"][t]

    def now(self):
        return self.clock()
