"""可用保证金、51008 冷却，以及说不清来源的权益跳动。

下单看的是「还能拿来开仓的保证金」，不是 USDT 总权益 eq。
eq 里可以含着别的策略浮盈、交易机器人占用（stgyEq）、冻结（frozenBal）
或者其他币种折进来的数，所以按 eq/4 下单会被 OKX 以 51008 整批拒绝。

日志事件（bot.log / decisions.jsonl）：
  account/startup、account/pre_order
      USDT 的 availBal、availEq、frozenBal、eq，以及账户档、其他币种、
      全部类型的持仓和挂单（不只有 SWAP）。
  executor/margin_reject
      交易所回了 51008。这一次 POST 不会再发。
  executor/margin_cooldown
      该合约冷却 MARGIN_COOLDOWN_SEC（默认 30 分钟），下次张数按更小的比例。
  executor/margin_skip
      计划保证金已经大于刚读到的可用保证金，POST 根本不发。
  equity/equity_unexplained
      权益和本策略账本对不上。峰值不因此抬高；已有峰值也不自动下调。
      账户止损仍按起始权益，较峰值回撤的算法也不变。
  equity/peak_update
      这笔上涨对得上已实现盈亏和本策略持仓浮盈，峰值才抬高。
"""
import config

ORDER_INST_TYPES = ("SPOT", "MARGIN", "SWAP", "FUTURES", "OPTION")
USDT_KEYS = ("availBal", "availEq", "frozenBal", "eq", "cashBal", "ordFrozen", "upl",
             "isoEq", "stgyEq", "spotInUseAmt", "eqUsd", "disEq", "liab", "isoUpl")
ACCOUNT_KEYS = ("totalEq", "availEq", "adjEq", "isoEq", "imr", "mmr", "ordFroz", "upl", "mgnRatio")


def num_or_none(v):
    """空字符串是「这个账户模式没有这个字段」，不是 0。"""
    if v is None:
        return None
    if isinstance(v, str) and v.strip() == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def pick_available(usdt, account, acct_lv, td_mode):
    """返回 (可用保证金, 字段名)。

    全仓（本策略 tdMode=cross）：
      - 单币种保证金 acctLv=2：用 USDT availEq（币种可用保证金）。空则 availBal。
      - 跨币种 / 组合保证金 acctLv=3/4：51008 写的是 USDT 保证金不足，同时账户档
        availEq 也可能更紧。两边都有就取较小的那个。
      - 简单模式 acctLv=1：availBal。
    逐仓：从 availBal 划保证金。
    字段缺失返回 (None, "")，调用方退回总权益；真正的 0 就是没有可用保证金。
    """
    lv = str(acct_lv or "")
    mode = str(td_mode or "cross").lower()
    usdt = usdt or {}
    account = account or {}
    usdt_eq = num_or_none(usdt.get("availEq"))
    usdt_bal = num_or_none(usdt.get("availBal"))
    acct_eq = num_or_none(account.get("availEq"))

    if mode == "isolated" or lv == "1":
        if usdt_bal is not None:
            return usdt_bal, "availBal"
        if usdt_eq is not None:
            return usdt_eq, "availEq"
        return None, ""

    cands = []
    if usdt_eq is not None:
        cands.append((usdt_eq, "availEq", 0))
    elif usdt_bal is not None:
        cands.append((usdt_bal, "availBal", 1))
    if lv in ("3", "4") and acct_eq is not None:
        cands.append((acct_eq, "account_availEq", 2))
    if not cands:
        return None, ""
    cands.sort(key=lambda item: (item[0], item[2]))
    return cands[0][0], cands[0][1]


