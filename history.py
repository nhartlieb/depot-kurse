#!/usr/bin/env python3
"""Holt tägliche Schlusskurse (EUR) und schreibt history.json.

history.json: {"asOf": ISO, "series": {id: [[YYYY-MM-DD, close], ...]}, "errors": {...}}
- alle Positionen: die letzten ~420 Tage
- Positionen in FULL_IDS: komplette verfügbare Historie
Läuft höchstens alle 6 Stunden neu (sonst bleibt die Datei unverändert).
"""
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_quotes import SECURITIES, STOCKS, get_json, num, to_day, yahoo_symbols  # noqa: E402

TZ = ZoneInfo("Europe/Berlin")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.json")
FULL_IDS = {"A0F5UF", "A2N812", "A42SXH"}
RECENT_DAYS = 420
errors, debug = {}, {}


def log(pid, msg):
    errors.setdefault(pid, []).append(str(msg)[:300])
    print(f"[{pid}] {msg}", file=sys.stderr)


def clean(rows):
    d = {}
    for day, v in rows:
        if day and v is not None and v > 0:
            d[day] = round(float(v), 4)
    return sorted(d.items())


def h_justetf(isin, start):
    url = (f"https://www.justetf.com/api/etfs/{isin}/performance-chart?" + urllib.parse.urlencode({
        "locale": "de", "currency": "EUR", "valuesType": "MARKET_VALUE", "reduceData": "false",
        "includeDividends": "false", "features": "DIVIDENDS",
        "dateFrom": start.isoformat(), "dateTo": date.today().isoformat()}))
    d = get_json(url, {"Referer": f"https://www.justetf.com/de/etf-profile.html?isin={isin}"})
    ser = d.get("series") or []
    debug.setdefault("justetf_keys", sorted(d.keys()))
    return clean((to_day(p.get("date")), num(p.get("value"))) for p in ser)


def h_onvista(isin, start):
    s = get_json("https://api.onvista.de/api/v1/instruments/query?" + urllib.parse.urlencode({"searchValue": isin}))
    lst = s.get("list") or []
    inst = next((i for i in lst if i.get("isin") == isin), lst[0] if lst else None)
    et, ev = inst["entityType"], inst["entityValue"]
    snap = get_json(f"https://api.onvista.de/api/v1/instruments/{et}/{ev}/snapshot")
    ql = snap.get("quoteList")
    ql = ql.get("list") if isinstance(ql, dict) else ql
    # Börsenplätze mit EUR-Kursen, bevorzugt umsatzstarke
    pref = ["Lang & Schwarz", "LS Exchange", "Tradegate BSX", "gettex", "Xetra", "Frankfurt", "Stuttgart", "KVG"]
    mk = []
    for x in ql or []:
        m = x.get("market") or {}
        nid = x.get("idNotation") or m.get("idNotation")
        cur = (x.get("isoCurrency") or "EUR").upper()
        if nid and cur == "EUR":
            mk.append((pref.index(m.get("name")) if m.get("name") in pref else 99, m.get("name"), nid))
    mk.sort()
    debug[f"onvista_notations_{isin}"] = mk[:6]
    last_err = None
    start = max(start, date.today() - timedelta(days=365 * 5))
    for _, name, nid in mk[:4] or [(0, "default", None)]:
        try:
            q = {"idNotation": nid, "resolution": "1D", "startDate": start.isoformat(),
                 "endDate": date.today().isoformat()}
            h = get_json(f"https://api.onvista.de/api/v1/instruments/{et}/{ev}/chart_history?" + urllib.parse.urlencode(q))
            ts, last = h.get("datetimeLast") or [], h.get("last") or []
            rows = clean((to_day(t), num(v)) for t, v in zip(ts, last))
            if len(rows) >= 2:
                debug[f"onvista_market_{isin}"] = name
                return rows
            last_err = f"{name}: {len(rows)} Punkte, keys {sorted(h.keys())[:12]}"
        except Exception as e:  # noqa: BLE001
            last_err = f"{name}: {e}"
            log(isin, f"onvista {name}: {e}")
    raise ValueError(f"onvista: {last_err}")


def h_yahoo(pid, isin, start):
    years = max(1, (date.today() - start).days // 365 + 1)
    rng = "max" if years > 5 else f"{years}y"
    for sym in yahoo_symbols(pid, isin)[:4]:
        try:
            d = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d")
            r = d["chart"]["result"][0]
            if (r["meta"].get("currency") or "").upper() != "EUR":
                continue
            closes = r["indicators"]["quote"][0]["close"]
            rows = clean((to_day(t), c) for t, c in zip(r["timestamp"], closes))
            if len(rows) >= 2:
                return rows
        except Exception as e:  # noqa: BLE001
            log(pid, f"yahoo {sym}: {e}")
    raise ValueError("yahoo: nichts")


def h_eth(start):
    out = []
    end = datetime.now(timezone.utc)
    s = datetime.combine(start, datetime.min.time(), timezone.utc)
    while s < end:
        e = min(end, s + timedelta(days=290))
        c = get_json("https://api.exchange.coinbase.com/products/ETH-EUR/candles?" + urllib.parse.urlencode(
            {"granularity": 86400, "start": s.isoformat(), "end": e.isoformat()}))
        out += [(datetime.fromtimestamp(x[0], timezone.utc).date().isoformat(), x[4]) for x in c]
        s = e
        time.sleep(0.4)
    return clean(out)


def main():
    force = "--force" in sys.argv
    old = {}
    try:
        with open(OUT) as f:
            old = json.load(f)
    except Exception:  # noqa: BLE001
        pass
    if not force and old.get("asOf"):
        age = datetime.now(timezone.utc) - datetime.fromisoformat(old["asOf"])
        if age < timedelta(hours=6):
            print(f"history.json ist {age} alt – kein neuer Abruf")
            return
    series = dict(old.get("series") or {})
    recent_start = date.today() - timedelta(days=RECENT_DAYS)
    for pid, isin in list(SECURITIES.items()) + [("ETH", "ETH")]:
        start = date(2000, 1, 1) if pid in FULL_IDS else recent_start
        rows = None
        tries = ([("Coinbase", lambda: h_eth(recent_start))] if pid == "ETH" else
                 ([] if pid in STOCKS or pid == "A2N812" else [("justETF", lambda: h_justetf(isin, start))])
                 + [("onvista", lambda: h_onvista(isin, start)), ("Yahoo", lambda: h_yahoo(pid, isin, start))])
        for name, fn in tries:
            try:
                rows = fn()
                if rows and len(rows) >= 2:
                    print(f"{pid}: {len(rows)} Tage {rows[0][0]}..{rows[-1][0]} ({name})")
                    break
                log(pid, f"{name}: zu wenig Daten")
            except Exception as e:  # noqa: BLE001
                log(pid, f"{name}: {type(e).__name__}: {e}")
            time.sleep(0.5)
        if rows:
            series[pid] = rows
    doc = {"asOf": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "series": series, "errors": errors, "debug": debug}
    with open(OUT, "w") as f:
        json.dump(doc, f, separators=(",", ":"))


if __name__ == "__main__":
    main()
