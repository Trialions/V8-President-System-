# president_runtime.py — v8.2 ORTAK KARAR PIPELINE
from __future__ import annotations
from typing import Dict, List, Optional
from president_governor import PresidentGovernor
from branches.core_long_branch import CoreLongBranch
from branches.short_surgeon import ShortSurgeon
from branches.cascade_hunter import CascadeHunter
from modules.convex_position import ConvexPosition
from modules.decision_packet import Action, BranchVote, DecisionPacket, Side
from quality_score import compute_quality_score
from adaptive_risk import compute_adaptive_risk_hint


class PresidentRuntime:
    def __init__(self, cfg: dict, data_dir: str = "data", persist_risk: bool = False):
        self.cfg       = cfg
        self.president = PresidentGovernor(cfg, data_dir, persist_risk=persist_risk)
        self.core_long = CoreLongBranch(cfg)
        self.short     = ShortSurgeon(cfg)
        self.cascade   = CascadeHunter(cfg)
        self.convex    = ConvexPosition(cfg)

    def evaluate(self, symbol: str, candle_ts: float, score: float, result: dict,
                 regime: str = "NEUTRAL", htf_score: float = 50.0,
                 sentiment: str = "NEUTRAL",
                 prices: List[float] = None, highs: List[float] = None,
                 lows: List[float] = None, volumes: List[float] = None,
                 btc_prices: List[float] = None) -> DecisionPacket:

        prices = prices or []; highs = highs or []
        lows   = lows   or []; volumes = volumes or []

        votes: Dict[str, BranchVote] = {
            "core_long":      self.core_long.vote(symbol, score, result, regime, htf_score, sentiment),
            "short_surgeon":  self.short.vote(symbol, score, result, regime, btc_prices, now=candle_ts),
            "cascade_hunter": self.cascade.vote(symbol, score, prices, highs, lows, volumes, result),
        }
        # candle_ts dal oylarına da yazılır (log eşleşmesi için)
        for v in votes.values():
            v.candle_ts = candle_ts

        # V8.5.2 Intelligence Merge: V7 kalite/risk katmanı President'a yalnızca rapor verir.
        # Bu modüller OPEN/BLOCK kararı vermez; nihai karar PresidentGovernor içindedir.
        symbol_stats = None
        try:
            symbol_stats = (result or {}).get("symbol_stats")
        except Exception:
            symbol_stats = None
        q_report = compute_quality_score(symbol, result, regime, htf_score, symbol_stats, self.cfg)
        r_report = compute_adaptive_risk_hint(result, regime, q_report.score, self.cfg)
        market_state = {
            "regime": regime,
            "quality_score_report": q_report.to_dict(),
            "adaptive_risk_report": r_report.to_dict(),
        }
        pkt = self.president.decide(symbol, candle_ts, votes, market_state)
        pkt.extra["quality_score_report"] = q_report.to_dict()
        pkt.extra["adaptive_risk_report"] = r_report.to_dict()
        return pkt

    # ── Geri besleme ─────────────────────────────────────────────────
    def on_sl(self, symbol: str, verdict: str, candle_ts: float, chg_4h: float = 0.0):
        self.short.record_sl(symbol, verdict, candle_ts, chg_4h)

    def confirm_open(self, side: str):
        """Filtreler geçildikten sonra risk governor'a bildir."""
        self.president.confirm_open(side)

    def on_open(self, symbol: str, side: str, entry: float, sl_pct: float):
        self.convex.on_open(symbol, side, entry, sl_pct)

    def on_close(self, symbol: str, side: str, pnl_usd: float, candle_ts: float = 0.0):
        self.president.on_close(symbol, side, pnl_usd, candle_ts)
        self.convex.on_close(symbol)

    def check_pyramid(self, symbol: str, price: float) -> Optional[float]:
        return self.convex.check_add(symbol, price)

    def update_equity(self, equity: float):
        self.president.update_equity(equity)

    def get_state(self) -> dict:
        return self.president.get_state()