def assemble_margin_view(cfg, bal, td_mode):
    """把 /account/config 和 /account/balance 收成定仓和日志用的一页。"""
    cfg = cfg or {}
    bal = bal or {}
    details = list(bal.get("details") or [])
    usdt_raw = next((d for d in details if str(d.get("ccy") or "") == "USDT"), {}) or {}
    usdt = {k: num_or_none(usdt_raw.get(k)) for k in USDT_KEYS}
    account = {k: num_or_none(bal.get(k)) for k in ACCOUNT_KEYS}
    other = []
    for d in details:
        ccy = str(d.get("ccy") or "")
        if not ccy or ccy == "USDT":
            continue
        row = {"ccy": ccy}
        for k in ("eq", "availEq", "availBal", "frozenBal", "cashBal", "stgyEq", "upl", "eqUsd"):
            row[k] = num_or_none(d.get(k))
        interesting = ("eq", "availEq", "availBal", "frozenBal", "cashBal", "stgyEq", "upl", "eqUsd")
        if any(row[k] not in (None, 0.0) for k in interesting):
            other.append(row)
    avail, field = pick_available(usdt, account, cfg.get("acctLv"), td_mode)
    eq = usdt.get("eq")
    if eq is None:
        eq = account.get("totalEq")
    return {
        "acctLv": str(cfg.get("acctLv") or ""),
        "posMode": cfg.get("posMode") or "",
        "tdMode": td_mode or "",
        "usdt": usdt,
        "account": account,
        "other_ccy": other,
        "available": avail,
        "available_field": field,
        "eq": eq,
        "errors": [],
    }


def sizing_base(equity, available):
    """定仓基准。读到了可用保证金就用 min(总权益, 可用保证金)，避免按虚高的 eq 开仓，
    也不在可用保证金比权益还大时把仓位放大。没读到才退回总权益。"""
    eq = float(equity or 0)
    if available is None:
        return eq, "equity"
    avail = max(0.0, float(available))
    if eq > 0:
        return min(eq, avail), "min(eq,available)"
    return avail, "available"


def use_frac():
    return min(1.0, max(0.0, float(config.MARGIN_USE_FRAC)))


def scale_for(state, inst):
    raw = (state.d.get("margin_scale") or {}).get(inst)
    try:
        scale = float(raw if raw not in (None, "") else 1.0)
    except (TypeError, ValueError):
        scale = 1.0
    if scale <= 0:
        return 1.0
    return scale


def margin_cooldown_block(state, inst):
    """51008 之后的冷却。秒数 <=0 则关闭，不看 state 里残留的到期时间。"""
    if int(config.MARGIN_COOLDOWN_SEC) <= 0:
        return False, ""
    until = float((state.d.get("margin_cooldown") or {}).get(inst) or 0)
    now = float(state.now())
    if now < until:
        left = int(until - now)
        return True, f"保证金不足冷却中，还剩 {left}s（51008 后本合约先歇一会，不连着下单）"
    return False, ""


def on_margin_reject(state, journal, inst):
    """交易所已经拒绝。不在这里再 POST。冷却，并缩小该合约下次的张数比例。"""
    scales = state.d.setdefault("margin_scale", {})
    try:
        prev = float(scales.get(inst) or 1.0)
    except (TypeError, ValueError):
        prev = 1.0
    if prev <= 0:
        prev = 1.0
    nxt = prev * float(config.MARGIN_RETRY_FRACTION)
    floor = float(config.MARGIN_SCALE_FLOOR)
    if floor > 0:
        nxt = max(floor, nxt)
    scales[inst] = nxt
    cool = int(config.MARGIN_COOLDOWN_SEC)
    until = float(state.now()) + max(0, cool)
    if cool > 0:
        state.d.setdefault("margin_cooldown", {})[inst] = until
    state.save()
    journal.log("executor", "margin_cooldown",
                f"{inst} 51008 已拒绝，POST 不重试。冷却 {cool}s，下次名义 = 可用保证金基准 × "
                f"分配比例 / 持仓槽 × {nxt:.2f}",
                inst=inst, scale=nxt, until=until, prev_scale=prev)
    return nxt


def on_margin_filled(state, inst):
    """这个合约真正成交之后，恢复正常张数比例。"""
    changed = False
    scales = state.d.get("margin_scale") or {}
    if inst in scales:
        scales.pop(inst, None)
        changed = True
    cool = state.d.get("margin_cooldown") or {}
    if inst in cool:
        cool.pop(inst, None)
        changed = True
    if changed:
        state.save()


def book_realized(state):
    total = 0.0
    for v in (state.d.get("daily") or {}).values():
        try:
            total += float(v or 0)
        except (TypeError, ValueError):
            continue
    return total


def seed_basis(state, eq, our_upl):
    """把「现在」记成基线。用户显式重置，或本进程刚种下起始权益时用。"""
    state.d["swing_equity_basis"] = float(eq)
    state.d["swing_basis_realized"] = book_realized(state)
    state.d["swing_basis_upl"] = float(our_upl or 0)


