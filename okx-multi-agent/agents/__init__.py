"""六个代理：scanner / risk / position / executor / journal(logger) / equity，外加 net_tp 监控。
STRATEGY=swing_combo 时由 swing.py 提供体制、动量和 OI 脉冲，剥头皮路径不走这里。"""
