# true_walk_forward.py — GERCEK Walk-Forward (train/optimize/test/roll)
# ════════════════════════════════════════════════════════════════════
# Aylik segment testinden FARKI:
#   - Her fold'da TRAIN penceresinde parametre secimi yapilir
#   - Secilen parametre OUT-OF-SAMPLE test penceresinde olculur
#   - Pencere ileri kaydirilir (roll)
# Boylece overfit'e karsi gercek dayaniklilik olculur.
# ════════════════════════════════════════════════════════════════════
from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

from backtest import Backtester, _fetch_candles, _load_symbols, _load_cfg, resolve_president_execution_mode
from weekly_symbol_universe import select_universe_for_window, write_universe_history


def _to_ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _folds(start_str, end_str, train_days, test_days, roll_days):
    start = datetime.strptime(start_str, "%Y-%m-%d")
    end   = datetime.strptime(end_str, "%Y-%m-%d")
    folds = []
    cur = start
    while True:
        tr_start = cur
        tr_end   = tr_start + timedelta(days=train_days)
        te_start = tr_end
        te_end   = te_start + timedelta(days=test_days)
        if te_end > end:
            break
        folds.append((tr_start, tr_end, te_start, te_end))
        cur = cur + timedelta(days=roll_days)
    return folds


# Train'de denenecek kucuk parametre izgarasi (overfit'i sinirli tutmak icin az)
PARAM_GRID = [
    {"score_long_open": 92.0},
    {"score_long_open": 95.0},
    {"score_long_open": 97.0},
]


def _apply_params(cfg, params):
    c = copy.deepcopy(cfg)
    c.setdefault("thresholds", {})
    for k, v in params.items():
        c["thresholds"][k] = v
    return c


def run_true_walkforward(cfg, symbols, start_str, end_str, interval,
                         train_days, test_days, roll_days, out_dir):
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    folds = _folds(start_str, end_str, train_days, test_days, roll_days)
    print(f"[TRUE-WF] {len(folds)} fold | train={train_days}g test={test_days}g roll={roll_days}g", flush=True)

    fold_rows = []
    for i, (tr_s, tr_e, te_s, te_e) in enumerate(folds, 1):
        print(f"\n[Fold {i}/{len(folds)}] train {tr_s.date()}→{tr_e.date()} | test {te_s.date()}→{te_e.date()}", flush=True)

        # Fold için tarihsel evren seçimi: sadece train başlangıcından önceki lookback kullanılır.
        fold_symbols = symbols
        wcfg = cfg.get("weekly_symbol_rotation", {}) or {}
        if wcfg.get("enabled", False):
            lookback_days = int(wcfg.get("lookback_days", 30))
            candidate_top = int(wcfg.get("candidate_top", max(len(symbols), 70)))
            candidates = _load_symbols(candidate_top)
            lb_start = _to_ms(tr_s - timedelta(days=lookback_days))
            lb_end = _to_ms(tr_s)
            select_candles = {s: _fetch_candles(s, interval, lb_start, lb_end) for s in candidates}
            fold_symbols = select_universe_for_window(candidates, select_candles, top=len(symbols),
                                                      out_meta=str(out / f"fold{i}_symbols_meta.json"),
                                                      as_of_ms=lb_end)
            write_universe_history(str(out / "symbol_universe_history.csv"), f"fold{i}", fold_symbols, {"mode":"true_wf_fold", "as_of_ms": lb_end})

        # Veriyi bir kez cek (train+test birlikte)
        cbs, htf = {}, {}
        for sym in fold_symbols:
            cbs[sym] = _fetch_candles(sym, interval, _to_ms(tr_s), _to_ms(te_e))
            htf[sym] = _fetch_candles(sym, "1h", _to_ms(tr_s), _to_ms(te_e))

        def slice_candles(src, a, b):
            am, bm = _to_ms(a), _to_ms(b)
            return {s: [c for c in src.get(s, []) if am <= c["open_time"] < bm] for s in fold_symbols}

        train_cbs = slice_candles(cbs, tr_s, tr_e)
        train_htf = slice_candles(htf, tr_s, tr_e)
        test_cbs  = slice_candles(cbs, te_s, te_e)
        test_htf  = slice_candles(htf, te_s, te_e)

        # ── TRAIN: en iyi parametreyi sec ──
        best, best_pnl = None, -1e18
        for params in PARAM_GRID:
            tcfg = _apply_params(cfg, params)
            bt = Backtester(tcfg, str(out / f"fold{i}_train_{params['score_long_open']:.0f}"), interval=interval)
            res = bt.run(fold_symbols, train_cbs, train_htf)
            pnl = float(res["summary"].get("Net_PnL_USD", 0) or 0)
            if pnl > best_pnl:
                best_pnl, best = pnl, params
        print(f"  seçilen param: {best} (train PnL={best_pnl:.2f})", flush=True)

        # ── TEST (OOS): secilen parametre ──
        tcfg = _apply_params(cfg, best)
        bt = Backtester(tcfg, str(out / f"fold{i}_test"), interval=interval)
        res = bt.run(fold_symbols, test_cbs, test_htf)
        s = res["summary"]
        fold_rows.append({
            "Fold": i,
            "Train": f"{tr_s.date()}→{tr_e.date()}",
            "Test":  f"{te_s.date()}→{te_e.date()}",
            "Secilen_Param": json.dumps(best),
            "Train_PnL": f"{best_pnl:.2f}",
            "OOS_Islem": s.get("Toplam_Islem", 0),
            "OOS_WinRate": s.get("Kazanma_Orani", "0%"),
            "OOS_NetPnL": s.get("Net_PnL_USD", "0"),
            "OOS_MaxDD": s.get("Max_DD_Pct", "0%"),
        })
        print(f"  OOS sonuç: PnL={s.get('Net_PnL_USD')} WR={s.get('Kazanma_Orani')}", flush=True)

    # Ozet
    total_oos = sum(float(r["OOS_NetPnL"] or 0) for r in fold_rows)
    pos_folds = sum(1 for r in fold_rows if float(r["OOS_NetPnL"] or 0) > 0)
    summary = {
        "Donem": f"{start_str} → {end_str}",
        "Fold_Sayisi": len(fold_rows),
        "Pozitif_Fold": pos_folds,
        "Toplam_OOS_PnL": round(total_oos, 2),
        "Ort_Fold_PnL": round(total_oos / len(fold_rows), 2) if fold_rows else 0,
        "Train_Gun": train_days, "Test_Gun": test_days, "Roll_Gun": roll_days,
    }
    json.dump(summary, open(out / "true_wf_summary.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    import csv as _csv
    if fold_rows:
        with open(out / "true_wf_folds.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = _csv.DictWriter(f, fieldnames=list(fold_rows[0].keys()), delimiter=";")
            w.writeheader(); w.writerows(fold_rows)
    print(f"\n[TRUE-WF] Bitti. Toplam OOS PnL={total_oos:.2f} | Pozitif fold={pos_folds}/{len(fold_rows)}", flush=True)
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--train-days", type=int, default=60)
    ap.add_argument("--test-days", type=int, default=30)
    ap.add_argument("--roll-days", type=int, default=30)
    ap.add_argument("--out", default="truewf_results/run")
    ap.add_argument("--config", default="config_online.yaml")
    a = ap.parse_args()
    cfg = _load_cfg(a.config)
    resolve_president_execution_mode(cfg)
    syms = _load_symbols(a.top)
    run_true_walkforward(cfg, syms, a.start, a.end, a.interval,
                         a.train_days, a.test_days, a.roll_days, a.out)


if __name__ == "__main__":
    main()
