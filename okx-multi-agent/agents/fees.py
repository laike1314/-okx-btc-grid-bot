"""账户手续费率缓存（每小时刷新）。手续费是双边的：开仓一次 + 平仓一次。"""
import time
import config


class FeeBook:
    def __init__(self, client, journal):
        self.c, self.j = client, journal
        self.taker, self.maker, self.ts = config.DEFAULT_TAKER_FEE, config.DEFAULT_MAKER_FEE, 0

    def refresh(self, force=False):
        if not force and time.time() - self.ts < 3600:
            return
        try:
            self.taker, self.maker = self.c.fee_rates()
            self.j.log("fees", "fee_rates", f"taker={self.taker:.5f} maker={self.maker:.5f}（账户实际费率）")
        except Exception as e:
            self.j.log("fees", "fee_default", f"读取费率失败，用默认 taker={self.taker}: {e}")
        self.ts = time.time()
