"""Swing 组合策略（STRATEGY=swing_combo）。

不改 scalp 的打分和出场。这里只负责：
  4H 体制、截面动量、OI 脉冲、加权合成、家族出场参数、
  跨运行对账（交易所持仓 + state.json）。

OI 方向（文档约定，按顺序用第一个能定方向的）：
  1. 回看窗口内主动买量 - 主动卖量，失衡 >= SWING_TAKER_IMB_MIN
  2. 合约多空账户比：> 1+tilt 做多，< 1-tilt 做空（跟随账户倾斜，不反向）
  3. 窗口内价格残余漂移，绝对值至少 0.05 ATR
  4. 资金费反向：正资金费（多头付钱）做空，负资金费做多
价格在窗口内的波动超过 SWING_OI_MAX_FLAT_ATR 倍 ATR 时，OI 这条腿记 0（不是「价格没动」）。
"""
import math
import time

import config
from .indicators import atr, adx

AGENT = "swing"


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, float(x)))


def family_from_tag(tag):
    text = str(tag or "").strip().lower()
    if "mom" in text:
        return "momentum"
    if text == "swoi" or text.startswith("swoi"):
        return "oi"
    return ""


def profile_for(family):
    """出场参数。unknown 用更长的 OI 窗口，避免把认不出的仓过早时间平仓。"""
    if family == "momentum":
        return {"family": "momentum", "tp_atr": float(config.SWING_MOM_TP_ATR),
                "sl_atr": float(config.SWING_MOM_SL_ATR), "trail": True,
                "min_hold": int(config.SWING_MOM_MIN_HOLD_SEC),
                "max_hold": int(config.SWING_MOM_MAX_HOLD_SEC), "tag": "swmom"}
    fam = "oi" if family == "oi" else "unknown"
    return {"family": fam, "tp_atr": float(config.SWING_OI_TP_ATR),
            "sl_atr": float(config.SWING_OI_SL_ATR), "trail": False,
            "min_hold": int(config.SWING_OI_MIN_HOLD_SEC),
            "max_hold": int(config.SWING_OI_MAX_HOLD_SEC),
            "tag": "swoi" if fam == "oi" else ""}


def classify_regime(adx_v, dist_atr):
    """ADX 或价格偏离 MA 的 ATR 距离，任一达到阈值就是 trend。"""
    if float(adx_v or 0) >= config.SWING_REGIME_ADX:
        return "trend"
    if abs(float(dist_atr or 0)) >= config.SWING_REGIME_ATR_DIST:
        return "trend"
    return "range"


def rank_signed(values):
    """截面排名映射到 [-1, 1]。最高（最强动量）为 +1，最低为 -1。并列取平均名次。"""
    keys = [k for k, v in values.items() if v is not None]
    n = len(keys)
    if n <= 1:
        return {k: 0.0 for k in keys}
    order = sorted(keys, key=lambda k: float(values[k]))
    out = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and float(values[order[j + 1]]) == float(values[order[i]]):
            j += 1
        pct = ((i + j) / 2) / (n - 1)
        signed = pct * 2 - 1
        for k in order[i:j + 1]:
            out[k] = signed
        i = j + 1
    return out


def combine(regime, mom_score, mom_dir, oi_score, oi_dir):
    """加权合成。两条腿都有方向且相反：不做。同向：加 bonus，总分封顶 1。"""
    if regime == "trend":
        wm, wo = float(config.SWING_W_TREND_MOM), float(config.SWING_W_TREND_OI)
    else:
        wm, wo = float(config.SWING_W_RANGE_MOM), float(config.SWING_W_RANGE_OI)
    leg = float(config.SWING_MIN_LEG)

    def vote(score, direction):
        if direction in ("long", "short") and float(score or 0) >= leg:
            return direction
        return None

    mom_vote, oi_vote = vote(mom_score, mom_dir), vote(oi_score, oi_dir)
    blocked = bool(mom_vote and oi_vote and mom_vote != oi_vote)
    bonus = float(config.SWING_AGREE_BONUS) if (mom_vote and oi_vote and mom_vote == oi_vote) else 0.0
    if blocked or (mom_vote is None and oi_vote is None):
        return {"combined": 0.0, "direction": None, "blocked": blocked, "bonus": 0.0,
                "family": None, "w_mom": wm, "w_oi": wo}
    total = _clamp(wm * float(mom_score or 0) + wo * float(oi_score or 0) + bonus)
    direction = mom_vote or oi_vote
    contrib_m, contrib_o = wm * float(mom_score or 0), wo * float(oi_score or 0)
    if mom_vote and not oi_vote:
        family = "momentum"
    elif oi_vote and not mom_vote:
        family = "oi"
    elif contrib_m >= contrib_o:
        family = "momentum"
    else:
        family = "oi"
    return {"combined": total, "direction": direction, "blocked": False, "bonus": bonus,
            "family": family, "w_mom": wm, "w_oi": wo}


def fee_allows(tp_pct, round_trip):
    mult = float(config.SWING_MIN_EDGE_FEE_MULT)
    if mult <= 0:
        return True
    return float(tp_pct) + 1e-12 >= float(round_trip) * mult


def size_qty(equity, entry, ct_val, lot, min_sz, available=None, fraction=1.0, lever=1):
    """名义 ≈ 虚拟权益 × SWING_ALLOC_PCT / 最大同时持仓数 × fraction。

    equity 是虚拟权益（SWING_CAPITAL_USDT + 切换后已实现盈亏 + 本策略浮盈），
    不是交易所总权益，也不再先收成 min(权益, 可用保证金)。
    名义再封顶到可用保证金 × MARGIN_USE_FRAC，因此不会超过真实 availEq。
    杠杆另行设置，不把这个名义上限放大。fraction 是 51008 之后的缩小比例。
    """
    from agents.margin import use_frac
    slots = max(1, int(config.SWING_MAX_POSITIONS))
    base = float(equity or 0)
    try:
        frac = float(fraction if fraction is not None else 1.0)
    except (TypeError, ValueError):
        frac = 1.0
    if frac <= 0 or base <= 0:
        return None
    notional = base * float(config.SWING_ALLOC_PCT) / slots * frac
    if available is not None:
        # 名义本身不超过可用保证金 × MARGIN_USE_FRAC，杠杆不把上限放大。
        cap = max(0.0, float(available)) * use_frac()
        notional = min(notional, cap)
    per = float(entry) * float(ct_val)
    if per <= 0 or notional <= 0:
        return None
    qty = notional / per
    step = float(lot)
    if step <= 0:
        return None
    qty = math.floor(qty / step) * step
    if qty < float(min_sz):
        return None
    from okx_client import fmt_num
    return float(fmt_num(qty, step))


def hold_limits(meta):
    prof = profile_for((meta or {}).get("family") or "unknown")
    mn = meta.get("min_hold_sec") if meta and meta.get("min_hold_sec") else prof["min_hold"]
    mx = meta.get("max_hold_sec") if meta and meta.get("max_hold_sec") else prof["max_hold"]
    return int(mn or 0), int(mx or 0)


def time_exit_due(meta, now):
    """到期平仓。最短持仓未到之前不因时间出场（交易所止损止盈不受这条约束）。"""
    opened = float((meta or {}).get("opened_ts") or 0)
    if opened <= 0:
        return False
    mn, mx = hold_limits(meta)
    if mx <= 0:
        return False
    return (float(now) - opened) >= max(mx, mn)


