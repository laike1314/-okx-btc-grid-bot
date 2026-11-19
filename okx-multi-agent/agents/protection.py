"""算法单 / 普通挂单的回读判断。不访问网络。

成交价靠近账本止盈，不能证明止盈单当时还在。仓位消失后的默认原因
「交易所止盈/止损」只说明仓没了。这里只认回读到的触发价和覆盖数量。
"""


def _num(raw):
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip() == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def trigger_set(algo, key):
    val = _num((algo or {}).get(key))
    return val is not None and val > 0


# 撤单时这些 sCode 表示委托已经不在，可以清掉残单记录。
_GONE_CODES = {"51400", "51401", "51402", "51603"}


def ack_error(payload):
    """业务 sCode。顶层 code=0 时，单条 sCode 仍可能是失败。空和 0 算成功。"""
    rows = payload if isinstance(payload, list) else [payload]
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("sCode") or "")
        if code and code != "0":
            msg = str(row.get("sMsg") or "")
            return code, msg
    return None


def ack_is_gone(payload):
    got = ack_error(payload)
    if not got:
        return False
    code, msg = got
    text = msg.lower()
    return code in _GONE_CODES or "does not exist" in text or "already" in text


def algo_active(algo):
    """未触发的保护只认 live。没带回 state 的旧响应仍视为有效，避免误判缺失。

    pause 不算正在保护。failCode 非 0 表示这张单已经失败。
    """
    state = str((algo or {}).get("state") or "")
    if state and state != "live":
        return False
    fail = str((algo or {}).get("failCode") or "")
    if fail and fail != "0":
        return False
    return True


def side_matches(algo, direction, pos_side):
    """平多必须卖，平空必须买。双向持仓的 posSide 必须是要平的那一侧。"""
    side = str((algo or {}).get("side") or "").lower()
    ps = str((algo or {}).get("posSide") or "").lower()
    if direction in ("long", "short") and side:
        expect = "sell" if direction == "long" else "buy"
        if side != expect:
            return False
    if ps in ("long", "short"):
        if direction in ("long", "short") and ps != direction:
            return False
        if pos_side and str(pos_side).lower() not in (ps, "net"):
            return False
    return True


def tighter_sl(direction, *prices):
    """多单止损越高越紧，空单止损越低越紧。不返回更差的价格。"""
    vals = []
    for raw in prices:
        val = _num(raw)
        if val is not None and val > 0:
            vals.append(val)
    if not vals:
        return None
    return max(vals) if direction == "long" else min(vals)


def close_fraction(algo):
    """closeFraction>=1 表示交易所全平，覆盖当时的全部持仓，不看 sz。"""
    return _num((algo or {}).get("closeFraction"))


def coverage(algo, pos_qty):
    """返回 (covered_qty_or_None, kind)。

    kind:
      full       closeFraction>=1，覆盖全部持仓
      fraction   0<closeFraction<1，covered = 持仓 * 比例
      sz         用张数 sz
      unknown    回读里既没有 closeFraction 也没有 sz，不能当成已覆盖
    """
    frac = close_fraction(algo)
    if frac is not None and frac > 0:
        if frac + 1e-9 >= 1:
            qty = _num(pos_qty)
            return (qty if qty is not None else 0.0), "full"
        qty = _num(pos_qty)
        if qty is None:
            return None, "fraction"
        return qty * frac, "fraction"
    sz = _num((algo or {}).get("sz"))
    if sz is None or sz <= 0:
        return None, "unknown"
    return sz, "sz"


def leg_problem(algo, need_tp):
    if not trigger_set(algo, "slTriggerPx"):
        return "no_sl"
    if need_tp and not trigger_set(algo, "tpTriggerPx"):
        return "no_tp"
    return None


def coverage_problem(algo, pos_qty):
    """数量对不上才返回原因。未知覆盖返回 unknown，调用方不能把它当成缺失后撤单。"""
    covered, kind = coverage(algo, pos_qty)
    if kind == "unknown":
        return "unknown"
    if kind == "full":
        return None
    qty = _num(pos_qty)
    if qty is None or qty <= 0:
        return "qty_unknown"
    tol = max(1e-6, abs(qty) * 1e-6)
    if covered + tol < qty:
        return "short"
    return None


def is_full_protection(algo, pos_qty, need_tp, direction=None, pos_side=None):
    if not algo_active(algo):
        return False
    if not side_matches(algo, direction, pos_side):
        return False
    if leg_problem(algo, need_tp):
        return False
    problem = coverage_problem(algo, pos_qty)
    return problem is None


def describe_algo(algo, inst="", pos_side=""):
    algo = algo or {}
    frac = close_fraction(algo)
    if frac is not None and frac + 1e-9 >= 1:
        cover = "closeFraction=1 全平"
    elif frac is not None and frac > 0:
        cover = f"closeFraction={algo.get('closeFraction')}"
    else:
        cover = f"sz={algo.get('sz') or ''}"
    inst_id = algo.get("instId") or inst or ""
    side = algo.get("posSide") or pos_side or ""
    return (f"算法单 instId={inst_id} posSide={side} algoId={algo.get('algoId') or ''} "
            f"ordType={algo.get('ordType') or ''} tpTriggerPx={algo.get('tpTriggerPx') or ''} "
            f"slTriggerPx={algo.get('slTriggerPx') or ''} {cover}")


def describe_order(order, inst=""):
    order = order or {}
    inst_id = order.get("instId") or inst or ""
    return (f"普通挂单 instId={inst_id} posSide={order.get('posSide') or ''} "
            f"ordId={order.get('ordId') or ''} ordType={order.get('ordType') or ''} "
            f"side={order.get('side') or ''} sz={order.get('sz') or ''} px={order.get('px') or ''} "
            f"state={order.get('state') or ''} reduceOnly={order.get('reduceOnly') or ''} "
            f"accFillSz={order.get('accFillSz') or ''}")


def qty_differs(old, new):
    new_n = _num(new)
    if new_n is None:
        return False
    old_n = _num(old)
    if old_n is None:
        return False
    return abs(old_n - new_n) > max(1e-6, abs(new_n) * 1e-8)


def price_differs(old, new):
    new_n = _num(new)
    if new_n is None or new_n <= 0:
        return False
    old_n = _num(old)
    if old_n is None:
        return False
    return abs(old_n - new_n) > max(1e-8, abs(new_n) * 1e-8)
