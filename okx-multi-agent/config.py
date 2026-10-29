"""全局配置：保守、模拟盘安全的默认值。

所有参数都可以用同名环境变量覆盖，例如:  export MAX_LEVERAGE=5
API 密钥只从环境变量读取，绝不写在代码里。
"""
import os


def _load_dotenv():
    """读取同目录 .env（如果有），不覆盖已存在的环境变量。无需额外依赖。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and not os.getenv(k):
            os.environ[k] = v


_load_dotenv()


def _env(name, default, cast=str):
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    if cast is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if cast is list:
        return [s.strip() for s in raw.split(",") if s.strip()]
    return cast(raw)


def _csv_env(name):
    """逗号分隔列表。未设置或空字符串返回 None（表示「走自动交易池」）。"""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return None
    items = [s.strip() for s in str(raw).split(",") if s.strip()]
    return items or None


# ---------------- 账户 / 安全 ----------------
OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")
BASE_URL = _env("OKX_BASE_URL", "https://www.okx.com")
# 模拟盘开关：固定为 "1"。本项目只允许模拟盘，main.py 启动时会再检查一次。
SIMULATED_TRADING = "1"

VERSION = "MA-1.2.0"

# ---------------- 交易池 ----------------
# SYMBOLS 非空 = 调试强制列表，跳过市值排名和股票发现。不设则启动时自动组建。
SYMBOLS_OVERRIDE = _csv_env("SYMBOLS")
SYMBOLS = list(SYMBOLS_OVERRIDE or [])          # 实际扫描列表，由 universe.apply 填充
UNIVERSE_META = {"source": "unset", "crypto": [], "stocks": [], "symbols": []}
CRYPTO_TOP_N = max(0, _env("CRYPTO_TOP_N", 30, int))
# 股票永续点差宽、价格容易失真（模拟盘上 AMD/EWJ 一类）。默认关闭，需要时再打开。
ENABLE_US_STOCKS = _env("ENABLE_US_STOCKS", False, bool)
UNIVERSE_REFRESH_SEC = max(0, _env("UNIVERSE_REFRESH_SEC", 3600, int))  # 0 = 只在启动时建一次
SCAN_BATCH = max(1, _env("SCAN_BATCH", 8, int))   # 每批扫完穿插一次全持仓监控
PUBLIC_REQ_GAP_SEC = max(0.0, _env("PUBLIC_REQ_GAP_SEC", 0.12, float))  # GET 最小间隔，给限速留余量

# ---------------- Scanner 扫描代理 ----------------
SIGNAL_BAR = _env("SIGNAL_BAR", "5m")          # 信号周期（剥头皮用 5m，避免 1m 噪音）
TREND_BAR = _env("TREND_BAR", "15m")           # 大周期方向过滤
CANDLE_LIMIT = 120
EMA_FAST = 9
EMA_SLOW = 21
ATR_PERIOD = 14
ADX_PERIOD = 14
MIN_ADX = _env("MIN_ADX", 28.0, float)                    # 趋势强度下限（低于此直接不做）
VOLUME_SPIKE_MULT = _env("VOLUME_SPIKE_MULT", 1.5, float) # 量能异动：最新成交量 > 均量 x 倍数
BREAKOUT_LOOKBACK = 20                                    # 突破近 N 根高/低点
MIN_SIGNAL_SCORE = _env("MIN_SIGNAL_SCORE", 7, int)       # 满分 8

# ---------------- Risk 风控闸门（对应推文里的 Rug 检测） ----------------
MAX_SPREAD_BPS = _env("MAX_SPREAD_BPS", 6.0, float)          # 开仓点差上限（模拟盘 ETH 常见 ~4bps）
MAX_ABS_FUNDING_RATE = _env("MAX_ABS_FUNDING_RATE", 0.0005, float)  # |资金费率| > 0.05% 视为极端
MAX_ATR_PCT = _env("MAX_ATR_PCT", 0.012, float)              # ATR/价格 太大 = 波动过猛
MIN_ATR_PCT = _env("MIN_ATR_PCT", 0.0008, float)             # 太小 = 赚不够双边手续费
DAILY_LOSS_CAP_PCT = _env("DAILY_LOSS_CAP_PCT", 0.02, float) # 滚动窗口已实现亏损达参考权益的比例则停开仓
ROLLING_LOSS_WINDOW_SEC = _env("ROLLING_LOSS_WINDOW_SEC", 86400, int)  # 亏损上限回看窗口，不按北京时间 0 点清零
MAX_CONSECUTIVE_LOSSES = _env("MAX_CONSECUTIVE_LOSSES", 3, int)
# <=0 表示关闭。关闭时连亏/止损都不挡再开仓，state.json 里残留的到期时间也不再生效。
LOSS_COOLDOWN_SEC = _env("LOSS_COOLDOWN_SEC", 1800, int)      # 连亏后全局冷却
SYMBOL_COOLDOWN_SEC = _env("SYMBOL_COOLDOWN_SEC", 900, int)   # 单币止损后冷却
# 同向再入场至少等一根这个周期的 K 线收盘（短于 5m 会抬到 5m）。
REENTRY_BAR = _env("REENTRY_BAR", "5m")
# 同一方向的加密货币持仓个数上限。0 = 不限制。股票不计入。
MAX_SAME_DIR_CRYPTO = _env("MAX_SAME_DIR_CRYPTO", 2, int)
MIN_EDGE_FEE_MULT = 3.0   # 预期止盈距离必须 >= 双边手续费+滑点 的 3 倍

# ---------------- Position 仓位代理 ----------------
RISK_PER_TRADE_PCT = _env("RISK_PER_TRADE_PCT", 0.0025, float)  # 每笔止损最多亏权益 0.25%
MAX_MARGIN_PCT = _env("MAX_MARGIN_PCT", 0.05, float)            # 单笔保证金不超过权益 5%
# <=0 表示不限制同时持仓个数。同一 instId 仍只留一个方向（见 risk.check）。
MAX_POSITIONS = _env("MAX_POSITIONS", 0, int)
LEVERAGE = _env("LEVERAGE", 5, int)
MAX_LEVERAGE = 10
TD_MODE = "cross"
ATR_SL_MULT = _env("ATR_SL_MULT", 2.5, float)      # 止损距离 = ATR 倍数（加宽，避免手续费占掉 1/3R）
RISK_REWARD = _env("RISK_REWARD", 1.0, float)      # 交易所止盈 = 1R，不再干等 2R
MIN_SL_PCT = _env("MIN_SL_PCT", 0.004, float)      # 止损距离下限 0.4%
MAX_SL_PCT = _env("MAX_SL_PCT", 0.02, float)

# ---------------- Executor 执行代理 ----------------
CLOSE_IOC_TRIES = 3
ORDER_FILL_WAIT_SEC = _env("ORDER_FILL_WAIT_SEC", 0.4, float)
FILL_POLL_TRIES = _env("FILL_POLL_TRIES", 5, int)
# 成交价偏离计划入场超过这个比例就平掉，不记账。
MAX_FILL_SLIP_PCT = _env("MAX_FILL_SLIP_PCT", 0.001, float)

# ---------------- Net-TP 净利止盈监控 ----------------
ENABLE_NET_TP = _env("ENABLE_NET_TP", True, bool)
NET_TP_MIN_USDT = _env("NET_TP_MIN_USDT", 0.3, float)
NET_TP_MIN_PCT = _env("NET_TP_MIN_PCT", 0.0015, float)  # 净利达到名义价值的 0.15% 就准备落袋
NET_TP_CONFIRM_CHECKS = _env("NET_TP_CONFIRM_CHECKS", 2, int)
# 浮盈达到止损距离的这个比例后，把止损抬到锁定大部分浮盈（仍须在标记价正确一侧）。
TRAIL_ARM_R = _env("TRAIL_ARM_R", 0.4, float)
TRAIL_LOCK_FRAC = _env("TRAIL_LOCK_FRAC", 0.6, float)
# 持仓超过这么多秒就市价平。0 = 不限时。
MAX_HOLD_SEC = _env("MAX_HOLD_SEC", 900, int)
SLIPPAGE_BPS = 2.0
DEFAULT_TAKER_FEE = 0.0005
DEFAULT_MAKER_FEE = 0.0002
MAX_CLOSE_SPREAD_BPS = _env("MAX_CLOSE_SPREAD_BPS", 8.0, float)  # 净利平仓时点差上限

# ---------------- 主循环 ----------------
SCAN_INTERVAL_SEC = _env("SCAN_INTERVAL_SEC", 20, int)    # 扫描/开仓频率
MONITOR_INTERVAL_SEC = _env("MONITOR_INTERVAL_SEC", 2, int)  # 持仓净利监控频率
RUN_HOURS = _env("RUN_HOURS", 0.0, float)                 # 0 = 一直跑
STOP_AFTER_FLAT = _env("STOP_AFTER_FLAT", False, bool)    # 到时后等最后一笔平仓再退出

# ---------------- 文件 ----------------
DATA_DIR = _env("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
