# block_outcome_analyzer.py — V8.5.2 BOA v2
# Açılmayan/bloklanan sinyallerin 4/8/12/24h ilk TP/SL sonucunu ölçer.
from __future__ import annotations
import csv
from collections import defaultdict
from pathlib import Path
from typing import Optional, Iterable


def _candle_idx_at(candles: list, ts_ms: int) -> int:
    lo, hi = 0, max(0, len(candles) - 1)
    while lo < hi:
        mid = (lo + hi) // 2
        if int(candles[mid].get("open_time", 0)) < ts_ms:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _first_hit(candles: list, start_idx: int, entry: float, tp_pct: float, sl_pct: float, horizon_ms: int, start_ms: int, side: str = "LONG") -> str:
    side = str(side or "LONG").upper()
    if side == "SHORT":
        tp_price = entry * (1 - tp_pct)
        sl_price = entry * (1 + sl_pct)
    else:
        tp_price = entry * (1 + tp_pct)
        sl_price = entry * (1 - sl_pct)
    end_ms = start_ms + horizon_ms
    tp_bar = sl_bar = None
    for i, c in enumerate(candles[start_idx:], start=start_idx):
        if int(c.get("open_time", 0)) > end_ms:
            break
        high = float(c.get("high", entry)); low = float(c.get("low", entry))
        if side == "SHORT":
            if tp_bar is None and low <= tp_price: tp_bar = i
            if sl_bar is None and high >= sl_price: sl_bar = i
        else:
            if tp_bar is None and high >= tp_price: tp_bar = i
            if sl_bar is None and low <= sl_price: sl_bar = i
    if tp_bar is None and sl_bar is None: return "NONE"
    if tp_bar is not None and sl_bar is None: return "TP_FIRST"
    if sl_bar is not None and tp_bar is None: return "SL_FIRST"
    if tp_bar == sl_bar: return "BOTH"
    return "TP_FIRST" if tp_bar < sl_bar else "SL_FIRST"


def _window_metrics(candles: list, start_idx: int, entry: float, tp_pct: float, sl_pct: float, horizon_hours: int, start_ms: int, side: str = "LONG") -> dict:
    horizon_ms = int(horizon_hours * 3600 * 1000)
    end_ms = start_ms + horizon_ms
    future = [c for c in candles[start_idx:] if int(c.get("open_time", 0)) <= end_ms]
    if not future:
        return {"max_up_pct": None, "max_down_pct": None, "close_return_pct": None, "first_hit": "NONE", "verdict": "NO_DATA"}
    side = str(side or "LONG").upper()
    if side == "SHORT":
        max_fav = max((entry - float(c.get("low", entry))) / entry * 100 for c in future)
        max_adv = min((entry - float(c.get("high", entry))) / entry * 100 for c in future)
        close_r = (entry - float(future[-1].get("close", entry))) / entry * 100
    else:
        max_fav = max((float(c.get("high", entry)) - entry) / entry * 100 for c in future)
        max_adv = min((float(c.get("low", entry)) - entry) / entry * 100 for c in future)
        close_r = (float(future[-1].get("close", entry)) - entry) / entry * 100
    first = _first_hit(candles, start_idx, entry, tp_pct, sl_pct, horizon_ms, start_ms, side)
    tp_threshold = tp_pct * 100
    sl_threshold = sl_pct * 100
    if first == "TP_FIRST": verdict = "GEREKSIZ_ENGEL"
    elif first == "SL_FIRST": verdict = "DOGRU_ENGEL"
    elif first == "BOTH": verdict = "BELIRSIZ_VOLATIL"
    elif max_fav >= tp_threshold and abs(max_adv) < sl_threshold * 0.5: verdict = "KACAN_TREND"
    elif max_fav >= tp_threshold and abs(max_adv) >= sl_threshold: verdict = "BELIRSIZ_VOLATIL"
    elif abs(max_adv) >= sl_threshold: verdict = "DOGRU_ENGEL"
    else: verdict = "BELIRSIZ"
    return {"max_up_pct": round(max_fav, 3), "max_down_pct": round(max_adv, 3), "close_return_pct": round(close_r, 3), "first_hit": first, "verdict": verdict}


