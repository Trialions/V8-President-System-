# symbols_builder.py — Binance'ten hacme gore top USDT cifti listesi
# Ag yoksa symbols_top70.json fallback'ine duser.
from __future__ import annotations
import json
import os
from typing import List


def build_top_usdt(top: int = 70, quote: str = "USDT",
                   exclude_stables: bool = True) -> List[str]:
    """24s hacme gore en yuksek USDT ciftlerini dondurur."""
    stables = {"USDCUSDT", "BUSDUSDT", "TUSDUSDT", "FDUSDUSDT", "DAIUSDT", "USDPUSDT"}
    try:
        import requests
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr", timeout=15)
        data = r.json()
        rows = [d for d in data if d.get("symbol", "").endswith(quote)]
        rows.sort(key=lambda d: float(d.get("quoteVolume", 0)), reverse=True)
        out = []
        for d in rows:
            s = d["symbol"]
            if exclude_stables and s in stables:
                continue
            if any(x in s for x in ("UP", "DOWN", "BULL", "BEAR")):
                continue
            out.append(s)
            if len(out) >= top:
                break
        if out:
            try:
                json.dump(out, open("symbols_top70.json", "w", encoding="utf-8"))
            except Exception:
                pass
            return out
    except Exception as e:
        print(f"[symbols_builder] canli liste alinamadi, fallback: {e}")

    # Fallback
    if os.path.exists("symbols_top70.json"):
        try:
            syms = json.load(open("symbols_top70.json", encoding="utf-8"))
            if isinstance(syms, list) and syms:
                return syms[:top]
        except Exception:
            pass
    return ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT",
            "DOGEUSDT","AVAXUSDT","LINKUSDT","DOTUSDT"][:top]


if __name__ == "__main__":
    syms = build_top_usdt(40)
    print(f"{len(syms)} sembol: {syms[:10]} ...")