def signal_exit_reason(meta, row, now):
    """最短持仓之后，持仓方向不再被合成信号支持就平。数据缺失不平。"""
    opened = float((meta or {}).get("opened_ts") or 0)
    if opened <= 0 or row is None:
        return None
    mn, _mx = hold_limits(meta)
    if float(now) - opened < mn:
        return None
    held = (meta or {}).get("direction")
    if row.get("blocked") or row.get("direction") != held:
        return "信号退出"
    if float(row.get("combined") or 0) < float(config.SWING_EXIT_SCORE):
        return "信号变弱"
    return None


def family_counts(positions, open_meta):
    live = {p.get("instId") for p in positions or []}
    counts = {"momentum": 0, "oi": 0, "unknown": 0}
    for inst, meta in (open_meta or {}).items():
        if inst not in live:
            continue
        fam = (meta or {}).get("family") or "unknown"
        if fam not in counts:
            fam = "unknown"
        counts[fam] += 1
    return counts


def our_position_count(positions, open_meta):
    live = {p.get("instId") for p in positions or []}
    return sum(1 for inst in (open_meta or {}) if inst in live)


def candle_features(candles):
    closes = [c["c"] for c in candles or []]
    look = max(int(config.SWING_MOM_LOOKBACK), int(config.SWING_OI_LOOKBACK), 1)
    need = max(int(config.SWING_MA_PERIOD), look + 1, int(config.SWING_ADX_PERIOD) * 2 + 2)
    if len(closes) < need:
        return None
    price = float(closes[-1])
    a = atr(candles, int(config.SWING_ATR_PERIOD))
    if not a or price <= 0:
        return None
    adx_v, _pdi, _ndi = adx(candles, int(config.SWING_ADX_PERIOD))
    ma = sum(closes[-int(config.SWING_MA_PERIOD):]) / int(config.SWING_MA_PERIOD)
    dist = (price - ma) / a
    prev = float(closes[-1 - int(config.SWING_MOM_LOOKBACK)])
    ret = (price / prev - 1.0) if prev else 0.0
    atr_pct = a / price
    mom_raw = ret / atr_pct if atr_pct else 0.0
    bar_ts = int(candles[-1].get("ts") or 0)
    return {"price": price, "atr": a, "atr_pct": atr_pct, "adx": adx_v, "dist_atr": dist,
            "mom_raw": mom_raw, "regime": classify_regime(adx_v, dist), "bar_ts": bar_ts}


def _oi_change_z(series, lookback):
    vals = [float(x or 0) for x in series or []]
    if len(vals) <= lookback:
        return None, None
    chgs = []
    for i in range(lookback, len(vals)):
        prev = vals[i - lookback]
        if prev <= 0:
            continue
        chgs.append((vals[i] - prev) / prev)
    if not chgs:
        return None, None
    last = chgs[-1]
    if len(chgs) < 5:
        return last, None
    base = chgs[:-1]
    mu = sum(base) / len(base)
    var = sum((x - mu) ** 2 for x in base) / len(base)
    sd = math.sqrt(var)
    z = (last - mu) / sd if sd > 1e-12 else None
    return last, z


def oi_direction(taker, look, ls_ratio, drift, atr_px, funding):
    buy = sell = 0.0
    for row in list(taker or [])[-look:]:
        buy += float(row.get("buy") or 0)
        sell += float(row.get("sell") or 0)
    total = buy + sell
    if total > 0:
        imb = (buy - sell) / total
        if abs(imb) >= float(config.SWING_TAKER_IMB_MIN):
            return "long" if imb > 0 else "short"
    if ls_ratio is not None:
        tilt = float(config.SWING_LS_TILT)
        if float(ls_ratio) >= 1 + tilt:
            return "long"
        if float(ls_ratio) <= 1 - tilt and float(ls_ratio) > 0:
            return "short"
    if atr_px and abs(float(drift)) >= 0.05 * float(atr_px):
        return "long" if float(drift) > 0 else "short"
    if funding is not None and abs(float(funding)) >= float(config.SWING_FUNDING_FADE):
        return "short" if float(funding) > 0 else "long"
    return None


def bar_span_sec(bar):
    text = str(bar or "").strip()
    try:
        if text.endswith("m"):
            return int(text[:-1]) * 60
        if text.endswith("H"):
            return int(text[:-1]) * 3600
        if text.endswith("D"):
            return int(text[:-1]) * 86400
    except ValueError:
        return 0
    return 0


def close_age_sec(open_ts_ms, bar_sec, now_ms):
    """K 线 ts 是开盘时间。新鲜度按收盘时间算，避免把刚收盘的 4H 误判成 8 小时前的缓存。"""
    if not open_ts_ms or not bar_sec:
        return None
    close_ms = int(open_ts_ms) + int(bar_sec) * 1000
    return (int(now_ms) - close_ms) / 1000.0


def oi_block_reason(info, score, direction):
    """OI 腿为什么是 0。不改变分数，只给日志一个原因。"""
    info = info or {}
    if direction in ("long", "short") and float(score or 0) > 0:
        return ""
    if info.get("flat") is False:
        return "not_flat"
    if info.get("oi_chg") is None:
        return "no_oi" if info.get("candle_ts") else "no_data"
    if float(info.get("oi_chg") or 0) <= 0:
        return "oi_not_up"
    if direction not in ("long", "short"):
        return "no_direction"
    return "below_bar"


def oi_pulse(candles, oi_hist, taker, ls_ratio, funding, atr_px):
    """返回 (score 0-1, direction or None, debug)。价格不平静或 OI 没增加则分数为 0。

    分数公式保持原样：OI 增幅要超过约 2% 或 z>0.5，成交量高于自身均量才开始计分。
    不因为想开仓就把门槛放低。
    """
    look = max(1, int(config.SWING_OI_LOOKBACK))
    closes = [c["c"] for c in candles or []]
    vols = [c["vol"] for c in candles or []]
    info = {"flat": False, "oi_chg": None, "oi_z": None, "vol_ratio": None,
            "oi_ts": None, "candle_ts": int(candles[-1]["ts"]) if candles else None}

    def done(score, direction):
        info["reason"] = oi_block_reason(info, score, direction)
        return score, direction, info

    if len(closes) <= look or not atr_px:
        return done(0.0, None)
    move = abs(float(closes[-1]) - float(closes[-1 - look]))
    flat = move <= float(atr_px) * float(config.SWING_OI_MAX_FLAT_ATR)
    info["flat"] = flat
    info["move_atr"] = move / float(atr_px) if atr_px else None
    hist = [x for x in (oi_hist or []) if isinstance(x, dict)]
    if hist:
        info["oi_ts"] = int(hist[-1].get("ts") or 0) or None
    series = [(x.get("oiUsd") or x.get("oi") or 0) for x in hist]
    chg, z = _oi_change_z(series, look)
    info["oi_chg"], info["oi_z"] = chg, z
    if not flat or chg is None or chg <= 0:
        return done(0.0, None)
    z_part = _clamp((z - 0.5) / 2.5) if z is not None else 0.0
    abs_part = _clamp((chg - 0.02) / 0.15)
    oi_level = max(z_part, abs_part)
    recent = sum(vols[-look:]) / look
    base_vols = vols[:-look]
    base = sum(base_vols) / len(base_vols) if base_vols else 0.0
    vol_ratio = (recent / base) if base > 0 else 1.0
    info["vol_ratio"] = vol_ratio
    v_part = _clamp((vol_ratio - 1.0) / 1.5)
    score = _clamp(0.6 * oi_level + 0.4 * v_part)
    direction = oi_direction(taker, look, ls_ratio, float(closes[-1]) - float(closes[-1 - look]),
                             atr_px, funding)
    if direction is None:
        return done(0.0, None)
    return done(score, direction)