def build_block_outcome(block_log: list, all_candles: dict, tp_pct: float = 0.03, sl_pct: float = 0.015, horizons_hours: list | None = None, cooldown_bars: int = 12, bar_seconds: int = 3600, only_reasons: set | None = None, max_per_reason: int = 5000) -> list:
    if horizons_hours is None:
        horizons_hours = [4, 8, 12, 24]
    cooldown_ms = cooldown_bars * bar_seconds * 1000
    last_seen = {}
    reason_count = defaultdict(int)
    rows = []
    for r in block_log:
        sym = r.get("symbol", "")
        reason = r.get("cause", r.get("reason", ""))
        ts_ms = int(r.get("ts_ms", 0) or 0)
        if not sym or not ts_ms or sym not in all_candles:
            continue
        if only_reasons and reason not in only_reasons:
            continue
        if reason_count[reason] >= max_per_reason:
            continue
        key = (sym, reason)
        if ts_ms - last_seen.get(key, 0) < cooldown_ms:
            continue
        last_seen[key] = ts_ms
        candles = all_candles.get(sym) or []
        start_idx = _candle_idx_at(candles, ts_ms)
        if start_idx >= len(candles):
            continue
        entry = float(r.get("price") or candles[start_idx].get("close", 0.0))
        if entry <= 0:
            continue
        eval_idx = start_idx + 1  # same-candle lookahead önleme
        if eval_idx >= len(candles):
            continue
        row = {
            "ts_ms": ts_ms, "symbol": sym, "side": r.get("side", "LONG"), "reason": reason,
            "regime": r.get("regime", ""), "score": r.get("score", ""), "entry_price": round(entry, 6),
        }
        for h in horizons_hours:
            m = _window_metrics(candles, eval_idx, entry, tp_pct, sl_pct, h, ts_ms, r.get("side", "LONG"))
            pfx = f"h{h}_"
            for k, v in m.items(): row[pfx + k] = v
        rows.append(row); reason_count[reason] += 1
    return rows


def _summarize(rows: list, group_key: str, horizons: list | None = None) -> list:
    horizons = horizons or [4, 8, 12, 24]
    groups = defaultdict(list)
    for r in rows:
        groups[r.get(group_key, "UNKNOWN")].append(r)
    out = []
    for key, grp in sorted(groups.items()):
        n = len(grp); rec = {group_key: key, "count": n}
        for h in horizons:
            p = f"h{h}_"
            valid = [r for r in grp if r.get(p + "max_up_pct") is not None]
            if not valid: continue
            vn = len(valid)
            rec[p+"avg_max_up_pct"] = round(sum(r[p+"max_up_pct"] for r in valid) / vn, 3)
            rec[p+"avg_max_down_pct"] = round(sum(r[p+"max_down_pct"] for r in valid) / vn, 3)
            rec[p+"avg_close_return_pct"] = round(sum(r[p+"close_return_pct"] for r in valid) / vn, 3)
            fh = [r[p+"first_hit"] for r in valid]
            vd = [r[p+"verdict"] for r in valid]
            rec[p+"tp_first_rate"] = round(fh.count("TP_FIRST") / vn * 100, 1)
            rec[p+"sl_first_rate"] = round(fh.count("SL_FIRST") / vn * 100, 1)
            rec[p+"gereksiz_pct"] = round(vd.count("GEREKSIZ_ENGEL") / vn * 100, 1)
            rec[p+"dogru_pct"] = round(vd.count("DOGRU_ENGEL") / vn * 100, 1)
            rec[p+"kacan_pct"] = round(vd.count("KACAN_TREND") / vn * 100, 1)
        out.append(rec)
    out.sort(key=lambda x: x.get("h24_gereksiz_pct", 0.0), reverse=True)
    return out


def _write_csv(path: Path, rows: list):
    if not rows: return
    keys = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=keys, delimiter=";", extrasaction="ignore")
        w.writeheader(); w.writerows(rows)


def write_block_outcome_reports(out_dir: Path, rows: list, horizons: list | None = None):
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    if not rows: return
    horizons = horizons or [4, 8, 12, 24]
    _write_csv(out_dir / "block_outcome_analysis_v2.csv", rows)
    _write_csv(out_dir / "block_outcome_summary_v2_by_reason.csv", _summarize(rows, "reason", horizons))
    _write_csv(out_dir / "block_outcome_summary_v2_by_symbol.csv", _summarize(rows, "symbol", horizons))
    _write_csv(out_dir / "block_outcome_summary_v2_by_regime.csv", _summarize(rows, "regime", horizons))
