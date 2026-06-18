# backtest.py — TRBOT President System V8 — Backtest Motoru
# BOA (Block Outcome Analyzer), ghost signal, post-analiz, filter events dahil
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

# ─── Strateji ve Hesaplama ────────────────────────────────────────────────────
from strategy_core import score_symbol
from adaptive_sl import compute as adaptive_sl_compute
from market_regime import MarketRegimeDetector
from symbol_manager import SymbolManager
from adaptive_exit import classify_trade
from block_outcome_analyzer import build_block_outcome, write_block_outcome_reports

# ─── President karar motoru (ortak pipeline) ──────────────────────────────────
from president_runtime import PresidentRuntime
from modules.decision_packet import Action, Side

# ─── Sabitler ─────────────────────────────────────────────────────────────────
COMMISSION_PCT_DEFAULT = 0.0004  # %0.04
SLIPPAGE_PCT_DEFAULT   = 0.0003  # %0.03

# ─── Yardimci ─────────────────────────────────────────────────────────────────
def _load_cfg(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _fetch_candles(symbol: str, interval: str, start_ts: int, end_ts: int,
                   cache_dir: str = "data/cache") -> List[dict]:
    """Binance REST API ile mum verisi ceker, cache'e kaydeder."""
    import requests
    os.makedirs(cache_dir, exist_ok=True)
    fn = os.path.join(cache_dir, f"{symbol}_{interval}_{start_ts}_{end_ts}.json")
    if os.path.exists(fn):
        with open(fn, encoding="utf-8") as f:
            return json.load(f)
    url = "https://api.binance.com/api/v3/klines"
    all_candles = []
    cur = start_ts
    while cur < end_ts:
        try:
            r = requests.get(url, params={
                "symbol": symbol, "interval": interval,
                "startTime": cur, "endTime": end_ts, "limit": 1000
            }, timeout=15)
            data = r.json()
            if not data or not isinstance(data, list):
                break
            for d in data:
                all_candles.append({
                    "open_time":  d[0], "open": float(d[1]),
                    "high":       float(d[2]), "low": float(d[3]),
                    "close":      float(d[4]), "volume": float(d[5]),
                    "close_time": d[6],
                })
            cur = data[-1][0] + 1
            if len(data) < 1000:
                break
            time.sleep(0.1)
        except Exception as e:
            print(f"[WARN] Veri cekim hatasi {symbol}: {e}")
            break
    with open(fn, "w", encoding="utf-8") as f:
        json.dump(all_candles, f)
    return all_candles


def _load_symbols(top: int = 20) -> List[str]:
    """symbols_top70.json veya fallback listesi."""
    try:
        if os.path.exists("symbols_top70.json"):
            with open("symbols_top70.json", encoding="utf-8") as f:
                syms = json.load(f)
            if isinstance(syms, list) and syms:
                return syms[:top]
    except Exception:
        pass
    return ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
            "ADAUSDT","DOTUSDT","AVAXUSDT","MATICUSDT","LINKUSDT"][:top]


