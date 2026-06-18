# market_regime.py — BTC bazli piyasa rejimi tespit modulu
# V7 ile ayni mantik, V8 ile uyumlu
from __future__ import annotations
from collections import deque


class MarketRegimeDetector:
    """
    BTC fiyat hareketine gore piyasa rejimini tespit eder:
    TREND | KONSOL | BEARISH | NEUTRAL
    """

    def __init__(self, cfg: dict = None):
        cfg = cfg or {}
        mr = cfg.get("market_regime", {})
        self.enabled = bool(mr.get("enabled", True))
        self._last_regime = "NEUTRAL"
        self._btc_closes  = deque(maxlen=200)

    def update(self, btc_close: float):
        """BTC fiyati guncelle ve rejimi yeniden hesapla."""
        self._btc_closes.append(float(btc_close))
        if not self.enabled or len(self._btc_closes) < 50:
            return
        self._last_regime = self._detect()

    def get_regime(self) -> str:
        return self._last_regime

    def _detect(self) -> str:
        prices = list(self._btc_closes)
        if len(prices) < 50:
            return "NEUTRAL"

        import numpy as np
        arr = np.array(prices[-50:], dtype=float)

        # EMA20 / EMA50
        def ema(s, span):
            result = [s[0]]
            k = 2 / (span + 1)
            for p in s[1:]:
                result.append(p * k + result[-1] * (1 - k))
            return result

        ema20 = ema(list(arr), 20)[-1]
        ema50 = ema(list(arr), 50)[-1]
        price = arr[-1]

        # ATR (son 14 bar)
        h = arr[-15:]
        l = arr[-15:]
        c = arr[-15:]
        trs = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
               for i in range(1, len(c))]
        atr = float(np.mean(trs)) if trs else 0.0
        atr_pct = atr / price * 100 if price > 0 else 0.0

        trend_gap = abs(ema20 - ema50) / ema50 * 100 if ema50 > 0 else 0.0

        # Son 20 mumdaki toplam degisim
        change_20 = (price - arr[-20]) / arr[-20] * 100 if arr[-20] > 0 else 0.0

        if change_20 <= -5.0:
            return "BEARISH"
        elif trend_gap > 2.0 and ema20 > ema50:
            return "TREND"
        elif atr_pct < 1.0 and trend_gap < 1.0:
            return "KONSOL"
        else:
            return "NEUTRAL"