def assemble(features):
    """features: instId -> 单币原始特征（含 mom_raw / oi_score / oi_dir / regime / price...）。"""
    signed = rank_signed({k: v.get("mom_raw") for k, v in features.items() if v.get("mom_raw") is not None})
    rows = []
    for inst, feat in features.items():
        s = float(signed.get(inst, 0.0))
        mom_score = abs(s)
        mom_dir = None
        if mom_score >= float(config.SWING_MIN_LEG):
            mom_dir = "long" if s > 0 else "short"
        merged = combine(feat.get("regime") or "range", mom_score, mom_dir,
                         feat.get("oi_score") or 0, feat.get("oi_dir"))
        prof = profile_for(merged["family"] or "oi")
        rows.append({
            "instId": inst, "direction": merged["direction"], "score": merged["combined"],
            "combined": merged["combined"], "blocked": merged["blocked"], "bonus": merged["bonus"],
            "family": merged["family"], "regime": feat.get("regime"),
            "mom_score": mom_score, "mom_dir": mom_dir, "oi_score": feat.get("oi_score") or 0,
            "oi_dir": feat.get("oi_dir"), "price": feat.get("price"), "atr": feat.get("atr"),
            "atr_pct": feat.get("atr_pct"), "adx": feat.get("adx"), "dist_atr": feat.get("dist_atr"),
            "tp_atr": prof["tp_atr"] if merged["family"] else None,
            "sl_atr": prof["sl_atr"] if merged["family"] else None,
            "min_hold_sec": prof["min_hold"] if merged["family"] else None,
            "max_hold_sec": prof["max_hold"] if merged["family"] else None,
            "tag": prof["tag"] if merged["family"] in ("momentum", "oi") else "",
            "oi_info": feat.get("oi_info") or {},
            "oi_reason": (feat.get("oi_info") or {}).get("reason") or "",
            "bar_ts": feat.get("bar_ts"),
        })
    rows.sort(key=lambda r: (-(r["combined"] or 0), r["instId"]))
    return rows


def _neutral_oi():
    return 0.0, None, {"flat": False, "oi_chg": None, "oi_z": None, "vol_ratio": None}


def score_all(client, journal, symbols, pulse=None):
    """拉完全部交易池再排名。pulse 在每 8 个合约后调用一次，用来穿插持仓监控。"""
    features = {}
    bar = config.SWING_REGIME_BAR
    limit = max(120, int(config.SWING_OI_HIST_LIMIT) + int(config.SWING_OI_LOOKBACK) + 5)
    for i, inst in enumerate(symbols or []):
        if pulse and i and i % 8 == 0:
            try:
                pulse()
            except Exception as e:
                journal.log(AGENT, "pulse_error", f"扫描间隙监控失败: {e}")
        try:
            candles = client.candles(inst, bar, limit)
            feat = candle_features(candles)
        except Exception as e:
            journal.log(AGENT, "candle_error", f"{inst} {bar} K线失败: {e}", inst=inst)
            continue
        if not feat:
            journal.log(AGENT, "no_feature", f"{inst} {bar} K线不足以计算体制/动量", inst=inst)
            continue
        oi_score, oi_dir, oi_info = _neutral_oi()
        try:
            hist = client.open_interest_history(inst, config.SWING_OI_PERIOD, int(config.SWING_OI_HIST_LIMIT))
            taker = client.taker_volume(inst, config.SWING_OI_PERIOD, int(config.SWING_OI_HIST_LIMIT))
            ls_rows = client.long_short_ratio(inst, config.SWING_OI_PERIOD, 5)
            ls = ls_rows[-1]["ratio"] if ls_rows else None
            funding = client.funding_rate(inst)
            oi_score, oi_dir, oi_info = oi_pulse(candles, hist, taker, ls, funding, feat["atr"])
            span_ms = (bar_span_sec(config.SWING_OI_PERIOD) or bar_span_sec(bar) or 0) * 1000
            cts, ots = feat.get("bar_ts"), oi_info.get("oi_ts")
            if cts and ots and span_ms and abs(int(cts) - int(ots)) > span_ms:
                oi_info["oi_lag_sec"] = abs(int(cts) - int(ots)) / 1000.0
                oi_info["reason"] = "oi_misaligned"
                oi_score, oi_dir = 0.0, None
                journal.log(AGENT, "oi_misaligned",
                            f"{inst} OI 末根与 4H 收盘相差 {oi_info['oi_lag_sec']:.0f}s，这根 OI 腿记 0",
                            inst=inst, lag=oi_info["oi_lag_sec"])
        except Exception as e:
            journal.log(AGENT, "oi_error", f"{inst} OI/主动成交获取失败，本轮 OI 腿记 0: {e}", inst=inst)
            oi_info = dict(_neutral_oi()[2], reason="fetch_error", error=str(e))
        feat.update(oi_score=oi_score, oi_dir=oi_dir, oi_info=oi_info)
        features[inst] = feat
    rows = assemble(features)
    _log_freshness(journal, bar, features)
    for row in rows:
        reason = row.get("oi_reason") or ""
        chg = (row.get("oi_info") or {}).get("oi_chg")
        chg_txt = "" if chg is None else f" chg={chg:.3%}"
        why = (f"{row['regime']} mom {row['mom_score']:.2f}/{row['mom_dir']} "
               f"oi {row['oi_score']:.2f}/{row['oi_dir']}"
               f"{(' ' + reason) if reason else ''}{chg_txt} → {row['combined']:.2f}"
               f"{' 分歧' if row['blocked'] else ''} {row['direction'] or '-'}"
               f"/{row['family'] or '-'}")
        journal.log(AGENT, "score", f"{row['instId']} {why}", inst=row["instId"],
                    regime=row["regime"], mom=row["mom_score"], oi=row["oi_score"],
                    combined=row["combined"], direction=row["direction"], family=row["family"],
                    blocked=row["blocked"], oi_reason=reason)
    return rows


def _log_freshness(journal, bar, features):
    """每轮一行：已收盘 K 线距今多久。4H 信号在两根收盘之间分数不变，不是行情缓存。"""
    span = bar_span_sec(bar)
    if not span:
        return
    now_ms = int(time.time() * 1000)
    ages, stale = [], []
    for inst, feat in features.items():
        ts = feat.get("bar_ts")
        age = close_age_sec(ts, span, now_ms)
        if age is None:
            continue
        ages.append(age)
        if age > 2 * span:
            stale.append(inst)
            journal.log(AGENT, "stale_candle",
                        f"{inst} {bar} 收盘距今 {age:.0f}s，超过 2 根，怀疑行情没更新",
                        inst=inst, age=age)
    if not ages:
        return
    journal.log(AGENT, "freshness",
                f"{bar} 已收盘距今 {min(ages):.0f}-{max(ages):.0f}s"
                f"（一根 {span}s；超过 2 根才算陈旧，本轮 {len(stale)} 个）",
                stale=len(stale), symbols=len(ages))


def entry_candidates(rows):
    thr = float(config.SWING_ENTRY_THRESHOLD)
    out = []
    for row in rows or []:
        if row.get("blocked") or not row.get("direction") or not row.get("family"):
            continue
        if float(row.get("combined") or 0) + 1e-12 < thr:
            continue
        out.append(row)
    return out


def _reduce_only(order):
    return str((order or {}).get("reduceOnly") or "").strip().lower() in ("true", "1", "yes")


