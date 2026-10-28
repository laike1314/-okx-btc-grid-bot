"""OKX 多代理模拟盘交易系统 - 入口 / 调度器

用法:
  python main.py                 # 真实 OKX 模拟盘（需要 OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE）
  python main.py --smoke         # 离线冒烟测试：Mock 行情 + 撮合，不需要密钥、不联网
  python main.py --once          # 真实模拟盘只跑一轮扫描（检查连通性/看信号，不进入循环）
  python main.py --hours 6       # 跑 6 小时；配 --stop-after-flat 则到时后等最后一笔平仓再退出

调度顺序（每轮）:
  Equity 同步全部持仓 → Net-TP 逐仓监控 → [每 SCAN_INTERVAL] 分批 Scanner → Risk → Position → Executor
  扫描批次之间会再监控一遍全部持仓，避免交易池很大时漏看。
  Logger(journal) 贯穿所有代理，每个决策带原因写入 data/decisions.jsonl 和 data/bot.log
"""
import argparse
import os
import signal
import sys
import time

import config
import universe
from okx_client import OkxClient, MockOkxClient
from agents.journal import Journal
from agents.state import SharedState
from agents.fees import FeeBook
from agents.scanner import Scanner
from agents.risk import RiskGate, position_cap_reached
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


def _offline_self_checks():
    """冒烟启动前跑纯本地断言：交易池筛选、仓位上限、全持仓监控。不联网。"""
    from agents.risk import offline_self_check as risk_check
    from agents.net_tp import offline_self_check as mon_check
    universe.offline_self_check()
    risk_check()
    mon_check()


def monitor_positions(a):
    """Equity + Net-TP 覆盖交易所返回的每一个未平仓 SWAP，不因某一仓失败而停。

    Net-TP 若在本轮平掉了仓，先把平仓记账完成，再把平仓后的持仓交回开仓逻辑，
    避免用平仓前的列表把刚平掉的合约当成「已有持仓」。
    """
    positions = a["client"].positions()
    a["equity"].sync(positions)
    before = {p["instId"] for p in positions}
    a["net_tp"].check(positions)
    after = a["client"].positions()
    if {p["instId"] for p in after} != before:
        a["equity"].sync(after)
        return after
    return positions


def _can_open(a, positions):
    j = a["journal"]
    try:
        equity = a["client"].equity()
    except Exception as e:
        j.log("risk", "equity_error", f"读取权益失败，本轮不开仓: {e}")
        return False
    block = a["risk"].global_block(equity)
    if block:
        j.log("risk", "blocked", f"暂停开仓：{block}")
        return False
    if position_cap_reached(len(positions)):
        j.log("risk", "full", f"持仓 {len(positions)}/{config.MAX_POSITIONS}，本轮只监控不开仓")
        return False
    return True


def _maybe_refresh_universe(a, data_dir):
    if a["client"].is_mock or config.SYMBOLS_OVERRIDE:
        return
    every = config.UNIVERSE_REFRESH_SEC
    if every <= 0 or time.time() - a["universe_at"] < every:
        return
    try:
        universe.apply(a["client"], a["journal"], data_dir)
    except Exception as e:
        a["journal"].log("universe", "refresh_error", f"刷新交易池失败，继续用上一份: {e}")
    a["universe_at"] = time.time()


def scan_and_trade(a):
    c, j = a["client"], a["journal"]
    positions = monitor_positions(a)   # 先结算刚平掉的仓，避免同币立刻重开时漏记
    if not _can_open(a, positions):
        return
    symbols = list(config.SYMBOLS)
    if not symbols:
        j.log("scanner", "empty", "交易池为空，本轮不开仓")
        return
    batch = max(1, int(config.SCAN_BATCH))
    for i in range(0, len(symbols), batch):
        if i and not c.is_mock:
            positions = monitor_positions(a)
            if not _can_open(a, positions):
                return
        held = {p["instId"] for p in positions}
        try:
            candidates = a["scanner"].scan(symbols[i:i + batch], skip=held)
        except Exception as e:
            j.log("scanner", "scan_error", f"本批扫描失败（继续下一批）: {e}")
            continue
        for cand in candidates:
            try:
                equity = c.equity()
                ok, why = a["risk"].check(cand, positions, equity)
            except Exception as e:
                j.log("risk", "error", f"{cand.get('instId')} 风控检查失败: {e}", inst=cand.get("instId"))
                continue
            j.log("risk", "approve" if ok else "reject", f"{cand['instId']} {why}", inst=cand["instId"], ok=ok)
            if not ok:
                continue
            try:
                plan = a["position"].plan(cand, equity)
                fill = a["executor"].open(plan) if plan else None
            except Exception as e:
                j.log("executor", "error", f"{cand['instId']} 开仓流程失败: {e}", inst=cand["instId"])
                continue
            if fill:
                a["equity"].register_open(plan, fill)
                positions = c.positions()
            if position_cap_reached(len(positions)):
                j.log("risk", "full", f"持仓 {len(positions)}/{config.MAX_POSITIONS}，本轮停止继续开仓")
                return


def _start_line():
    meta = config.UNIVERSE_META or {}
    cap = "不限" if config.MAX_POSITIONS <= 0 else str(config.MAX_POSITIONS)
    source = meta.get("source") or "unset"
    if str(source).startswith("SYMBOLS"):
        pool = f"交易池 {len(config.SYMBOLS)} 个（SYMBOLS 强制列表）"
    else:
        pool = (f"交易池 {len(config.SYMBOLS)} 个（加密 {len(meta.get('crypto') or [])}"
                f" + 股票永续 {len(meta.get('stocks') or [])}，{source}）")
    return (f"多代理系统 {config.VERSION} 启动，{pool}，"
            f"杠杆 {config.LEVERAGE}x，同时持仓 {cap}，单笔风险 {config.RISK_PER_TRADE_PCT:.2%}")


def run(args):
    if args.smoke:
        _offline_self_checks()
    a = build(args.smoke, args.data_dir)
    c, j = a["client"], a["journal"]
    universe.apply(c, j, args.data_dir)
    a["universe_at"] = time.time()
    j.log("main", "start", _start_line())
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
            expired = deadline and time.time() >= deadline
            if expired:
                monitor_positions(a)
                if not args.stop_after_flat or not c.positions():
                    j.log("main", "stop", "到达运行时长" + ("，且已无持仓" if args.stop_after_flat else ""))
                    break
            elif args.smoke or time.time() - last_scan >= config.SCAN_INTERVAL_SEC:
                last_scan = time.time()
                _maybe_refresh_universe(a, args.data_dir)
                scan_and_trade(a)
            else:
                monitor_positions(a)
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