# ─── Ana Backtest Sinifi ──────────────────────────────────────────────────────
class Backtester:

    def __init__(self, cfg: dict, out_dir: str, mode: str = "normal",
                 president_enabled: bool = True, interval: str = "1h"):
        self.cfg     = cfg
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.mode    = mode
        self.interval = interval
        self.president_enabled = bool(president_enabled)

        risk  = cfg.get("risk", {})
        lim   = cfg.get("limits", {})
        thr   = cfg.get("thresholds", {})
        misc  = cfg.get("misc", {})
        account = cfg.get("account", {})
        ptp   = cfg.get("partial_tp", {})
        mtf   = cfg.get("mtf", {})

        self.equity          = float(account.get("starting_equity_usdt", misc.get("starting_equity_usdt", cfg.get("risk", {}).get("starting_equity_usdt", 1000.0))))
        self.commission      = float(misc.get("commission_pct", 0.04)) / 100
        self.slippage        = float(misc.get("slippage_pct", 0.03)) / 100

        self.sl_pct          = float(risk.get("hard_stop_pct", 1.5)) / 100
        self.tp_pct          = float(risk.get("take_profit_min_pct", 3.0)) / 100
        self.trail_step      = float(risk.get("trailing_step_pct", 0.7)) / 100
        self.atr_multiplier  = float(risk.get("atr_multiplier", 2.0))
        self.max_stop_pct    = float(risk.get("max_stop_pct", 4.5)) / 100
        self.use_atr_stop    = bool(risk.get("use_atr_stop", True))
        self.use_trailing    = bool(risk.get("use_trailing", True))
        self.min_hold_bars   = max(1, int(risk.get("min_hold_minutes", 60)) // 60)
        self.risk_per_trade  = float(risk.get("risk_per_trade_pct", 1.0)) / 100
        self.min_profit_cls  = float(risk.get("min_profit_close_pct", 3.0)) / 100

        dt = cfg.get("dynamic_trail", {})
        self.dynamic_trail   = bool(dt.get("enabled", True))
        self.dt_min          = float(dt.get("min_pct", 0.5)) / 100
        self.dt_max          = float(dt.get("max_pct", 2.5)) / 100
        self.dt_atr_m        = float(dt.get("atr_mult", 0.5))

        self.score_long_open  = float(thr.get("score_long_open", 97.0))
        self.score_short_open = float(thr.get("score_short_open", 5.0))
        self.score_close      = float(thr.get("score_close", 50.0))

        self.max_open_pos    = int(lim.get("max_open_positions", 3))
        self.max_trades_day  = int(lim.get("max_trades_per_day", 8))
        self.max_hold_bars   = int(lim.get("max_hold_hours", 48))
        self.daily_target    = float(lim.get("daily_target_pct", 10.0)) / 100
        self.daily_loss_lim  = float(lim.get("daily_loss_limit_pct", 3.0)) / 100

        self.partial_tp_en   = bool(ptp.get("enabled", True))
        self.tp1_r_mult      = float(ptp.get("tp1_r_mult", 0.75))
        self.tp1_close_pct   = float(ptp.get("close_pct", 0.40))

        # V8.5 TP1 Progress Manager: TP1'e ilerlemeyen pozisyonda riski azaltır.
        tpm = cfg.get("tp1_progress_manager", {})
        self.tp1_prog_enabled = bool(tpm.get("enabled", True))
        self.tp1_prog_check_bars = int(tpm.get("check_after_bars", 5))
        self.tp1_prog_min_progress = float(tpm.get("min_progress_to_tp1", 0.25))
        self.tp1_prog_reduce_pct = float(tpm.get("reduce_pct", 0.35))
        self.tp1_prog_only_if_not_profitable = bool(tpm.get("only_reduce_if_not_profitable", True))
        self.tp1_prog_tighten_bars = int(tpm.get("tighten_trail_after_bars", 4))
        self.tp1_prog_tighten_mult = float(tpm.get("tighten_trail_mult", 0.55))
        self.tp1_prog_early_exit_bars = int(tpm.get("early_exit_after_bars", 8))
        self.tp1_prog_early_exit_r = float(tpm.get("early_exit_if_change_below_r", -0.45))

        self.mtf_enabled     = bool(mtf.get("enabled", True))
        self.mtf_long_min    = float(mtf.get("htf_long_min", 55.0))

        # ADX filtresi
        adx_f = cfg.get("adx_filter", {})
        self.adx_filter_en   = bool(adx_f.get("enabled", True))
        self.adx_thr         = float(adx_f.get("threshold", 29.0))

        # require_adx_when_filter_enabled: ADX filtresi açıkken ADX=0 (hesaplanamadı)
        # gelirse sinyal otomatik BYPASS edilir (eski davranış). Bu alan True olursa
        # ADX=0 durumunda da sinyal bloklanır (bypass yok) — varsayılan eski davranışla
        # aynı (False) olduğu için mevcut testler etkilenmez.
        ie = cfg.get("indicator_engine", {})
        self.require_adx_strict = bool(ie.get("require_adx_when_filter_enabled", False))

        # BTC genel düşüş filtresi — varsayılan KAPALI (enabled=false). Açılırsa,
        # BTC son N mumda drop_pct'ten fazla düştüyse TÜM LONG sinyaller bloklanır
        # (Core Long dahil — short_surgeon.btc_risk_off'tan AYRI, o sadece SHORT
        # dalına özel bir koruma; bu filtre LONG tarafı için genel bir güvenlik kapısı).
        btc_f = cfg.get("btc_filter", {})
        self.btc_filter_en      = bool(btc_f.get("enabled", False))
        self.btc_filter_candles = int(btc_f.get("lookback_candles", 4))
        self.btc_filter_drop    = float(btc_f.get("drop_pct", 1.5))

        # Backtest'in varsayılan President modu — SADECE CLI'da --president-mode
        # verilmediyse kullanılır (CLI argümanı her zaman önceliklidir, geriye
        # uyumluluk bozulmaz). "simulated_active" = mevcut varsayılan davranış
        # (president_enabled=True, shadow_mode config'teki değeriyle).
        self.default_president_mode = str(cfg.get("backtest", {}).get(
            "president_execution_mode", "simulated_active"))

        # Kara liste
        bl = cfg.get("symbol_blacklist", {})
        self.bl_enabled      = bool(bl.get("enabled", False))
        self.bl_symbols      = set(s.upper() for s in (bl.get("symbols") or []))

        # Ghost analiz
        ghost = cfg.get("ghost_trade_analysis", {})
        self.ghost_en        = bool(ghost.get("enabled", True))
        self.ghost_fwd_bars  = int(ghost.get("lookforward_bars", 12))
        self.ghost_min_score = float(ghost.get("min_score_to_track", 90.0))

        self.regime_detector = MarketRegimeDetector(cfg)
        self.sym_mgr = SymbolManager(cfg, starting_equity=self.equity)

        rot = cfg.get("position_rotation", {})
        self.rotation_enabled = bool(rot.get("enabled", False))
        self.rotation_min_score = float(rot.get("min_candidate_score", 90.0))
        self.rotation_min_delta = float(rot.get("min_score_delta", 12.0))
        self.rotation_shadow = bool(rot.get("shadow_mode", True))
        self.rotation_allow_close_profitable = bool(rot.get("allow_close_profitable", False))
        self.rotation_max_per_day = int(rot.get("max_rotations_per_day", 2))
        self._daily_rotations: Dict[str, int] = defaultdict(int)

        # ── President global candidate ranking / BOA feedback ────────────────
        pr = cfg.get("president", {}) or {}
        gr = pr.get("global_ranking", {}) or {}
        self.global_ranking_enabled = bool(gr.get("enabled", True))
        self.rank_reject_log = bool(gr.get("write_rank_rejections", True))
        self.rank_max_candidates_per_bar = int(gr.get("max_candidates_per_bar", 999))
        self.rank_bad_quality_below = float(gr.get("bad_quality_below", 58.0))
        self.rank_chop_labels = set(str(x).upper() for x in gr.get("chop_labels", ["CHOP_RISK", "EXHAUSTED"]))

        bf = pr.get("boa_feedback", {}) or {}
        self.boa_feedback_enabled = bool(bf.get("enabled", True))
        self.boa_feedback_weight = float(bf.get("weight", 1.0))
        self.boa_feedback_max_adj = float(bf.get("max_adjustment", 6.0))
        self.boa_feedback_min_count = int(bf.get("min_count", 8))
        self.boa_feedback_file = Path(str(bf.get("memory_file", "data/boa_feedback_memory.json")))
        self.boa_feedback_memory = self._load_boa_feedback_memory()

        # ── President karar motoru (backtest/WF/robustluk hepsi ayni motoru kullanir)
        self.runtime = None
        if self.president_enabled:
            self.runtime = PresidentRuntime(cfg, data_dir=str(self.out_dir / "_president"))

        # Durum degiskenleri
        self.open_positions: Dict[str, dict] = {}
        self.trades:    List[dict] = []
        self.equity_curve: List[Tuple[str, float]] = []
        self.filter_events: List[dict] = []
        self.ranking_events: List[dict] = []
        self.ghost_signals: List[dict] = []
        self.block_outcomes: List[dict] = []
        self.block_events:   List[dict] = []   # GERCEK BOA: bloklanan sinyaller
        self._cbs: Dict[str, List[dict]] = {}  # post-analiz icin mum referansi
        self._daily_trade_count: Dict[str, int] = defaultdict(int)
        self._daily_pnl: Dict[str, float] = defaultdict(float)
        self._pnl_running = 0.0

    # ── Ana Dongu ─────────────────────────────────────────────────────
    def run(self, symbols: List[str], candles_by_sym: Dict[str, List[dict]],
            htf_candles: Dict[str, List[dict]] = None) -> dict:
        """
        Tek bir backtest dongusu.
        candles_by_sym: {symbol: [mum_dict,...]}
        htf_candles: {symbol: [1h_mum_dict,...]} (MTF icin)
        """
        htf_candles = htf_candles or {}
        self._cbs = candles_by_sym
        self._run_symbols = list(symbols or [])
        all_ts = sorted(set(
            c["open_time"]
            for sym in symbols
            for c in candles_by_sym.get(sym, [])
        ))

        # Her sembol icin mum indexi
        sym_idx = {sym: 0 for sym in symbols}
        sym_prices  = {sym: [] for sym in symbols}
        sym_highs   = {sym: [] for sym in symbols}
        sym_lows    = {sym: [] for sym in symbols}
        sym_vols    = {sym: [] for sym in symbols}
        htf_prices  = {sym: [] for sym in symbols}

        # HTF pre-load
        for sym in symbols:
            for c in htf_candles.get(sym, []):
                htf_prices[sym].append(float(c["close"]))

        for ts in all_ts:
            # Sembol bazli guncelle
            for sym in symbols:
                clist = candles_by_sym.get(sym, [])
                idx   = sym_idx[sym]
                if idx < len(clist) and clist[idx]["open_time"] == ts:
                    c = clist[idx]
                    sym_prices[sym].append(float(c["close"]))
                    sym_highs[sym].append(float(c["high"]))
                    sym_lows[sym].append(float(c["low"]))
                    sym_vols[sym].append(float(c["volume"]))
                    # BTC ile rejim guncelle
                    if sym == "BTCUSDT":
                        self.regime_detector.update(float(c["close"]))
                    sym_idx[sym] += 1

            # Tarih
            date_str = time.strftime("%Y-%m-%d", time.gmtime(ts // 1000))
            regime   = self.regime_detector.get_regime()

            # V8.5.5: aynı mumdaki tüm adayları önce topla, sonra President ranking ile seç.
            ranking_candidates = []
            ts_str  = time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts // 1000))

            for sym in symbols:
                prices  = sym_prices[sym]
                if len(prices) < 50:
                    continue
                highs   = sym_highs[sym]
                lows    = sym_lows[sym]
                vols    = sym_vols[sym]
                htf_p   = htf_prices.get(sym, [])

                result  = score_symbol(prices, highs, lows, vols)
                score   = result["final_score"]

                # Acik pozisyon yonetimi ranking'den önce yapılır; kapanan pozisyon aynı mumda kapasite açabilir.
                if sym in self.open_positions:
                    self._manage_position(sym, prices, result, ts_str, date_str, ts)
                    continue

                self._resolve_pending_sl_bt(sym, ts)

                if self.global_ranking_enabled and self.president_enabled and self.runtime:
                    cand = self._evaluate_candidate_for_ranking(
                        sym, score, result, prices, highs, lows, vols, htf_p, regime, ts_str, date_str, ts,
                        sym_prices.get("BTCUSDT", []),
                    )
                    if cand:
                        ranking_candidates.append(cand)
                else:
                    self._try_open(sym, score, result, prices, highs, lows, vols,
                                   htf_p, regime, ts_str, date_str, ts,
                                   sym_prices.get("BTCUSDT", []))

            if self.global_ranking_enabled and self.president_enabled and self.runtime and ranking_candidates:
                self._open_ranked_candidates(ranking_candidates, ts_str, date_str, ts)

            # Equity kaydi (her timestamp)
            self.equity_curve.append((
                time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts // 1000)),
                round(self.equity + self._pnl_running, 4)
            ))

        # Donem sonu: tum acik pozisyonlari kapat (son mum zamanıyla)
        last_candle_ts = all_ts[-1] if all_ts else 0
        self._force_close_all(last_candle_ts)
        # Force-close sonrası equity_curve'e gerçek son değeri yaz
        final_eq = round(self.equity + self._pnl_running, 4)
        if self.equity_curve:
            last_ts = self.equity_curve[-1][0]
            self.equity_curve.append((last_ts + " [EOT]", final_eq))
        self._post_boa_analysis()
        return self._generate_report()


    # ── Profesyonel PnL yardımcıları ─────────────────────────────────
    def _fee_cost(self, price: float, qty: float) -> float:
        return float(price) * float(qty) * (self.commission + self.slippage)

    def _gross_pnl(self, side: str, entry: float, exit_price: float, qty: float) -> float:
        return ((exit_price - entry) if side == "LONG" else (entry - exit_price)) * qty

    # ── V8.5.5 President Global Ranking / BOA Feedback ───────────────
    def _load_boa_feedback_memory(self) -> dict:
        """Önceki testlerden üretilmiş BOA hafızasını okur. Aynı testin gelecek verisini kullanmaz."""
        try:
            path = self.boa_feedback_file
            if not path.is_absolute():
                path = Path.cwd() / path
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8")) or {}
        except Exception:
            pass
        return {}

    def _boa_feedback_report(self, sym: str, side: str, regime: str, reason_hint: str = "") -> dict:
        """BOA hafızasından küçük bir edge üretir. President'ı baypas etmez; sadece feature'dır."""
        if not self.boa_feedback_enabled:
            return {"enabled": False, "adjustment": 0.0, "reason": "disabled"}
        mem = self.boa_feedback_memory or {}
        keys = [
            f"symbol:{sym}:{side}",
            f"regime:{regime}:{side}",
            f"reason:{reason_hint}:{side}" if reason_hint else "",
            f"side:{side}",
        ]
        adj_sum = 0.0; weight_sum = 0.0; used = []
        for k in keys:
            if not k or k not in mem:
                continue
            rec = mem.get(k, {}) or {}
            n = int(rec.get("count", 0) or 0)
            if n < self.boa_feedback_min_count:
                continue
            edge = float(rec.get("edge", 0.0) or 0.0)
            w = min(1.0, n / max(self.boa_feedback_min_count * 4, 1))
            adj_sum += edge * w; weight_sum += w
            used.append({"key": k, "count": n, "edge": round(edge, 3)})
        adj = (adj_sum / weight_sum) if weight_sum > 0 else 0.0
        adj = max(-self.boa_feedback_max_adj, min(self.boa_feedback_max_adj, adj * self.boa_feedback_weight))
        return {"enabled": True, "adjustment": round(adj, 3), "used": used}

    def _evaluate_candidate_for_ranking(self, sym: str, score: float, result: dict,
                                        prices: list, highs: list, lows: list, vols: list,
                                        htf_p: list, regime: str, ts_str: str, date_str: str,
                                        ts_ms: int = 0, btc_prices: list = None):
        """Sert filtrelerden geçip President kararı OPEN olan adayı ranking havuzuna alır."""
        price   = prices[-1] if prices else 0.0
        if price <= 0:
            return None
        atr_pct = result.get("components", {}).get("atr_pct", 0.0)
        adx_val = result.get("components", {}).get("adx", 0.0)
        is_long_candidate = score >= self.score_long_open

        if sym != "BTCUSDT" and self.bl_enabled and sym in self.bl_symbols:
            self._log_filter("SYMBOL_BLACKLIST", sym, score, ts_str)
            if is_long_candidate:
                self._record_block(ts_ms, sym, "SYMBOL_BLACKLIST", price, regime, score)
            return None

        if self._daily_trade_count.get(date_str, 0) >= self.max_trades_day:
            if is_long_candidate:
                self._log_filter("DAILY_TRADE_LIMIT", sym, score, ts_str)
                self._record_block(ts_ms, sym, "DAILY_TRADE_LIMIT", price, regime, score)
            return None

        adx_blocks = self.adx_filter_en and is_long_candidate and (
            (adx_val > 0 and adx_val < self.adx_thr) or
            (adx_val <= 0 and self.require_adx_strict)
        )
        if adx_blocks:
            self._log_filter("ADX_TOO_LOW", sym, score, ts_str, extra={"adx": round(adx_val, 1)})
            self._record_block(ts_ms, sym, "ADX_TOO_LOW", price, regime, score)
            return None

        if self.btc_filter_en and is_long_candidate and btc_prices and len(btc_prices) >= self.btc_filter_candles + 1:
            _b_start = btc_prices[-(self.btc_filter_candles + 1)]
            _b_end   = btc_prices[-1]
            _b_drop  = (_b_end - _b_start) / _b_start * 100 if _b_start > 0 else 0.0
            if _b_drop <= -self.btc_filter_drop:
                self._log_filter("BTC_FILTER_DROP", sym, score, ts_str, extra={"btc_drop_pct": round(_b_drop, 2)})
                self._record_block(ts_ms, sym, "BTC_FILTER_DROP", price, regime, score)
                return None

        htf_sc = 100.0 if not self.mtf_enabled else 50.0
        if self.mtf_enabled and len(htf_p) >= 50:
            try:
                htf_sc = score_symbol(htf_p)["final_score"]
            except Exception:
                htf_sc = 50.0
            if is_long_candidate and htf_sc < self.mtf_long_min:
                self._log_filter("MTF_NO_CONFIRM", sym, score, ts_str, extra={"htf": round(htf_sc, 1)})
                self._record_block(ts_ms, sym, "MTF_NO_CONFIRM", price, regime, score)
                return None

        ts_sec = (ts_ms / 1000) if ts_ms else time.time()
        sentiment = "BEARISH" if regime == "BEARISH" else ("BULLISH" if regime in ("BULL", "TREND") else "NEUTRAL")

        # BOA feedback aynı testin geleceğini kullanmaz; sadece geçmiş/önceki hafıza feature'ıdır.
        # Side henüz kesinleşmediği için ilk etapta nötr verilir; President side seçtikten sonra packet.extra'da güncellenir.
        result = dict(result or {})
        result["boa_feedback_report"] = self._boa_feedback_report(sym, "LONG", regime)
        packet = self.runtime.evaluate(sym, ts_sec, score, result, regime, htf_sc, sentiment,
                                       prices, highs, lows, vols, btc_prices)
        if packet.side.value in ("LONG", "SHORT"):
            boa_rep = self._boa_feedback_report(sym, packet.side.value, regime)
            packet.extra["boa_feedback_report"] = boa_rep
            # President kararından sonra ranking score'a da aynı küçük edge eklenir.
            rank_score = max(0.0, min(100.0, float(packet.final_score) + float(boa_rep.get("adjustment", 0.0))))
        else:
            rank_score = float(packet.final_score)

        if packet.action == Action.OPEN:
            return {
                "symbol": sym, "score": score, "result": result, "packet": packet,
                "price": price, "adx_val": adx_val, "atr_pct": atr_pct, "regime": regime,
                "ts_str": ts_str, "date_str": date_str, "ts_ms": ts_ms,
                "prices": prices, "highs": highs, "lows": lows, "vols": vols,
                "htf_score": htf_sc, "rank_score": round(rank_score, 3),
                "quality_score": float((packet.extra.get("quality_score_report") or {}).get("score", 50.0) or 50.0),
                "symbol_mult": self.sym_mgr.size_multiplier(sym) if hasattr(self, "sym_mgr") else 1.0,
                "boa_feedback": packet.extra.get("boa_feedback_report", {}),
            }

        open_votes_list = [v for v in packet.branch_votes.values() if v.action == Action.OPEN]
        if open_votes_list or is_long_candidate:
            cause = "PRESIDENT_" + (packet.reason.split()[0] if packet.reason else "BLOCK")
            intended_side = (packet.side.value if packet.side.value != "NONE" else (open_votes_list[0].side.value if open_votes_list else "LONG"))
            self._record_block(ts_ms, sym, cause, price, regime, score, side=intended_side)
        if self.ghost_en and score >= self.ghost_min_score:
            self._log_ghost(sym, score, result, prices, ts_str, "PRESIDENT_BLOCK")
        return None

    def _rank_reject_reason(self, cand: dict, selected_min_score: float = 0.0) -> str:
        q = float(cand.get("quality_score", 50.0) or 50.0)
        sym_mult = float(cand.get("symbol_mult", 1.0) or 1.0)
        label = str(getattr(cand.get("packet"), "label", "") or "").upper()
        if q < self.rank_bad_quality_below:
            return "RANK_REJECTED_BAD_QUALITY"
        if sym_mult < 0.70:
            return "RANK_REJECTED_SYMBOL_PENALTY"
        if label in self.rank_chop_labels:
            return "RANK_REJECTED_CHOP_RISK"
        return "RANK_REJECTED_LOWER_SCORE"

    def _open_ranked_candidates(self, candidates: list, ts_str: str, date_str: str, ts_ms: int):
        """Aynı timestamp adaylarını birlikte sıralar ve kapasiteye göre en iyileri açar.

        V8.5.7 kuralı:
        - Eski kaba MAX_POSITIONS sebebi aynı-candle ranking içinde kullanılmaz.
        - Eğer test daha önceki mumlardan dolayı zaten tamamen doluysa: MAX_POSITIONS_ALREADY_FULL.
        - Eğer aynı mumda adaylar arası seçim yapıldıysa: RANK_SELECTED / RANK_REJECTED_* yazılır.
        """
        if not candidates:
            return
        candidates = sorted(
            candidates,
            key=lambda c: (float(c.get("rank_score", 0.0)), float(c.get("quality_score", 0.0))),
            reverse=True,
        )
        if self.rank_max_candidates_per_bar > 0:
            candidates = candidates[:self.rank_max_candidates_per_bar]

        start_active = len(self.open_positions)
        available = max(0, self.max_open_pos - start_active)
        opened = []

        # Kapasite bu mum başlamadan zaten doluysa bunu ranking reddi gibi değil, gerçek portföy doluluğu gibi yaz.
        if available <= 0:
            for rank, cand in enumerate(candidates, start=1):
                self._log_filter("MAX_POSITIONS_ALREADY_FULL", cand["symbol"], cand["rank_score"], ts_str, extra={
                    "rank": rank, "side": cand["packet"].side.value, "label": cand["packet"].label,
                    "quality_score": round(cand.get("quality_score", 0.0), 2),
                    "active_positions": start_active, "max_positions": self.max_open_pos,
                    "boa_adj": (cand.get("boa_feedback") or {}).get("adjustment", 0.0),
                })
                self._record_block(cand["ts_ms"], cand["symbol"], "MAX_POSITIONS_ALREADY_FULL", cand["price"], cand["regime"], cand["score"], side=cand["packet"].side.value)
            return

        for cand in candidates:
            if available <= 0:
                break
            sym = cand["symbol"]
            if sym in self.open_positions:
                continue
            packet = cand["packet"]
            branch_scores = {k: round(v.score, 2) for k, v in packet.branch_votes.items()}
            self._open_from_decision(
                sym, packet.side.value, cand["price"], cand["score"], cand["adx_val"], cand["atr_pct"],
                cand["regime"], cand["ts_str"], cand["date_str"], packet.sl_pct, packet.size_mult,
                label=packet.label, decision_id=packet.decision_id, branch_scores=branch_scores,
                htf_score=cand.get("htf_score", 50.0), prices=cand["prices"], highs=cand["highs"], lows=cand["lows"], vols=cand["vols"],
                packet_extra=getattr(packet, "extra", {}), score_components=(cand["result"].get("components", {}) or {}),
                rank_context={
                    "rank_score": cand.get("rank_score", ""),
                    "candidate_count": len(candidates),
                    "rank_position": candidates.index(cand) + 1,
                    "boa_feedback": cand.get("boa_feedback") or {},
                },
            )
            opened.append(sym)
            available -= 1

        if not self.rank_reject_log:
            return
        selected_min = min([c.get("rank_score", 0.0) for c in candidates if c["symbol"] in opened], default=0.0)
        for rank, cand in enumerate(candidates, start=1):
            if cand["symbol"] in opened:
                self._log_ranking_event("RANK_SELECTED", cand, ts_str, rank, selected_min, opened_count=len(opened), total_candidates=len(candidates))
                continue
            reason = self._rank_reject_reason(cand, selected_min)
            self._log_ranking_event(reason, cand, ts_str, rank, selected_min, opened_count=len(opened), total_candidates=len(candidates))
            self._record_block(cand["ts_ms"], cand["symbol"], reason, cand["price"], cand["regime"], cand["score"], side=cand["packet"].side.value)
            if self.ghost_en and cand["score"] >= self.ghost_min_score:
                self._log_ghost(cand["symbol"], cand["score"], cand["result"], cand["prices"], ts_str, reason)

    def _log_ranking_event(self, cause: str, cand: dict, ts_str: str, rank: int, selected_min: float, opened_count: int, total_candidates: int):
        """Hem filter_events.csv hem ayrı candidate_ranking_events.csv için zengin ranking logu."""
        pkt = cand.get("packet")
        ev_extra = {
            "rank": rank,
            "side": pkt.side.value if pkt else "",
            "label": pkt.label if pkt else "",
            "quality_score": round(cand.get("quality_score", 0.0), 2),
            "selected_min_rank_score": round(selected_min, 3),
            "opened_count": opened_count,
            "total_candidates": total_candidates,
            "active_positions_after": len(self.open_positions),
            "max_positions": self.max_open_pos,
            "boa_adj": (cand.get("boa_feedback") or {}).get("adjustment", 0.0),
        }
        self._log_filter(cause, cand["symbol"], cand.get("rank_score", cand.get("score", 0.0)), ts_str, extra=ev_extra)
        row = {"ts": ts_str, "symbol": cand["symbol"], "cause": cause, "rank_score": round(cand.get("rank_score", 0.0), 3)}
        row.update(ev_extra)
        self.ranking_events.append(row)

    def _maybe_rotate_for_candidate(self, sym: str, score: float, price: float,
                                    ts_str: str, date_str: str, ts_ms: int) -> bool:
        """
        SAFETY PATCH V8.4.1:
        Rotation artık President kararından ÖNCE fiziksel pozisyon kapatmaz.
        Burada sadece aday loglanır. Yer açma/kapama kararı ayrı President aksiyonu
        haline getirilene kadar return False kalır.
        """
        if not self.rotation_enabled or len(self.open_positions) < self.max_open_pos:
            return False
        if score < self.rotation_min_score:
            return False
        if self._daily_rotations.get(date_str, 0) >= self.rotation_max_per_day:
            return False

        weakest_sym, weakest_pos = None, None
        weakest_score = 999.0
        weakest_change = 0.0
        for osym, pos in self.open_positions.items():
            last_price = float(pos.get("last_price", pos.get("entry", price)))
            entry = float(pos.get("entry", last_price))
            mult = 1 if pos.get("side") == "LONG" else -1
            change = ((last_price - entry) / entry * mult) if entry else 0.0
            # Kârdaki pozisyonlar varsayılan olarak rotasyon dışıdır.
            if change > 0 and not self.rotation_allow_close_profitable:
                continue
            ps = float(pos.get("score", 0.0))
            # Basit zayıflık: düşük açılış skoru + mevcut zarar
            weakness = ps + max(change * 100, -20)
            if weakness < weakest_score:
                weakest_sym, weakest_pos, weakest_score, weakest_change = osym, pos, weakness, change

        if not weakest_sym or score < float(weakest_pos.get("score", 0.0)) + self.rotation_min_delta:
            return False

        self._log_filter("ROTATION_CANDIDATE_SHADOW", sym, score, ts_str, extra={
            "candidate": sym,
            "candidate_score": round(score, 2),
            "would_replace": weakest_sym,
            "old_score": round(float(weakest_pos.get("score", 0.0)), 2),
            "old_unrealized_pct": round(weakest_change * 100, 3),
            "shadow_only": True,
        })
        # Güvenlik: President ROTATE_AND_OPEN aksiyonu yazılana kadar fiziksel kapatma yok.
        return False

    # ── Pozisyon Yonetimi ─────────────────────────────────────────────
    def _manage_position(self, sym: str, prices: list, result: dict,
                         ts_str: str, date_str: str, ts_ms: int = 0):
        pos   = self.open_positions[sym]
        price = prices[-1]
        mult  = 1 if pos["side"] == "LONG" else -1
        change = (price - pos["entry"]) / pos["entry"] * mult
        bars_held = pos.get("bars_held", 0) + 1
        pos["bars_held"]  = bars_held
        pos["last_price"] = price   # force_close için son bilinen fiyat
        pos_sl_pct = pos.get("sl_pct", self.sl_pct)
        reason = None

        # Partial TP
        if self.partial_tp_en and not pos.get("tp1_done", False):
            tp1_target = pos_sl_pct * self.tp1_r_mult
            if change >= tp1_target:
                partial_qty = pos["qty"] * float(pos.get("tp1_close_pct", self.tp1_close_pct))
                gross = self._gross_pnl(pos["side"], pos["entry"], price, partial_qty)
                exit_cost = self._fee_cost(price, partial_qty)
                pnl = gross - exit_cost
                self._pnl_running += pnl
                pos["qty"] -= partial_qty
                pos["tp1_done"] = True
                pos["tp1_pnl"]  = round(pnl, 4)
                pos["tp1_exit_cost"] = round(exit_cost, 6)

        # V8.5 TP1 Progress Manager — TP1'e ilerlemeyen trade'de riski azalt
        if self.tp1_prog_enabled and self.partial_tp_en and not pos.get("tp1_done", False):
            tp1_target = max(pos_sl_pct * self.tp1_r_mult, 0.0001)
            progress_to_tp1 = change / tp1_target

            # TP1 yoksa trail'i daha erken sıkılaştır
            if bars_held >= self.tp1_prog_tighten_bars and self.use_trailing:
                original_trail = pos.get("original_trail_step", pos.get("trail_step", self.trail_step))
                pos["trail_step"] = max(0.0015, min(pos.get("trail_step", original_trail), original_trail * self.tp1_prog_tighten_mult))

            # Pozisyon TP1 yönünde ilerlemiyorsa tek seferlik risk azalt
            should_reduce = (
                bars_held >= self.tp1_prog_check_bars
                and not pos.get("tp1_progress_reduced", False)
                and progress_to_tp1 < self.tp1_prog_min_progress
                and (not self.tp1_prog_only_if_not_profitable or change <= 0)
            )
            if should_reduce and pos.get("qty", 0) > 0:
                reduce_qty = pos["qty"] * max(0.0, min(0.95, self.tp1_prog_reduce_pct))
                gross = self._gross_pnl(pos["side"], pos["entry"], price, reduce_qty)
                exit_cost = self._fee_cost(price, reduce_qty)
                pnl = gross - exit_cost
                self._pnl_running += pnl
                pos["qty"] -= reduce_qty
                pos["tp1_progress_reduced"] = True
                pos["tp1_progress_pnl"] = round(pos.get("tp1_progress_pnl", 0.0) + pnl, 4)
                pos["tp1_progress_exit_cost"] = round(pos.get("tp1_progress_exit_cost", 0.0) + exit_cost, 6)
                pos["tp1_progress_exit_price"] = round(price, 6)
                pos["tp1_progress_reduce_qty"] = round(reduce_qty, 8)

            # Hâlâ TP1 yok ve R bazında fazla geri gittiyse erken çıkış
            if bars_held >= self.tp1_prog_early_exit_bars and change <= pos_sl_pct * self.tp1_prog_early_exit_r:
                reason = "EarlyNoTP1"

        # Trail
        if self.use_trailing and change > 0:
            pos_trail = pos.get("trail_step", self.trail_step)
            locked    = pos.get("trail_locked", None)
            if locked is None or change > locked + pos_trail:
                pos["trail_locked"] = change

        # Exit kontrolu
        if reason is None and change <= -pos_sl_pct:
            reason = "SL"
        elif reason is None and bars_held >= int(pos.get("max_hold_bars_override") or self.max_hold_bars):
            reason = "MaxHold"
        elif reason is None and change >= self.tp_pct and change >= self.min_profit_cls:
            reason = "TP"
        elif reason is None and change >= self.min_profit_cls:
            score = result.get("final_score", 50.0)
            if pos["side"] == "LONG" and score < self.score_close:
                reason = "ScoreClose"
        elif reason is None and self.use_trailing:
            locked    = pos.get("trail_locked")
            pos_trail = pos.get("trail_step", self.trail_step)
            if locked is not None and change < locked - pos_trail and bars_held >= self.min_hold_bars:
                reason = "Trail"

        # Convex pyramid (sadece kazanan pozisyona ekle — side-aware)
        if self.runtime and not reason:
            add_mult = self.runtime.check_pyramid(sym, price)
            if add_mult:
                extra = pos["qty"] * add_mult
                pos["qty"] += extra
                pos["pyramid_adds"] = pos.get("pyramid_adds", 0) + 1

        if reason:
            self._close_position(sym, price, change, reason, ts_str, date_str, ts_ms)

    def _try_open(self, sym: str, score: float, result: dict,
                  prices: list, highs: list, lows: list, vols: list,
                  htf_p: list, regime: str, ts_str: str, date_str: str,
                  ts_ms: int = 0, btc_prices: list = None):

        price   = prices[-1] if prices else 0.0
        atr_pct = result.get("components", {}).get("atr_pct", 0.0)
        adx_val = result.get("components", {}).get("adx", 0.0)
        # Bu skor bir "aday sinyal" mi? (yalniz LONG esigine bakar — sert kapilar
        # President'tan ONCE calistigi icin henuz hangi taraf onerildigini bilmeyiz;
        # bu erken bloklar varsayilan olarak LONG kabul edilir — sistem LONG-agirlikli)
        is_candidate = score >= self.score_long_open

        # ── Sert kapı (portföy/limit) ───────────────────────────────────
        # NOT: _maybe_rotate_for_candidate() HER ZAMAN False döner (shadow-only
        # güvenlik tasarımı, V8.4.1) — yani bu çağrı hiçbir zaman fiziksel pozisyon
        # kapatmaz, sadece ROTATION_CANDIDATE_SHADOW logu üretir. Bu nedenle
        # sıralama (önce/sonra olması) şu an pratik bir fark yaratmıyor; yine de
        # ileride gerçek rotasyon eklenirse güvenli olsun diye fonksiyon
        # PORTFÖY DOLU kontrolünün içinde, en erken noktada çağrılır.
        portfolio_full = len(self.open_positions) >= self.max_open_pos
        if portfolio_full:
            self._maybe_rotate_for_candidate(sym, score, price, ts_str, date_str, ts_ms)
            # V8.5.7: kapasite doluluğu artık açıkça gerçek aktif pozisyon sayısıyla loglanır.
            # Aktif pozisyon < limit iken bu sebep yazılamaz; o durum rank/rejection mantığına bırakılır.
            self._log_filter("MAX_POSITIONS_ALREADY_FULL", sym, score, ts_str, extra={
                "active_positions": len(self.open_positions),
                "max_positions": self.max_open_pos,
            })
            if is_candidate:
                self._record_block(ts_ms, sym, "MAX_POSITIONS_ALREADY_FULL", price, regime, score)
            if self.ghost_en and score >= self.ghost_min_score:
                self._log_ghost(sym, score, result, prices, ts_str, "MAX_POSITIONS_ALREADY_FULL")
            return

        if sym != "BTCUSDT" and self.bl_enabled and sym in self.bl_symbols:
            self._log_filter("SYMBOL_BLACKLIST", sym, score, ts_str)
            if is_candidate:
                self._record_block(ts_ms, sym, "SYMBOL_BLACKLIST", price, regime, score)
            return

        daily_trades = self._daily_trade_count.get(date_str, 0)
        if daily_trades >= self.max_trades_day:
            return

        # ADX filtresi (BOA adayi)
        # require_adx_strict=False (varsayılan): ADX=0 (hesaplanamadı) ise bypass
        # require_adx_strict=True: ADX=0 da bloklanır (strict mod)
        adx_blocks = self.adx_filter_en and is_candidate and (
            (adx_val > 0 and adx_val < self.adx_thr) or
            (adx_val <= 0 and self.require_adx_strict)
        )
        if adx_blocks:
            self._log_filter("ADX_TOO_LOW", sym, score, ts_str, extra={"adx": round(adx_val, 1)})
            self._record_block(ts_ms, sym, "ADX_TOO_LOW", price, regime, score)
            return

        # BTC genel düşüş filtresi (varsayılan KAPALI) — açıksa ve BTC son N
        # mumda drop_pct'ten fazla düştüyse, TÜM LONG adaylarını blokla.
        if self.btc_filter_en and is_candidate and btc_prices and \
           len(btc_prices) >= self.btc_filter_candles + 1:
            _b_start = btc_prices[-(self.btc_filter_candles + 1)]
            _b_end   = btc_prices[-1]
            _b_drop  = (_b_end - _b_start) / _b_start * 100 if _b_start > 0 else 0.0
            if _b_drop <= -self.btc_filter_drop:
                self._log_filter("BTC_FILTER_DROP", sym, score, ts_str,
                                 extra={"btc_drop_pct": round(_b_drop, 2)})
                self._record_block(ts_ms, sym, "BTC_FILTER_DROP", price, regime, score)
                return

        # MTF (BOA adayi)
        # MTF kapalıysa CoreLong HTF gate'i bypass edebilmek için nötr-altı 50 değil, 100 kullanılır.
        htf_sc = 100.0 if not self.mtf_enabled else 50.0
        if self.mtf_enabled and len(htf_p) >= 50:
            try:
                htf_sc = score_symbol(htf_p)["final_score"]
            except Exception:
                htf_sc = 50.0
            if is_candidate and htf_sc < self.mtf_long_min:
                self._log_filter("MTF_NO_CONFIRM", sym, score, ts_str, extra={"htf": round(htf_sc, 1)})
                self._record_block(ts_ms, sym, "MTF_NO_CONFIRM", price, regime, score)
                return

        if price <= 0:
            return

        # ── KARAR: President (varsayilan) veya Legacy ──────────────────────
        if self.president_enabled and self.runtime:
            ts_sec = (ts_ms / 1000) if ts_ms else time.time()
            sentiment = "BEARISH" if regime == "BEARISH" else ("BULLISH" if regime in ("BULL", "TREND") else "NEUTRAL")
            packet = self.runtime.evaluate(
                sym, ts_sec, score, result, regime, htf_sc, sentiment,
                prices, highs, lows, vols, btc_prices,
            )
            if packet.action == Action.OPEN:
                branch_scores = {k: round(v.score,2) for k,v in packet.branch_votes.items()}
                self._open_from_decision(sym, packet.side.value, price, score, adx_val, atr_pct,
                                         regime, ts_str, date_str, packet.sl_pct, packet.size_mult,
                                         label=packet.label, decision_id=packet.decision_id,
                                         branch_scores=branch_scores, htf_score=htf_sc,
                                         prices=prices, highs=highs, lows=lows, vols=vols,
                                         packet_extra=getattr(packet, "extra", {}),
                                         score_components=(result.get("components", {}) or {}))
            else:
                # Acik oy vardiysa ama President acmadiysa -> BOA blok adayi
                open_votes_list = [v for v in packet.branch_votes.values() if v.action == Action.OPEN]
                had_open = len(open_votes_list) > 0
                if had_open or is_candidate:
                    cause = "PRESIDENT_" + (packet.reason.split()[0] if packet.reason else "BLOCK")
                    # Bloklanan sinyalin yönü: President'ın seçtiği taraf, yoksa ilk açık oy, yoksa LONG
                    intended_side = (packet.side.value if packet.side.value != "NONE"
                                     else (open_votes_list[0].side.value if open_votes_list else "LONG"))
                    self._record_block(ts_ms, sym, cause, price, regime, score, side=intended_side)
                if self.ghost_en and score >= self.ghost_min_score:
                    self._log_ghost(sym, score, result, prices, ts_str, "PRESIDENT_BLOCK")
            return

        # ── Legacy (president_enabled=False) — A/B karsilastirma icin ──────
        adsl = adaptive_sl_compute(regime=regime, atr_pct=atr_pct,
            base_score_threshold=self.score_long_open, base_atr_multiplier=self.atr_multiplier,
            base_trail_step=self.trail_step, cfg=self.cfg)
        eff_thr, sl_pct = adsl["score_threshold"], adsl["sl_pct"]
        side = "LONG" if score >= eff_thr else ("SHORT" if score <= self.score_short_open else None)
        if side is None:
            if self.ghost_en and score >= self.ghost_min_score:
                self._log_ghost(sym, score, result, prices, ts_str, "BELOW_THRESHOLD")
            return
        self._open_from_decision(sym, side, price, score, adx_val, atr_pct, regime,
                                 ts_str, date_str, sl_pct, 1.0, label="LEGACY", htf_score=htf_sc,
                                 prices=prices, highs=highs, lows=lows, vols=vols,
                                 score_components=(result.get("components", {}) or {}))

    def _open_from_decision(self, sym, side, price, score, adx_val, atr_pct, regime,
                            ts_str, date_str, sl_pct, size_mult, label="",
                            decision_id="", branch_scores=None, htf_score=50.0,
                            prices=None, highs=None, lows=None, vols=None, packet_extra=None,
                            score_components=None, rank_context=None):
        """Karar paketinden pozisyon ac (President veya Legacy ortak yolu)."""
        adsl = adaptive_sl_compute(regime=regime, atr_pct=atr_pct,
            base_score_threshold=self.score_long_open, base_atr_multiplier=self.atr_multiplier,
            base_trail_step=self.trail_step, cfg=self.cfg)
        trail = adsl["trail_step"]
        symbol_mult = self.sym_mgr.size_multiplier(sym) if hasattr(self, "sym_mgr") else 1.0
        # V8.5.2 Adaptive Exit: policy üretir; President kararını baypas etmez.
        symbol_stats = None
        try:
            symbol_stats = self.sym_mgr.get_all_stats().get(sym, {}) if hasattr(self, "sym_mgr") else None
        except Exception:
            symbol_stats = None
        ae = classify_trade(symbol=sym, side=side, score=score, htf_score=htf_score,
                            regime=regime, components={"adx": adx_val, "atr_pct": atr_pct},
                            cfg=self.cfg, prices=prices or [], highs=highs or [], lows=lows or [],
                            volumes=vols or [], symbol_stats=symbol_stats)
        if ae.enabled and not ae.shadow_mode:
            size_mult *= float(ae.policy.size_mult or 1.0)
            trail = max(0.001, float(ae.policy.trail_step_pct) / 100.0)
        mr = self.cfg.get("market_regime", {})
        regime_mult = 1.0
        if str(regime).upper() == "NEUTRAL":
            regime_mult = float(mr.get("neutral_size_mult", 1.0))
        elif str(regime).upper() == "KONSOL":
            regime_mult = float(mr.get("konsol_size_mult", 1.0))
        final_size_mult = max(0.05, size_mult * symbol_mult * regime_mult)
        current_equity = self.equity + self._pnl_running
        risk_usdt = current_equity * self.risk_per_trade * max(0.1, final_size_mult)
        qty       = max(0.0001, risk_usdt / (price * max(sl_pct, 0.001)))
        entry_cost = self._fee_cost(price, qty)
        self._pnl_running -= entry_cost

        self.open_positions[sym] = {
            "side": side, "entry": price, "qty": qty, "sl_pct": sl_pct,
            "trail_step": trail, "open_ts": ts_str, "open_date": date_str,
            "score": score, "adx": adx_val, "atr_pct": atr_pct, "regime": regime,
            "label": label, "bars_held": 0, "tp1_done": False, "tp1_pnl": 0.0,
            "tp1_progress_reduced": False, "tp1_progress_pnl": 0.0,
            "trail_locked": None, "pyramid_adds": 0, "original_trail_step": trail,
            "entry_cost": round(entry_cost, 6),
            "decision_id": decision_id,              # President kararı zinciri
            "branch_scores": branch_scores or {},    # Dal skorları
            "symbol_size_mult": round(symbol_mult, 4),
            "regime_size_mult": round(regime_mult, 4),
            "final_size_mult": round(final_size_mult, 4),
            "raw_score": (score_components or {}).get("raw_score", score),
            "normalized_score": (score_components or {}).get("normalized_score", score),
            "long_score": (score_components or {}).get("long_score", score),
            "short_feature_score": (score_components or {}).get("short_score", ""),
            "score_model": (score_components or {}).get("score_model", ""),
            "president_score": (packet_extra or {}).get("president_score", ""),
            "rank_score": (rank_context or {}).get("rank_score", ""),
            "rank_position": (rank_context or {}).get("rank_position", ""),
            "rank_candidate_count": (rank_context or {}).get("candidate_count", ""),
            "boa_feedback_adj": ((rank_context or {}).get("boa_feedback", {}) or {}).get("adjustment", ""),
            "ae_class": ae.trade_class,
            "ae_policy": ae.policy_name,
            "ae_continuation_score": ae.continuation_score,
            "ae_confidence": ae.confidence,
            "ae_reasons": ae.reasons[:240],
            "tp1_close_pct": float(ae.policy.tp1_close_pct),
            "max_hold_bars_override": (int(float(ae.policy.max_hold_hours)) if ae.policy.max_hold_hours else None),
            "quality_score_report": (packet_extra or {}).get("quality_score_report", {}),
            "adaptive_risk_report": (packet_extra or {}).get("adaptive_risk_report", {}),
        }
        if self.runtime:
            self.runtime.on_open(sym, side, price, sl_pct)
            self.runtime.confirm_open(side)  # filtreler zaten geçildi, risk sayacı artır
        self._daily_trade_count[date_str] = self._daily_trade_count.get(date_str, 0) + 1

    def _close_position(self, sym: str, price: float, change: float,
                        reason: str, ts_str: str, date_str: str, ts_ms: int = 0):
        pos   = self.open_positions.pop(sym, None)
        if not pos:
            return
        entry = pos["entry"]
        qty   = pos["qty"]
        pnl_raw = self._gross_pnl(pos["side"], entry, price, qty)
        exit_cost = round(self._fee_cost(price, qty), 6)
        pnl = pnl_raw - exit_cost
        self._pnl_running += pnl
        self._daily_pnl[date_str] = self._daily_pnl.get(date_str, 0.0) + pnl

        # Net_PnL = partial net + final net - entry cost
        tp1_pnl      = pos.get("tp1_pnl", 0.0)
        tp1_prog_pnl = pos.get("tp1_progress_pnl", 0.0)
        entry_cost   = pos.get("entry_cost", 0.0)
        total_net = round(pnl + tp1_pnl + tp1_prog_pnl - entry_cost, 4)

        trade = {
            "Sembol":        sym,
            "Yon":           pos["side"],
            "Giris":         pos.get("open_ts", ts_str),
            "Cikis":         ts_str,
            "GirisFiyati":   round(entry, 6),
            "CikisFiyati":   round(price, 6),
            "KarPct":        round(change * 100, 3),
            "Final_PnL":     round(pnl, 4),       # çıkış PnL (exit_cost dahil)
            "TP1_PnL":       round(tp1_pnl, 4),   # partial TP PnL
            "TP1_Progress_PnL": round(tp1_prog_pnl, 4),
            "Net_PnL":       total_net,            # GERÇEK TOPLAM — summary bunu kullanır
            "Giris_Komisyon": round(entry_cost, 4),
            "Cikis_Komisyon": round(exit_cost, 4),
            "Sebep":         reason,
            "Skor":          round(pos.get("score", 0), 2),
            "ADX":           round(pos.get("adx", 0), 2),
            "ATR_Pct":       round(pos.get("atr_pct", 0), 4),
            "Rejim":         pos.get("regime", ""),
            "Label":         pos.get("label", ""),
            "AE_Class":      pos.get("ae_class", ""),
            "AE_Policy":     pos.get("ae_policy", ""),
            "AE_ContinuationScore": pos.get("ae_continuation_score", ""),
            "AE_Confidence": pos.get("ae_confidence", ""),
            "AE_Reasons":    pos.get("ae_reasons", ""),
            "SymbolSizeMult": pos.get("symbol_size_mult", ""),
            "RegimeSizeMult": pos.get("regime_size_mult", ""),
            "FinalSizeMult": pos.get("final_size_mult", ""),
            "RawScore":      pos.get("raw_score", ""),
            "NormalizedScore": pos.get("normalized_score", ""),
            "LongScore":     pos.get("long_score", ""),
            "ShortFeatureScore": pos.get("short_feature_score", ""),
            "ScoreModel":    pos.get("score_model", ""),
            "EntryScore":    round(pos.get("score", 0), 2),
            "PresidentScore": pos.get("president_score", ""),
            "RankScore":     pos.get("rank_score", ""),
            "RankPosition":  pos.get("rank_position", ""),
            "RankCandidateCount": pos.get("rank_candidate_count", ""),
            "BOAFeedbackAdj": pos.get("boa_feedback_adj", ""),
            "BarsHeld":      pos.get("bars_held", 0),
            "TP1_Done":      int(pos.get("tp1_done", False)),
            "TP1_Progress_Reduced": int(pos.get("tp1_progress_reduced", False)),
            "TP1_Progress_ExitPrice": pos.get("tp1_progress_exit_price", ""),
            "TP1_Progress_ReduceQty": pos.get("tp1_progress_reduce_qty", ""),
            "PyramidAdds":   pos.get("pyramid_adds", 0),
            "DecisionID":    pos.get("decision_id", ""),
            "CoreScore":     pos.get("branch_scores", {}).get("core_long", ""),
            "ShortScore":    pos.get("branch_scores", {}).get("short_surgeon", ""),
            "CascadeScore":  pos.get("branch_scores", {}).get("cascade_hunter", ""),
            "QualityScore":  (pos.get("quality_score_report", {}) or {}).get("score", ""),
            "AdaptiveRiskMult": (pos.get("adaptive_risk_report", {}) or {}).get("risk_mult", ""),
        }
        self.trades.append(trade)

        # ── SL_DOGRU: pending kaydı — 4h SONRA verdict üretilir (look-ahead önleme) ──
        if self.runtime and reason == "SL" and pos["side"] == "LONG":
            ts_sec = (ts_ms / 1000) if ts_ms else time.time()
            # Gerçek hayatta 4h sonra bilinir; backtest'te de aynı şekilde geciktirilir
            if not hasattr(self, "_pending_sl_bt"):
                self._pending_sl_bt = {}
            self._pending_sl_bt[sym] = {"ts_ms": ts_ms, "ts_sec": ts_sec}

        # President risk governor'a GERÇEK toplam PnL gönder (TP1 + kapanış − giriş maliyeti)
        if self.runtime:
            self.runtime.on_close(sym, pos["side"], total_net, candle_ts=(ts_ms/1000 if ts_ms else 0.0))
            self.runtime.update_equity(self.equity + self._pnl_running)
        if hasattr(self, "sym_mgr"):
            self.sym_mgr.record_trade(sym, total_net)
            self.sym_mgr.update_equity(self.equity + self._pnl_running)

        # block_outcomes (exit reason ozeti — eski "BOA" artik exit ozeti olarak kalir)
        self.block_outcomes.append({
            "ts":     ts_str,
            "symbol": sym,
            "reason": reason,
            "pnl":    round(total_net, 4),
            "side":   pos["side"],
            "score":  round(pos.get("score", 0), 2),
        })

    def _force_close_all(self, last_candle_ts: int = 0):
        """Dönem sonu — her açık pozisyonu son bilinen fiyat VE gerçek mum zamanıyla kapat."""
        exit_ts_str = (time.strftime("%Y-%m-%d %H:%M", time.gmtime(last_candle_ts // 1000))
                      if last_candle_ts else "EOT")
        for sym in list(self.open_positions.keys()):
            pos     = self.open_positions.pop(sym)
            entry   = pos["entry"]
            # Son bilinen fiyat: equity_curve son noktası değil, sembolün son kapanışı
            last_p  = pos.get("last_price", entry)   # _manage_position her barı günceller
            mult    = 1 if pos["side"] == "LONG" else -1
            change  = (last_p - entry) / entry * mult
            pnl_raw = self._gross_pnl(pos["side"], entry, last_p, pos["qty"])
            tp1_pnl   = pos.get("tp1_pnl", 0.0)
            tp1_prog_pnl = pos.get("tp1_progress_pnl", 0.0)
            entry_cost_fc = pos.get("entry_cost", 0.0)
            exit_cost_fc  = round(self._fee_cost(last_p, pos["qty"]), 6)
            pnl     = pnl_raw - exit_cost_fc
            total   = round(pnl + tp1_pnl + tp1_prog_pnl - entry_cost_fc, 4)
            self._pnl_running += pnl
            self.trades.append({
                "Sembol": sym, "Yon": pos["side"],
                "Giris": pos.get("open_ts",""), "Cikis": exit_ts_str,
                "GirisFiyati": round(entry, 6), "CikisFiyati": round(last_p, 6),
                "KarPct": round(change * 100, 3),
                "Final_PnL": round(pnl, 4), "TP1_PnL": round(tp1_pnl, 4),
                "TP1_Progress_PnL": round(tp1_prog_pnl, 4),
                "Net_PnL": total,
                "Giris_Komisyon": round(entry_cost_fc, 4),
                "Cikis_Komisyon": round(exit_cost_fc, 4),
                "Sebep": "EndOfTest",
                "Skor": pos.get("score",0), "ADX": 0, "ATR_Pct": 0,
                "Rejim": pos.get("regime",""), "Label": pos.get("label",""),
                "SymbolSizeMult": pos.get("symbol_size_mult", ""),
                "RegimeSizeMult": pos.get("regime_size_mult", ""),
                "FinalSizeMult": pos.get("final_size_mult", ""),
                "RawScore": pos.get("raw_score", ""),
                "NormalizedScore": pos.get("normalized_score", ""),
                "LongScore": pos.get("long_score", ""),
                "ShortFeatureScore": pos.get("short_feature_score", ""),
                "ScoreModel": pos.get("score_model", ""),
                "EntryScore": round(pos.get("score", 0), 2),
                "PresidentScore": pos.get("president_score", ""),
                "RankScore": pos.get("rank_score", ""),
                "RankPosition": pos.get("rank_position", ""),
                "RankCandidateCount": pos.get("rank_candidate_count", ""),
                "BOAFeedbackAdj": pos.get("boa_feedback_adj", ""),
                "BarsHeld": pos.get("bars_held",0),
                "TP1_Done": int(pos.get("tp1_done",False)),
                "TP1_Progress_Reduced": int(pos.get("tp1_progress_reduced",False)),
                "TP1_Progress_ExitPrice": pos.get("tp1_progress_exit_price", ""),
                "TP1_Progress_ReduceQty": pos.get("tp1_progress_reduce_qty", ""),
                "PyramidAdds": pos.get("pyramid_adds",0),
                "DecisionID": pos.get("decision_id",""),
                "CoreScore": pos.get("branch_scores",{}).get("core_long",""),
                "ShortScore": pos.get("branch_scores",{}).get("short_surgeon",""),
                "CascadeScore": pos.get("branch_scores",{}).get("cascade_hunter",""),
            })
            if self.runtime:
                self.runtime.on_close(sym, pos["side"], total, candle_ts=(last_candle_ts/1000 if last_candle_ts else 0.0))
                self.runtime.update_equity(self.equity + self._pnl_running)
            if hasattr(self, "sym_mgr"):
                self.sym_mgr.record_trade(sym, total)
                self.sym_mgr.update_equity(self.equity + self._pnl_running)

    # ── SL_DOGRU pending çözücü (look-ahead önleme) ────────────────────
    def _resolve_pending_sl_bt(self, sym: str, ts_ms: int):
        """
        SL_DOGRU: SL anında geleceği görmek yerine 4h bekler.
        Her barda çağrılır; 4h geçtiyse o anki fiyatla verdict üretir.
        """
        if not self.runtime: return
        pend = getattr(self, "_pending_sl_bt", {})
        rec  = pend.get(sym)
        if not rec: return
        if ts_ms - rec["ts_ms"] < 4 * 3600 * 1000:
            return  # Henüz 4h geçmedi
        chg_4h = self._future_change(sym, rec["ts_ms"], 4)
        if chg_4h is None:
            verdict, chg_4h = "BELIRSIZ", 0.0
        elif chg_4h <= -0.01:
            verdict = "SL_DOGRU"
        elif chg_4h >= 0.01:
            verdict = "ERKEN_SL"
        else:
            verdict = "BELIRSIZ"
        self.runtime.on_sl(sym, verdict, rec["ts_sec"], chg_4h)
        del pend[sym]

    # ── GERCEK BOA: blok kaydi + ileriye donuk sonuc ──────────────────
    def _record_block(self, ts_ms, sym, cause, price, regime, score, side="LONG"):
        """Bloklanan bir aday sinyali kaydeder (BOA post-analizi icin)."""
        self.block_events.append({
            "ts_ms": ts_ms, "symbol": sym, "cause": cause,
            "price": price, "regime": regime, "score": round(score, 2),
            "side": side,
        })

    def _future_change(self, sym, ts_ms, hours) -> Optional[float]:
        """ts_ms anindan 'hours' saat sonraki fiyat degisimi (ondalik)."""
        if not ts_ms:
            return None
        candles = self._cbs.get(sym, [])
        if not candles:
            return None
        target = ts_ms + int(hours * 3600 * 1000)
        base = None
        fut  = None
        for c in candles:
            if c["open_time"] >= ts_ms and base is None:
                base = float(c["close"])
            if c["open_time"] >= target:
                fut = float(c["close"]); break
        if base is None or base <= 0:
            return None
        if fut is None:  # yeterli ileri veri yok
            return None
        return (fut - base) / base

    def _future_outcome(self, sym, ts_ms, entry, hours, side="LONG") -> Tuple[str, float]:
        """
        Side-aware BOA: LONG için high>=TP, LOW için low<=SL (kazanan).
        SHORT için ters: low<=TP, high>=SL.
        """
        candles = self._cbs.get(sym, [])
        if not candles or entry <= 0:
            return ("BELIRSIZ", 0.0)
        end_ms = ts_ms + int(hours * 3600 * 1000)
        if side == "SHORT":
            tp = entry * (1 - self.tp_pct)   # short TP: fiyat düşer
            sl = entry * (1 + self.sl_pct)   # short SL: fiyat artar
        else:
            tp = entry * (1 + self.tp_pct)
            sl = entry * (1 - self.sl_pct)
        last_close = entry
        for c in candles:
            if c["open_time"] < ts_ms: continue
            if c["open_time"] > end_ms: break
            last_close = float(c["close"])
            if side == "SHORT":
                if float(c["low"])  <= tp: return ("GEREKSIZ_ENGEL",  self.tp_pct * 100)
                if float(c["high"]) >= sl: return ("DOGRU_ENGEL",    -self.sl_pct * 100)
            else:
                if float(c["high"]) >= tp: return ("GEREKSIZ_ENGEL",  self.tp_pct * 100)
                if float(c["low"])  <= sl: return ("DOGRU_ENGEL",    -self.sl_pct * 100)
        chg = (last_close - entry) / entry * (1 if side == "LONG" else -1) * 100
        return ("BELIRSIZ", round(chg, 3))

    def _post_boa_analysis(self):
        """Her bloklanan sinyal icin 4h/12h/24h hipotetik sonuc + ozetler."""
        self.boa_4h:  List[dict] = []
        self.boa_12h: List[dict] = []
        self.boa_24h: List[dict] = []
        for ev in self.block_events:
            for hours, bucket in ((4, self.boa_4h), (12, self.boa_12h), (24, self.boa_24h)):
                verdict, chg = self._future_outcome(ev["symbol"], ev["ts_ms"], ev["price"], hours, ev.get("side","LONG"))
                bucket.append({
                    "ts_ms": ev["ts_ms"], "symbol": ev["symbol"], "side": ev.get("side","LONG"),
                    "cause": ev["cause"], "regime": ev["regime"], "score": ev["score"],
                    "verdict": verdict, "hyp_pnl_pct": chg,
                })

    # ── Filter / Ghost Loglama ────────────────────────────────────────
    def _log_filter(self, cause: str, sym: str, score: float,
                    ts: str, extra: dict = None):
        ev = {
            "ts": ts, "symbol": sym, "cause": cause, "score": round(score, 2),
            "active_positions": len(self.open_positions),
            "max_positions": self.max_open_pos,
        }
        if extra:
            ev.update(extra)
        self.filter_events.append(ev)

    def _log_ghost(self, sym: str, score: float, result: dict,
                   prices: list, ts: str, cause: str):
        self.ghost_signals.append({
            "ts":    ts,
            "symbol": sym,
            "score": round(score, 2),
            "cause": cause,
            "atr":   round(result.get("components", {}).get("atr_pct", 0), 4),
        })

    # ── Rapor Olustur ─────────────────────────────────────────────────
    def _max_drawdown(self) -> float:
        """Equity egrisinden maksimum dususu (%) hesaplar."""
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0][1]
        max_dd = 0.0
        for _, eq in self.equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100 if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
        return round(max_dd, 2)

    def _sharpe(self) -> float:
        """Equity getirilerinden basit Sharpe orani."""
        import math
        if len(self.equity_curve) < 2:
            return 0.0
        vals = [eq for _, eq in self.equity_curve]
        rets = [(vals[i] - vals[i-1]) / vals[i-1]
                for i in range(1, len(vals)) if vals[i-1] > 0]
        if not rets:
            return 0.0
        mean = sum(rets) / len(rets)
        var  = sum((r - mean) ** 2 for r in rets) / len(rets)
        std  = math.sqrt(var)
        return round(mean / std * math.sqrt(len(rets)), 3) if std > 0 else 0.0

    def _generate_report(self) -> dict:
        trades = self.trades
        n       = len(trades)
        wins    = [t for t in trades if t.get("Net_PnL", t.get("KarUSD", 0)) > 0]
        losses  = [t for t in trades if t.get("Net_PnL", t.get("KarUSD", 0)) <= 0]
        net_pnl = sum(t.get("Net_PnL", t.get("KarUSD", 0)) for t in trades)
        win_rate= len(wins) / n * 100 if n > 0 else 0.0
        avg_win = sum(t.get("Net_PnL", t.get("KarUSD", 0)) for t in wins) / len(wins) if wins else 0.0
        avg_loss= sum(t.get("Net_PnL", t.get("KarUSD", 0)) for t in losses) / len(losses) if losses else 0.0

        # Exit sebep ozeti
        by_reason = defaultdict(lambda: {"count": 0, "pnl": 0.0})
        for t in trades:
            r = t["Sebep"]
            by_reason[r]["count"] += 1
            by_reason[r]["pnl"]   += t.get("Net_PnL", t.get("KarUSD", 0))

        max_dd  = self._max_drawdown()
        sharpe  = self._sharpe()

        summary = {
            "Toplam_Islem":   n,
            "Kazanma_Orani":  f"{win_rate:.2f}%",
            "Net_PnL_USD":    f"{net_pnl:.4f}",
            "Max_DD_Pct":     f"{max_dd:.2f}%",
            "Sharpe":         f"{sharpe:.2f}",
            "Ort_Kazanc_USD": f"{avg_win:.4f}",
            "Ort_Kayip_USD":  f"{avg_loss:.4f}",
            "Baslangic_Equity": f"{self.equity:.2f}",
            "Bitis_Equity":   f"{self.equity + self._pnl_running:.4f}",
            "Getiri_Pct":     f"{net_pnl / self.equity * 100:.3f}%",
        }
        for reas, data in by_reason.items():
            summary[f"Exit_{reas}_Sayi"]= data["count"]
            summary[f"Exit_{reas}_PnL"] = f"{data['pnl']:.4f}"

        # President karar sayaclari
        summary["President_Enabled"] = int(self.president_enabled)
        summary["Bloklanan_Sinyal"]  = len(self.block_events)
        if self.runtime:
            try:
                st = self.runtime.get_state()
                summary["President_Gunluk_PnL"] = f"{st.get('daily_pnl', 0):.2f}"
            except Exception:
                pass

        # Ranking / universe denetim metrikleri
        rank_causes = [r.get("cause", "") for r in getattr(self, "ranking_events", [])]
        summary["Ranking_Event_Count"] = len(rank_causes)
        summary["Rank_Selected_Count"] = sum(1 for c in rank_causes if c == "RANK_SELECTED")
        summary["Rank_Rejected_Count"] = sum(1 for c in rank_causes if str(c).startswith("RANK_REJECTED"))
        summary["MaxPositionsAlreadyFull_Count"] = sum(1 for e in self.filter_events if e.get("cause") == "MAX_POSITIONS_ALREADY_FULL")
        summary["TradedSymbolCount"] = len(set(t.get("Sembol") for t in trades)) if trades else 0
        summary["ActiveUniverseSize"] = len(getattr(self, "_run_symbols", []) or [])

        # CSV dosyalari yaz
        self._write_trades_csv()
        self._write_summary_csv(summary)
        self._write_equity_csv()
        self._write_filter_csv()
        self._write_ghost_csv()
        self._write_boa_csv()
        self._write_real_boa_csv()       # GERCEK BOA (4h/12h/24h + by_reason/symbol/regime)
        if not (self.out_dir / "boa_feedback_memory.json").exists():
            (self.out_dir / "boa_feedback_memory.json").write_text(json.dumps({}, ensure_ascii=False, indent=2), encoding="utf-8")
        self._write_universe_audit_files()
        self._write_config_snapshot()

        return {
            "summary":      summary,
            "trades":       trades,
            "equity_curve": self.equity_curve,
        }

    # ── CSV Yazma ─────────────────────────────────────────────────────
    def _write_trades_csv(self):
        path = self.out_dir / "backtest_trades.csv"
        if not self.trades:
            return
        keys = list(self.trades[0].keys())
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=keys, delimiter=";")
            w.writeheader()
            w.writerows(self.trades)

    def _write_summary_csv(self, summary: dict):
        path = self.out_dir / "backtest_summary.csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            for k, v in summary.items():
                w.writerow([k, v])

    def _write_equity_csv(self):
        path = self.out_dir / "equity_curve.csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Timestamp", "Equity"])
            for ts, eq in self.equity_curve:
                w.writerow([ts, round(eq, 4)])

    def _write_filter_csv(self):
        path = self.out_dir / "filter_events.csv"
        if self.filter_events:
            keys = []
            for row in self.filter_events:
                for k in row.keys():
                    if k not in keys:
                        keys.append(k)
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=keys, delimiter=";", extrasaction="ignore")
                w.writeheader(); w.writerows(self.filter_events)

        # V8.5.7: ranking olayları ayrı ve denetlenebilir dosyaya da yazılır.
        if self.ranking_events:
            rpath = self.out_dir / "candidate_ranking_events.csv"
            keys = []
            for row in self.ranking_events:
                for k in row.keys():
                    if k not in keys:
                        keys.append(k)
            with open(rpath, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=keys, delimiter=";", extrasaction="ignore")
                w.writeheader(); w.writerows(self.ranking_events)

    def _write_ghost_csv(self):
        path = self.out_dir / "ghost_signal_analysis.csv"
        if not self.ghost_signals:
            return
        keys = list(self.ghost_signals[0].keys())
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=keys, delimiter=";")
            w.writeheader()
            w.writerows(self.ghost_signals)

    def _write_boa_csv(self):
        """Exit sebep özeti — Net_PnL kolonunu kullanır."""
        path = self.out_dir / "block_outcome_summary_by_reason.csv"
        by_reason = defaultdict(lambda: {"count": 0, "pnl": 0.0, "wins": 0})
        for t in self.trades:
            r   = t.get("Sebep", "Unknown")
            pnl = t.get("Net_PnL", t.get("KarUSD", 0.0))
            by_reason[r]["count"] += 1
            by_reason[r]["pnl"]   += pnl
            if pnl > 0:
                by_reason[r]["wins"] += 1
        rows = []
        for r, d in sorted(by_reason.items(), key=lambda x: -x[1]["count"]):
            wr = d["wins"] / d["count"] * 100 if d["count"] else 0
            rows.append({"Sebep": r, "Sayi": d["count"],
                         "WinRate": f"{wr:.1f}%", "ToplamPnL": f"{d['pnl']:.4f}",
                         "OrtPnL": f"{d['pnl']/d['count']:.4f}" if d["count"] else "0"})
        if not rows:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["Sebep","Sayi","WinRate","ToplamPnL","OrtPnL"],
                               delimiter=";")
            w.writeheader(); w.writerows(rows)

    def _write_boa_feedback_memory(self, rows_v2: list):
        """BOA sonuçlarından sonraki koşularda kullanılabilecek hafıza üretir.
        Bu dosya mevcut backtest içinde geçmişe uygulanmaz; lookahead yapmamak için sonraki koşularda okunur.
        """
        if not self.boa_feedback_enabled or not rows_v2:
            return
        agg = defaultdict(lambda: {"count": 0, "tp": 0, "sl": 0, "close_sum": 0.0})
        def add(key, r):
            d = agg[key]; d["count"] += 1
            fh = r.get("h24_first_hit", "")
            if fh == "TP_FIRST": d["tp"] += 1
            elif fh == "SL_FIRST": d["sl"] += 1
            try: d["close_sum"] += float(r.get("h24_close_return_pct", 0.0) or 0.0)
            except Exception: pass
        for r in rows_v2:
            sym = r.get("symbol", "")
            side = str(r.get("side", "LONG") or "LONG").upper()
            regime = r.get("regime", "")
            reason = r.get("reason", "")
            if sym: add(f"symbol:{sym}:{side}", r)
            if regime: add(f"regime:{regime}:{side}", r)
            if reason: add(f"reason:{reason}:{side}", r)
            add(f"side:{side}", r)
        mem = {}
        for k, d in agg.items():
            n = d["count"] or 1
            tp_rate = d["tp"] / n
            sl_rate = d["sl"] / n
            avg_close = d["close_sum"] / n
            # Edge puanı: TP-first pozitif, SL-first negatif, 24h kapanış yönü küçük ek etki.
            edge = (tp_rate - sl_rate) * self.boa_feedback_max_adj + max(-1.5, min(1.5, avg_close * 0.30))
            edge = max(-self.boa_feedback_max_adj, min(self.boa_feedback_max_adj, edge))
            mem[k] = {"count": d["count"], "tp_first": d["tp"], "sl_first": d["sl"],
                      "avg_close_return_pct": round(avg_close, 4), "edge": round(edge, 4)}
        try:
            path = self.boa_feedback_file
            if not path.is_absolute(): path = Path.cwd() / path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(mem, ensure_ascii=False, indent=2), encoding="utf-8")
            (self.out_dir / "boa_feedback_memory.json").write_text(json.dumps(mem, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            try: (self.out_dir / "boa_feedback_error.txt").write_text(str(e), encoding="utf-8")
            except Exception: pass


    def _write_universe_audit_files(self):
        """Backtest output klasöründe aktif evren ve varsa meta/history dosyalarını zorunlu görünür yapar.

        Haftalık universe canlı/WF tarafında dinamik değişse bile tek backtest sonucu incelenirken
        en azından hangi aktif sembol listesiyle koşulduğu output içinde kalmalıdır.
        """
        try:
            symbols = list(getattr(self, "_run_symbols", []) or [])
            (self.out_dir / "active_universe_symbols.json").write_text(json.dumps(symbols, ensure_ascii=False, indent=2), encoding="utf-8")
            hist_rows = [{"refresh_index": 0, "mode": "static_backtest_universe", "symbols": ",".join(symbols), "count": len(symbols)}]
            with open(self.out_dir / "symbol_universe_history.csv", "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=["refresh_index", "mode", "symbols", "count"], delimiter=";")
                w.writeheader(); w.writerows(hist_rows)
            # Mevcut meta varsa output'a kopyala; yoksa placeholder üret.
            meta_src = Path("symbols_top70_meta.json")
            if meta_src.exists():
                try:
                    (self.out_dir / "symbols_top70_meta.json").write_text(meta_src.read_text(encoding="utf-8"), encoding="utf-8")
                except Exception:
                    pass
            if not (self.out_dir / "symbols_top70_meta.json").exists():
                (self.out_dir / "symbols_top70_meta.json").write_text(json.dumps({
                    "note": "symbols_top70_meta.json not found; active_universe_symbols.json records actual symbols used.",
                    "active_universe_size": len(symbols),
                    "symbols": symbols,
                }, ensure_ascii=False, indent=2), encoding="utf-8")
            (self.out_dir / "weekly_universe_log.csv").write_text("ts;event;detail\n;STATIC_BACKTEST_UNIVERSE;active_universe_symbols.json written\n", encoding="utf-8-sig")
        except Exception as e:
            try: (self.out_dir / "universe_audit_error.txt").write_text(str(e), encoding="utf-8")
            except Exception: pass

    def _write_config_snapshot(self):
        path = self.out_dir / "config_snapshot.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, ensure_ascii=False, indent=2, default=str)

    def _write_real_boa_csv(self):
        # V8.5.2: V7 Block Outcome Analyzer v2 — same-candle lookahead önlemeli, first_hit ve 4/8/12/24h raporları.
        try:
            boa_cfg = self.cfg.get("block_outcome_analysis", {}) or {}
            if boa_cfg.get("enabled", True) and getattr(self, "block_events", None):
                rows_v2 = build_block_outcome(
                    self.block_events, self._cbs,
                    tp_pct=float(boa_cfg.get("tp_pct", self.tp_pct)),
                    sl_pct=float(boa_cfg.get("sl_pct", self.sl_pct)),
                    horizons_hours=list(boa_cfg.get("horizons_hours", [4,8,12,24])),
                    cooldown_bars=int(boa_cfg.get("cooldown_bars", 12)),
                    bar_seconds=int(boa_cfg.get("bar_seconds", 3600)),
                )
                write_block_outcome_reports(self.out_dir, rows_v2, list(boa_cfg.get("horizons_hours", [4,8,12,24])))
                self._write_boa_feedback_memory(rows_v2)
        except Exception as e:
            try:
                (self.out_dir / "boa_v2_error.txt").write_text(str(e), encoding="utf-8")
            except Exception:
                pass
        buckets = {
            "block_outcome_4h.csv":  getattr(self, "boa_4h",  []),
            "block_outcome_12h.csv": getattr(self, "boa_12h", []),
            "block_outcome_24h.csv": getattr(self, "boa_24h", []),
        }
        keys = ["ts_ms", "symbol", "side", "cause", "regime", "score", "verdict", "hyp_pnl_pct"]
        for fname, rows in buckets.items():
            if not rows: continue
            with open(self.out_dir / fname, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=keys, delimiter=";", extrasaction="ignore")
                w.writeheader(); w.writerows(rows)

        rows24 = getattr(self, "boa_24h", [])
        if not rows24:
            return

        def _summarize(key):
            agg = defaultdict(lambda: {"n": 0, "gereksiz": 0, "dogru": 0, "pnl": 0.0})
            for r in rows24:
                k = r.get(key, "?")
                agg[k]["n"]   += 1
                agg[k]["pnl"] += r.get("hyp_pnl_pct", 0)
                if r.get("verdict") == "GEREKSIZ_ENGEL": agg[k]["gereksiz"] += 1
                elif r.get("verdict") == "DOGRU_ENGEL":  agg[k]["dogru"]    += 1
            out = []
            for k, d in sorted(agg.items(), key=lambda x: -x[1]["n"]):
                n = d["n"] or 1
                out.append({
                    key: k, "Blok_Sayisi": d["n"],
                    "Gereksiz_Engel": d["gereksiz"],
                    "Dogru_Engel":    d["dogru"],
                    "Gereksiz_Pct":   f"{d['gereksiz']/n*100:.1f}%",
                    "Ort_Hipotetik_PnL": f"{d['pnl']/n:.3f}%",
                })
            return out

        for key, fname, col in [
            ("cause",  "boa_summary_by_reason.csv", "Sebep"),
            ("symbol", "boa_summary_by_symbol.csv", "Sembol"),
            ("regime", "boa_summary_by_regime.csv", "Rejim"),
        ]:
            rows = _summarize(key)
            if not rows: continue
            for r in rows:
                r[col] = r.pop(key)
            fnames = [col, "Blok_Sayisi", "Gereksiz_Engel", "Dogru_Engel", "Gereksiz_Pct", "Ort_Hipotetik_PnL"]
            with open(self.out_dir / fname, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=fnames, delimiter=";", extrasaction="ignore")
                w.writeheader(); w.writerows(rows)


# ─── CLI Giris Noktasi ────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="TRBOT V8 Backtest")
    parser.add_argument("--days",     type=int, default=30)
    parser.add_argument("--interval", type=str, default="1h")
    parser.add_argument("--top",      type=int, default=20)
    parser.add_argument("--out",      type=str, default="backtest_results/latest")
    parser.add_argument("--start",    type=str, default="")
    parser.add_argument("--end",      type=str, default="")
    parser.add_argument("--config",   type=str, default="config_online.yaml")
    parser.add_argument("--president-mode", type=str, default="",
                        choices=["", "shadow", "live", "legacy"],
                        help="shadow=emir yok, live=gercek acilis, legacy=President bypass")
    args = parser.parse_args()

    cfg = _load_cfg(args.config)
    symbols = _load_symbols(args.top)

    # President modu override'i — CLI argümanı varsa o öncelikli (geriye uyumlu).
    # CLI argümanı VERİLMEDİYSE (--president-mode boş), config'teki
    # backtest.president_execution_mode okunur ve varsayılan olarak uygulanır.
    # Bu, dokümantasyonda (HYBRID_CONFIG_NOTES.md) bahsedilen ama önceden hiç
    # okunmayan alanı gerçek bir etkiye kavuşturur — CLI hâlâ her zaman üstün.
    president_enabled = True
    pmode = args.president_mode
    if not pmode:
        _default_mode = str(cfg.get("backtest", {}).get("president_execution_mode", ""))
        if _default_mode == "shadow":
            pmode = "shadow"
        elif _default_mode in ("simulated_active", "live", "active"):
            pmode = "live"
        elif _default_mode == "legacy":
            pmode = "legacy"

    if pmode == "shadow":
        cfg.setdefault("president", {})["shadow_mode"] = True
    elif pmode == "live":
        cfg.setdefault("president", {})["shadow_mode"] = False
    elif pmode == "legacy":
        president_enabled = False

    # Tarih hesapla
    if args.start and args.end:
        import datetime
        def _to_ms(s):
            return int(datetime.datetime.strptime(s, "%Y-%m-%d").timestamp() * 1000)
        start_ms = _to_ms(args.start)
        end_ms   = _to_ms(args.end)
    else:
        end_ms   = int(time.time() * 1000)
        start_ms = end_ms - args.days * 24 * 3600 * 1000

    print(f"[Backtest] {len(symbols)} sembol | {args.interval} | {args.days} gun")
    print(f"[Backtest] Cikti klasoru: {args.out}")

    # Veri cek
    candles_by_sym = {}
    htf_candles    = {}
    for sym in symbols:
        print(f"  Veri: {sym}", end=" ", flush=True)
        candles_by_sym[sym] = _fetch_candles(sym, args.interval, start_ms, end_ms)
        htf_candles[sym]    = _fetch_candles(sym, "1h", start_ms, end_ms)
        print(f"({len(candles_by_sym[sym])} mum)")

    bt = Backtester(cfg, args.out, president_enabled=president_enabled, interval=args.interval)
    bt._write_config_snapshot()
    result = bt.run(symbols, candles_by_sym, htf_candles)

    summary = result["summary"]
    print("\n── Backtest Sonucu ──────────────────────────────────")
    for k, v in summary.items():
        if not k.startswith("Exit_"):
            print(f"  {k}: {v}")
    print(f"\nSonuclar: {args.out}")


if __name__ == "__main__":
    main()