def note_residual(state, inst, ord_id):
    if not inst or not ord_id:
        return
    book = state.d.setdefault("residual_orders", {})
    ids = list(book.get(inst) or [])
    if ord_id not in ids:
        ids.append(ord_id)
    book[inst] = ids


def clear_residual(state, inst, ord_id=None):
    book = state.d.get("residual_orders") or {}
    if inst not in book:
        return
    if ord_id is None:
        book.pop(inst, None)
        return
    left = [i for i in book.get(inst) or [] if i != ord_id]
    if left:
        book[inst] = left
    else:
        book.pop(inst, None)


def residual_blocks(state, inst):
    return bool((state.d.get("residual_orders") or {}).get(inst))


def read_positions(client):
    """成功才返回列表。失败返回 (None, error)，空列表表示交易所确认没有持仓。"""
    try:
        rows = client.positions()
    except Exception as e:
        return None, e
    if rows is None:
        return None, RuntimeError("positions 返回 None")
    return list(rows), None


def settle_entry_residuals(client, state, journal, positions, scan_tags=False):
    """撤掉本策略合约上还活着的开仓残单。撤完再读持仓，避免漏掉撤单瞬间的成交。

    读不到普通挂单时不能当成没有残单。减仓单不动。失败也不另下一张反向单。
    启动对账可以再扫一遍带 swmom/swoi 标签的委托，把重启前没写进账本的残单也撤掉。
    """
    from .protection import ack_error, ack_is_gone
    positions = list(positions or [])
    book = state.d.get("open_meta") or {}
    remembered = state.d.get("residual_orders") or {}
    insts = {p.get("instId") for p in positions if p.get("instId")}
    insts.update(book.keys())
    insts.update(remembered.keys())
    if scan_tags:
        list_all = getattr(client, "pending_orders", None)
        if list_all:
            try:
                for order in list_all() or []:
                    if str(order.get("tag") or "") in ("swmom", "swoi") and order.get("instId"):
                        insts.add(order["instId"])
            except Exception as e:
                journal.log(AGENT, "orders_read_fail",
                            f"扫描带标签的普通挂单失败，不能当成没有残单: {e}")
            errors = list(getattr(client, "last_order_errors", None) or [])
            if errors:
                journal.log(AGENT, "orders_read_fail",
                            f"普通挂单有产品类型读取失败，不能当成已经扫全: {'; '.join(errors)}")
    fn = getattr(client, "open_orders", None)
    if not fn or not insts:
        return positions
    cancelled = False
    for inst in sorted(i for i in insts if i):
        read_ok = True
        try:
            orders = list(fn(inst) or [])
        except Exception as e:
            read_ok = False
            orders = []
            journal.log(AGENT, "orders_read_fail",
                        f"{inst} 回读普通挂单失败，不能当成没有残单: {e}", inst=inst)
        from .protection import describe_order
        seen = []
        for order in orders:
            journal.log(AGENT, "open_order", f"{inst} {describe_order(order, inst)}",
                        inst=inst, ordId=order.get("ordId"), ordType=order.get("ordType"))
            if _reduce_only(order):
                continue
            if order.get("ordId"):
                seen.append(order.get("ordId"))
        if read_ok:
            for oid in list(remembered.get(inst) or []):
                if oid not in seen:
                    clear_residual(state, inst, oid)
        else:
            for oid in remembered.get(inst) or []:
                if oid not in seen:
                    seen.append(oid)
        for oid in seen:
            try:
                ack = client.cancel_order(inst, oid)
            except Exception as e:
                note_residual(state, inst, oid)
                journal.log(AGENT, "residual_live",
                            f"{inst} 开仓残单 {oid} 撤销失败，本轮不开新仓: {e}",
                            inst=inst, ordId=oid)
                continue
            if ack_is_gone(ack):
                clear_residual(state, inst, oid)
                cancelled = True
                continue
            bad = ack_error(ack)
            if bad:
                note_residual(state, inst, oid)
                journal.log(AGENT, "residual_live",
                            f"{inst} 开仓残单 {oid} 撤销未成功 sCode={bad[0]} sMsg={bad[1]}，"
                            f"本轮不开新仓",
                            inst=inst, ordId=oid, code=bad[0], sMsg=bad[1])
                continue
            clear_residual(state, inst, oid)
            cancelled = True
            journal.log(AGENT, "residual_cancel",
                        f"{inst} 撤销开仓残单 {oid}，随后按交易所持仓改账本张数",
                        inst=inst, ordId=oid)
    if not cancelled:
        return positions
    fresh, err = read_positions(client)
    if err:
        journal.log(AGENT, "reconcile_pos_err",
                    f"撤残单后读持仓失败，沿用撤单前的持仓，不把失败当成空仓: {err}")
        return positions
    return fresh


def apply_exchange_position(meta, position, journal):
    """用交易所持仓改已有账本的张数和开仓均价。空字段只是补齐，不算一笔新成交。

    返回 True 表示相对原账本发生了数量或成本变化。方向不一致时改成交易所方向，不下单。
    """
    from .protection import price_differs, qty_differs
    inst = position.get("instId")
    changed = False
    side = position.get("side")
    if side and meta.get("direction") and side != meta.get("direction"):
        journal.log(AGENT, "side_drift",
                    f"{inst} 账本方向 {meta.get('direction')} 与交易所 {side} 不一致，"
                    f"按持仓改正，不另下反向开仓单",
                    inst=inst, book_side=meta.get("direction"), exch_side=side)
        meta["direction"] = side
        changed = True
    elif side and not meta.get("direction"):
        meta["direction"] = side
    new_qty, new_entry = position.get("qty"), position.get("avgPx")
    old_qty, old_entry = meta.get("qty"), meta.get("entry")
    qty_moved = qty_differs(old_qty, new_qty)
    entry_moved = price_differs(old_entry, new_entry)
    if old_qty in (None, "") and new_qty not in (None, ""):
        meta["qty"] = new_qty
    elif qty_moved or entry_moved:
        journal.log(AGENT, "qty_sync",
                    f"{inst} 账本 {old_qty}张 @ {old_entry} 与交易所 {new_qty}张 @ {new_entry} 不一致，"
                    f"按持仓改账本。这不是新开仓，数量也不叠加",
                    inst=inst, book_qty=old_qty, exch_qty=new_qty,
                    book_entry=old_entry, exch_entry=new_entry)
        meta["qty"] = new_qty
        if new_entry not in (None, "", 0, "0"):
            try:
                if float(new_entry) > 0:
                    meta["entry"] = float(new_entry)
            except (TypeError, ValueError):
                meta["entry"] = new_entry
        changed = True
    if position.get("posId") and not meta.get("pos_id"):
        meta["pos_id"] = str(position.get("posId"))
    if old_entry in (None, "") and new_entry not in (None, "", 0, "0"):
        try:
            if float(new_entry) > 0:
                meta["entry"] = float(new_entry)
        except (TypeError, ValueError):
            pass
    return changed or qty_moved or entry_moved


def _bracket_from_client(client, inst):
    sl = tp = None
    try:
        algos = client.pending_algos(inst)
    except Exception as e:
        return None, None, e
    for algo in algos or []:
        try:
            if algo.get("slTriggerPx") not in (None, "", "0", 0):
                sl = float(algo.get("slTriggerPx"))
        except (TypeError, ValueError):
            pass
        try:
            if algo.get("tpTriggerPx") not in (None, "", "0", 0):
                tp = float(algo.get("tpTriggerPx"))
        except (TypeError, ValueError):
            pass
    return sl, tp, None