def mark_fresh_start(state):
    """这一进程里才第一次写下起始权益，不能拿历史 daily 去倒减。"""
    state._start_seeded_now = True


def ensure_basis(state, eq, our_upl):
    """还没有基线时：沿用已经落盘的起始权益，已实现盈亏从那之后算。
    这样能看出「起始权益之后、账本对不上」的跳变。返回是否新种下了基线。"""
    if state.d.get("swing_equity_basis") not in (None, ""):
        return False
    start = state.d.get("swing_start_equity")
    try:
        start_f = float(start) if start not in (None, "") else 0.0
    except (TypeError, ValueError):
        start_f = 0.0
    if start_f > 0 and not getattr(state, "_start_seeded_now", False):
        state.d["swing_equity_basis"] = start_f
        state.d["swing_basis_realized"] = 0.0
        state.d["swing_basis_upl"] = 0.0
        return True
    seed_basis(state, eq, our_upl)
    return True


def _tolerance(expected):
    return max(float(config.EQUITY_UNEXPLAINED_USDT),
               abs(float(expected)) * float(config.EQUITY_UNEXPLAINED_PCT))


def note_swing_equity(state, journal, eq, our_upl=0.0):
    """峰值只在上涨能被本策略账本解释时抬高。

    说不清来源的跳涨不抬峰值，避免之后回落被当成回撤。
    已经记下的峰值不在这里下调（那会悄悄放宽闸门）。
    下跌照常留给账户止损和较峰值回撤，这里不改那两个比较。
    """
    eq = float(eq)
    our_upl = float(our_upl or 0)
    seeded = ensure_basis(state, eq, our_upl)
    basis = float(state.d.get("swing_equity_basis") or eq)
    realized_delta = book_realized(state) - float(state.d.get("swing_basis_realized") or 0)
    upl_delta = our_upl - float(state.d.get("swing_basis_upl") or 0)
    expected = basis + realized_delta + upl_delta
    gap = eq - expected
    tol = _tolerance(expected)
    explained = abs(gap) <= tol
    peak = float(state.d.get("swing_peak_equity") or 0)
    lifted = False
    if explained and eq > peak:
        state.d["swing_peak_equity"] = eq
        seed_basis(state, eq, our_upl)
        lifted = True
        journal.log("equity", "peak_update",
                    f"峰值抬到 {eq:.2f}，这笔变动对得上本策略已实现盈亏 {realized_delta:+.2f} "
                    f"和持仓浮盈变化 {upl_delta:+.2f}",
                    equity=eq, expected=round(expected, 4), gap=round(gap, 4), peak=eq)
    if lifted or seeded:
        state.save()
    if not explained:
        _log_unexplained(state, journal, eq, expected, gap, peak, tol, realized_delta, upl_delta)
    return {"explained": explained, "gap": gap, "expected": expected, "tolerance": tol,
            "peak": float(state.d.get("swing_peak_equity") or 0), "lifted": lifted, "seeded": seeded}


def _log_unexplained(state, journal, eq, expected, gap, peak, tol, realized_delta, upl_delta):
    sig = (round(gap), round(peak, 2))
    if getattr(state, "_eq_gap_sig", None) == sig:
        return
    state._eq_gap_sig = sig
    dd = (peak - eq) / peak if peak else 0.0
    if gap > 0:
        peak_note = (f"峰值保持 {peak:.2f}，这笔说不清来源的上涨不抬峰值。"
                     f"已有峰值不自动下调，较峰值回撤大约 {dd:.1%}，闸门算法不变。")
    else:
        peak_note = (f"峰值仍是 {peak:.2f}（较峰值约 {dd:.1%}）。"
                     f"下跌仍计入回撤和账户止损，闸门没有放宽。")
    start = state.d.get("swing_start_equity")
    journal.log(
        "equity", "equity_unexplained",
        f"USDT权益 {eq:.2f} 与账本可解释的 {expected:.2f} 相差 {gap:+.2f}"
        f"（容差 {tol:.2f}，已实现 {realized_delta:+.2f}，本策略浮盈变化 {upl_delta:+.2f}，"
        f"起始权益 {start}）。{peak_note}"
        f"账户止损仍按起始权益。只有已经停机时，SWING_RESET_HALT=1 才会重设起点和峰值。",
        equity=eq, expected=round(expected, 4), gap=round(gap, 4), peak=peak,
        tolerance=round(tol, 4))


