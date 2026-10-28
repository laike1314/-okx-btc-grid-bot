"""纯 Python 指标：EMA / ATR / ADX（Wilder）。"""


def ema(values, period):
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def true_ranges(c):
    return [max(c[i]["h"] - c[i]["l"], abs(c[i]["h"] - c[i - 1]["c"]), abs(c[i]["l"] - c[i - 1]["c"]))
            for i in range(1, len(c))]


def atr(c, period=14):
    tr = true_ranges(c)
    if len(tr) < period:
        return 0.0
    a = sum(tr[:period]) / period
    for t in tr[period:]:
        a = (a * (period - 1) + t) / period
    return a


def adx(c, period=14):
    """返回 (adx, +DI, -DI)。"""
    if len(c) < period * 2 + 1:
        return 0.0, 0.0, 0.0
    tr = true_ranges(c)
    pdm, ndm = [], []
    for i in range(1, len(c)):
        up, dn = c[i]["h"] - c[i - 1]["h"], c[i - 1]["l"] - c[i]["l"]
        pdm.append(up if up > dn and up > 0 else 0.0)
        ndm.append(dn if dn > up and dn > 0 else 0.0)
    atr_s, p_s, n_s = sum(tr[:period]), sum(pdm[:period]), sum(ndm[:period])
    dx = []
    pdi = ndi = 0.0
    for i in range(period, len(tr)):
        atr_s = atr_s - atr_s / period + tr[i]
        p_s = p_s - p_s / period + pdm[i]
        n_s = n_s - n_s / period + ndm[i]
        pdi = 100 * p_s / atr_s if atr_s else 0
        ndi = 100 * n_s / atr_s if atr_s else 0
        dx.append(100 * abs(pdi - ndi) / (pdi + ndi) if pdi + ndi else 0)
    if len(dx) < period:
        return 0.0, pdi, ndi
    a = sum(dx[:period]) / period
    for d in dx[period:]:
        a = (a * (period - 1) + d) / period
    return a, pdi, ndi
