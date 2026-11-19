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


def is_full_protection(algo, pos_qty, need_tp):
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
