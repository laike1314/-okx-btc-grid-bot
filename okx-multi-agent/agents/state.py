"""共享状态（连亏、冷却、当日盈亏、持仓元数据），落盘到 data/state.json，重启不丢。"""
import json
import os
import time
from datetime import datetime, timezone, timedelta

BJ = timezone(timedelta(hours=8))


class SharedState:
    def __init__(self, data_dir, clock=None):
        self.clock = clock or time.time   # Mock 模式用模拟时钟，冷却按 K 线推进
        self.path = os.path.join(data_dir, "state.json")
        self.d = {"consecutive_losses": 0, "global_cooldown_until": 0, "symbol_cooldown": {},
                  "daily": {}, "open_meta": {}, "day_start_equity": {}}
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
        return self.d["daily"].get(self.today(), 0.0)

    def add_daily_pnl(self, v):
        t = self.today()
        self.d["daily"][t] = self.d["daily"].get(t, 0.0) + v

    def day_start_equity(self, eq):
        t = self.today()
        if t not in self.d["day_start_equity"]:
            self.d["day_start_equity"][t] = eq
            self.save()
        return self.d["day_start_equity"][t]

    def now(self):
        return self.clock()
