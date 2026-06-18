# branches/short_surgeon.py — Short Surgeon dali
# v8.1 DUZELTMELER:
#   - record_sl() artik SL_DOGRU sinyalinin TEK kaynagi (engine/backtest cagirir)
#   - HATALI "BELIRSIZ + dusuk skor -> short" mantigi KALDIRILDI
#   - Short yalniz dogrulanmis SL_DOGRU (fiyat dusmeye devam etti) + momentum ile acilir
#   - BranchVote.action=OPEN + shadow bayragi (Action.SHADOW kaldirildi)
from __future__ import annotations

import time
from typing import Dict, Optional
from modules.decision_packet import Action, BranchVote, Side


class ShortSurgeon:
    """
    Short icin uzmanlasmis dal. 4 mod (su an SL_DOGRU + BTC_RISK_OFF aktif).
    record_sl() ile beslenir; verdict='SL_DOGRU' geldiyse short onerir.
    """

    NAME = "short_surgeon"

    def __init__(self, cfg: dict):
        ss  = cfg.get("short_surgeon", {})
        self.enabled = bool(ss.get("enabled", True))
        self.shadow  = bool(ss.get("shadow_mode", True))

        modes = ss.get("modes", {})
        sl_d  = modes.get("sl_dogru_short", {}) or ss.get("sl_dogru", {})
        self.sld_enabled      = bool(sl_d.get("enabled", True))
        self.sld_lookback_hrs = float(sl_d.get("max_hold_hours", sl_d.get("lookback_hours", 18)))
        self.sld_min_drop     = float(sl_d.get("min_drop_4h_pct", 1.0))
        self.sld_size_mult    = float(sl_d.get("size_mult", 0.5))

        bro = modes.get("btc_risk_off_short", {}) or ss.get("btc_risk_off", {})
        self.bro_enabled   = bool(bro.get("enabled", False))
        self.bro_drop_pct  = float(bro.get("btc_drop_pct", 2.0))
        self.bro_candles   = int(bro.get("lookback_candles", 3))
        self.bro_size_mult = float(bro.get("size_mult", bro.get("short_size_mult", 0.5)))

        # symbol -> {ts, verdict, chg_4h}
        self._sl_records: Dict[str, dict] = {}

    # ── Besleme: engine/backtest SL kapanisini bildirir ──────────────
    def record_sl(self, symbol: str, verdict: str, ts: float, chg_4h: float = 0.0):
        """
        verdict: 'SL_DOGRU' (fiyat dusmeye devam etti) | 'ERKEN_SL' (toparladi) | 'BELIRSIZ'
        chg_4h : SL sonrasi 4 saatlik fiyat degisimi (ondalik, orn -0.018)
        """
        self._sl_records[symbol] = {"ts": float(ts), "verdict": verdict, "chg_4h": float(chg_4h)}

    def get_sl_records(self) -> dict:
        return dict(self._sl_records)

    # ── Oy ───────────────────────────────────────────────────────────
    def vote(self, symbol, score, result, regime, btc_prices=None, now=None) -> BranchVote:
        if not self.enabled:
            return self._block("BRANCH_DISABLED")

        now = now if now is not None else time.time()
        v = self._check_sl_dogru(symbol, now)
        if v:
            return v
        if self.bro_enabled and btc_prices:
            v = self._check_btc_risk_off(symbol, btc_prices)
            if v:
                return v
        return self._block("NO_SHORT_SIGNAL")

    def _check_sl_dogru(self, symbol, now: float) -> Optional[BranchVote]:
        if not self.sld_enabled:
            return None
        rec = self._sl_records.get(symbol)
        if not rec:
            return None
        elapsed_h = (now - rec["ts"]) / 3600
        if elapsed_h > self.sld_lookback_hrs:
            self._sl_records.pop(symbol, None)
            return None
        # YALNIZ dogrulanmis SL_DOGRU + yeterli dusus -> short
        if rec["verdict"] == "SL_DOGRU" and rec["chg_4h"] <= -(self.sld_min_drop / 100):
            conf = min(1.0, abs(rec["chg_4h"]) / (self.sld_min_drop / 100) * 0.7)
            return BranchVote(
                self.NAME, Action.OPEN, Side.SHORT,
                score=min(100, 60 + abs(rec["chg_4h"]) * 1000),
                confidence=round(conf, 3), shadow=self.shadow,
                reason=f"SL_DOGRU chg4h={rec['chg_4h']:.3f}",
                params={"sl_pct": 0.015, "size_mult": self.sld_size_mult, "mode": "SL_DOGRU"},
            )
        return None

    def _check_btc_risk_off(self, symbol, btc_prices) -> Optional[BranchVote]:
        if len(btc_prices) < self.bro_candles + 1:
            return None
        start, end = btc_prices[-(self.bro_candles + 1)], btc_prices[-1]
        drop = (end - start) / start * 100 if start > 0 else 0.0
        if drop <= -self.bro_drop_pct:
            return BranchVote(
                self.NAME, Action.OPEN, Side.SHORT, score=70.0,
                confidence=0.6, shadow=self.shadow,
                reason=f"BTC_RISK_OFF drop={drop:.2f}%",
                params={"sl_pct": 0.020, "size_mult": self.bro_size_mult, "mode": "BTC_RISK_OFF"},
            )
        return None

    def _block(self, reason):
        return BranchVote(self.NAME, Action.BLOCK, Side.NONE, 0.0, 0.0, reason, shadow=self.shadow)
