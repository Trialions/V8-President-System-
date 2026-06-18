# branches/cascade_hunter.py — Sikisma + konveksite skoru
# v8.1: BranchVote.action=OPEN + shadow bayragi. Futures flow V8.3.
from __future__ import annotations

import numpy as np
from typing import List, Optional
from modules.decision_packet import Action, BranchVote, Side


class CascadeHunter:
    NAME = "cascade_hunter"

    def __init__(self, cfg: dict):
        ch = cfg.get("cascade_hunter", {})
        self.enabled        = bool(ch.get("enabled", True))
        self.shadow         = bool(ch.get("shadow_mode", True))
        self.comp_bars      = int(ch.get("compression_lookback", ch.get("compression_bars", 10)))
        self.min_comp_bars  = int(ch.get("min_compression_bars", 8))
        self.comp_atr_mult  = float(ch.get("compression_atr_mult", 0.5))
        self.min_conv_score = float(ch.get("min_convexity_score", 60.0))
        self.breakout_vol   = float(ch.get("breakout_vol_mult", 1.5))

        # price_volume_only: True (varsayılan) = mevcut davranış, konveksite
        # SADECE fiyat/hacimden hesaplanır. False ise futures_flow.enabled de
        # True olmak şartıyla OI (open interest) verisi varsa konveksite skoru
        # bununla düzeltilir — ama gerçek bir OI veri kaynağı henüz entegre
        # değil, bu yüzden veri akışı boşsa (futures_oi parametresi verilmezse)
        # otomatik ve güvenli şekilde fiyat/hacim moduna düşülür (crash olmaz).
        self.price_volume_only = bool(ch.get("price_volume_only", True))
        ff = ch.get("futures_flow", {})
        self.futures_flow_en        = bool(ff.get("enabled", False)) and not self.price_volume_only
        self.futures_oi_change_thr  = float(ff.get("oi_change_threshold", 0.05))

    def vote(self, symbol, score, prices, highs, lows, volumes, result,
             futures_oi: Optional[List[float]] = None) -> BranchVote:
        if not self.enabled:
            return self._block("BRANCH_DISABLED")
        if len(prices) < self.comp_bars + 5:
            return self._block("INSUFFICIENT_DATA")

        compression = self._detect_compression(highs, lows, prices)
        conv_score  = self._convexity_score(prices, volumes)

        # futures_flow kapısı: sadece price_volume_only=False VE futures_flow.
        # enabled=True VE gerçekten OI verisi verilmişse devreye girer. Veri
        # yoksa (futures_oi=None veya yetersiz) sessizce atlanır — fiyat/hacim
        # skoru tek başına kullanılır, davranış price_volume_only=True ile
        # aynı kalır.
        oi_note = ""
        if self.futures_flow_en and futures_oi and len(futures_oi) >= 2:
            oi_chg = (futures_oi[-1] - futures_oi[0]) / futures_oi[0] if futures_oi[0] else 0.0
            if abs(oi_chg) >= self.futures_oi_change_thr:
                # OI artışı + sıkışma = konveksiteyi güçlendir; OI düşüşü = zayıflat
                conv_score = float(np.clip(conv_score + (15.0 if oi_chg > 0 else -15.0), 0, 100))
                oi_note = f" oi_chg={oi_chg:+.1%}"

        if not compression:
            return self._block(f"NO_COMPRESSION conv={conv_score:.1f}{oi_note}")
        if conv_score < self.min_conv_score:
            return self._block(f"LOW_CONVEXITY conv={conv_score:.1f}{oi_note}")

        side = Side.LONG if prices[-1] > prices[-2] else Side.SHORT
        return BranchVote(
            self.NAME, Action.OPEN, side, score=conv_score,
            confidence=round(min(1.0, conv_score / 100), 3), shadow=self.shadow,
            reason=f"CASCADE_OK conv={conv_score:.1f}{oi_note}",
            params={"sl_pct": 0.020, "size_mult": 0.6, "mode": "CASCADE"},
        )

    def _detect_compression(self, highs, lows, closes) -> bool:
        n = self.comp_bars
        if len(closes) < n + 5:
            return False
        seg_h, seg_l, seg_c = np.array(highs[-n:]), np.array(lows[-n:]), np.array(closes[-n:])
        prev_c = seg_c[:-1]
        tr = np.maximum(seg_h[1:] - seg_l[1:],
                        np.maximum(np.abs(seg_h[1:] - prev_c), np.abs(seg_l[1:] - prev_c)))
        atr_recent = float(np.mean(tr[-3:])) if len(tr) >= 3 else 0.0
        atr_old    = float(np.mean(tr[:3]))  if len(tr) >= 6 else atr_recent
        if atr_old <= 0:
            return False
        return (atr_recent / atr_old) < self.comp_atr_mult

    def _convexity_score(self, prices, volumes) -> float:
        score = 50.0
        if len(prices) >= 10:
            seg = prices[-10:]
            price_range = (max(seg) - min(seg)) / (min(seg) + 1e-9) * 100
            score += max(0, 25 - price_range * 5)
        if len(volumes) >= 10:
            recent_vol = np.mean(volumes[-3:]); old_vol = np.mean(volumes[-10:-3])
            if old_vol > 0:
                score += min(25, (recent_vol / old_vol - 1.0) * 20)
        return float(np.clip(score, 0, 100))

    def _block(self, reason):
        return BranchVote(self.NAME, Action.BLOCK, Side.NONE, 0.0, 0.0, reason, shadow=self.shadow)
