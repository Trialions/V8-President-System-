# walk_forward.py — TRBOT V8 — Walk-Forward Test
# Klasik walk-forward: aylik bagimsiz backtest segmentleri
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List

import yaml

from backtest import Backtester, _fetch_candles, _load_symbols, resolve_president_execution_mode
from backtest import Backtester, _fetch_candles, _load_symbols
from weekly_symbol_universe import select_universe_for_window, write_universe_history


def _load_cfg(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _month_ranges(start_str: str, end_str: str) -> List[tuple]:
    """start_str..end_str arasindaki aylik (baslangic_ms, bitis_ms, ay_str) listesi doner."""
    import datetime
    start = datetime.datetime.strptime(start_str, "%Y-%m-%d")
    end   = datetime.datetime.strptime(end_str, "%Y-%m-%d")
    ranges = []
    cur = start.replace(day=1)
    while cur <= end:
        # Ayin son gunu
        if cur.month == 12:
            nxt = cur.replace(year=cur.year+1, month=1, day=1)
        else:
            nxt = cur.replace(month=cur.month+1, day=1)
        seg_end = min(nxt, end + datetime.timedelta(days=1))
        ranges.append((
            int(cur.timestamp() * 1000),
            int(seg_end.timestamp() * 1000),
            cur.strftime("%Y-%m"),
        ))
        cur = nxt
    return ranges


def run_walkforward(cfg: dict, symbols: List[str], start_str: str,
                    end_str: str, interval: str, out_dir: str):
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    months = _month_ranges(start_str, end_str)
    monthly_results = []

    for start_ms, end_ms, month_str in months:
        month_dir = out_path / month_str
        month_dir.mkdir(exist_ok=True)
        print(f"\n[WF] Ay: {month_str} ─────────────────────────────────")

        # monthly WF segmentinde opsiyonel tarihsel evren seçimi.
        # Not: gerçek haftalık robustluk için robustness_test.py daha doğru araçtır.
        seg_symbols = symbols
        wcfg = cfg.get("weekly_symbol_rotation", {}) or {}
        if wcfg.get("enabled", False):
            lookback_days = int(wcfg.get("lookback_days", 30))
            candidate_top = int(wcfg.get("candidate_top", max(len(symbols), 70)))
            candidates = _load_symbols(candidate_top)
            lookback_start = start_ms - lookback_days * 24 * 3600 * 1000
            select_candles = {s: _fetch_candles(s, interval, lookback_start, start_ms) for s in candidates}
            seg_symbols = select_universe_for_window(candidates, select_candles, top=len(symbols),
                                                     out_meta=str(month_dir / "symbols_segment_meta.json"),
                                                     as_of_ms=start_ms)
            write_universe_history(str(out_path / "symbol_universe_history.csv"), month_str, seg_symbols, {"mode":"walk_forward_segment", "as_of_ms": start_ms})
        candles_by_sym = {}
        htf_candles    = {}
        for sym in seg_symbols:
            clist = _fetch_candles(sym, interval, start_ms, end_ms)
            candles_by_sym[sym] = clist
            htf_candles[sym]    = _fetch_candles(sym, "1h", start_ms, end_ms)
            print(f"  {sym}: {len(clist)} mum", flush=True)

        bt     = Backtester(cfg, str(month_dir), interval=interval)
        result = bt.run(seg_symbols, candles_by_sym, htf_candles)
        summ   = result["summary"]

        row = {
            "Ay":           month_str,
            "Islem":        summ.get("Toplam_Islem", 0),
            "WinRate":      summ.get("Kazanma_Orani", "0%"),
            "NetPnL":       summ.get("Net_PnL_USD", "0"),
            "Getiri":       summ.get("Getiri_Pct", "0%"),
            "BitisEquity":  summ.get("Bitis_Equity", "0"),
        }
        monthly_results.append(row)
        print(f"  → PnL: {row['NetPnL']} | WR: {row['WinRate']} | "
              f"Islem: {row['Islem']}")

    # Toplam ozet
    total_pnl = sum(float(r["NetPnL"]) for r in monthly_results)
    positive   = sum(1 for r in monthly_results if float(r["NetPnL"]) > 0)
    total_tr   = sum(int(r["Islem"]) for r in monthly_results)

    wf_summary = {
        "Donem":           f"{start_str} → {end_str}",
        "Toplam_Ay":       len(monthly_results),
        "Pozitif_Ay":      positive,
        "Toplam_Islem":    total_tr,
        "Toplam_PnL":      round(total_pnl, 4),
        "Ort_Aylik_PnL":   round(total_pnl / max(len(monthly_results), 1), 4),
    }

    # wf_summary.json
    with open(out_path / "wf_summary.json", "w", encoding="utf-8") as f:
        json.dump(wf_summary, f, ensure_ascii=False, indent=2)

    # wf_monthly.csv
    if monthly_results:
        with open(out_path / "wf_monthly.csv", "w", newline="",
                  encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(monthly_results[0].keys()),
                               delimiter=";")
            w.writeheader()
            w.writerows(monthly_results)

    print(f"\n[WF] TAMAMLANDI ───────────────────────────────────")
    print(f"  Toplam PnL: {total_pnl:.4f} USD")
    print(f"  Pozitif ay: {positive}/{len(monthly_results)}")
    print(f"  Sonuc:      {out_dir}")
    return wf_summary


def main():
    parser = argparse.ArgumentParser(description="TRBOT V8 Walk-Forward Test")
    parser.add_argument("--start",    type=str, required=True)
    parser.add_argument("--end",      type=str, required=True)
    parser.add_argument("--interval", type=str, default="1h")
    parser.add_argument("--top",      type=int, default=20)
    parser.add_argument("--out",      type=str, default="walkforward_results/latest")
    parser.add_argument("--config",   type=str, default="config_online.yaml")
    args = parser.parse_args()

    cfg     = _load_cfg(args.config)
    resolve_president_execution_mode(cfg)
    symbols = _load_symbols(args.top)
    run_walkforward(cfg, symbols, args.start, args.end,
                    args.interval, args.out)


if __name__ == "__main__":
    main()
