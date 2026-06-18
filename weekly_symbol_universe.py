# weekly_symbol_universe.py — V8.5.2 haftalık tarihsel/canlı sembol evreni
from __future__ import annotations
import json, time, math
from pathlib import Path
from typing import List, Tuple, Dict, Any


def load_current_symbols(top: int = 20, path: str = "symbols_top70.json") -> List[str]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, list) and data:
            return [str(s).upper() for s in data[:top]]
    except Exception:
        pass
    return ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"][:top]


def score_symbol_from_candles(symbol: str, candles: list) -> Dict[str, Any]:
    if not candles:
        return {"symbol": symbol, "score": -1e9, "reason": "NO_DATA"}
    closes = [float(c.get("close", 0)) for c in candles if float(c.get("close", 0) or 0) > 0]
    vols = [float(c.get("volume", 0)) for c in candles]
    if len(closes) < 30:
        return {"symbol": symbol, "score": -1e9, "reason": f"LOW_BARS:{len(closes)}"}
    avg_notional = sum(c*v for c, v in zip(closes[-min(len(closes), 120):], vols[-min(len(vols), 120):])) / max(1, min(len(closes), 120))
    returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes)) if closes[i-1] > 0]
    volat = (sum((r - (sum(returns)/len(returns)))**2 for r in returns) / max(1, len(returns))) ** 0.5 if returns else 0.0
    trend = (closes[-1] - closes[max(0, len(closes)-30)]) / closes[max(0, len(closes)-30)] if closes[max(0, len(closes)-30)] > 0 else 0.0
    liquidity_score = math.log10(max(avg_notional, 1.0)) * 10
    stability_score = max(0.0, 25.0 - volat * 500.0)
    trend_score = max(-10.0, min(10.0, trend * 100.0))
    score = liquidity_score + stability_score + trend_score
    return {"symbol": symbol, "score": round(score, 4), "avg_notional": round(avg_notional, 3), "volatility": round(volat, 6), "trend_30": round(trend, 5), "bars": len(closes), "reason": "OK"}


def select_universe_for_window(candidate_symbols: List[str], candles_by_sym: Dict[str, list], top: int = 20, out_meta: str | None = None, as_of_ms: int | None = None) -> List[str]:
    rows = [score_symbol_from_candles(s, candles_by_sym.get(s, [])) for s in candidate_symbols]
    rows.sort(key=lambda r: r.get("score", -1e9), reverse=True)
    selected = [r["symbol"] for r in rows if r.get("score", -1e9) > -1e8][:top]
    if out_meta:
        payload = {"as_of_ms": as_of_ms, "generated_at": int(time.time()), "top": top, "selected": selected, "rows": rows}
        Path(out_meta).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return selected or candidate_symbols[:top]


def write_universe_history(path: str, period_label: str, symbols: List[str], meta: Dict[str, Any] | None = None):
    p = Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    import csv
    with open(p, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        if new: w.writerow(["period", "symbols", "meta_json"])
        w.writerow([period_label, ",".join(symbols), json.dumps(meta or {}, ensure_ascii=False)])
