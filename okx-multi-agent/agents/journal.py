"""Logger 代理：每个决策（含原因）写入 JSONL，同时输出一行人类可读日志。"""
import json
import os
import time
from datetime import datetime, timezone, timedelta

BJ = timezone(timedelta(hours=8))


class Journal:
    def __init__(self, data_dir, echo=True):
        os.makedirs(data_dir, exist_ok=True)
        self.echo = echo
        self.jsonl = open(os.path.join(data_dir, "decisions.jsonl"), "a", encoding="utf-8")
        self.human = open(os.path.join(data_dir, "bot.log"), "a", encoding="utf-8")

    def close(self):
        for fh in (self.jsonl, self.human):
            try:
                fh.close()
            except Exception:
                pass

    def log(self, agent, event, msg, **fields):
        now = datetime.now(BJ)
        rec = {"ts": now.isoformat(timespec="seconds"), "epoch": round(time.time(), 3),
               "agent": agent, "event": event, "msg": msg}
        rec.update({k: (round(v, 6) if isinstance(v, float) else v) for k, v in fields.items()})
        self.jsonl.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self.jsonl.flush()
        line = f"{now:%m-%d %H:%M:%S} [{agent:<8}] {event:<14} {msg}"
        if self.echo:
            print(line, flush=True)
        self.human.write(line + "\n")
        self.human.flush()
