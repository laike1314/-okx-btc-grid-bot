"""Logger 代理：每个决策（含原因）写入 JSONL，同时输出一行人类可读日志。"""
import json
import os
import time
from datetime import datetime, timezone, timedelta

BJ = timezone(timedelta(hours=8))

# 离线冒烟把整段 K 线在同一秒打完，迁移和启动对账也会在这一秒补记旧账。
# 前缀只加在展示文案上，方便和本轮实盘执行分开看，不删原文。
REPLAY_MARK = "【历史回放】"
KIND_MARK = {"migrate": "【历史迁移】", "cleanup": "【历史清理】"}


class Journal:
    def __init__(self, data_dir, echo=True, replay=False):
        os.makedirs(data_dir, exist_ok=True)
        self.echo = echo
        self.replay = bool(replay)
        self.jsonl = open(os.path.join(data_dir, "decisions.jsonl"), "a", encoding="utf-8")
        self.human = open(os.path.join(data_dir, "bot.log"), "a", encoding="utf-8")

    def close(self):
        for fh in (self.jsonl, self.human):
            try:
                fh.close()
            except Exception:
                pass

    def log(self, agent, event, msg, **fields):
        kind = fields.pop("historical", None)
        marks, tags = [], []
        if self.replay:
            marks.append(REPLAY_MARK)
            tags.append("replay")
        if kind in KIND_MARK:
            marks.append(KIND_MARK[kind])
            tags.append(kind)
        elif kind:
            marks.append("【历史】")
            tags.append(str(kind))
        shown = "".join(marks) + msg
        now = datetime.now(BJ)
        rec = {"ts": now.isoformat(timespec="seconds"), "epoch": round(time.time(), 3),
               "agent": agent, "event": event, "msg": shown}
        if tags:
            rec["historical"] = "+".join(tags)
        rec.update({k: (round(v, 6) if isinstance(v, float) else v) for k, v in fields.items()})
        self.jsonl.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self.jsonl.flush()
        line = f"{now:%m-%d %H:%M:%S} [{agent:<8}] {event:<14} {shown}"
        if self.echo:
            print(line, flush=True)
        self.human.write(line + "\n")
        self.human.flush()


def offline_self_check():
    """回放、迁移、旧仓清理要带历史前缀；本轮普通日志不带。"""
    import tempfile
    d = tempfile.mkdtemp(prefix="ma-journal-")
    live = Journal(d, echo=False)
    replay = Journal(tempfile.mkdtemp(prefix="ma-journal-replay-"), echo=False, replay=True)
    try:
        live.log("executor", "closed", "ETH-USDT-SWAP 已平仓：超时平仓")
        replay.log("executor", "closed", "ETH-USDT-SWAP 已平仓：超时平仓")
        replay.log("main", "summary", "结束：12 笔，胜 8 负 4，净盈亏 +54.62")
        live.log("equity", "capital_migrate", "波段改用固定本金", historical="migrate")
        live.log("equity", "trade_closed", "BTC-USDT-SWAP 平仓（超时平仓）", historical="cleanup")
        live_rows = [json.loads(line) for line in open(live.jsonl.name, encoding="utf-8") if line.strip()]
        rep_rows = [json.loads(line) for line in open(replay.jsonl.name, encoding="utf-8") if line.strip()]
        assert live_rows[0]["msg"] == "ETH-USDT-SWAP 已平仓：超时平仓", live_rows[0]
        assert "historical" not in live_rows[0]
        assert rep_rows[0]["msg"].startswith(REPLAY_MARK) and "超时平仓" in rep_rows[0]["msg"]
        assert rep_rows[0]["historical"] == "replay"
        assert "12 笔，胜 8 负 4，净盈亏 +54.62" in rep_rows[1]["msg"]
        assert rep_rows[1]["msg"].startswith(REPLAY_MARK)
        assert live_rows[1]["msg"].startswith("【历史迁移】") and live_rows[1]["historical"] == "migrate"
        assert live_rows[2]["msg"].startswith("【历史清理】") and live_rows[2]["historical"] == "cleanup"
        human = open(replay.human.name, encoding="utf-8").read()
        assert "【历史回放】ETH-USDT-SWAP 已平仓：超时平仓" in human
        assert "【历史回放】结束：12 笔，胜 8 负 4，净盈亏 +54.62" in human
        both = Journal(tempfile.mkdtemp(prefix="ma-journal-both-"), echo=False, replay=True)
        try:
            both.log("equity", "capital_migrate", "波段改用固定本金", historical="migrate")
            row = json.loads(open(both.jsonl.name, encoding="utf-8").read().strip())
            assert row["msg"].startswith("【历史回放】【历史迁移】"), row["msg"]
            assert row["historical"] == "replay+migrate"
        finally:
            both.close()
    finally:
        live.close()
        replay.close()
