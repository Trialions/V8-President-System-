# branches/core_long_branch.py — V7 long mantigi (President uyumlu)
# v8.1: SADECE LONG uretir. Short karari artik yalniz ShortSurgeon'da.
from __future__ import annotations

from modules.decision_packet import Action, BranchVote, Side
import adaptive_sl


class CoreLongBranch:
    """
    V7 long stratejisini President sistemine adapte eder.
    YALNIZCA LONG sinyali uretir (short kaldirildi).
    """

    NAME = "core_long"

    def __init__(self, cfg: dict):
        self.cfg     = cfg
        thr          = cfg.get("thresholds", {})
        risk         = cfg.get("risk", {})
        cl           = cfg.get("core_long", {})
        self.enabled = bool(cl.get("enabled", True))
        self.shadow  = bool(cl.get("shadow_mode", False))
        self.thr_long  = float(thr.get("score_long_open", 97.0))
        mtf = cfg.get("mtf", {})
        # MTF kapalıysa HTF gate BYPASS edilir.
        # Aksi halde htf_score=50 default'u tüm LONG sinyalleri sessizce durdurabilir.
        self.htf_gate_enabled = bool(mtf.get("enabled", True)) and bool(cl.get("htf_gate_enabled", True))
        self.htf_block_min = float(cl.get("htf_block_min", 55.0))
        self.htf_penalty_min = float(cl.get("htf_penalty_min", 70.0))
        self.htf_boost_min = float(cl.get("htf_boost_min", 85.0))
        self.sl_pct    = float(risk.get("hard_stop_pct", 1.5)) / 100
        self.atr_mult  = float(risk.get("atr_multiplier", 2.0))
        self.trail     = float(risk.get("trailing_step_pct", 0.7)) / 100

    def vote(self, symbol, score, result, regime, htf_score, sentiment) -> BranchVote:
        if not self.enabled:
            return self._block("BRANCH_DISABLED")

        atr_pct = result.get("components", {}).get("atr_pct", 0.0)
        adsl    = adaptive_sl.compute(
            regime=regime, atr_pct=atr_pct,
            base_score_threshold=self.thr_long,
            base_atr_multiplier=self.atr_mult,
            base_trail_step=self.trail, cfg=self.cfg,
        )
        eff_thr = adsl["score_threshold"]
        sl_pct  = adsl["sl_pct"]

        # SADECE LONG
        if score < eff_thr:
            return self._block(f"SCORE_BELOW_THR score={score:.1f} thr={eff_thr:.1f}")
        if sentiment == "BEARISH" or regime == "BEARISH":
            return self._block(f"REGIME_BEARISH_NO_LONG regime={regime}")

        # HTF/MTF artık sadece log değil, gerçek karar kapısıdır.
        # Ancak MTF config'te kapalıysa HTF kapısı uygulanmaz.
        htf_reason = f"htf={htf_score:.1f}"
        if self.htf_gate_enabled:
            if htf_score < self.htf_block_min:
                return self._block(f"HTF_WEAK htf={htf_score:.1f} min={self.htf_block_min:.1f}")
        else:
            htf_reason = "htf=BYPASS_MTF_DISABLED"

        confidence = min(1.0, (score - eff_thr) / max(100 - eff_thr, 1) + 0.3)
        if self.htf_gate_enabled:
            if htf_score < self.htf_penalty_min:
                confidence *= 0.70
            elif htf_score >= self.htf_boost_min:
                confidence = min(1.0, confidence + 0.12)
        return BranchVote(
            branch_name=self.NAME, action=Action.OPEN, side=Side.LONG,
            score=score, confidence=round(confidence, 3), shadow=self.shadow,
            reason=f"CORE_LONG_OK regime={regime} {htf_reason}",
            params={"sl_pct": sl_pct, "size_mult": 1.0, "trail_step": adsl["trail_step"]},
        )

    def _block(self, reason):
        return BranchVote(self.NAME, Action.BLOCK, Side.NONE, 0.0, 0.0, reason, shadow=self.shadow)
