# robustness_test.py — TRBOT V8 — Guclukluk (Robustluk) Testi
# Haftalik sembol rotasyonu ile BOA tabanli guclukluk analizi
from __future__ import annotations

import argparse
import csv
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import List

import yaml

from backtest import Backtester, _fetch_candles, _load_symbols
from weekly_symbol_universe import select_universe_for_window, write_universe_history


def _load_cfg(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _week_ranges(start_str: str, end_str: str):
    import datetime
    start = datetime.datetime.strptime(start_str, "%Y-%m-%d")
    end   = datetime.datetime.strptime(end_str, "%Y-%m-%d")
    ranges = []
    cur = start
    while cur < end:
        nxt = min(cur + datetime.timedelta(days=7), end)
        ranges.append((
            int(cur.timestamp() * 1000),
            int(nxt.timestamp() * 1000),
            cur.strftime("%Y-W%V"),
        ))
        cur = nxt
    return ranges


def run_robustness(cfg: dict, start_str: str, end_str: str,
                   interval: str, top: int, out_dir: str):
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    weeks   = _week_ranges(start_str, end_str)
    results = []

    for start_ms, end_ms, week_str in weeks:
        week_dir = out_path / week_str
        week_dir.mkdir(exist_ok=True)
        print(f"\n[ROB] Hafta: {week_str}")

        # Bu hafta için taze/tarihsel sembol listesi.
        # weekly_symbol_rotation.enabled=True ise seçim sadece hafta başlangıcından ÖNCEKİ lookback verisiyle yapılır (look-ahead yok).
        wcfg = cfg.get("weekly_symbol_rotation", {}) or {}
        if wcfg.get("enabled", False):
            lookback_days = int(wcfg.get("lookback_days", 30))
            candidate_top = int(wcfg.get("candidate_top", max(top, 70)))
            candidates = _load_symbols(candidate_top)
            lookback_start = start_ms - lookback_days * 24 * 3600 * 1000
            select_candles = {s: _fetch_candles(s, interval, lookback_start, start_ms) for s in candidates}
            symbols = select_universe_for_window(candidates, select_candles, top=top,
                                                 out_meta=str(week_dir / "symbols_weekly_meta.json"),
                                                 as_of_ms=start_ms)
            write_universe_history(str(out_path / "symbol_universe_history.csv"), week_str, symbols, {"mode":"robustness_weekly", "as_of_ms": start_ms})
        else:
            symbols = _load_symbols(top)
        candles_by_sym = {}
        htf_candles    = {}
        for sym in symbols:
            candles_by_sym[sym] = _fetch_candles(sym, interval, start_ms, end_ms)
            htf_candles[sym]    = _fetch_candles(sym, "1h", start_ms, end_ms)

        bt     = Backtester(cfg, str(week_dir), interval=interval)
        result = bt.run(symbols, candles_by_sym, htf_candles)
        summ   = result["summary"]

        row = {
            "Hafta":    week_str,
            "Semboller": len(symbols),
            "Islem":    summ.get("Toplam_Islem", 0),
            "WinRate":  summ.get("Kazanma_Orani", "0%"),
            "NetPnL":   summ.get("Net_PnL_USD", "0"),
            "Getiri":   summ.get("Getiri_Pct", "0%"),
        }
        results.append(row)
        print(f"  PnL: {row['NetPnL']} | Islem: {row['Islem']}")

    total_pnl  = sum(float(r["NetPnL"]) for r in results)
    positive   = sum(1 for r in results if float(r["NetPnL"]) > 0)
    rob_summary= {
        "Donem":       f"{start_str} → {end_str}",
        "Toplam_Hafta":len(results),
        "Pozitif_Hafta":positive,
        "Toplam_PnL":  round(total_pnl, 4),
        "Ort_Haftalik_PnL": round(total_pnl / max(len(results), 1), 4),
    }

    with open(out_path / "robustness_summary.json", "w", encoding="utf-8") as f:
        json.dump(rob_summary, f, ensure_ascii=False, indent=2)

    if results:
        with open(out_path / "robustness_weekly.csv", "w", newline="",
                  encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(results[0].keys()), delimiter=";")
            w.writeheader()
            w.writerows(results)

    print(f"\n[ROB] Toplam PnL: {total_pnl:.4f} | "
          f"Pozitif: {positive}/{len(results)}")
    return rob_summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start",    required=True)
    parser.add_argument("--end",      required=True)
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--top",      type=int, default=20)
    parser.add_argument("--out",      default="robustness_results/latest")
    parser.add_argument("--config",   default="config_online.yaml")
    args = parser.parse_args()

    cfg = _load_cfg(args.config)
    run_robustness(cfg, args.start, args.end, args.interval, args.top, args.out)


if __name__ == "__main__":
    main()