def _fmt_usdt(v):
    if v is None:
        return "—"
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_positions(positions):
    rows = list(positions or [])
    if not rows:
        return "持仓 无（全类型，含 MARGIN/SWAP/FUTURES/OPTION）"
    counts = {}
    for p in rows:
        key = str(p.get("instType") or "?")
        counts[key] = counts.get(key, 0) + 1
    count_s = ",".join(f"{k}:{n}" for k, n in sorted(counts.items()))
    bits = []
    for p in rows[:12]:
        bits.append(
            f"{p.get('instType') or '?'} {p.get('instId')} {p.get('side')} "
            f"qty={p.get('qty')} notional={_fmt_usdt(p.get('notional'))} "
            f"upl={_fmt_usdt(p.get('upl'))} imr={_fmt_usdt(p.get('imr'))} "
            f"margin={_fmt_usdt(p.get('margin'))} lever={p.get('lever')} {p.get('mgnMode') or ''}".rstrip()
        )
    more = f" …共{len(rows)}" if len(rows) > 12 else ""
    return f"持仓 {len(rows)}（{count_s}） " + "; ".join(bits) + more


def _fmt_orders(orders):
    rows = list(orders or [])
    if not rows:
        return "挂单 无（全类型，含 SPOT/MARGIN/SWAP/FUTURES/OPTION）"
    bits = []
    for o in rows[:12]:
        bits.append(
            f"{o.get('instType') or '?'} {o.get('instId')} {o.get('side')} "
            f"{o.get('ordType')} sz={o.get('sz')} px={o.get('px')} {o.get('state')}"
        )
    more = f" …共{len(rows)}" if len(rows) > 12 else ""
    return f"挂单 {len(rows)} " + "; ".join(bits) + more


def format_snapshot(view, positions, orders, inst=None, margin=None, notional=None):
    view = view or {}
    usdt = view.get("usdt") or {}
    acct = view.get("account") or {}
    head = (f"USDT availBal={_fmt_usdt(usdt.get('availBal'))} "
            f"availEq={_fmt_usdt(usdt.get('availEq'))} "
            f"frozenBal={_fmt_usdt(usdt.get('frozenBal'))} "
            f"eq={_fmt_usdt(usdt.get('eq'))} "
            f"cashBal={_fmt_usdt(usdt.get('cashBal'))} "
            f"ordFrozen={_fmt_usdt(usdt.get('ordFrozen'))} "
            f"upl={_fmt_usdt(usdt.get('upl'))} "
            f"stgyEq={_fmt_usdt(usdt.get('stgyEq'))} "
            f"isoEq={_fmt_usdt(usdt.get('isoEq'))} "
            f"spotInUseAmt={_fmt_usdt(usdt.get('spotInUseAmt'))}")
    acct_s = (f"账户 acctLv={view.get('acctLv') or '—'} posMode={view.get('posMode') or '—'} "
              f"tdMode={view.get('tdMode') or '—'} totalEq={_fmt_usdt(acct.get('totalEq'))} "
              f"availEq={_fmt_usdt(acct.get('availEq'))} adjEq={_fmt_usdt(acct.get('adjEq'))} "
              f"imr={_fmt_usdt(acct.get('imr'))} mmr={_fmt_usdt(acct.get('mmr'))} "
              f"ordFroz={_fmt_usdt(acct.get('ordFroz'))}")
    avail_s = f"可用保证金={_fmt_usdt(view.get('available'))}({view.get('available_field') or '无'})"
    if margin is not None:
        avail_s += f" 本单保证金={_fmt_usdt(margin)} 名义={_fmt_usdt(notional)}"
    others = list(view.get("other_ccy") or [])
    if others:
        bits = []
        for c in others[:8]:
            bits.append(
                f"{c.get('ccy')} eq={_fmt_usdt(c.get('eq'))} availEq={_fmt_usdt(c.get('availEq'))} "
                f"frozen={_fmt_usdt(c.get('frozenBal'))} stgy={_fmt_usdt(c.get('stgyEq'))}"
            )
        other_s = "其他币种 " + "; ".join(bits)
        if len(others) > 8:
            other_s += f" …共{len(others)}"
    else:
        other_s = "其他币种 无"
    err = view.get("errors") or []
    err_s = (" 读取问题 " + "; ".join(str(e) for e in err)) if err else ""
    prefix = f"{inst} " if inst else ""
    return (f"{prefix}{head} | {acct_s} | {avail_s} | {other_s} | "
            f"{_fmt_positions(positions)} | {_fmt_orders(orders)}{err_s}")