def reconcile(client, state, journal, executor=None):
    """用交易所持仓和最近平仓记录补齐 state。没有标签、也不在账本里的仓不接管。

    起点、峰值和停机看虚拟权益，不看交易所总权益。
    """
    now = state.now()
    positions, pos_err = read_positions(client)
    if pos_err:
        journal.log(AGENT, "reconcile_pos_err",
                    f"读持仓失败，不把失败当成空仓，不改账本、不平仓、不把浮盈记成 0: {pos_err}")
        return
    positions = settle_entry_residuals(client, state, journal, positions, scan_tags=True)
    meta_map = state.d.setdefault("open_meta", {})
    qty_fixes = 0
    live = set()
    for p in positions:
        inst = p.get("instId")
        if not inst:
            continue
        live.add(inst)
        meta = meta_map.get(inst)
        tag = ""
        if meta is None or not (meta or {}).get("family"):
            try:
                tag = client.latest_fill_tag(inst) or ""
            except Exception as e:
                journal.log(AGENT, "tag_err", f"{inst} 读成交标签失败: {e}", inst=inst)
            fam = family_from_tag(tag)
            if meta is None and not fam:
                journal.log(AGENT, "skip_foreign",
                            f"{inst} 无本策略状态也无 swmom/swoi 标签，不接管", inst=inst)
                continue
            if meta is None:
                meta = {}
                meta_map[inst] = meta
            if fam and not meta.get("family"):
                meta["family"] = fam
                journal.log(AGENT, "tag_family", f"{inst} 由订单标签 {tag} 识别为 {fam}", inst=inst)
        meta.setdefault("direction", p.get("side"))
        drifted = apply_exchange_position(meta, p, journal)
        if drifted:
            qty_fixes += 1
        meta.setdefault("qty", p.get("qty"))
        meta.setdefault("entry", p.get("avgPx"))
        if not meta.get("opened_ts"):
            ms = int(p.get("cTime") or 0)
            if ms > 10 ** 11:
                meta["opened_ts"] = ms / 1000.0
                meta["open_ms"] = ms
            else:
                meta["opened_ts"] = now
                meta["open_ms"] = int(now * 1000)
                journal.log(AGENT, "open_time_unknown",
                            f"{inst} 没有开仓时间，最短持仓从本次对账起算", inst=inst)
        if not meta.get("family"):
            meta["family"] = "unknown"
            journal.log(AGENT, "family_unknown",
                        f"{inst} 认不出入场家族，按 OI 的更长持仓窗口管理，不跟踪止损", inst=inst)
        sl, tp, read_err = _bracket_from_client(client, inst)
        if read_err:
            journal.log(AGENT, "protect_read_fail",
                        f"{inst} 回读算法单失败，不能当成没有保护，本次不补挂: {read_err}", inst=inst)
        else:
            if sl and not meta.get("sl"):
                meta["sl"] = sl
            if tp and not meta.get("tp"):
                meta["tp"] = tp
        if read_err:
            pass
        elif executor is not None and (not meta.get("sl") or not meta.get("tp")):
            try:
                got = executor.protect_adopted(p, meta)
            except Exception as e:
                journal.log(AGENT, "protect_err", f"{inst} 补挂止损止盈失败: {e}", inst=inst)
                got = None
            if isinstance(got, tuple) and len(got) == 2:
                meta["sl"], meta["tp"] = got
        elif executor is not None and meta.get("sl") and meta.get("tp"):
            try:
                executor.confirm_protection(
                    inst, meta.get("direction") or p.get("side"),
                    meta.get("sl"), meta.get("tp"), p.get("qty"), p.get("posSide"),
                    reason="qty_sync" if drifted else "startup")
                from .protection import tighter_sl
                live_sl = getattr(executor, "last_protect_sl", None)
                better = tighter_sl(meta.get("direction") or p.get("side"), meta.get("sl"), live_sl)
                if better and meta.get("sl") not in (None, "") and abs(float(better) - float(meta["sl"])) > 1e-8:
                    journal.log(AGENT, "sl_keep_tighter",
                                f"{inst} 账本止损 {meta.get('sl')} 比交易所 {better} 更差，"
                                f"按更紧的止损改账本，不把交易所止损放宽",
                                inst=inst, sl=better, book_sl=meta.get("sl"))
                    meta["sl"] = better
            except Exception as e:
                journal.log(AGENT, "protect_err", f"{inst} 回读保护单失败: {e}", inst=inst)
        prof = profile_for(meta.get("family") or "unknown")
        meta.setdefault("min_hold_sec", prof["min_hold"])
        meta.setdefault("max_hold_sec", prof["max_hold"])
    if int(config.SWING_COOLDOWN_SEC) > 0 and hasattr(client, "recent_closed"):
        try:
            hist = client.recent_closed(100) or []
        except Exception as e:
            hist = []
            journal.log(AGENT, "hist_err", f"读平仓历史失败，冷却只用已有 state: {e}")
        for row in hist:
            inst = row.get("instId")
            ut = float(row.get("uTime") or 0)
            if ut > 10 ** 11:
                ut /= 1000.0
            if not inst or ut <= 0:
                continue
            until = ut + int(config.SWING_COOLDOWN_SEC)
            if until <= now:
                continue
            prev = float(state.d.setdefault("symbol_cooldown", {}).get(inst, 0) or 0)
            if until > prev:
                state.d["symbol_cooldown"][inst] = until
    our_upl = 0.0
    for p in positions:
        if p.get("instId") not in meta_map:
            continue
        try:
            our_upl += float(p.get("upl") or 0)
        except (TypeError, ValueError):
            pass
    state.d["swing_last_our_upl"] = our_upl
    from agents.margin import swing_equity_status, virtual_equity
    try:
        status = swing_equity_status(state, journal, our_upl)
    except Exception as e:
        journal.log(AGENT, "equity_mark_err", f"虚拟权益更新失败，峰值暂不更新: {e}")
        status = None
    if config.SWING_RESET_HALT and state.d.get("swing_halted"):
        veq = virtual_equity(state, our_upl) if status is None else status["equity"]
        state.d["swing_halted"] = False
        state.d["swing_start_equity"] = veq
        state.d["swing_peak_equity"] = veq
        journal.log(AGENT, "halt_reset",
                    f"SWING_RESET_HALT=1，已解除账户停机，起点和峰值重置为当前虚拟权益 {veq:.2f}")
        status = {"equity": veq, "start": veq, "peak": veq}
    exch = None
    try:
        exch = float(client.equity())
    except Exception as e:
        journal.log(AGENT, "reconcile_equity_err",
                    f"读交易所权益失败（不影响虚拟本金和定仓）: {e}")
    state.d["swing_last_reconcile"] = now
    state.save()
    ours = sum(1 for inst in meta_map if inst in live)
    veq_s = None if not status else round(status["equity"], 2)
    exch_s = None if exch is None else round(exch, 2)
    journal.log(AGENT, "reconcile",
                f"对账完成：本策略持仓 {ours}，交易所持仓 {len(positions)}，"
                f"账本按持仓改写 {qty_fixes}，"
                f"虚拟权益 {veq_s} 起始 {state.d.get('swing_start_equity')} "
                f"峰值 {state.d.get('swing_peak_equity')} "
                f"交易所USDT权益 {exch_s}（只作对照）"
                f"{'，账户停机中' if state.d.get('swing_halted') else ''}")


