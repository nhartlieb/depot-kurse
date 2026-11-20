#!/usr/bin/env python3
"""Holt Intraday-Kurse (30 Min.) der letzten Handelstage und schreibt intraday.json.

intraday.json: {"asOf": ISO, "series": {id: [[unix_sekunden, kurs_eur], ...]}, "src": {id: quelle}, "errors": {...}}
Quellen: Yahoo Finance (30m), Ethereum über Coinbase (1h), US-Werte per EURUSD umgerechnet.
"""
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_quotes import SECURITIES, get_json, yahoo_symbols  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "intraday.json")
DAYS = 9
US_SYMBOL = {"A42SXH": "ANT"}  # Notierung in USD, wird umgerechnet
errors, src = {}, {}


def log(pid, msg):
    errors.setdefault(pid, []).append(str(msg)[:300])
    print(f"[{pid}] {msg}", file=sys.stderr)


def yahoo_bars(sym, interval="30m", rng="1mo"):
    d = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?"
                 + urllib.parse.urlencode({"interval": interval, "range": rng}))
    r = d["chart"]["result"][0]
    q = (r.get("indicators", {}).get("quote") or [{}])[0]
    closes = q.get("close") or []
    rows = [[int(t), round(float(c), 4)] for t, c in zip(r.get("timestamp") or [], closes) if c is not None]
    return (r["meta"].get("currency") or "").upper(), rows


def cut(rows):
    lim = int((datetime.now(timezone.utc) - timedelta(days=DAYS)).timestamp())
    return [x for x in rows if x[0] >= lim]


def main():
    series = {}
    fx = None
    for pid, isin in SECURITIES.items():
        got = None
        if pid in US_SYMBOL:
            try:
                if fx is None:
                    _, fx = yahoo_bars("EURUSD=X")
                cur, rows = yahoo_bars(US_SYMBOL[pid])
                if cur == "USD" and rows and fx:
                    out, j = [], 0
                    for t, v in rows:
                        while j + 1 < len(fx) and fx[j + 1][0] <= t:
                            j += 1
                        if fx[j][0] <= t:
                            out.append([t, round(v / fx[j][1], 4)])
                    got = (out, f"Yahoo {US_SYMBOL[pid]} (USD→EUR)")
            except Exception as e:  # noqa: BLE001
                log(pid, f"yahoo {US_SYMBOL[pid]}: {e}")
        if not got:
            for sym in yahoo_symbols(pid, isin)[:5]:
                try:
                    cur, rows = yahoo_bars(sym)
                    rows = cut(rows)
                    if cur == "EUR" and len(rows) >= 10:
                        got = (rows, f"Yahoo {sym}")
                        break
                    log(pid, f"{sym}: {cur}, {len(rows)} Werte")
                except Exception as e:  # noqa: BLE001
                    log(pid, f"{sym}: {e}")
                time.sleep(0.3)
        if got:
            series[pid] = cut(got[0])
            src[pid] = got[1]
            print(f"{pid}: {len(series[pid])} Werte ({got[1]})")
    try:
        end = datetime.now(timezone.utc)
        c = get_json("https://api.exchange.coinbase.com/products/ETH-EUR/candles?" + urllib.parse.urlencode(
            {"granularity": 3600, "start": (end - timedelta(days=DAYS)).isoformat(), "end": end.isoformat()}))
        series["ETH"] = sorted([int(x[0]) + 3600, round(float(x[4]), 2)] for x in c)
        src["ETH"] = "Coinbase 1h"
        print(f"ETH: {len(series['ETH'])} Werte")
    except Exception as e:  # noqa: BLE001
        log("ETH", f"coinbase: {e}")
    doc = {"asOf": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "series": series, "src": src, "errors": errors}
    with open(OUT, "w") as f:
        json.dump(doc, f, separators=(",", ":"))


if __name__ == "__main__":
    main()