def snapshot_fields(view, positions, orders):
    view = view or {}
    return {
        "acctLv": view.get("acctLv"),
        "available": view.get("available"),
        "available_field": view.get("available_field"),
        "eq": view.get("eq"),
        "usdt": view.get("usdt") or {},
        "account": view.get("account") or {},
        "other_ccy": list(view.get("other_ccy") or [])[:8],
        "positions": list(positions or [])[:12],
        "pending_orders": list(orders or [])[:12],
        "position_count": len(list(positions or [])),
        "order_count": len(list(orders or [])),
        "errors": list(view.get("errors") or []),
    }


def load_positions(client):
    errors = []
    fn = getattr(client, "positions_all", None)
    if fn:
        try:
            return list(fn() or []), errors
        except Exception as e:
            errors.append(f"positions_all: {e}")
    else:
        errors.append("客户端没有 positions_all")
    fn2 = getattr(client, "positions", None)
    if not fn2:
        return [], errors
    try:
        rows = []
        for p in fn2() or []:
            row = dict(p)
            row.setdefault("instType", "SWAP")
            rows.append(row)
        errors.append("持仓已回退为仅 SWAP")
        return rows, errors
    except Exception as e:
        errors.append(f"positions: {e}")
        return [], errors


def load_orders(client):
    fn = getattr(client, "pending_orders", None)
    if not fn:
        return [], ["客户端没有 pending_orders"]
    try:
        orders = list(fn() or [])
    except Exception as e:
        return [], [f"pending_orders: {e}"]
    extra = list(getattr(client, "last_order_errors", None) or [])
    return orders, [str(e) for e in extra]


def log_account_snapshot(journal, client, event, inst=None, margin=None, notional=None):
    """启动时和每笔 POST 之前记一页账户。读失败也要留下错误，不能把下单流程带崩。"""
    errors = []
    view = None
    fn = getattr(client, "margin_view", None)
    if fn:
        try:
            view = fn()
        except Exception as e:
            errors.append(f"balance: {e}")
    else:
        errors.append("客户端没有 margin_view")
    positions, perr = load_positions(client)
    orders, oerr = load_orders(client)
    errors.extend(perr)
    errors.extend(oerr)
    if not isinstance(view, dict):
        view = {"usdt": {}, "account": {}, "other_ccy": [], "available": None,
                "available_field": "", "acctLv": "", "posMode": "", "tdMode": "",
                "eq": None, "errors": []}
    else:
        view = dict(view)
    view["errors"] = list(view.get("errors") or []) + errors
    msg = format_snapshot(view, positions, orders, inst=inst, margin=margin, notional=notional)
    journal.log("account", event, msg, **snapshot_fields(view, positions, orders))
    return view