def enforce_account_stop(agents):
    """较虚拟权益的起始值回撤达到停机线：记下停机，并平掉本策略自己的仓。不平陌生仓。"""
    client, state, journal, ex = agents["client"], agents["state"], agents["journal"], agents["executor"]
    if state.d.get("swing_halted"):
        _flatten_ours(client, state, journal, ex, "账户停机，平掉本策略剩余仓位")
        return
    from agents.margin import swing_equity_status
    try:
        status = swing_equity_status(state, journal)
    except Exception as e:
        journal.log(AGENT, "equity_error", f"检查虚拟权益止损失败: {e}")
        return
    veq, start = status["equity"], status["start"]
    if start <= 0:
        return
    if veq <= start * (1 - float(config.SWING_ACCOUNT_STOP_PCT)):
        state.d["swing_halted"] = True
        state.save()
        journal.log(AGENT, "account_stop",
                    f"虚拟权益 {veq:.2f} 较起始 {start:.2f} 回撤达到 {config.SWING_ACCOUNT_STOP_PCT:.0%}，停机并平本策略仓")
        _flatten_ours(client, state, journal, ex, "账户回撤止损")


def _flatten_ours(client, state, journal, ex, reason):
    try:
        positions = list(client.positions() or [])
    except Exception as e:
        journal.log(AGENT, "flatten_err", f"读取持仓失败，未能平仓: {e}")
        return
    book = state.d.get("open_meta") or {}
    for p in positions:
        if p.get("instId") not in book:
            continue
        meta = book.setdefault(p["instId"], {})
        meta["close_reason"] = reason
        try:
            ex.close(p, reason)
        except Exception as e:
            journal.log(AGENT, "flatten_err", f"{p.get('instId')} 平仓失败: {e}", inst=p.get("instId"))
    state.save()


def run_cycle(agents, monitor, can_open):
    """一整轮：监控 → 账户止损 → 全池打分 → 信号出场 → 再开新仓。"""
    journal = agents["journal"]
    client = agents["client"]
    state = agents["state"]
    monitor(agents)
    enforce_account_stop(agents)
    monitor(agents)

    def pulse():
        monitor(agents)

    rows = score_all(client, journal, list(config.SYMBOLS), pulse=pulse)
    by_inst = {r["instId"]: r for r in rows}
    positions, pos_err = read_positions(client)
    if pos_err:
        journal.log(AGENT, "reconcile_pos_err",
                    f"读持仓失败，本轮不按空仓处理，不平仓也不开新仓: {pos_err}")
        return
    _apply_signal_exits(agents, positions, by_inst)
    positions = monitor(agents)
    if positions is None or not can_open(agents, positions):
        return
    held = {p["instId"] for p in positions}
    for cand in entry_candidates(rows):
        if cand["instId"] in held or residual_blocks(state, cand["instId"]):
            if residual_blocks(state, cand["instId"]):
                journal.log(AGENT, "residual_live",
                            f"{cand['instId']} 还有未撤开仓残单，本轮不开新仓",
                            inst=cand["instId"])
            continue
        try:
            from agents.margin import swing_equity_status
            equity = swing_equity_status(state, journal)["equity"]
            ok, why = agents["risk"].check(cand, positions, equity)
        except Exception as e:
            journal.log("risk", "error", f"{cand['instId']} 风控检查失败: {e}", inst=cand["instId"])
            continue
        journal.log("risk", "approve" if ok else "reject", f"{cand['instId']} {why}",
                    inst=cand["instId"], ok=ok)
        if not ok:
            continue
        try:
            from agents.margin import scale_for, on_margin_reject, on_margin_filled
            scale = scale_for(state, cand["instId"])
            plan = agents["position"].plan(cand, equity, size_scale=scale)
            fill = agents["executor"].open(plan) if plan else None
        except Exception as e:
            journal.log("executor", "error", f"{cand['instId']} 开仓流程失败: {e}", inst=cand["instId"])
            continue
        if isinstance(fill, dict) and str(fill.get("rejected") or "") == "51008":
            on_margin_reject(state, journal, cand["instId"])
            continue
        resid = getattr(agents["executor"], "residual_ord_id", None)
        if resid:
            note_residual(state, cand["instId"], resid)
            state.save()
        if fill and agents["equity"].register_open(plan, fill):
            on_margin_filled(state, cand["instId"])
            positions = client.positions()
            held = {p["instId"] for p in positions}
        if not can_open(agents, positions):
            return


def _apply_signal_exits(agents, positions, by_inst):
    state, journal, ex = agents["state"], agents["journal"], agents["executor"]
    now = state.now()
    book = state.d.get("open_meta") or {}
    for p in positions or []:
        inst = p.get("instId")
        meta = book.get(inst)
        if not meta:
            continue
        row = by_inst.get(inst)
        if inst not in by_inst:
            continue
        reason = signal_exit_reason(meta, row, now)
        if not reason:
            mn, _mx = hold_limits(meta)
            opened = float(meta.get("opened_ts") or 0)
            if opened and now - opened < mn and row is not None:
                journal.log(AGENT, "min_hold",
                            f"{inst} 未到最短持仓 {mn}s（已持有 {int(now - opened)}s），不因信号平仓",
                            inst=inst)
            continue
        meta["close_reason"] = reason
        journal.log(AGENT, "signal_exit", f"{inst} {reason}", inst=inst, family=meta.get("family"))
        try:
            ex.close(p, reason)
        except Exception as e:
            journal.log(AGENT, "signal_exit_err", f"{inst} 信号平仓失败: {e}", inst=inst)
    state.save()


