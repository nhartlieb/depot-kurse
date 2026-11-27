#!/usr/bin/env python3
"""Tagesschlusskurse (in EUR) der größten Fondsbestandteile, damit die Gewichte
aus den Fonds-Factsheets mit der Kursentwicklung fortgeschrieben werden können.

stocks.json: {"asOf": ISO, "eur": {Name: [[YYYY-MM-DD, kurs_eur], ...]}, "sym": {Name: Symbol}, "errors": {...}}
Die Namen entsprechen den Namen in positions.holdings des Dashboards.
Läuft höchstens alle 6 Stunden neu.
"""
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_quotes import get_json, to_day  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stocks.json")
SYMBOLS = {
    "NVIDIA": "NVDA", "Apple": "AAPL", "Microsoft": "MSFT", "Alphabet": "GOOGL", "Amazon": "AMZN",
    "Broadcom": "AVGO", "Meta Platforms": "META", "Tesla": "TSLA", "AMD": "AMD",
    "Micron Technology": "MU", "TSMC": "2330.TW", "Samsung Electronics": "005930.KS",
    "SK hynix": "000660.KS", "Tencent": "0700.HK", "Alibaba": "9988.HK", "MediaTek": "2454.TW",
    "Delta Electronics": "2308.TW", "China Construction Bank": "0939.HK", "Hon Hai (Foxconn)": "2317.TW",
    "IREN": "IREN", "SanDisk": "SNDK", "Robinhood": "HOOD", "Marvell Technology": "MRVL",
    "Infineon": "IFX.DE", "Hinge Health": "HNGE", "Oscar Health": "OSCR",
    "Bitcoin": "BTC-EUR", "Ethereum": "ETH-EUR", "BNB": "BNB-EUR", "XRP": "XRP-EUR", "Solana": "SOL-EUR",
}
errors = {}


def closes(sym, rng="2y"):
    d = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?"
                 + urllib.parse.urlencode({"interval": "1d", "range": rng}))
    r = d["chart"]["result"][0]
    c = (r.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    rows = {}
    for t, v in zip(r.get("timestamp") or [], c):
        if v is not None:
            rows[to_day(t)] = float(v)
    return (r["meta"].get("currency") or "").upper(), sorted(rows.items())


def at(rows, day):
    """letzter Wert bis einschließlich day"""
    lo, hi, best = 0, len(rows) - 1, None
    while lo <= hi:
        m = (lo + hi) // 2
        if rows[m][0] <= day:
            best, lo = rows[m][1], m + 1
        else:
            hi = m - 1
    return best if best is not None else (rows[0][1] if rows else None)


def main():
    force = "--force" in sys.argv
    try:
        with open(OUT) as f:
            old = json.load(f)
        if not force and datetime.now(timezone.utc) - datetime.fromisoformat(old["asOf"]) < timedelta(hours=6):
            print("stocks.json ist aktuell – kein neuer Abruf")
            return
    except Exception:  # noqa: BLE001
        pass
    fx = {}
    eur = {}
    for name, sym in SYMBOLS.items():
        try:
            cur, rows = closes(sym)
            if not rows:
                raise ValueError("keine Kurse")
            if cur != "EUR":
                if cur not in fx:
                    _, fx[cur] = closes(f"EUR{cur}=X")
                rate = fx[cur]
                rows = [(d, v / at(rate, d)) for d, v in rows if at(rate, d)]
            eur[name] = [[d, round(v, 4)] for d, v in rows]
            print(f"{name}: {len(rows)} Tage ({sym}, {cur})")
        except Exception as e:  # noqa: BLE001
            errors[name] = f"{sym}: {e}"[:200]
            print(f"[{name}] {e}", file=sys.stderr)
        time.sleep(0.3)
    with open(OUT, "w") as f:
        json.dump({"asOf": datetime.now(timezone.utc).isoformat(timespec="seconds"), "eur": eur,
                   "sym": SYMBOLS, "errors": errors}, f, separators=(",", ":"))


if __name__ == "__main__":
    main()
