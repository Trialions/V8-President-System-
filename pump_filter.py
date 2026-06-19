# pump_filter.py — V8.5.8 Pump/Manipülasyon Anomali Filtresi
#
# Amaç: Ani, anormal hacim patlaması + sert fiyat hareketi kombinasyonunu
# (pump-and-dump şüphesi) tespit eder. Backtest.py ve engine.py TARAFINDAN
# AYNI fonksiyon çağrılır (parity ilkesi — iki motor asla farklı mantık
# kullanmaz).
#
# DAVRANIŞ (Ömer'in kararı): SERT BLOK DEĞİL. Sadece:
#   1) Giriş skorunu bir ceza puanı kadar düşürür (score_penalty)
#   2) Pozisyon boyutunu bir çarpanla küçültür (size_mult)
# Sinyal tamamen engellenmez; President normal akışta zayıflamış skor ve
# küçültülmüş boyutla kararını verir. Bu, mevcut diğer "soft" feature'larla
# (Quality Score, Adaptive Risk) aynı felsefeyi izler: rapor üretir,
# OPEN/BLOCK kararını baypas etmez.
from __future__ import annotations
from typing import Any, Dict, List, Mapping, Optional


def compute_pump_risk(prices: Optional[List[float]], vols: Optional[List[float]],
                      cfg: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """
    Son `price_lookback_bars` mumdaki hacim patlaması + fiyat sıçramasını ölçer.

    vol_ratio       = son N mum hacim ortalaması / önceki 20 mumun (N hariç) ortalaması
    price_chg_pct   = son N mumda mutlak fiyat değişimi (%)
    is_pump         = vol_ratio >= vol_spike_mult VE price_chg_pct >= price_spike_pct

    Dönüş sözlüğü her zaman aynı anahtarları içerir (GUI/CSV için tutarlı şema):
        enabled, is_pump, score_penalty, size_mult, vol_ratio, price_chg_pct
    """
    pf = (cfg or {}).get("pump_filter", {}) or {}
    enabled = bool(pf.get("enabled", True))
    empty = {
        "enabled": enabled, "is_pump": False, "score_penalty": 0.0,
        "size_mult": 1.0, "vol_ratio": 0.0, "price_chg_pct": 0.0,
    }
    if not enabled:
        return empty

    prices = prices or []
    vols   = vols or []
    lookback = max(1, int(pf.get("price_lookback_bars", 3)))
    base_window = int(pf.get("base_window_bars", 20))
    if len(prices) < lookback + 1 or len(vols) < base_window:
        return empty

    recent_vol = sum(vols[-lookback:]) / lookback
    base_slice = vols[-base_window:-lookback] if base_window > lookback else vols[-base_window:]
    base_vol   = (sum(base_slice) / len(base_slice)) if base_slice else 0.0
    vol_ratio  = (recent_vol / base_vol) if base_vol > 0 else 0.0

    p_then = prices[-(lookback + 1)]
    p_now  = prices[-1]
    price_chg_pct = abs((p_now - p_then) / p_then * 100) if p_then > 0 else 0.0

    vol_spike_mult  = float(pf.get("vol_spike_mult", 4.0))
    price_spike_pct = float(pf.get("price_spike_pct", 8.0))
    is_pump = (vol_ratio >= vol_spike_mult) and (price_chg_pct >= price_spike_pct)

    score_penalty = float(pf.get("score_penalty", 8.0)) if is_pump else 0.0
    size_mult     = float(pf.get("size_mult", 0.5)) if is_pump else 1.0

    return {
        "enabled": True,
        "is_pump": is_pump,
        "score_penalty": round(score_penalty, 2),
        "size_mult": round(size_mult, 3),
        "vol_ratio": round(vol_ratio, 3),
        "price_chg_pct": round(price_chg_pct, 3),
    }
