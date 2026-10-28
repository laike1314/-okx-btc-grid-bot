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


# ---------------- 账户 / 安全 ----------------
OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")
BASE_URL = _env("OKX_BASE_URL", "https://www.okx.com")
# 模拟盘开关：固定为 "1"。本项目只允许模拟盘，main.py 启动时会再检查一次。
SIMULATED_TRADING = "1"

VERSION = "MA-1.0.0"

# ---------------- Scanner 扫描代理 ----------------
SYMBOLS = _env("SYMBOLS", ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"], list)
SIGNAL_BAR = _env("SIGNAL_BAR", "5m")          # 信号周期
TREND_BAR = _env("TREND_BAR", "1H")            # 大周期方向过滤
CANDLE_LIMIT = 120
EMA_FAST = 9
EMA_SLOW = 21
ATR_PERIOD = 14
ADX_PERIOD = 14
MIN_ADX = _env("MIN_ADX", 25.0, float)                    # 趋势强度下限
VOLUME_SPIKE_MULT = _env("VOLUME_SPIKE_MULT", 1.5, float) # 量能异动：最新成交量 > 均量 x 倍数
BREAKOUT_LOOKBACK = 20                                    # 突破近 N 根高/低点
MIN_SIGNAL_SCORE = _env("MIN_SIGNAL_SCORE", 6, int)       # 满分 8

# ---------------- Risk 风控闸门（对应推文里的 Rug 检测） ----------------
MAX_SPREAD_BPS = _env("MAX_SPREAD_BPS", 6.0, float)          # 开仓点差上限（模拟盘 ETH 常见 ~4bps）
MAX_ABS_FUNDING_RATE = _env("MAX_ABS_FUNDING_RATE", 0.0005, float)  # |资金费率| > 0.05% 视为极端
MAX_ATR_PCT = _env("MAX_ATR_PCT", 0.012, float)              # ATR/价格 太大 = 波动过猛
MIN_ATR_PCT = _env("MIN_ATR_PCT", 0.0008, float)             # 太小 = 赚不够双边手续费
DAILY_LOSS_CAP_PCT = _env("DAILY_LOSS_CAP_PCT", 0.02, float) # 当日亏损达权益 2% 停止开仓
MAX_CONSECUTIVE_LOSSES = _env("MAX_CONSECUTIVE_LOSSES", 2, int)
LOSS_COOLDOWN_SEC = _env("LOSS_COOLDOWN_SEC", 1800, int)     # 连亏后全局冷却
SYMBOL_COOLDOWN_SEC = _env("SYMBOL_COOLDOWN_SEC", 4 * 3600, int)  # 单币止损后冷却
MIN_EDGE_FEE_MULT = 3.0   # 预期止盈距离必须 >= 双边手续费+滑点 的 3 倍

# ---------------- Position 仓位代理 ----------------
RISK_PER_TRADE_PCT = _env("RISK_PER_TRADE_PCT", 0.0025, float)  # 每笔止损最多亏权益 0.25%
MAX_MARGIN_PCT = _env("MAX_MARGIN_PCT", 0.05, float)            # 单笔保证金不超过权益 5%
MAX_POSITIONS = _env("MAX_POSITIONS", 1, int)
LEVERAGE = _env("LEVERAGE", 5, int)
MAX_LEVERAGE = 10
TD_MODE = "cross"
ATR_SL_MULT = _env("ATR_SL_MULT", 1.5, float)      # 止损 = 1.5 ATR
RISK_REWARD = _env("RISK_REWARD", 2.0, float)      # 交易所止盈 = 2R
MIN_SL_PCT = 0.0015
MAX_SL_PCT = 0.015

# ---------------- Executor 执行代理 ----------------
CLOSE_IOC_TRIES = 3
ORDER_FILL_WAIT_SEC = 1.0

# ---------------- Net-TP 净利止盈监控 ----------------
ENABLE_NET_TP = _env("ENABLE_NET_TP", True, bool)
NET_TP_MIN_USDT = _env("NET_TP_MIN_USDT", 0.5, float)
NET_TP_MIN_PCT = _env("NET_TP_MIN_PCT", 0.002, float)   # 名义价值 0.2%
NET_TP_CONFIRM_CHECKS = 3
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