def offline_self_check():
    """不联网。可用保证金字段、按可用保证金定仓、51008 不重试、峰值不被虚高抬起。"""
    import tempfile
    from okx_client import OkxError

    assert num_or_none("") is None
    assert num_or_none(None) is None
    assert num_or_none("0") == 0.0
    assert num_or_none("12.5") == 12.5

    # 单币种全仓：USDT availEq。空字符串不当成 0。
    assert pick_available({"availEq": "800", "availBal": "100"}, {"availEq": ""}, "2", "cross") == (800.0, "availEq")
    assert pick_available({"availEq": "", "availBal": "100"}, {"availEq": ""}, "2", "cross") == (100.0, "availBal")
    assert pick_available({"availEq": "0", "availBal": "100"}, {}, "2", "cross") == (0.0, "availEq")
    # 逐仓 / 简单模式用 availBal。
    assert pick_available({"availEq": "800", "availBal": "15"}, {}, "2", "isolated") == (15.0, "availBal")
    assert pick_available({"availEq": "800", "availBal": "15"}, {}, "1", "cross") == (15.0, "availBal")
    # 跨币种：USDT availEq 和账户 availEq 取更小的，51008 写的是 USDT。
    assert pick_available({"availEq": "40", "availBal": "10"}, {"availEq": "15"}, "3", "cross") == (15.0, "account_availEq")
    assert pick_available({"availEq": "5", "availBal": "1"}, {"availEq": "80"}, "4", "cross") == (5.0, "availEq")
    assert pick_available({}, {}, "2", "cross") == (None, "")

    view = assemble_margin_view(
        {"acctLv": "2", "posMode": "net_mode"},
        {"totalEq": "100494.68", "adjEq": "", "availEq": "", "imr": "", "mmr": "", "ordFroz": "",
         "details": [
             {"ccy": "USDT", "eq": "100494.68", "availEq": "800.5", "availBal": "1200",
              "frozenBal": "90000", "cashBal": "95000", "ordFrozen": "0", "upl": "0",
              "stgyEq": "5000", "isoEq": "0", "spotInUseAmt": ""},
             {"ccy": "BTC", "eq": "0.01", "availEq": "", "availBal": "0.01", "frozenBal": "0",
              "cashBal": "0.01", "stgyEq": "0", "upl": "0", "eqUsd": "600"},
         ]},
        "cross")
    assert view["available"] == 800.5 and view["available_field"] == "availEq"
    assert view["eq"] == 100494.68
    assert view["usdt"]["frozenBal"] == 90000
    assert view["usdt"]["stgyEq"] == 5000
    assert view["other_ccy"] and view["other_ccy"][0]["ccy"] == "BTC"
    assert abs(sizing_base(100494.68, 800.5)[0] - 800.5) < 1e-9
    assert sizing_base(1000, 4000)[0] == 1000
    assert sizing_base(1000, None) == (1000.0, "equity")

    text = format_snapshot(
        view,
        [{"instType": "MARGIN", "instId": "BTC-USDT", "side": "long", "qty": 0.01,
          "notional": 600, "upl": -3, "imr": 20, "margin": 20, "lever": 3, "mgnMode": "cross"},
         {"instType": "SWAP", "instId": "ETH-USDT-SWAP", "side": "short", "qty": 1,
          "notional": 3000, "upl": 10, "imr": 3000, "margin": "", "lever": 1, "mgnMode": "cross"}],
        [{"instType": "SPOT", "instId": "BTC-USDT", "side": "buy", "ordType": "limit",
          "sz": "0.01", "px": "60000", "state": "live"}])
    for token in ("availBal=1200.00", "availEq=800.50", "frozenBal=90000.00", "eq=100494.68",
                  "stgyEq=5000.00", "MARGIN BTC-USDT", "SPOT BTC-USDT", "其他币种 BTC"):
        assert token in text, token

    from agents.swing import size_qty
    assert size_qty(10_000, 100, 0.1, 1, 1) == 250
    # 可用 4000、权益 100000：名义按 4000/4，而不是 100000/4。
    assert size_qty(100_000, 100, 0.1, 1, 1, available=4000) == 100, size_qty(100_000, 100, 0.1, 1, 1, available=4000)
    assert size_qty(100_000, 100, 0.1, 1, 1, available=4000, fraction=0.5) == 50
    assert size_qty(1000, 100, 0.1, 1, 1, available=4000) == 25
    assert size_qty(100_000, 100, 0.1, 1, 1, available=0) is None

    from agents.journal import Journal
    from agents.state import SharedState
    from agents.risk import RiskGate

    d = tempfile.mkdtemp()
    j = Journal(d, echo=False)
    saved = config.STRATEGY
    try:
        st = SharedState(d, clock=lambda: 1_000_000.0)
        st.d["swing_start_equity"] = 83466.29
        st.d["swing_peak_equity"] = 102134.58
        st.d["daily"] = {"2026-11-08": -7156.36, "2026-11-10": 1467.63}
        info = note_swing_equity(st, j, 100494.68, 0)
        assert info["explained"] is False
        assert st.d["swing_peak_equity"] == 102134.58  # 不抬，也不自动下调
        assert abs(st.d["swing_equity_basis"] - 83466.29) < 1e-6
        # 同一差额不重复刷日志。
        note_swing_equity(st, j, 100494.68, 0)

        st2 = SharedState(tempfile.mkdtemp(), clock=lambda: 50_000.0)
        st2.d["swing_start_equity"] = 10000
        st2.d["swing_peak_equity"] = 10000
        st2.d["swing_equity_basis"] = 10000
        st2.d["swing_basis_realized"] = 0
        st2.d["swing_basis_upl"] = 0
        st2.d["daily"] = {}
        info = note_swing_equity(st2, j, 10400, 400)
        assert info["explained"] and info["lifted"]
        assert abs(st2.d["swing_peak_equity"] - 10400) < 1e-6
        # 说不清的上涨不抬。
        info = note_swing_equity(st2, j, 15000, 400)
        assert info["explained"] is False
        assert abs(st2.d["swing_peak_equity"] - 10400) < 1e-6

        config.STRATEGY = "swing_combo"
        gate = RiskGate(None, j, st2, None)
        # 10400 的峰值，8900 大约回撤 14%，但还没到起始权益的 -20%。
        why = gate.global_block(8900)
        assert why and "回撤" in why, why
        assert st2.d.get("swing_halted") is not True
        why = gate.global_block(7000)
        assert st2.d.get("swing_halted") is True
        assert why and "止损" in why, why

        # 51008：一次 POST，冷却，缩小比例，风控在冷却内拒绝。
        from agents.executor import Executor
        from okx_client import OkxClient

        class _C:
            is_mock = True
            posts = 0

            def instrument(self, inst):
                return {"ctVal": 0.01, "lotSz": "0.01", "minSz": 0.01, "tickSz": "0.1", "maxLever": 20}

            def set_leverage(self, *a, **k):
                return None

            def ticker(self, inst):
                return {"last": 100.0, "bid": 99.9, "ask": 100.1, "vol_usdt_24h": 50_000_000}

            def place_order(self, body):
                self.posts += 1
                raise OkxError("51008", "Insufficient USDT margin")

        config.STRATEGY = "scalp"
        client = _C()
        ex = Executor(client, j)
        ex.long_short = False
        plan = {"instId": "SOL-USDT-SWAP", "direction": "long", "qty": 1.0, "lever": 1,
                "entry_ref": 99.9, "signal_px": 100.0, "sl": 90.0, "tp": 110.0,
                "sl_pct": 0.01, "tp_pct": 0.01, "margin": 50.0, "notional": 50.0}
        got = ex.open(plan)
        assert isinstance(got, dict) and got.get("rejected") == "51008", got
        assert client.posts == 1
        got2 = ex.open(dict(plan))
        assert client.posts == 2  # 调用方再次 open 才会再 POST；函数内部不重试

        st3 = SharedState(tempfile.mkdtemp(), clock=lambda: 10_000.0)
        on_margin_reject(st3, j, "SOL-USDT-SWAP")
        assert abs(st3.d["margin_scale"]["SOL-USDT-SWAP"] - 0.5) < 1e-9
        assert st3.d["margin_cooldown"]["SOL-USDT-SWAP"] == 10_000.0 + config.MARGIN_COOLDOWN_SEC
        on_margin_reject(st3, j, "SOL-USDT-SWAP")
        assert abs(st3.d["margin_scale"]["SOL-USDT-SWAP"] - 0.25) < 1e-9
        on_margin_reject(st3, j, "SOL-USDT-SWAP")
        assert abs(st3.d["margin_scale"]["SOL-USDT-SWAP"] - 0.25) < 1e-9  # 地板

        class _NoTicker:
            def ticker(self, inst):
                raise AssertionError("冷却中不应再询价")

        blocked, why = margin_cooldown_block(st3, "SOL-USDT-SWAP")
        assert blocked and "51008" in why
        config.STRATEGY = "swing_combo"
        from agents.fees import FeeBook
        gate3 = RiskGate(_NoTicker(), j, st3, FeeBook(_NoTicker(), j))
        ok, why = gate3.check(
            {"instId": "SOL-USDT-SWAP", "direction": "long", "family": "momentum",
             "regime": "trend", "combined": 0.8, "atr": 1, "atr_pct": 0.02, "price": 100},
            [], 10_000)
        assert ok is False and "51008" in why, why
        on_margin_filled(st3, "SOL-USDT-SWAP")
        assert "SOL-USDT-SWAP" not in (st3.d.get("margin_scale") or {})
        assert "SOL-USDT-SWAP" not in (st3.d.get("margin_cooldown") or {})

        # 计划张数走客户端读到的可用保证金，不走传入的总权益。
        from agents.position import PositionManager

        class _Avail:
            def margin_view(self):
                return {"available": 4000.0, "available_field": "availEq", "eq": 100000.0,
                        "usdt": {"availEq": 4000.0, "eq": 100000.0}, "account": {}, "errors": []}

            def instrument(self, inst):
                return {"ctVal": 0.1, "lotSz": "1", "minSz": 1, "tickSz": "0.1", "maxLever": 20}

        class _Fees:
            maker = 0.0002
            taker = 0.0005

        planner = PositionManager(_Avail(), j, _Fees())
        cand = {"instId": "BTC-USDT-SWAP", "direction": "long", "family": "momentum",
                "regime": "trend", "combined": 0.8, "atr": 1.0, "atr_pct": 0.02,
                "price": 100.0, "bid": 99.9, "ask": 100.1, "spread_bps": 2}
        planned = planner.plan(cand, 100_000, size_scale=0.5)
        assert planned and abs(planned["notional"] - 500) < 1.0, planned
        assert planned["lever"] == 1

        # 挂单按类型分别拉，持仓不按 instType 过滤。POST 51008 只打一次。
        _http_coverage()
    finally:
        config.STRATEGY = saved
        j.close()


