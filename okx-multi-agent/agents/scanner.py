"""Scanner 代理：扫描 BTC/ETH/SOL 永续，寻找「强趋势 + 量能/价格异动」机会并打分。

打分（满分 8）:
  1H 大周期 EMA 方向         2 分（必须）
  5m EMA9/21 同向 + 收盘在慢线同侧  2 分（必须）
  ADX >= MIN_ADX             1 分
  DI 方向一致                1 分
  成交量异动 (> 均量 x 倍数)  1 分
  突破近 N 根高/低点          1 分
"""
import config
from .indicators import ema, atr, adx

AGENT = "scanner"


class Scanner:
    def __init__(self, client, journal):
        self.c, self.j = client, journal

    def _trend(self, candles):
        closes = [x["c"] for x in candles]
        f, s = ema(closes, config.EMA_FAST), ema(closes, config.EMA_SLOW)
        if not f or not s:
            return 0
        if f[-1] > s[-1] and closes[-1] > s[-1]:
            return 1
        if f[-1] < s[-1] and closes[-1] < s[-1]:
            return -1
        return 0

    def analyze(self, inst):
        c5 = self.c.candles(inst, config.SIGNAL_BAR, config.CANDLE_LIMIT)
        c1h = self.c.candles(inst, config.TREND_BAR, 100)
        if len(c5) < 60 or len(c1h) < 30:
            return None, "K线不足"
        big, small = self._trend(c1h), self._trend(c5)
        price = c5[-1]["c"]
        a = atr(c5, config.ATR_PERIOD)
        adx_v, pdi, ndi = adx(c5, config.ADX_PERIOD)
        reasons, score = [], 0
        info = {"price": price, "atr": a, "atr_pct": a / price if price else 0, "adx": adx_v,
                "trend_1h": big, "trend_5m": small}
        if big == 0 or small != big:
            return dict(info, score=0), f"方向不一致 1H={big} 5m={small}"
        direction = "long" if big > 0 else "short"
        score += 4
        reasons.append("1H+5m同向")
        if adx_v >= config.MIN_ADX:
            score += 1
            reasons.append(f"ADX{adx_v:.0f}")
        if (direction == "long" and pdi > ndi) or (direction == "short" and ndi > pdi):
            score += 1
            reasons.append("DI一致")
        vols = [x["vol"] for x in c5[-21:-1]]
        avg_v = sum(vols) / len(vols) if vols else 0
        if avg_v and c5[-1]["vol"] > avg_v * config.VOLUME_SPIKE_MULT:
            score += 1
            reasons.append(f"放量x{c5[-1]['vol'] / avg_v:.1f}")
        look = c5[-config.BREAKOUT_LOOKBACK - 1:-1]
        if direction == "long" and price > max(x["h"] for x in look):
            score += 1
            reasons.append("突破前高")
        if direction == "short" and price < min(x["l"] for x in look):
            score += 1
            reasons.append("跌破前低")
        info.update(direction=direction, score=score, reasons=reasons)
        return info, "、".join(reasons)

    def scan(self):
        out = []
        for inst in config.SYMBOLS:
            try:
                info, why = self.analyze(inst)
            except Exception as e:
                self.j.log(AGENT, "scan_error", f"{inst} 行情获取失败: {e}", inst=inst)
                continue
            if not info or info.get("score", 0) < config.MIN_SIGNAL_SCORE:
                self.j.log(AGENT, "no_signal", f"{inst} 分数 {info.get('score', 0) if info else 0}"
                           f"/{config.MIN_SIGNAL_SCORE} 不足: {why}", inst=inst,
                           **{k: v for k, v in (info or {}).items() if k != "reasons"})
                continue
            info["instId"] = inst
            self.j.log(AGENT, "candidate", f"{inst} {info['direction']} 分数 {info['score']}: {why}",
                       inst=inst, **{k: v for k, v in info.items() if k not in ("reasons", "instId")})
            out.append(info)
        out.sort(key=lambda x: (-x["score"], -x["adx"]))
        return out