def _retry_and_gate_checks():
    """空行情可重试；下单 POST 遇到空响应不重试。波段点差 20bps、成交额 200 万，剥头皮阈值不动。"""
    import okx_client
    from okx_client import OkxClient, OkxError

    class _Resp:
        def __init__(self, status, payload, text=None):
            self.status_code = status
            self._payload = payload
            self.text = text if text is not None else (__import__("json").dumps(payload))

        def json(self):
            if isinstance(self._payload, Exception):
                raise self._payload
            return self._payload

    class _Sess:
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = []

        def request(self, method, url, headers=None, data=None, timeout=None):
            self.calls.append(method)
            return self.responses.pop(0)

    old_gap = config.PUBLIC_REQ_GAP_SEC
    config.PUBLIC_REQ_GAP_SEC = 0
    slept = []
    real_sleep = okx_client.time.sleep
    okx_client.time.sleep = lambda s: slept.append(s)
    try:
        client = OkxClient.__new__(OkxClient)
        client.key, client.secret, client.passphrase = "k", "s", "p"
        client.base = "https://www.okx.com"
        client._next_get_at = 0.0
        client._next_rubik_at = 0.0
        client.s = _Sess([
            _Resp(200, {}),
            _Resp(200, {"code": None, "msg": None, "data": None}),
            _Resp(200, {"code": "0", "data": [{"ok": 1}]}),
        ])
        got = client.request("GET", "/api/v5/market/candles", {"instId": "BTC-USDT-SWAP"})
        assert got == [{"ok": 1}], got
        assert client.s.calls == ["GET", "GET", "GET"], client.s.calls

        client.s = _Sess([_Resp(200, {"code": None, "msg": None})])
        try:
            client.request("POST", "/api/v5/trade/order", body={"instId": "BTC-USDT-SWAP"}, retries=4)
            raise AssertionError("空的下单响应不该被当成成功")
        except OkxError as e:
            assert e.code == "EMPTY", e
        assert client.s.calls == ["POST"], client.s.calls

        client.s = _Sess([_Resp(200, None, text="")])
        client.s.responses[0].json = lambda: (_ for _ in ()).throw(ValueError("empty"))
        try:
            client.request("POST", "/api/v5/trade/order", body={"sz": "1"}, retries=3)
            raise AssertionError("下单网络错误不该重试后还返回")
        except OkxError as e:
            assert "未重试" in str(e), e
        assert client.s.calls == ["POST"]

        client.s = _Sess([
            _Resp(429, {"code": "50011", "msg": "rate"}),
            _Resp(200, {"code": "0", "data": [{"ordId": "1"}]}),
        ])
        got = client.request("POST", "/api/v5/trade/order", body={"instId": "BTC-USDT-SWAP"}, retries=3)
        assert got == [{"ordId": "1"}], got
        assert client.s.calls == ["POST", "POST"]
    finally:
        config.PUBLIC_REQ_GAP_SEC = old_gap
        okx_client.time.sleep = real_sleep

    from agents.journal import Journal
    from agents.fees import FeeBook
    from agents.risk import RiskGate
    from agents.state import SharedState
    import tempfile

    class _C:
        def ticker(self, inst):
            return {"bid": 100.0, "ask": 100.07, "last": 100.0, "vol_usdt_24h": 9_000_000.0}

        def mark_price(self, inst):
            return 100.0

        def index_price(self, inst):
            return 100.0

        def funding_rate(self, inst):
            return 0.0

    class _Wide(_C):
        def ticker(self, inst):
            return {"bid": 100.0, "ask": 100.25, "last": 100.0, "vol_usdt_24h": 9_000_000.0}

    class _Thin(_C):
        def ticker(self, inst):
            return {"bid": 100.0, "ask": 100.01, "last": 100.0, "vol_usdt_24h": 1_000_000.0}

    class _Gap(_C):
        def mark_price(self, inst):
            return 102.0

    j = Journal(tempfile.mkdtemp(), echo=False)
    try:
        st = SharedState(tempfile.mkdtemp(), clock=lambda: 1_000_000.0)
        fees = FeeBook(_C(), j)
        saved = config.STRATEGY
        config.STRATEGY = "swing_combo"
        try:
            cand = {"instId": "ARB-USDT-SWAP", "direction": "long", "family": "momentum",
                    "regime": "trend", "combined": 0.7, "atr": 1.0, "atr_pct": 0.01, "price": 100.07}
            ok, why = RiskGate(_C(), j, st, fees).check(cand, [], 10_000)
            assert ok, why
            ok, why = RiskGate(_Wide(), j, st, fees).check(dict(cand), [], 10_000)
            assert ok is False and "点差" in why, why
            ok, why = RiskGate(_Thin(), j, st, fees).check(dict(cand), [], 10_000)
            assert ok is False and "成交额" in why, why
            ok, why = RiskGate(_Gap(), j, st, fees).check(dict(cand), [], 10_000)
            assert ok is False and "标记价" in why and "> 100" in why, why
        finally:
            config.STRATEGY = saved
    finally:
        j.close()


