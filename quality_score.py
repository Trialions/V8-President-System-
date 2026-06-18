# quality_score.py — V8.5.2 President feature-only quality report
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any, Dict, Mapping

@dataclass(frozen=True)
class QualityScoreReport:
    enabled: bool
    score: float
    confidence: float
    size_hint: float
    reasons: str
    mode: str = "feature_only"
    def to_dict(self): return asdict(self)


def _f(v, default=0.0):
    try: return float(v if v is not None else default)
    except Exception: return default


def compute_quality_score(symbol: str, result: Mapping[str, Any], regime: str, htf_score: float, symbol_stats: Mapping[str, Any] | None, cfg: Mapping[str, Any]) -> QualityScoreReport:
    qcfg = (cfg or {}).get("quality_score", {}) or {}
    enabled = bool(qcfg.get("enabled", True))
    mode = str(qcfg.get("mode", "feature_only"))
    if not enabled:
        return QualityScoreReport(False, 50.0, 0.0, 1.0, "quality_score_disabled", mode)
    comp = dict((result or {}).get("components", {}) or {})
    entry_score = _f((result or {}).get("final_score"), 50.0)
    adx = _f(comp.get("adx"), 0.0)
    rsi = _f(comp.get("rsi"), 50.0)
    atr = _f(comp.get("atr_pct"), 0.0)
    volr = _f(comp.get("vol_ratio", comp.get("volume_ratio")), 1.0)
    regime_u = str(regime or "NEUTRAL").upper()
    score = 50.0
    reasons = []
    score += max(-15, min(15, (entry_score - 95.0) * 1.5)); reasons.append(f"entry={entry_score:.1f}")
    score += max(-12, min(12, (htf_score - 60.0) * 0.30)); reasons.append(f"htf={htf_score:.1f}")
    if adx >= 35: score += 10; reasons.append("adx_strong")
    elif adx >= 28: score += 6; reasons.append("adx_ok")
    elif adx > 0: score -= 8; reasons.append("adx_weak")
    if 48 <= rsi <= 68: score += 6; reasons.append("rsi_ok")
    elif rsi > 72: score -= 12; reasons.append("rsi_hot")
    elif rsi < 42: score -= 8; reasons.append("rsi_low")
    if 0.8 <= atr <= 4.0: score += 4; reasons.append("atr_ok")
    elif atr > 5.0: score -= 8; reasons.append("atr_high")
    elif 0 < atr < 0.6: score -= 6; reasons.append("atr_low")
    if volr >= 1.4: score += 5; reasons.append("vol_confirm")
    elif volr < 0.75: score -= 5; reasons.append("vol_dry")
    if regime_u in ("KONSOL", "CHOP", "RANGE"): score -= 7; reasons.append("regime_chop")
    elif regime_u in ("TREND", "BULL", "BULLISH"): score += 5; reasons.append("regime_trend")
    if symbol_stats:
        trades = int(_f(symbol_stats.get("trades", 0), 0))
        wins = int(_f(symbol_stats.get("wins", 0), 0))
        pnl = _f(symbol_stats.get("pnl", 0), 0)
        if trades >= 4:
            wr = wins / max(trades, 1)
            score += max(-8, min(8, (wr - 0.45) * 20)); reasons.append(f"sym_wr={wr:.2f}")
            if pnl < 0: score -= 4; reasons.append("sym_pnl_neg")
    score = max(0.0, min(100.0, score))
    conf = min(1.0, max(0.25, abs(score - 50.0) / 50.0))
    if score < 40: size_hint = 0.55
    elif score < 50: size_hint = 0.70
    elif score > 75: size_hint = 1.05
    else: size_hint = 1.0
    return QualityScoreReport(True, round(score, 3), round(conf, 3), round(size_hint, 3), ";".join(reasons), mode)
