"""OKX 多代理模拟盘交易系统 - 入口 / 调度器

用法:
  python main.py                 # 真实 OKX 模拟盘（需要 OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE）
  python main.py --smoke         # 离线冒烟测试：Mock 行情 + 撮合，不需要密钥、不联网
  python main.py --once          # 真实模拟盘只跑一轮扫描（检查连通性/看信号，不进入循环）
  python main.py --hours 6       # 跑 6 小时；配 --stop-after-flat 则到时后等最后一笔平仓再退出

调度顺序（每轮）:
  Equity 同步持仓 → Net-TP 监控(每 2s) → [每 20s] Scanner → Risk → Position → Executor → Equity 登记
  Logger(journal) 贯穿所有代理，每个决策带原因写入 data/decisions.jsonl 和 data/bot.log
"""
import argparse
import os
import signal
import sys
import time

import config
from okx_client import OkxClient, MockOkxClient
from agents.journal import Journal
from agents.state import SharedState
from agents.fees import FeeBook
from agents.scanner import Scanner
from agents.risk import RiskGate
from agents.position import PositionManager
from agents.executor import Executor
from agents.net_tp import NetTpMonitor
from agents.equity import EquityTracker

STOP = False


def _stop(*_):
    global STOP
    STOP = True


def build(mock, data_dir):
    os.makedirs(data_dir, exist_ok=True)
    j = Journal(data_dir)
    if mock:
        client = MockOkxClient()
        j.log("main", "mode", "离线冒烟测试模式（Mock 行情，不联网、不需要密钥）")
    else:
        missing = [k for k in ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE") if not os.getenv(k)]
        if missing:
            print("❌ 缺少环境变量: " + ", ".join(missing))
            print("   请设置 OKX 模拟盘 API 密钥（见 .env.example / README.md），或用 `python main.py --smoke` 离线试跑。")
            sys.exit(2)
        client = OkxClient(config.OKX_API_KEY, config.OKX_SECRET_KEY, config.OKX_PASSPHRASE)
        j.log("main", "mode", f"OKX 模拟盘模式 (x-simulated-trading: {config.SIMULATED_TRADING})")
    state = SharedState(data_dir, clock=(lambda: client.now_ms / 1000) if mock else None)
    fees = FeeBook(client, j)
    risk = RiskGate(client, j, state, fees)
    ex = Executor(client, j)
    agents = {
        "client": client, "journal": j, "state": state, "fees": fees,
        "scanner": Scanner(client, j), "risk": risk, "position": PositionManager(client, j, fees),
        "executor": ex, "net_tp": NetTpMonitor(client, j, fees, ex, state),
        "equity": EquityTracker(client, j, state, risk, data_dir),
    }
    return agents


def scan_and_trade(a):
    c, j = a["client"], a["journal"]
    equity = c.equity()
    positions = c.positions()
    a["equity"].sync(positions)        # 先结算刚平掉的仓位，避免同币立刻重开时漏记
    block = a["risk"].global_block(equity)
    if block:
        j.log("risk", "blocked", f"暂停开仓：{block}")
        return
    if len(positions) >= config.MAX_POSITIONS:
        j.log("risk", "full", f"持仓 {len(positions)}/{config.MAX_POSITIONS}，本轮只监控不开仓")
        return
    for cand in a["scanner"].scan():
        ok, why = a["risk"].check(cand, positions, equity)
        j.log("risk", "approve" if ok else "reject", f"{cand['instId']} {why}", inst=cand["instId"], ok=ok)
        if not ok:
            continue
        plan = a["position"].plan(cand, equity)
        if not plan:
            continue
        fill = a["executor"].open(plan)
        if fill:
            a["equity"].register_open(plan, fill)
            positions = c.positions()
        if len(positions) >= config.MAX_POSITIONS:
            break


def run(args):
    a = build(args.smoke, args.data_dir)
    c, j = a["client"], a["journal"]
    j.log("main", "start", f"多代理系统 {config.VERSION} 启动，币种 {','.join(config.SYMBOLS)}，"
          f"杠杆 {config.LEVERAGE}x，最多 {config.MAX_POSITIONS} 仓，单笔风险 {config.RISK_PER_TRADE_PCT:.2%}")
    if not c.is_mock:
        try:
            eq = c.equity()
            j.log("main", "auth_ok", f"已连上 OKX 模拟盘，USDT 权益 {eq:.2f}")
        except Exception as e:
            j.log("main", "auth_fail", f"无法读取模拟盘账户（密钥错误/不是模拟盘密钥/IP 白名单？）: {e}")
            sys.exit(3)
    a["fees"].refresh(force=True)
    a["executor"].init_mode()
    a["equity"].snapshot()

    if args.once:
        a["equity"].sync(c.positions())
        scan_and_trade(a)
        j.log("main", "done", "--once 单轮完成")
        return

    hours = args.hours if args.hours is not None else config.RUN_HOURS
    deadline = time.time() + hours * 3600 if hours else None
    steps = args.steps if args.smoke else None
    last_scan, i = 0.0, 0
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    while not STOP:
        i += 1
        if steps and i > steps:
            break
        try:
            c.step()                                   # Mock：推进一根 K 线；真实客户端无操作
            a["fees"].refresh()
            positions = c.positions()
            a["equity"].sync(positions)
            a["net_tp"].check(positions)
            expired = deadline and time.time() >= deadline
            if expired:
                if not args.stop_after_flat or not c.positions():
                    j.log("main", "stop", "到达运行时长" + ("，且已无持仓" if args.stop_after_flat else ""))
                    break
            elif args.smoke or time.time() - last_scan >= config.SCAN_INTERVAL_SEC:
                last_scan = time.time()
                scan_and_trade(a)
        except Exception as e:
            j.log("main", "loop_error", f"本轮异常（继续运行）: {type(e).__name__}: {e}")
            time.sleep(0 if args.smoke else 5)
        if not args.smoke:
            time.sleep(config.MONITOR_INTERVAL_SEC)

    a["equity"].sync(c.positions())
    stats = a["equity"]._write_stats(c.equity())
    j.log("main", "summary", f"结束：{stats['trades']} 笔，胜 {stats['wins']} 负 {stats['losses']}，"
          f"胜率 {stats['win_rate']}，净盈亏 {stats['net_pnl']:+.2f}，PF {stats['profit_factor']}，"
          f"最大回撤 {stats['max_drawdown_pct']}%，权益 {stats['equity']:.2f}", **stats)


def main():
    p = argparse.ArgumentParser(description="OKX 多代理模拟盘交易系统")
    p.add_argument("--smoke", action="store_true", help="离线 Mock 冒烟测试，不需要密钥")
    p.add_argument("--steps", type=int, default=600, help="冒烟测试推进的 K 线数（默认 600）")
    p.add_argument("--once", action="store_true", help="只跑一轮扫描")
    p.add_argument("--hours", type=float, default=None, help="运行小时数，默认一直跑")
    p.add_argument("--stop-after-flat", action="store_true", default=config.STOP_AFTER_FLAT,
                   help="到时后等持仓平完再退出")
    p.add_argument("--data-dir", default=None, help="日志/状态目录，默认 ./data（冒烟测试用 ./data_smoke）")
    args = p.parse_args()
    if args.data_dir is None:
        base = os.path.dirname(os.path.abspath(__file__))
        args.data_dir = os.path.join(base, "data_smoke") if args.smoke else config.DATA_DIR
    run(args)


if __name__ == "__main__":
    main()
