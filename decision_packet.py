# modules/decision_packet.py — v8.2
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Any
import time, uuid


class Action(Enum):
    OPEN   = "OPEN"
    BLOCK  = "BLOCK"
    WATCH  = "WATCH"
    SHADOW = "SHADOW"
    CLOSE  = "CLOSE"
    ROTATE = "ROTATE"


class Side(Enum):
    LONG  = "LONG"
    SHORT = "SHORT"
    NONE  = "NONE"


@dataclass
class BranchVote:
    branch_name: str
    action: Action          # OPEN | BLOCK | WATCH (asla SHADOW değil)
    side: Side
    score: float            # 0-100
    confidence: float       # 0.0-1.0 (zorunlu standart)
    reason: str
    shadow: bool = False    # True → bu dal güvenilmez; karar shadow kalır
    params: Dict[str, Any] = field(default_factory=dict)
    candle_ts: float = 0.0  # mum zamanı (epoch saniye)

    def __post_init__(self):
        # confidence her zaman 0-1 aralığında
        self.confidence = float(max(0.0, min(1.0, self.confidence)))
        self.score      = float(max(0.0, min(100.0, self.score)))


@dataclass
class DecisionPacket:
    symbol: str
    action: Action
    side: Side
    final_score: float
    size_mult: float
    sl_pct: float
    reason: str
    branch_votes: Dict[str, BranchVote] = field(default_factory=dict)
    is_shadow: bool = False
    label: str = ""
    candle_ts: float = 0.0   # mum zamanı — bilgisayar saati DEĞİL
    decision_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    extra: Dict[str, Any] = field(default_factory=dict)

    def winning_votes(self):
        return {k: v for k, v in self.branch_votes.items()
                if v.action == Action.OPEN and v.side == self.side}

    def to_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "symbol": self.symbol,
            "action": self.action.value,
            "side": self.side.value,
            "final_score": round(self.final_score, 2),
            "size_mult": round(self.size_mult, 3),
            "sl_pct": round(self.sl_pct, 4),
            "reason": self.reason,
            "label": self.label,
            "is_shadow": self.is_shadow,
            "candle_ts": self.candle_ts,
            "votes": {
                k: {"action": v.action.value, "side": v.side.value,
                    "score": round(v.score, 2), "confidence": round(v.confidence, 3),
                    "shadow": v.shadow, "reason": v.reason}
                for k, v in self.branch_votes.items()
            }
        }