def offline_self_check():
    """不联网。体制、截面排名、合成、费用、持仓时间、张数。"""
    from agents.margin import offline_self_check as margin_check
    margin_check()
    assert classify_regime(30, 0.2) == "trend"
    assert classify_regime(10, 2.0) == "trend"
    assert classify_regime(10, 0.4) == "range"
    assert family_from_tag("swmom") == "momentum"
    assert family_from_tag("swoi") == "oi"
    assert family_from_tag("") == ""
    assert family_from_tag("other") == ""

    signed = rank_signed({"a": 1, "b": 2, "c": 3})
    assert abs(signed["a"] + 1) < 1e-9 and abs(signed["c"] - 1) < 1e-9 and abs(signed["b"]) < 1e-9
    ties = rank_signed({"a": 1, "b": 1, "c": 5})
    assert abs(ties["a"] - ties["b"]) < 1e-9
    assert ties["c"] > ties["a"]
    assert rank_signed({"only": 3})["only"] == 0.0

    old_leg = config.SWING_MIN_LEG
    old_bonus = config.SWING_AGREE_BONUS
    try:
        config.SWING_MIN_LEG = 0.15
        config.SWING_AGREE_BONUS = 0.1
        blocked = combine("trend", 0.9, "long", 0.9, "short")
        assert blocked["blocked"] and blocked["combined"] == 0 and blocked["direction"] is None
        trend = combine("trend", 0.9, "long", 0.4, "long")
        # 0.7*0.9 + 0.3*0.4 + 0.1 = 0.85，动量贡献更大
        assert abs(trend["combined"] - 0.85) < 1e-9
        assert trend["family"] == "momentum" and trend["direction"] == "long" and trend["bonus"] == 0.1
        rng = combine("range", 0.9, "long", 0.4, "long")
        # 0.3*0.9 + 0.7*0.4 + 0.1 = 0.65，OI 贡献更大
        assert abs(rng["combined"] - 0.65) < 1e-9 and rng["family"] == "oi"
        solo = combine("trend", 1.0, "long", 0.0, None)
        assert solo["blocked"] is False and abs(solo["combined"] - 0.7) < 1e-9
        assert solo["family"] == "momentum" and solo["bonus"] == 0
        assert combine("range", 0.05, "long", 0.05, "short")["combined"] == 0
    finally:
        config.SWING_MIN_LEG = old_leg
        config.SWING_AGREE_BONUS = old_bonus

    assert fee_allows(0.01, 0.001) is True
    assert fee_allows(0.002, 0.001) is False  # 需要 3 倍

    meta = {"family": "momentum", "direction": "long", "opened_ts": 1000,
            "min_hold_sec": 12 * 3600, "max_hold_sec": 24 * 3600}
    assert time_exit_due(meta, 1000 + 10 * 3600) is False
    assert time_exit_due(meta, 1000 + 20 * 3600) is False
    assert time_exit_due(meta, 1000 + 24 * 3600) is True
    against = {"direction": "short", "combined": 0.9, "blocked": False}
    assert signal_exit_reason(meta, against, 1000 + 3600) is None          # 最短持仓内不因信号走
    assert signal_exit_reason(meta, against, 1000 + 13 * 3600) == "信号退出"
    weak = {"direction": "long", "combined": 0.2, "blocked": False}
    assert signal_exit_reason(meta, weak, 1000 + 13 * 3600) == "信号变弱"
    hold = {"direction": "long", "combined": 0.5, "blocked": False}
    assert signal_exit_reason(meta, hold, 1000 + 13 * 3600) is None
    assert signal_exit_reason(meta, None, 1000 + 13 * 3600) is None

    qty = size_qty(10_000, 100, 0.1, 1, 1)
    # 名义 2500 / (100*0.1) = 250
    assert qty == 250, qty
    assert size_qty(100, 65_000, 0.01, 0.01, 0.01) is None or size_qty(100, 65_000, 0.01, 0.01, 0.01) >= 0

    book = {"BTC-USDT-SWAP": {"family": "momentum"}, "ETH-USDT-SWAP": {"family": "momentum"},
            "SOL-USDT-SWAP": {"family": "oi"}, "XRP-USDT-SWAP": {"family": "momentum"}}
    pos = [{"instId": "BTC-USDT-SWAP"}, {"instId": "ETH-USDT-SWAP"}, {"instId": "SOL-USDT-SWAP"}]
    counts = family_counts(pos, book)
    assert counts["momentum"] == 2 and counts["oi"] == 1
    assert our_position_count(pos, book) == 3

    # 价格平静 + OI 上升 + 主动买 → 有方向的 OI 分
    candles = []
    px = 100.0
    for i in range(40):
        candles.append({"ts": i, "o": px, "h": px * 1.002, "l": px * 0.998, "c": px,
                        "vol": 100 + (50 if i >= 34 else 0), "confirm": True})
    oi = [{"oiUsd": 1000 * (1 + 0.002 * i)} for i in range(30)]
    oi[-1] = {"oiUsd": oi[-7]["oiUsd"] * 1.25}  # 窗口涨 25%，异常
    taker = [{"buy": 80, "sell": 20, "ts": i} for i in range(10)]
    # ATR 用真实波动；价格几乎不动，应判 flat
    a = atr(candles, 14)
    score, direction, info = oi_pulse(candles, oi, taker, 1.0, 0.0, a)
    assert info["flat"] is True
    assert direction == "long" and score > 0.5, (score, direction, info)
    assert info["reason"] == ""

    # 持仓量下降：价格再平也是 0，原因是 oi_not_up，不是把分数抬上去。
    down = [{"oiUsd": 1000 * (1.01 ** (30 - i))} for i in range(30)]
    score_d, dir_d, info_d = oi_pulse(candles, down, taker, 1.2, 0.0, a)
    assert score_d == 0 and dir_d is None and info_d["reason"] == "oi_not_up", info_d

    # 24h 波动超过 1 倍 ATR：not_flat，即使 OI 大增也不计分。
    jumped = [dict(c) for c in candles]
    jumped[-1] = dict(jumped[-1], c=jumped[-1]["c"] + a * 3)
    score_j, dir_j, info_j = oi_pulse(jumped, oi, taker, 1.2, 0.0, a)
    assert score_j == 0 and info_j["reason"] == "not_flat", info_j

    # 缺历史：no_oi。小增幅且量能没放大：below_bar，分数仍是 0。
    score_m, dir_m, info_m = oi_pulse(candles, [], taker, 1.2, 0.0, a)
    assert score_m == 0 and info_m["reason"] == "no_oi"
    mild = [{"ts": i, "oiUsd": 1000 * (1 + 0.001 * i)} for i in range(30)]
    quiet = [dict(c, vol=100) for c in candles]
    score_b, dir_b, info_b = oi_pulse(quiet, mild, taker, 1.2, 0.0, a)
    assert score_b == 0 and info_b["reason"] == "below_bar", (score_b, info_b)

    span = 4 * 3600
    now_ms = 1_000_000_000_000
    opened = now_ms - span * 1000 - 30_000
    assert close_age_sec(opened, span, now_ms) < 60
    assert close_age_sec(now_ms - 10 * span * 1000, span, now_ms) > 2 * span
    assert config.SWING_ENTRY_THRESHOLD == 0.65
    assert config.MAX_SPREAD_BPS == 6 and config.SWING_MAX_SPREAD_BPS == 20
    assert config.MIN_VOL_USDT_24H == 10_000_000 and config.SWING_MIN_VOL_USDT_24H == 2_000_000
    assert config.MAX_MARK_INDEX_BPS == 50 and config.SWING_MAX_MARK_INDEX_BPS == 100
    from agents.risk import check_liquidity_split
    check_liquidity_split()

    _retry_and_gate_checks()

    # 状态往返不丢家族和冷却
    import tempfile
    from agents.state import SharedState
    d = tempfile.mkdtemp()
    st = SharedState(d, clock=lambda: 1_000_000.0)
    st.d["open_meta"]["BTC-USDT-SWAP"] = {"family": "oi", "opened_ts": 10, "direction": "short"}
    st.d["swing_start_equity"] = 10000
    st.d["swing_peak_equity"] = 11000
    st.d["symbol_cooldown"]["ETH-USDT-SWAP"] = 2_000_000
    st.save()
    st2 = SharedState(d, clock=lambda: 1_000_000.0)
    assert st2.d["open_meta"]["BTC-USDT-SWAP"]["family"] == "oi"
    assert st2.d["swing_peak_equity"] == 11000
    assert st2.d["symbol_cooldown"]["ETH-USDT-SWAP"] == 2_000_000

    # 离线走一遍风控、1x 四分仓、订单标签和家族上限。不联网。
    from okx_client import MockOkxClient
    from agents.journal import Journal
    from agents.fees import FeeBook
    from agents.risk import RiskGate
    from agents.position import PositionManager
    from agents.executor import Executor
    saved = config.STRATEGY
    config.STRATEGY = "swing_combo"
    j = None
    try:
        d2 = tempfile.mkdtemp()
        j = Journal(d2, echo=False)
        client = MockOkxClient()
        st = SharedState(d2, clock=lambda: client.now_ms / 1000.0)
        fees = FeeBook(client, j)
        risk = RiskGate(client, j, st, fees)
        planner = PositionManager(client, j, fees)
        ex = Executor(client, j)
        ex.long_short = False
        inst = "BTC-USDT-SWAP"
        feat = candle_features(client.candles(inst, "4H", 80))
        assert feat and feat["atr_pct"] > 0
        ask = client.ticker(inst)["ask"]
        cand = {"instId": inst, "direction": "long", "family": "momentum", "regime": "trend",
                "combined": 0.91, "score": 0.91, "mom_score": 0.9, "oi_score": 0.4,
                "price": ask, "atr": feat["atr"], "atr_pct": feat["atr_pct"], "adx": feat["adx"],
                "tag": "swmom"}
        from agents.margin import swing_equity_status
        book = swing_equity_status(st, j, 0)
        assert abs(book["equity"] - float(config.SWING_CAPITAL_USDT)) < 1e-6, book
        assert abs(float(st.d["swing_start_equity"]) - float(config.SWING_CAPITAL_USDT)) < 1e-6
        # 传入交易所总权益也不改变虚拟账本上的通过/拒绝。
        ok, why = risk.check(cand, [], 102000)
        assert ok, why
        plan = planner.plan(cand, book["equity"])
        assert plan and plan["lever"] == 1 and plan["tag"] == "swmom", plan
        target = book["equity"] * config.SWING_ALLOC_PCT / config.SWING_MAX_POSITIONS
        assert abs(plan["notional"] - target) / target < 0.05, (plan["notional"], target)
        assert plan["sl"] < plan["entry_ref"] < plan["tp"]
        fill = ex.open(plan)
        assert fill and fill["qty"] > 0
        assert client.pos[inst]["tag"] == "swmom"
        assert client.pending_algos(inst)
        st.d["open_meta"] = {
            "BTC-USDT-SWAP": {"family": "momentum"},
            "AAA-USDT-SWAP": {"family": "momentum"},
            "BBB-USDT-SWAP": {"family": "momentum"},
        }
        held = [{"instId": "BTC-USDT-SWAP", "side": "long"},
                {"instId": "AAA-USDT-SWAP", "side": "long"},
                {"instId": "BBB-USDT-SWAP", "side": "long"}]
        eth = dict(cand, instId="ETH-USDT-SWAP", price=client.ticker("ETH-USDT-SWAP")["ask"],
                   atr=feat["atr"], atr_pct=max(feat["atr_pct"], 0.01))
        ok, why = risk.check(eth, held, client.equity())
        assert ok is False and "家族" in why, why
        st.d["symbol_cooldown"]["ETH-USDT-SWAP"] = st.now() + 5000
        ok, why = risk.check(eth, [], client.equity())
        assert ok is False and "冷却" in why, why
        st.d["swing_halted"] = True
        assert risk.global_block(client.equity())
    finally:
        config.STRATEGY = saved
        if j is not None:
            j.close()
    from agents.fill_reconcile import offline_self_check as fill_check
    fill_check()


if __name__ == "__main__":
    offline_self_check()
    print("swing offline self-check ok")
