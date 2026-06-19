# engine.py — TRBOT President System V8 — Islem Motoru
# V7 ile tam geri uyumlu + President dal entegrasyonu (PresidentRuntime ortak pipeline)
import time
import csv
import threading
from collections import deque
from pathlib import Path

import adaptive_sl
from strategy_core import score_symbol
from market_regime import MarketRegimeDetector
from symbol_manager import SymbolManager
from adaptive_exit import classify_trade
from pump_filter import compute_pump_risk
from logger import log_info, log_error, log_event

# President sistemi — TEK ortak pipeline (backtest ile parité)
from president_runtime import PresidentRuntime
from modules.decision_packet import Action, Side


class TradeEngine:
    """
    TRBOT V8 canli islem motoru.
    - V7 mantigi korunmakta (tum filtreler, adaptive SL, vb.)
    - President sistemi oy toplayarak nihai karari verir
    - Short Surgeon ve Cascade Hunter shadow modda izler
    """

    def __init__(self, symbols: list, cfg: dict = None, data_dir: str = "data"):
        self.cfg      = cfg or {}
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        risk  = self.cfg.get("risk", {})
        lim   = self.cfg.get("limits", {})
        thr   = self.cfg.get("thresholds", {})
        misc  = self.cfg.get("misc", {})
        account = self.cfg.get("account", {})
        mtf   = self.cfg.get("mtf", {})

        # Risk parametreleri
        self.commission        = float(misc.get("commission_pct", 0.04)) / 100
        self.slippage          = float(misc.get("slippage_pct", 0.03)) / 100
        self.equity           = float(account.get("starting_equity_usdt", misc.get("starting_equity_usdt", risk.get("starting_equity_usdt", 1000.0))))
        self.tp_pct           = float(risk.get("take_profit_min_pct", 3.0)) / 100
        self.sl_pct           = float(risk.get("hard_stop_pct", 1.5)) / 100
        self.use_atr_stop     = bool(risk.get("use_atr_stop", True))
        self.atr_multiplier   = float(risk.get("atr_multiplier", 2.0))
        self.max_stop_pct     = float(risk.get("max_stop_pct", 4.5)) / 100
        self.trail            = bool(risk.get("use_trailing", True))
        self.trail_step       = float(risk.get("trailing_step_pct", 0.7)) / 100
        self.min_hold         = int(risk.get("min_hold_minutes", 60)) * 60
        self.risk_per_trade   = float(risk.get("risk_per_trade_pct", 1.0)) / 100
        self.min_profit_close = float(risk.get("min_profit_close_pct", 3.0)) / 100

        # Dynamic trail
        dt = self.cfg.get("dynamic_trail", {})
        self.dynamic_trail_enabled = bool(dt.get("enabled", True))

        # Limitler
        self.max_trades_day = int(lim.get("max_trades_per_day", 8))
        self.max_open_pos   = int(lim.get("max_open_positions", 3))
        self.daily_target   = float(lim.get("daily_target_pct", 10.0)) / 100
        self.max_hold_sec   = int(lim.get("max_hold_hours", 48)) * 3600
        self.daily_loss_lim = float(lim.get("daily_loss_limit_pct", 3.0)) / 100
        rot = self.cfg.get("position_rotation", {})
        self.rotation_enabled = bool(rot.get("enabled", False))
        self.rotation_min_score = float(rot.get("min_candidate_score", 90.0))
        self.rotation_min_delta = float(rot.get("min_score_delta", 12.0))
        self.rotation_shadow = bool(rot.get("shadow_mode", True))
        self.rotation_allow_close_profitable = bool(rot.get("allow_close_profitable", False))

        # Esikler
        self.score_long_open  = float(thr.get("score_long_open", 97.0))
        self.score_short_open = float(thr.get("score_short_open", 5.0))
        self.score_close      = float(thr.get("score_close", 50.0))

        # MTF
        self.mtf_enabled   = bool(mtf.get("enabled", True))
        self.mtf_long_min  = float(mtf.get("htf_long_min", 55.0))
        self.mtf_short_max = float(mtf.get("htf_short_max", 45.0))

        # Filtreler
        adx_f = self.cfg.get("adx_filter", {})
        self.adx_filter_enabled   = bool(adx_f.get("enabled", True))
        self.adx_filter_threshold = float(adx_f.get("threshold", 29.0))

        # require_adx_when_filter_enabled: backtest.py ile parite (bkz. orada
        # eklenen yorum). Varsayılan False = mevcut davranış (ADX=0 → bypass).
        ie = self.cfg.get("indicator_engine", {})
        self.require_adx_strict = bool(ie.get("require_adx_when_filter_enabled", False))

        # BTC genel düşüş filtresi — varsayılan KAPALI, backtest.py ile parite.
        btc_f = self.cfg.get("btc_filter", {})
        self.btc_filter_en      = bool(btc_f.get("enabled", False))
        self.btc_filter_candles = int(btc_f.get("lookback_candles", 4))
        self.btc_filter_drop    = float(btc_f.get("drop_pct", 1.5))

        rsi_f = self.cfg.get("rsi_filter", {})
        self.rsi_filter_enabled = bool(rsi_f.get("enabled", False))
        self.rsi_max_long       = float(rsi_f.get("max_long", 73.0))
        self.rsi_min_short      = float(rsi_f.get("min_short", 30.0))

        atr_f = self.cfg.get("atr_filter", {})
        self.atr_filter_enabled = bool(atr_f.get("enabled", False))
        self.atr_filter_min     = float(atr_f.get("min_atr_pct", 0.8))

        misc_cfg = self.cfg.get("misc", {})
        self.vol_mult    = float(misc_cfg.get("volume_burst_multiplier", 2.0))
        self.min_notional= float(misc_cfg.get("min_notional_usdt", 30000.0))

        # Kara liste
        self.blacklist: dict = {}
        bl = self.cfg.get("symbol_blacklist", {})
        self.blacklist_enabled = bool(bl.get("enabled", False))
        self.blacklist_symbols = set(s.upper() for s in (bl.get("symbols") or []))

        # Market regime + Sembol yoneticisi
        self.regime  = MarketRegimeDetector(cfg)
        self.sym_mgr = SymbolManager(cfg, starting_equity=self.equity)

        # Dinamik esik
        dt2 = self.cfg.get("dynamic_threshold", {})
        self.dynamic_threshold_enabled = bool(dt2.get("enabled", False))
        self.dt_trend_score    = float(dt2.get("trend_score",   92.0))
        self.dt_konsol_score   = float(dt2.get("konsol_score",  99.0))
        self.dt_bearish_score  = float(dt2.get("bearish_score", 999.0))
        self.dt_neutral_score  = float(dt2.get("neutral_score", self.score_long_open))
        self.dt_min_score      = float(dt2.get("min_score",     85.0))
        self.dt_max_score      = float(dt2.get("max_score",     999.0))
        self.dt_strong_discount= float(dt2.get("strong_setup_discount", 2.0))
        self.dt_strong_htf     = float(dt2.get("strong_htf",   75.0))
        self.dt_strong_adx     = float(dt2.get("strong_adx",   35.0))
        self.dt_strong_rsi_max = float(dt2.get("strong_rsi_max",68.0))

        # KONSOL filtresi
        mr = self.cfg.get("market_regime", {})
        self.konsol_breakout_only   = bool(mr.get("konsol_breakout_only",   False))
        self.konsol_min_score       = float(mr.get("konsol_min_score",      98.0))
        self.konsol_min_adx         = float(mr.get("konsol_min_adx",        30.0))
        self.konsol_min_atr_pct     = float(mr.get("konsol_min_atr_pct",    1.2))
        self.konsol_min_vol_ratio   = float(mr.get("konsol_min_vol_ratio",  1.5))
        self.konsol_min_htf         = float(mr.get("konsol_min_htf",        70.0))
        self.konsol_rsi_max_long    = float(mr.get("konsol_rsi_max_long",   68.0))
        self.konsol_size_mult       = float(mr.get("konsol_size_mult",      0.5))

        # Partial TP
        ptp = self.cfg.get("partial_tp", {})
        self.partial_tp_enabled = bool(ptp.get("enabled", True))
        self.tp1_r_mult         = float(ptp.get("tp1_r_mult", 0.75))
        self.tp1_close_pct      = float(ptp.get("close_pct",  0.40))

        # V8.5 TP1 Progress Manager
        tpm = self.cfg.get("tp1_progress_manager", {})
        self.tp1_prog_enabled = bool(tpm.get("enabled", True))
        self.tp1_prog_check_bars = int(tpm.get("check_after_bars", 5))
        self.tp1_prog_min_progress = float(tpm.get("min_progress_to_tp1", 0.25))
        self.tp1_prog_reduce_pct = float(tpm.get("reduce_pct", 0.35))
        self.tp1_prog_only_if_not_profitable = bool(tpm.get("only_reduce_if_not_profitable", True))
        self.tp1_prog_tighten_bars = int(tpm.get("tighten_trail_after_bars", 4))
        self.tp1_prog_tighten_mult = float(tpm.get("tighten_trail_mult", 0.55))
        self.tp1_prog_early_exit_bars = int(tpm.get("early_exit_after_bars", 8))
        self.tp1_prog_early_exit_r = float(tpm.get("early_exit_if_change_below_r", -0.45))

        # Volatilite filtresi
        vpf = self.cfg.get("vol_position_filter", {})
        self.vpf_enabled       = bool(vpf.get("enabled", False))
        self.vpf_atr_threshold = float(vpf.get("high_atr_threshold", 2.5))
        self.vpf_size_mult     = float(vpf.get("high_atr_size_mult", 0.5))

        # Symbol quality
        sqf = self.cfg.get("symbol_quality_filter", {})
        self.sqf_enabled    = bool(sqf.get("enabled", True))
        self.sqf_weak_mult  = float(sqf.get("weak_symbol_multiplier", 0.50))
        self.sqf_min_qs     = float(sqf.get("min_qs", 8.0))
        self.sqf_min_atr    = float(sqf.get("min_atr_pct", 1.2))
        self.sqf_min_adx    = float(sqf.get("min_adx", 20.0))
        self.sqf_score_bon  = float(sqf.get("score_bonus", 5.0))

        # Fiyat serileri
        self.close_series    = {s: deque(maxlen=2048) for s in symbols}
        self.high_series     = {s: deque(maxlen=2048) for s in symbols}
        self.low_series      = {s: deque(maxlen=2048) for s in symbols}
        self.vol_series      = {s: deque(maxlen=2048) for s in symbols}
        self.last_close_time = {s: 0 for s in symbols}

        # HTF serileri
        self.htf_close_series = {s: deque(maxlen=500) for s in symbols}
        self.htf_high_series  = {s: deque(maxlen=500) for s in symbols}
        self.htf_low_series   = {s: deque(maxlen=500) for s in symbols}
        self.htf_vol_series   = {s: deque(maxlen=500) for s in symbols}
        self.htf_last_time    = {s: 0 for s in symbols}

        self.open_positions    = {}
        self.trade_count_today = 0
        self.pnl_total_usd     = 0.0
        self.daily_pnl_usd     = 0.0
        self._daily_fired      = False
        self.last_reset_day    = time.strftime("%Y-%m-%d")

        self.lock     = threading.Lock()
        self._stopped = False
        self.on_event = self.cfg.get("on_event")

        # President sistemi
        self.runtime = PresidentRuntime(cfg, data_dir=str(self.data_dir), persist_risk=True)
        # Kısayollar (geriye dönük uyumluluk)
        self.president = self.runtime.president
        self.short_surg= self.runtime.short
        self.convex    = self.runtime.convex

        self.allowed_symbol = None
        self.csv_path       = self.data_dir / "trade_logs.csv"
        self.events_path    = self.data_dir / "engine_events.log"

    # ── Durdur ────────────────────────────────────────────────────────
    def stop(self):
        with self.lock:
            self._stopped = True
            self._fire("ENGINE_STOP")

    # ── Event ─────────────────────────────────────────────────────────
    def _fire(self, etype: str, **kw):
        ts = int(time.time())
        line = f"{ts}\t{etype}\t" + " ".join(f"{k}={v}" for k,v in kw.items())
        try:
            with open(self.events_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        log_event(etype, **kw)
        if callable(self.on_event):
            try:
                self.on_event(etype, kw)
            except Exception:
                pass

    # ── Kara Liste ────────────────────────────────────────────────────
    def add_to_blacklist(self, symbol: str, hours: float = 24.0):
        with self.lock:
            self.blacklist[symbol] = time.time() + hours * 3600
            self._fire("BLACKLIST_ADD", symbol=symbol, hours=hours)

    def remove_from_blacklist(self, symbol: str):
        with self.lock:
            self.blacklist.pop(symbol, None)

    def get_blacklist(self) -> list:
        with self.lock:
            now = time.time()
            expired = [s for s, exp in self.blacklist.items() if now >= exp]
            for s in expired:
                del self.blacklist[s]
            return [(s, round((exp - now)/3600, 1)) for s, exp in self.blacklist.items()]

    # ── Veri Besleme LTF ──────────────────────────────────────────────
    def seed_from_candles(self, symbol: str, candles: list):
        with self.lock:
            d = self.close_series.setdefault(symbol, deque(maxlen=2048))
            h = self.high_series.setdefault(symbol,  deque(maxlen=2048))
            l = self.low_series.setdefault(symbol,   deque(maxlen=2048))
            v = self.vol_series.setdefault(symbol,   deque(maxlen=2048))
            for c in candles:
                d.append(float(c.get("close",  0)))
                h.append(float(c.get("high",   0)))
                l.append(float(c.get("low",    0)))
                v.append(float(c.get("volume", 0)))
            if candles:
                self.last_close_time[symbol] = int(candles[-1].get("close_time", 0))

    def on_candle(self, symbol: str, candle: dict):
        with self.lock:
            if self._stopped:
                return
            self._reset_daily_if_needed()
            price  = float(candle.get("close",  0))
            high   = float(candle.get("high",   0))
            low    = float(candle.get("low",    0))
            volume = float(candle.get("volume", 0))
            self.close_series[symbol].append(price)
            self.high_series[symbol].append(high)
            self.low_series[symbol].append(low)
            self.vol_series[symbol].append(volume)
            self.last_close_time[symbol] = int(candle.get("close_time", 0))
            prices  = list(self.close_series[symbol])
            highs   = list(self.high_series[symbol])
            lows    = list(self.low_series[symbol])
            volumes = list(self.vol_series[symbol])
            in_pos  = symbol in self.open_positions

        if len(prices) < 50:
            return

        # ── Bekleyen SL'leri coz (SL'den 4h sonra verdict hesapla) ────────
        self._resolve_pending_sl(symbol, price)

        try:
            from data_macro import get_sentiment_score
            news_score = get_sentiment_score()
        except Exception:
            news_score = 50.0

        result = score_symbol(prices, highs, lows, volumes, news_score)
        score  = result["final_score"]

        with self.lock:
            if self._stopped:
                return
            if in_pos:
                self._manage(symbol, price, score)
            else:
                self._try_open(symbol, price, score, prices, highs, lows, volumes, result)

    # ── Veri Besleme HTF ──────────────────────────────────────────────
    def seed_from_candles_htf(self, symbol: str, candles: list):
        with self.lock:
            pd = self.htf_close_series.setdefault(symbol, deque(maxlen=500))
            hd = self.htf_high_series.setdefault(symbol,  deque(maxlen=500))
            ld = self.htf_low_series.setdefault(symbol,   deque(maxlen=500))
            vd = self.htf_vol_series.setdefault(symbol,   deque(maxlen=500))
            for c in candles:
                pd.append(float(c.get("close",  0)))
                hd.append(float(c.get("high",   0)))
                ld.append(float(c.get("low",    0)))
                vd.append(float(c.get("volume", 0)))
            if candles:
                self.htf_last_time[symbol] = int(candles[-1].get("close_time", 0))

    def on_candle_htf(self, symbol: str, candle: dict):
        with self.lock:
            if self._stopped:
                return
            self.htf_close_series[symbol].append(float(candle.get("close",  0)))
            self.htf_high_series[symbol].append( float(candle.get("high",   0)))
            self.htf_low_series[symbol].append(  float(candle.get("low",    0)))
            self.htf_vol_series[symbol].append(  float(candle.get("volume", 0)))

    # ── HTF Skor ──────────────────────────────────────────────────────
    def _htf_score(self, symbol: str) -> float:
        prices  = list(self.htf_close_series.get(symbol, []))
        highs   = list(self.htf_high_series.get(symbol,  []))
        lows    = list(self.htf_low_series.get(symbol,   []))
        volumes = list(self.htf_vol_series.get(symbol,   []))
        if len(prices) < 50:
            return 50.0
        try:
            result = score_symbol(prices, highs, lows, volumes)
            return result["final_score"]
        except Exception:
            return 50.0

    # ── Gunluk Sifirlama ──────────────────────────────────────────────
    def _reset_daily_if_needed(self):
        today = time.strftime("%Y-%m-%d")
        if today != self.last_reset_day:
            self.trade_count_today = 0
            self.daily_pnl_usd     = 0.0
            self._daily_fired      = False
            self.last_reset_day    = today

    # ── Lot Hesapla ───────────────────────────────────────────────────
    def _lot(self, price: float, dynamic_sl_pct: float = None) -> float:
        if dynamic_sl_pct is None:
            dynamic_sl_pct = self.sl_pct
        current_equity = self.equity + self.pnl_total_usd
        risk_usdt = current_equity * self.risk_per_trade
        denom = price * max(dynamic_sl_pct, 0.001)
        return max(0.0001, risk_usdt / denom)

    # ── Gunluk Limit Kontrolleri ──────────────────────────────────────
    def _daily_target_hit(self) -> bool:
        if self._daily_fired:
            return True
        target = (self.equity + self.pnl_total_usd) * self.daily_target
        if self.daily_pnl_usd >= target:
            self._daily_fired = True
            self._fire("DAILY_TARGET_HIT", daily_pnl=round(self.daily_pnl_usd, 2))
            return True
        return False

    def _daily_loss_hit(self) -> bool:
        limit = (self.equity + self.pnl_total_usd) * self.daily_loss_lim
        if self.daily_pnl_usd <= -limit:
            self._fire("DAILY_LOSS_LIMIT", daily_pnl=round(self.daily_pnl_usd, 2))
            return True
        return False

    # ── Profesyonel PnL yardımcıları (backtest.py ile parite) ──────────
    def _fee_cost(self, price: float, qty: float) -> float:
        """Komisyon+slipaj NOTIONAL üzerinden mutlak tutar — backtest.py'deki
        _fee_cost ile birebir aynı formül (parite için kritik)."""
        return float(price) * float(qty) * (self.commission + self.slippage)

    def _gross_pnl(self, side: str, entry: float, exit_price: float, qty: float) -> float:
        return ((exit_price - entry) if side == "LONG" else (entry - exit_price)) * qty

    # ── Pozisyon Yonetimi ─────────────────────────────────────────────
    def _manage(self, symbol: str, price: float, score: float):
        pos = self.open_positions[symbol]
        age = time.time() - pos["ts_open"]
        mult = 1 if pos["side"] == "LONG" else -1
        change = (price - pos["entry"]) / pos["entry"] * mult
        pos["last_price"] = price
        pos["bars_held"] = int(pos.get("bars_held", 0)) + 1
        pos_sl_pct = pos.get("sl_pct", self.sl_pct)

        if change <= -pos_sl_pct:
            self._close(symbol, price, change, "SL")
            return

        # Partial TP
        if self.partial_tp_enabled and not pos.get("tp1_done", False):
            tp1_target = pos_sl_pct * self.tp1_r_mult
            if change >= tp1_target:
                partial_qty = pos["qty"] * float(pos.get("tp1_close_pct", self.tp1_close_pct))
                gross = self._gross_pnl(pos["side"], pos["entry"], price, partial_qty)
                pnl   = gross - self._fee_cost(price, partial_qty)
                # V8.5.9 FIX: running toplama EKLEME — _close'daki total_net hesabı
                # pos["tp1_pnl"]'i dahil ettiği için burada eklersek çifte sayılırdı.
                pos["qty"] -= partial_qty
                pos["tp1_done"] = True
                pos["tp1_pnl"]  = round(pnl, 4)
                self._fire("PARTIAL_TP", symbol=symbol, qty=round(partial_qty,6),
                           pnl=round(pnl,2), pct=round(change*100,2))

        # V8.5 TP1 Progress Manager — TP1'e ilerlemeyen pozisyonda riski azalt
        if self.tp1_prog_enabled and self.partial_tp_enabled and not pos.get("tp1_done", False):
            tp1_target = max(pos_sl_pct * self.tp1_r_mult, 0.0001)
            progress_to_tp1 = change / tp1_target
            bars_held = int(pos.get("bars_held", 0))

            if bars_held >= self.tp1_prog_tighten_bars and self.trail:
                original_trail = pos.get("original_trail_step", pos.get("trail_step", self.trail_step))
                pos["trail_step"] = max(0.0015, min(pos.get("trail_step", original_trail), original_trail * self.tp1_prog_tighten_mult))

            should_reduce = (
                bars_held >= self.tp1_prog_check_bars
                and not pos.get("tp1_progress_reduced", False)
                and progress_to_tp1 < self.tp1_prog_min_progress
                and (not self.tp1_prog_only_if_not_profitable or change <= 0)
            )
            if should_reduce and pos.get("qty", 0) > 0:
                reduce_qty = pos["qty"] * max(0.0, min(0.95, self.tp1_prog_reduce_pct))
                gross = self._gross_pnl(pos["side"], pos["entry"], price, reduce_qty)
                pnl   = gross - self._fee_cost(price, reduce_qty)
                # V8.5.9 FIX: running toplama EKLEME — _close'daki total_net hesabı
                # pos["tp1_progress_pnl"]'i dahil ettiği için çifte sayılırdı.
                pos["qty"] -= reduce_qty
                pos["tp1_progress_reduced"] = True
                pos["tp1_progress_pnl"] = round(pos.get("tp1_progress_pnl", 0.0) + pnl, 4)
                self._fire("TP1_PROGRESS_REDUCE", symbol=symbol, qty=round(reduce_qty,6), pnl=round(pnl,2), pct=round(change*100,2))

            if bars_held >= self.tp1_prog_early_exit_bars and change <= pos_sl_pct * self.tp1_prog_early_exit_r:
                self._close(symbol, price, change, "EarlyNoTP1")
                return

        # Convex pyramid
        add_mult = self.runtime.check_pyramid(symbol, price)
        if add_mult is not None and add_mult > 0:
            if add_mult:
                extra_qty = self._lot(price, pos_sl_pct) * add_mult
                pos["qty"] += extra_qty
                self._fire("PYRAMID_ADD", symbol=symbol, mult=round(add_mult,3))

        if self.trail and change > 0:
            pos_trail = pos.get("trail_step", self.trail_step)
            trail_locked = pos.get("trail_locked", None)
            if trail_locked is None or change > trail_locked + pos_trail:
                pos["trail_locked"] = change

        if age < self.min_hold:
            return
        if age >= self.max_hold_sec:
            self._close(symbol, price, change, "MaxHold")
            return

        reason = self._exit_reason(pos, price, change, score)
        if reason:
            self._close(symbol, price, change, reason)

    def _exit_reason(self, pos: dict, price: float, change: float, score: float):
        pos_sl_pct = pos.get("sl_pct", self.sl_pct)
        if change <= -pos_sl_pct:
            return "SL"
        if change >= self.tp_pct and change >= self.min_profit_close:
            return "TP"
        if change >= self.min_profit_close:
            if pos["side"] == "LONG"  and score < self.score_close:
                return "ScoreClose"
            if pos["side"] == "SHORT" and score > self.score_close:
                return "ScoreClose"
        locked    = pos.get("trail_locked", change)
        pos_trail = pos.get("trail_step", self.trail_step)
        if self.trail and change < locked - pos_trail:
            return "Trail"
        return None


    def _maybe_rotate_live(self, symbol: str, score: float) -> bool:
        """
        SAFETY PATCH V8.4.1:
        Canlı engine'de rotation President kararından önce pozisyon kapatamaz.
        Şimdilik yalnızca ROTATION_CANDIDATE_SHADOW event'i üretir ve False döner.
        """
        if not self.rotation_enabled or len(self.open_positions) < self.max_open_pos:
            return False
        if score < self.rotation_min_score:
            return False

        weakest_sym, weakest_pos = None, None
        weakest_score = 999.0
        weakest_change = 0.0
        for osym, pos in self.open_positions.items():
            last_price = float(pos.get("last_price", pos.get("entry", 0.0)) or 0.0)
            entry = float(pos.get("entry", last_price) or last_price)
            if not entry:
                continue
            mult = 1 if pos.get("side") == "LONG" else -1
            change = (last_price - entry) / entry * mult
            if change > 0 and not self.rotation_allow_close_profitable:
                continue
            ps = float(pos.get("score", 0.0))
            weakness = ps + max(change * 100, -20)
            if weakness < weakest_score:
                weakest_sym, weakest_pos, weakest_score, weakest_change = osym, pos, weakness, change

        if not weakest_sym or score < float(weakest_pos.get("score", 0.0)) + self.rotation_min_delta:
            return False

        self._fire("ROTATION_CANDIDATE_SHADOW", new_symbol=symbol, replaced=weakest_sym,
                   new_score=round(score, 2), old_score=round(float(weakest_pos.get("score", 0.0)), 2),
                   old_unrealized_pct=round(weakest_change * 100, 3), shadow_only=True)
        # Fiziksel kapatma yok. President ROTATE_AND_OPEN aksiyonu gelene kadar güvenli blok.
        return False

    # ── Pozisyon Acma ─────────────────────────────────────────────────
    def _try_open(self, symbol: str, price: float, score: float,
                  prices: list, highs: list, lows: list, volumes: list, result: dict):
        if self._stopped:                       return
        if len(self.open_positions) >= self.max_open_pos and not self._maybe_rotate_live(symbol, score): return
        if self.trade_count_today >= self.max_trades_day: return
        if self.allowed_symbol and symbol != self.allowed_symbol: return
        if self._daily_target_hit():            return
        if self._daily_loss_hit():              return

        # Kara liste
        bl_exp = self.blacklist.get(symbol)
        if bl_exp and time.time() < bl_exp:
            return
        elif bl_exp:
            del self.blacklist[symbol]

        if symbol != "BTCUSDT" and self.blacklist_enabled and symbol in self.blacklist_symbols:
            self._fire("OPEN_BLOCK", cause="SYMBOL_BLACKLIST", symbol=symbol)
            return

        # Hacim filtresi
        if len(prices) >= 20:
            avg_notional = (sum(prices[-20:])/20) * (sum(volumes[-20:])/20)
            if avg_notional < self.min_notional:
                return

        if len(volumes) >= 20:
            rv = sum(volumes[-3:]) / 3
            hv = sum(volumes[-20:-3]) / 17
            if hv > 0 and rv < hv * self.vol_mult:
                self._fire("OPEN_BLOCK", cause="LOW_VOLUME", symbol=symbol)
                return

        # Sentiment
        try:
            from data_macro import get_market_sentiment
            sentiment = get_market_sentiment()
        except Exception:
            sentiment = "NEUTRAL"

        # Regime
        regime   = self.regime.get_regime()
        htf_sc   = self._htf_score(symbol) if self.mtf_enabled else 100.0

        # ── FILTREler önce — President SONRA (risk sayacı bozulmasın) ──
        adx_val = result.get("components", {}).get("adx", 0.0)
        # require_adx_strict=False (varsayılan): ADX=0 (hesaplanamadı) ise bypass.
        # require_adx_strict=True: ADX=0 da bloklanır (backtest.py ile parite).
        adx_blocks = self.adx_filter_enabled and (
            (adx_val > 0 and adx_val < self.adx_filter_threshold) or
            (adx_val <= 0 and self.require_adx_strict)
        )
        if adx_blocks:
            self._fire("OPEN_BLOCK", cause="ADX_TOO_LOW", symbol=symbol); return
        if self.atr_filter_enabled:
            atr_val = result.get("components", {}).get("atr_pct", 0.0)
            if atr_val < self.atr_filter_min:
                self._fire("OPEN_BLOCK", cause="ATR_TOO_LOW", symbol=symbol); return
        if self.rsi_filter_enabled:
            rsi_val = result.get("components", {}).get("rsi", 50.0)
            if rsi_val > self.rsi_max_long:
                self._fire("OPEN_BLOCK", cause="RSI_TOO_HIGH", symbol=symbol); return
        if self.mtf_enabled and htf_sc < self.mtf_long_min:
            self._fire("OPEN_BLOCK", cause="MTF_NO_CONFIRM_LONG", symbol=symbol); return
        # BTC genel düşüş filtresi (varsayılan KAPALI) — backtest.py ile parite.
        if self.btc_filter_en:
            _btc_prices = list(self.close_series.get("BTCUSDT", []))
            if len(_btc_prices) >= self.btc_filter_candles + 1:
                _b_start = _btc_prices[-(self.btc_filter_candles + 1)]
                _b_end   = _btc_prices[-1]
                _b_drop  = (_b_end - _b_start) / _b_start * 100 if _b_start > 0 else 0.0
                if _b_drop <= -self.btc_filter_drop:
                    self._fire("OPEN_BLOCK", cause="BTC_FILTER_DROP", symbol=symbol,
                               btc_drop_pct=round(_b_drop, 2))
                    return

        # ── PRESIDENT KARAR — filtreler geçtikten sonra ──────────────
        # V8.5.8 Pump/Manipülasyon Filtresi — SERT BLOK DEĞİL (puan + boyut
        # cezası). backtest.py ile BİREBİR AYNI compute_pump_risk() çağrılır.
        pump_info = compute_pump_risk(prices, volumes, self.cfg)
        if pump_info.get("is_pump"):
            score = max(0.0, score - pump_info["score_penalty"])
            self._fire("OPEN_PENALTY", cause="PUMP_RISK_SOFT", symbol=symbol,
                      vol_ratio=pump_info["vol_ratio"], price_chg_pct=pump_info["price_chg_pct"],
                      score_penalty=pump_info["score_penalty"], size_mult=pump_info["size_mult"])

        candle_ts = float(self.last_close_time.get(symbol, 0)) / 1000 if self.last_close_time.get(symbol, 0) else time.time()
        packet = self.runtime.evaluate(
            symbol, candle_ts, score, result, regime, htf_sc, sentiment,
            prices, highs, lows, volumes,
            list(self.close_series.get("BTCUSDT", [])),
        )

        if packet.action not in (Action.OPEN,):
            return  # Shadow veya Block — risk sayacı ETKİLENMEDİ

        side = packet.side.value

        # ── Side-aware ikincil kontrol (RSI/MTF yöne göre değişir) ──────
        # Bu kontroller risk sayacından ÖNCE olduğu için güvenli: henüz
        # confirm_open çağrılmadı, bloklarsak hiçbir sayaç bozulmaz.
        if self.rsi_filter_enabled:
            rsi_val = result.get("components", {}).get("rsi", 50.0)
            if side == "SHORT" and rsi_val < self.rsi_min_short:
                self._fire("OPEN_BLOCK", cause="RSI_TOO_LOW", symbol=symbol); return
        if self.mtf_enabled and side == "SHORT" and htf_sc > self.mtf_short_max:
            self._fire("OPEN_BLOCK", cause="MTF_NO_CONFIRM_SHORT", symbol=symbol); return

        self._open(symbol, price, packet.side.value, result,
                   size_mult=packet.size_mult, sl_pct_override=packet.sl_pct,
                   htf_score=htf_sc, packet=packet, pump_context=pump_info)
        # Gerçek pozisyon açıldıktan SONRA risk governor'a bildir
        self.runtime.confirm_open(packet.side.value)

    def _open(self, symbol: str, price: float, side: str, result: dict,
              size_mult: float = 1.0, sl_pct_override: float = None,
              htf_score: float = 50.0, packet=None, pump_context: dict = None):
        atr_pct_val = result.get("components", {}).get("atr_pct", 0.0)
        regime      = self.regime.get_regime()

        adsl = adaptive_sl.compute(
            regime=regime,
            atr_pct=atr_pct_val,
            base_score_threshold=self.score_long_open,
            base_atr_multiplier=self.atr_multiplier,
            base_trail_step=self.trail_step,
            cfg=self.cfg,
        )

        if sl_pct_override and sl_pct_override > 0:
            final_sl_pct = sl_pct_override
        elif self.use_atr_stop and atr_pct_val > 0:
            final_sl_pct = adsl["sl_pct"]
        else:
            final_sl_pct = self.sl_pct

        pos_trail = adsl["trail_step"] if self.dynamic_trail_enabled else self.trail_step
        symbol_mult = self.sym_mgr.size_multiplier(symbol) if hasattr(self, "sym_mgr") else 1.0
        symbol_stats = None
        try:
            symbol_stats = self.sym_mgr.get_all_stats().get(symbol, {}) if hasattr(self, "sym_mgr") else None
        except Exception:
            symbol_stats = None
        ae = classify_trade(symbol=symbol, side=side, score=result.get("final_score", 0.0),
                            htf_score=htf_score, regime=regime, components=result.get("components", {}),
                            cfg=self.cfg, prices=list(self.close_series.get(symbol, [])),
                            highs=list(self.high_series.get(symbol, [])), lows=list(self.low_series.get(symbol, [])),
                            volumes=list(self.vol_series.get(symbol, [])), symbol_stats=symbol_stats)
        if ae.enabled and not ae.shadow_mode:
            size_mult *= float(ae.policy.size_mult or 1.0)
            pos_trail = max(0.001, float(ae.policy.trail_step_pct) / 100.0)
        # V8.5.8 Pump/Manipülasyon Filtresi — pozisyon boyutu küçültme (sert blok değil).
        pump_context = pump_context or {}
        if pump_context.get("is_pump"):
            size_mult *= float(pump_context.get("size_mult", 1.0))
        mr = self.cfg.get("market_regime", {})
        regime_mult = 1.0
        if str(regime).upper() == "NEUTRAL":
            regime_mult = float(mr.get("neutral_size_mult", 1.0))
        elif str(regime).upper() == "KONSOL":
            regime_mult = float(mr.get("konsol_size_mult", 1.0))
        final_size_mult = max(0.05, size_mult * symbol_mult * regime_mult)
        qty       = self._lot(price, dynamic_sl_pct=final_sl_pct) * final_size_mult

        if self.vpf_enabled and atr_pct_val > self.vpf_atr_threshold:
            qty *= self.vpf_size_mult

        entry_cost = self._fee_cost(price, qty)
        self.open_positions[symbol] = {
            "side":       side,
            "entry":      price,
            "qty":        qty,
            "entry_cost": round(entry_cost, 6),
            "ts_open":    time.time(),
            "sl_pct":     final_sl_pct,
            "trail_step": pos_trail,
            "score":      result.get("final_score", 0.0),
            "tp1_done":   False,
            "tp1_pnl":    0.0,
            "tp1_progress_reduced": False,
            "tp1_progress_pnl": 0.0,
            "bars_held": 0,
            "original_trail_step": pos_trail,
            "symbol_size_mult": round(symbol_mult, 4),
            "regime_size_mult": round(regime_mult, 4),
            "final_size_mult": round(final_size_mult, 4),
            "ae_class": ae.trade_class,
            "ae_policy": ae.policy_name,
            "ae_continuation_score": ae.continuation_score,
            "ae_confidence": ae.confidence,
            "ae_reasons": ae.reasons[:240],
            "tp1_close_pct": float(ae.policy.tp1_close_pct),
            "max_hold_bars_override": (int(float(ae.policy.max_hold_hours)) if ae.policy.max_hold_hours else None),
            "decision_id": getattr(packet, "decision_id", "") if packet else "",
            "label": getattr(packet, "label", "") if packet else "",
            "quality_score_report": (getattr(packet, "extra", {}) or {}).get("quality_score_report", {}) if packet else {},
            "adaptive_risk_report": (getattr(packet, "extra", {}) or {}).get("adaptive_risk_report", {}) if packet else {},
            "pump_risk": int(bool(pump_context.get("is_pump"))),
            "pump_vol_ratio": pump_context.get("vol_ratio", ""),
            "pump_price_chg_pct": pump_context.get("price_chg_pct", ""),
            "pump_score_penalty": pump_context.get("score_penalty", ""),
        }
        self.trade_count_today += 1
        self.runtime.on_open(symbol, side, price, final_sl_pct)  # FIX: side artık geçiriliyor
        self._log_trade(symbol, side, qty, price, "", 0.0, 0.0,
                        f"OPEN sl={final_sl_pct*100:.2f}% regime={regime}")
        self._fire("OPEN", symbol=symbol, side=side, entry=price,
                   score=result.get("final_score", 0.0), regime=regime,
                   ae_class=ae.trade_class, ae_policy=ae.policy_name,
                   decision_id=(getattr(packet, "decision_id", "") if packet else ""),
                   pump_risk=int(bool(pump_context.get("is_pump"))))

    def _resolve_pending_sl(self, symbol: str, price: float):
        """SL'den ~4h sonra verdict hesaplayip Short Surgeon'u besler."""
        pend = getattr(self, "_pending_sl", None)
        if not pend or symbol not in pend:
            return
        rec = pend[symbol]
        if time.time() - rec["ts"] < 4 * 3600:
            return  # henuz 4 saat dolmadi
        chg_4h = (price - rec["price"]) / rec["price"] if rec["price"] > 0 else 0.0
        if chg_4h <= -0.01:
            verdict = "SL_DOGRU"
        elif chg_4h >= 0.01:
            verdict = "ERKEN_SL"
        else:
            verdict = "BELIRSIZ"
        self.short_surg.record_sl(symbol, verdict, time.time(), chg_4h)
        pend.pop(symbol, None)

    def _close(self, symbol: str, price: float, change_pct: float, reason: str):
        pos = self.open_positions.pop(symbol, None)
        if not pos:
            return
        entry      = pos["entry"]
        qty        = pos["qty"]
        entry_cost = float(pos.get("entry_cost", 0.0))
        gross      = self._gross_pnl(pos["side"], entry, price, qty)
        exit_cost  = self._fee_cost(price, qty)
        pnl_usd    = gross - exit_cost   # bu kapanış parçasının komisyon dahil PnL'i
        # Risk Governor / Symbol Manager GERÇEK TOPLAMI görmeli: TP1 + TP1_Progress +
        # bu kapanış parçası − giriş komisyonu (backtest.py'deki total_net ile parite)
        total_net  = pnl_usd + pos.get("tp1_pnl", 0.0) + pos.get("tp1_progress_pnl", 0.0) - entry_cost
        # V8.5.9 FIX: önceden pnl_usd kullanılıyordu — TP1 PnL (satır 419/447'de ara olarak
        # eklendiği için çifte sayılıyordu) ve giriş komisyonu eksikti. Şimdi her iki running
        # toplam da total_net şemasına çekildi; TP1 ara eklemeleri (satır 419/447) artık
        # sadece pos["tp1_pnl"] / pos["tp1_progress_pnl"]'e yazılıyor, running toplama DEĞİL.
        # Bu, backtest.py'deki _pnl_running + _daily_pnl şemasıyla tam parity sağlar.
        self.pnl_total_usd += total_net
        self.daily_pnl_usd += total_net
        self.sym_mgr.record_trade(symbol, total_net)
        self.sym_mgr.update_equity(self.equity + self.pnl_total_usd)
        candle_ts_c = float(self.last_close_time.get(symbol, 0)) / 1000 if self.last_close_time.get(symbol, 0) else time.time()
        self.runtime.on_close(symbol, pos["side"], total_net, candle_ts_c)
        self.runtime.update_equity(self.equity + self.pnl_total_usd)

        # ── SL_DOGRU beslemesi: LONG SL'i bekleyen kuyruga al (4h sonra cozulur)
        if reason == "SL" and pos["side"] == "LONG":
            if not hasattr(self, "_pending_sl"):
                self._pending_sl = {}
            self._pending_sl[symbol] = {"ts": time.time(), "price": price}

        self._log_trade(symbol, pos["side"], qty, entry, price,
                        round(change_pct*100, 3), round(total_net, 3), reason)
        self._fire("EXIT", symbol=symbol, side=pos["side"], reason=reason,
                   pnl_usd=round(total_net, 2), pnl_pct=f"{change_pct*100:.2f}%")

    # ── CSV Log ───────────────────────────────────────────────────────
    def _log_trade(self, sym, side, qty, entry, exitp, kar_pct, kar_usd, note):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        new = not self.csv_path.exists() or self.csv_path.stat().st_size == 0
        try:
            with open(self.csv_path, "a", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f, delimiter=";")
                if new:
                    w.writerow(["Tarih","Sembol","Yon","GirisFiyati",
                                 "CikisFiyati","KarYuzde","KarUSD","Not"])
                w.writerow([ts, sym, side, entry or "",
                             exitp or "", kar_pct, kar_usd, note])
        except Exception as e:
            log_error(f"CSV yazma hatasi: {e}")

    # ── GUI Veri ──────────────────────────────────────────────────────
    def get_open_positions(self) -> list:
        with self.lock:
            out = []
            for sym, p in self.open_positions.items():
                age = int(time.time() - p["ts_open"])
                out.append({
                    "symbol":    sym,
                    "side":      p["side"],
                    "entry":     p["entry"],
                    "age_min":   round(age / 60, 1),
                    "trail_pct": round(p.get("trail_locked", 0.0)*100, 2),
                    "tp1_done":  p.get("tp1_done", False),
                })
            return out

    def get_pnl(self) -> dict:
        with self.lock:
            base = self.equity
            return {
                "usd":       round(self.pnl_total_usd, 2),
                "pct":       round(self.pnl_total_usd / base * 100, 3) if base else 0.0,
                "daily_usd": round(self.daily_pnl_usd, 2),
                "equity":    round(base + self.pnl_total_usd, 2),
            }

    def set_allowed_symbol(self, sym_or_none):
        with self.lock:
            self.allowed_symbol = sym_or_none