def _http_coverage():
    import okx_client
    from okx_client import OkxClient, OkxError

    class _Resp:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload
            self.text = __import__("json").dumps(payload)

        def json(self):
            return self._payload

    class _Sess:
        def __init__(self, handler):
            self.handler = handler
            self.calls = []

        def request(self, method, url, headers=None, data=None, timeout=None):
            self.calls.append((method, url))
            return self.handler(method, url)

    old_gap = config.PUBLIC_REQ_GAP_SEC
    config.PUBLIC_REQ_GAP_SEC = 0
    try:
        client = OkxClient.__new__(OkxClient)
        client.key, client.secret, client.passphrase = "k", "s", "p"
        client.base = "https://www.okx.com"
        client._inst_cache = {}
        client._next_get_at = 0.0
        client._next_rubik_at = 0.0

        def handler(method, url):
            if url.endswith("/api/v5/account/positions"):
                return _Resp({"code": "0", "data": [{
                    "instId": "BTC-USDT", "instType": "MARGIN", "pos": "0.01", "posSide": "net",
                    "avgPx": "60000", "markPx": "60000", "upl": "1", "notionalUsd": "600",
                    "lever": "3", "cTime": "0", "mgnMode": "cross", "imr": "20", "margin": "20",
                    "ccy": "BTC"}]})
            if "orders-pending" in url:
                if "instType=SPOT" in url:
                    return _Resp({"code": "0", "data": [{
                        "instId": "BTC-USDT", "instType": "SPOT", "ordId": "1", "side": "buy",
                        "posSide": "net", "ordType": "limit", "sz": "0.01", "px": "60000",
                        "state": "live", "reduceOnly": "false"}]})
                return _Resp({"code": "0", "data": []})
            if url.endswith("/api/v5/trade/order") or "/api/v5/trade/order?" in url:
                return _Resp({"code": "1", "msg": "All operations failed",
                              "data": [{"sCode": "51008", "sMsg": "Insufficient USDT margin"}]})
            return _Resp({"code": "0", "data": []})

        client.s = _Sess(handler)
        positions = client.positions_all()
        assert positions and positions[0]["instType"] == "MARGIN", positions
        assert all("instType=" not in url for _m, url in client.s.calls), client.s.calls
        client.s.calls.clear()
        orders = client.pending_orders()
        urls = " ".join(url for _m, url in client.s.calls)
        for inst_type in ORDER_INST_TYPES:
            assert f"instType={inst_type}" in urls, urls
        assert len(orders) == 1 and orders[0]["instType"] == "SPOT"
        client.s.calls.clear()
        try:
            client.request("POST", "/api/v5/trade/order", body={"instId": "BTC-USDT-SWAP", "sz": "1"}, retries=4)
            raise AssertionError("51008 不该被当成成功")
        except OkxError as e:
            assert e.code == "51008", e
        assert [m for m, _u in client.s.calls] == ["POST"]
    finally:
        config.PUBLIC_REQ_GAP_SEC = old_gap
