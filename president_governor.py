# president_governor.py — v8.2
# KRİTİK DÜZELTİLER:
#   - _packet() ts=time.time() kaldırıldı → candle_ts kullanılır
#   - final_score sadece KAZANAN tarafın oylarından hesaplanır
#   - RiskGovernor.can_open(side, candle_ts) çağrısı eklendi
#   - _log_decision candle_ts ile yazar (bilgisayar saati değil)
from __future__ import annotations
import csv, threading, time
from pathlib import Path
from typing import Dict
from modules.decision_packet import Action, BranchVote, DecisionPacket, Side
from modules.risk_governor import RiskGovernor


class PresidentGovernor:
    def __init__(self, cfg: dict, data_dir: str = "data", persist_risk: bool = False):
        self.cfg      = cfg
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        p = cfg.get("president", {})
        self.enabled   = bool(p.get("enabled", True))
        self.shadow    = bool(p.get("shadow_mode", False))
        self.min_votes = int(p.get("min_votes_to_open", 1))
        self.bonus     = float(p.get("consensus_bonus", 5.0))

        w = p.get("branch_weights", {})
        self.weights = {
            "core_long":      float(w.get("core_long", 0.7)),
            "short_surgeon":  float(w.get("short_surgeon", 0.2)),
            "cascade_hunter": float(w.get("cascade_hunter", 0.1)),
        }
        thr = p.get("decision_thresholds", {})
        self.t_scout  = float(thr.get("scout",  65))
        self.t_normal = float(thr.get("normal", 75))
        self.t_strong = float(thr.get("strong", 85))
        self.t_attack = float(thr.get("attack", 92))

        # V8.5 karar kalibrasyonu: ATTACK artık sadece skora göre verilmez.
        dc = p.get("decision_calibration", {})
        self.calib_enabled = bool(dc.get("enabled", True))
        self.attack_min_score = float(dc.get("attack_min_score", 94.0))
        self.attack_min_votes = int(dc.get("attack_min_winning_votes", 2))
        self.attack_min_confirm = float(dc.get("attack_min_confirm_score", 70.0))
        self.attack_require_non_core = bool(dc.get("attack_require_non_core_confirmation", True))
        self.attack_allow_shadow_confirm = bool(dc.get("attack_allow_shadow_confirmation", False))
        self.strong_min_score = float(dc.get("strong_min_score", 87.0))
        self.normal_min_score = float(dc.get("normal_min_score", 75.0))
        self.core_only_max_label = str(dc.get("core_only_max_label", "STRONG")).upper()
        self.label_size_mults = {
            "SCOUT": float(dc.get("scout_size_mult", 0.45)),
            "NORMAL": float(dc.get("normal_size_mult", 0.75)),
            "STRONG": float(dc.get("strong_size_mult", 0.90)),
            "ATTACK": float(dc.get("attack_size_mult", 1.00)),
        }
        self.core_only_size_mult = float(dc.get("core_only_size_mult", 0.70))

        rp = str(self.data_dir / "risk_state.json") if persist_risk else None
        self.risk  = RiskGovernor(cfg, persist_path=rp)
        self._lock = threading.Lock()

        self._dec_path    = self.data_dir / "president_decisions.csv"
        self._shadow_path = self.data_dir / "shadow_opportunities.csv"
        self._votes_path  = self.data_dir / "branch_votes.csv"
        self._init_csv_headers()

    # ── Ana Karar ─────────────────────────────────────────────────────
    def decide(self, symbol: str, candle_ts: float,
               votes: Dict[str, BranchVote],
               market_state: dict = None) -> DecisionPacket:
        with self._lock:
            market_state = market_state or {}
            open_votes = [v for v in votes.values() if v.action == Action.OPEN]

            # ── Taraf seçimi (ağırlıklı) ─────────────────────────────
            side_w = {Side.LONG: 0.0, Side.SHORT: 0.0}
            for v in open_votes:
                side_w[v.side] = side_w.get(v.side, 0.0) + \
                    v.score * self.weights.get(v.branch_name, 0.1) * v.confidence
            side = Side.NONE
            if open_votes:
                side = Side.LONG if side_w[Side.LONG] >= side_w[Side.SHORT] else Side.SHORT
                if side_w[side] == 0:
                    side = Side.NONE

            # ── Final skor YALNIZ kazanan taraftan ───────────────────
            winning = [v for v in open_votes if v.side == side]
            total_w  = sum(self.weights.get(v.branch_name, 0.1) * v.confidence for v in winning)
            weighted = sum(v.score * self.weights.get(v.branch_name, 0.1) * v.confidence for v in winning)
            final_score = weighted / total_w if total_w > 0 else 0.0

            # Karşı yön varsa conflict cezası
            opposing = [v for v in open_votes if v.side != side and v.side != Side.NONE]
            if opposing:
                conflict_w = sum(self.weights.get(v.branch_name, 0.1) * v.confidence for v in opposing)
                final_score = max(0.0, final_score - conflict_w * 10)

            # Konsensus bonusu
            if len(winning) >= 2:
                final_score = min(100.0, final_score + self.bonus)

            # V8.5.2 Intelligence Merge: kalite/risk raporları sadece President'a feature verir.
            # Nihai OPEN/BLOCK kararı hâlâ burada verilir; motorlar President'ı baypas etmez.
            q_report = (market_state or {}).get("quality_score_report", {}) or {}
            r_report = (market_state or {}).get("adaptive_risk_report", {}) or {}
            try:
                q_score = float(q_report.get("score", 50.0))
                q_adj = max(-5.0, min(5.0, (q_score - 50.0) * 0.10))
                final_score = max(0.0, min(100.0, final_score + q_adj))
            except Exception:
                q_score = 50.0

            self._log_votes(symbol, votes, candle_ts)

            # Yeterli oy?
            if len(winning) < self.min_votes or side == Side.NONE:
                pkt = self._make_packet(symbol, Action.BLOCK, Side.NONE, final_score, votes,
                    f"INSUFFICIENT_VOTES win={len(winning)} min={self.min_votes}", candle_ts=candle_ts)
                self._log_decision(pkt)
                return pkt

            # Risk vetosu (candle_ts ile doğru reset)
            ok, veto = self.risk.can_open(side.value, candle_ts)
            if not ok:
                pkt = self._make_packet(symbol, Action.BLOCK, side, final_score, votes,
                    f"RISK_VETO:{veto}", candle_ts=candle_ts)
                self._log_decision(pkt)
                return pkt

            # Shadow kararı
            all_shadow = all(v.shadow for v in winning)
            is_shadow  = self.shadow or all_shadow
            action     = Action.SHADOW if is_shadow else Action.OPEN

            best      = max(winning, key=lambda v: v.score)
            sl_pct    = best.params.get("sl_pct", 0.02)
            base_size_mult = best.params.get("size_mult", 1.0)
            label     = self._label(final_score, winning, side)
            size_mult = self._calibrated_size_mult(base_size_mult, label, winning)
            try:
                size_mult *= float(r_report.get("risk_mult", 1.0))
                size_mult *= float(q_report.get("size_hint", 1.0))
                size_mult = max(0.05, min(1.10, size_mult))
            except Exception:
                pass

            pkt = self._make_packet(symbol, action, side, final_score, votes,
                f"VOTES_OK win={len(winning)} score={final_score:.1f} label={label} q={q_report.get('score','')} risk_mult={r_report.get('risk_mult','')} {'SHADOW' if is_shadow else 'LIVE'}",
                sl_pct, size_mult, label, candle_ts=candle_ts)

            self._log_decision(pkt)
            if is_shadow:
                self._log_shadow(pkt)
            # NOT: record_open BURADA değil — gerçek pozisyon açıldıktan
            # sonra confirm_open() çağrılmalı (filtreler sonrası).
            return pkt

    def _label(self, score, winning=None, side=None):
        """
        V8.5: ATTACK etiketini kalibre eder.
        Önceden sadece final_score >= attack ise tüm trade ATTACK oluyordu.
        Artık ATTACK için çoklu dal teyidi ve non-core confirmation aranır.
        """
        winning = winning or []
        if not self.calib_enabled:
            if score >= self.t_attack: return "ATTACK"
            if score >= self.t_strong: return "STRONG"
            if score >= self.t_normal: return "NORMAL"
            if score >= self.t_scout:  return "SCOUT"
            return "WEAK"

        non_core_confirm = [v for v in winning
            if v.branch_name != "core_long"
            and v.score >= self.attack_min_confirm
            and (self.attack_allow_shadow_confirm or not v.shadow)]
        can_attack = (
            score >= self.attack_min_score
            and len(winning) >= self.attack_min_votes
            and (bool(non_core_confirm) or not self.attack_require_non_core)
        )
        if can_attack:
            return "ATTACK"

        # Core-only sinyaller artık ATTACK olamaz. En fazla STRONG/NORMAL.
        core_only = len([v for v in winning if v.branch_name != "core_long"]) == 0
        if score >= self.strong_min_score:
            return self.core_only_max_label if core_only else "STRONG"
        if score >= self.normal_min_score:
            return "NORMAL"
        if score >= self.t_scout:
            return "SCOUT"
        return "WEAK"

    def _calibrated_size_mult(self, base_size_mult: float, label: str, winning=None) -> float:
        """Etikete göre pozisyon boyutunu kontrollü düşürür."""
        winning = winning or []
        label = str(label or "NORMAL").upper()
        mult = float(base_size_mult or 1.0) * float(self.label_size_mults.get(label, 0.75))
        core_only = len([v for v in winning if v.branch_name != "core_long"]) == 0
        if core_only:
            mult *= self.core_only_size_mult
        return max(0.15, min(1.0, mult))

    def confirm_open(self, side: str):
        """Gerçek pozisyon açıldığında çağrılır (filtreler geçildikten sonra)."""
        self.risk.record_open(side)

    def on_close(self, symbol, side, pnl_usd, candle_ts: float = 0.0):
        self.risk.record_close(side)
        self.risk.record_trade_close(pnl_usd, candle_ts)

    def update_equity(self, equity): self.risk.update_equity(equity)
    def get_state(self):             return self.risk.get_state()

    def _make_packet(self, symbol, action, side, score, votes, reason,
                     sl_pct=0.02, size_mult=1.0, label="",
                     candle_ts: float = 0.0) -> DecisionPacket:
        return DecisionPacket(
            symbol=symbol, action=action, side=side,
            final_score=round(score, 2), size_mult=size_mult, sl_pct=sl_pct,
            reason=reason, branch_votes=votes,
            is_shadow=(action == Action.SHADOW),
            label=label, candle_ts=candle_ts)   # ← bilgisayar saati YOK

    # ── CSV ───────────────────────────────────────────────────────────
    def _init_csv_headers(self):
        if not self._dec_path.exists():
            self._w(self._dec_path,
                ["Tarih","DecisionID","Sembol","Action","Side","Score","Label","SizeMult","SL_Pct","Reason","Shadow"])
        if not self._shadow_path.exists():
            self._w(self._shadow_path,
                ["Tarih","DecisionID","Sembol","Side","Score","Label","SL_Pct","Reason"])
        if not self._votes_path.exists():
            self._w(self._votes_path,
                ["Tarih","Sembol","Dal","Action","Side","Score","Confidence","Shadow","Reason"])

    def _w(self, path, row):
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f, delimiter=";").writerow(row)

    def _ts_str(self, candle_ts: float) -> str:
        """candle_ts > 0 ise mum zamanını kullan, değilse bilgisayar saati."""
        ts = candle_ts if candle_ts > 0 else time.time()
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts))

    def _log_decision(self, p: DecisionPacket):
        ts = self._ts_str(p.candle_ts)
        try:
            with open(self._dec_path, "a", newline="", encoding="utf-8-sig") as f:
                csv.writer(f, delimiter=";").writerow([
                    ts, p.decision_id, p.symbol, p.action.value, p.side.value,
                    f"{p.final_score:.2f}", p.label,
                    f"{p.size_mult:.3f}", f"{p.sl_pct:.4f}", p.reason, int(p.is_shadow)])
        except Exception: pass

    def _log_shadow(self, p: DecisionPacket):
        ts = self._ts_str(p.candle_ts)
        try:
            with open(self._shadow_path, "a", newline="", encoding="utf-8-sig") as f:
                csv.writer(f, delimiter=";").writerow([
                    ts, p.decision_id, p.symbol, p.side.value,
                    f"{p.final_score:.2f}", p.label, f"{p.sl_pct:.4f}", p.reason])
        except Exception: pass

    def _log_votes(self, symbol, votes, candle_ts):
        ts = self._ts_str(candle_ts)
        try:
            with open(self._votes_path, "a", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                for name, v in votes.items():
                    w.writerow([ts, symbol, name, v.action.value, v.side.value,
                        f"{v.score:.2f}", f"{v.confidence:.3f}", int(v.shadow), v.reason])
        except Exception: pass

    def load_decisions(self, limit=200): return self._read(self._dec_path, limit)
    def load_shadows(self, limit=200):   return self._read(self._shadow_path, limit)
    def load_votes(self, limit=300):     return self._read(self._votes_path, limit)

    def _read(self, path, limit):
        if not path.exists(): return []
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                rows = [dict(r) for r in csv.DictReader(f, delimiter=";")]
            return rows[-limit:]
        except Exception: return []
