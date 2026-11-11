#!/usr/bin/env python3
"""Holt aktuelle EUR-Kurse für die Depot-Positionen und schreibt quotes.json.

Quellen je Wertpapier (in dieser Reihenfolge, die mit dem neuesten Kursdatum gewinnt):
  justETF-API, onvista-API, Yahoo Finance.
Ethereum: Coinbase, Ersatz CoinGecko.
Nur Standardbibliothek, damit der GitHub-Lauf nichts installieren muss.
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "quotes.json")

# Dokument-id im Dashboard -> ISIN
SECURITIES = {
    "A1JJTD": "IE00B3YLTY66",  # SPDR MSCI ACWI IMI
    "A1JX52": "IE00B3RBWM25",  # Vanguard FTSE All-World (aussch.)
    "A1XB5U": "IE00BJ0KDQ92",  # Xtrackers MSCI World
    "A111X9": "IE00BKM4GZ66",  # iShares Core MSCI EM IMI
    "A0F5UF": "DE000A0F5UF5",  # iShares Nasdaq-100 (DE)
    "A2TT3D": "CH0445689208",  # 21Shares Crypto Basket
    "EWG2LD": "DE000EWG2LD7",  # EUWAX Gold II
    "A2N812": "DE000A2N8127",  # BIT Global Technology Leaders (Fonds)
}
# Bevorzugte Yahoo-Symbole (Xetra / Stuttgart); weitere werden per ISIN-Suche ergänzt
YAHOO_HINTS = {
    "A1JJTD": ["SPYI.DE"],
    "A1JX52": ["VGWL.DE"],
    "A1XB5U": ["XDWD.DE"],
    "A111X9": ["IS3N.DE"],
    "A0F5UF": ["EXXT.DE"],
}

errors = {}
debug = {}


def log(pid, msg):
    errors.setdefault(pid, []).append(msg[:300])
    print(f"[{pid}] {msg}", file=sys.stderr)


def get(url, headers=None, timeout=20):
    h = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    }
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def get_json(url, headers=None):
    return json.loads(get(url, headers))


def num(x):
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, dict):
        for k in ("raw", "value", "amount"):
            if k in x:
                return num(x[k])
        return None
    s = str(x).replace("\xa0", "").replace("€", "").replace("EUR", "").strip()
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def to_day(v):
    """Beliebige Datumsangabe -> 'YYYY-MM-DD' (Europe/Berlin)."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        t = v / 1000 if v > 1e11 else v
        return datetime.fromtimestamp(t, TZ).date().isoformat()
    s = str(v).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](.*))?$", s)
    if m:
        if m.group(4):
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.tzinfo:
                    return dt.astimezone(TZ).date().isoformat()
            except ValueError:
                pass
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{2,4})", s)
    if m:
        y = int(m.group(3))
        y = y + 2000 if y < 100 else y
        return date(y, int(m.group(2)), int(m.group(1))).isoformat()
    return None


def prev_weekday(d):
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def last_trading_day(now):
    d = now.date()
    if d.weekday() < 5 and now.hour >= 9:
        return d
    return prev_weekday(d)


def ok(q):
    return q and q.get("price") and q["price"] > 0 and q.get("date")


# ---------------------------------------------------------------- Quellen
def src_justetf(isin):
    url = (f"https://www.justetf.com/api/etfs/{isin}/quote?"
           + urllib.parse.urlencode({"locale": "de", "currency": "EUR", "isin": isin}))
    d = get_json(url, {"Referer": f"https://www.justetf.com/de/etf-profile.html?isin={isin}"})
    debug.setdefault("justetf_keys", sorted(d.keys()) if isinstance(d, dict) else str(type(d)))
    price = num(d.get("latestQuote"))
    qdate = to_day(d.get("latestQuoteDate"))
    prev = num(d.get("previousQuote"))
    pdate = to_day(d.get("previousQuoteDate"))
    venue = d.get("quoteTradingVenue") or d.get("tradingVenue") or "justETF"
    if isinstance(venue, dict):
        venue = venue.get("name") or "justETF"
    return {"price": price, "date": qdate, "prev": prev, "prevDate": pdate,
            "venue": str(venue), "source": "justETF"}


def src_onvista(isin):
    s = get_json("https://api.onvista.de/api/v1/instruments/query?"
                 + urllib.parse.urlencode({"searchValue": isin}))
    lst = s.get("list") or s.get("instruments") or []
    inst = next((i for i in lst if i.get("isin") == isin), lst[0] if lst else None)
    if not inst:
        raise ValueError("onvista: nichts gefunden")
    et, ev = inst.get("entityType"), inst.get("entityValue")
    snap = get_json(f"https://api.onvista.de/api/v1/instruments/{et}/{ev}/snapshot")
    q = snap.get("quote") or {}
    debug.setdefault("onvista_quote_keys", sorted(q.keys()))
    price = num(q.get("last"))
    qdate = to_day(q.get("datetimeLast"))
    prev = num(q.get("previousLast") or q.get("prevClose") or q.get("previousClose"))
    pdate = to_day(q.get("datetimePreviousLast") or q.get("datetimePrevClose"))
    if prev is None and q.get("performance") is not None and price:
        prev = round(price - float(q["performance"]), 6)
    cur = (q.get("isoCurrency") or "EUR").upper()
    if cur != "EUR":
        raise ValueError(f"onvista: Währung {cur}")
    market = (q.get("market") or {}).get("name") or "onvista"
    return {"price": price, "date": qdate, "prev": prev, "prevDate": pdate,
            "venue": market, "source": "onvista"}


def yahoo_symbols(pid, isin):
    syms = list(YAHOO_HINTS.get(pid, []))
    try:
        d = get_json("https://query2.finance.yahoo.com/v1/finance/search?"
                     + urllib.parse.urlencode({"q": isin, "quotesCount": 15, "newsCount": 0}))
        for q in d.get("quotes", []):
            s = q.get("symbol")
            if s and s not in syms:
                syms.append(s)
    except Exception as e:  # noqa: BLE001
        log(pid, f"yahoo-suche: {e}")
    order = {"DE": 0, "SG": 1, "F": 2, "MU": 3, "DU": 4, "BE": 5, "HM": 6, "HA": 6}
    return sorted(syms, key=lambda s: (s not in YAHOO_HINTS.get(pid, []),
                                       order.get(s.rsplit(".", 1)[-1] if "." in s else "", 9)))


def src_yahoo(pid, isin):
    last_err = None
    for sym in yahoo_symbols(pid, isin)[:4]:
        try:
            d = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=10d&interval=1d")
            r = d["chart"]["result"][0]
            m = r["meta"]
            if (m.get("currency") or "").upper() != "EUR":
                last_err = f"{sym}: Währung {m.get('currency')}"
                continue
            ts = r.get("timestamp") or []
            closes = (r.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
            pts = [(to_day(t), c) for t, c in zip(ts, closes) if c is not None]
            price = num(m.get("regularMarketPrice"))
            qdate = to_day(m.get("regularMarketTime")) or (pts[-1][0] if pts else None)
            before = [p for p in pts if qdate and p[0] < qdate]
            prev, pdate = (before[-1][1], before[-1][0]) if before else (None, None)
            return {"price": price, "date": qdate, "prev": num(prev), "prevDate": pdate,
                    "venue": f"Yahoo {sym}", "source": "Yahoo"}
        except Exception as e:  # noqa: BLE001
            last_err = f"{sym}: {e}"
    raise ValueError(f"yahoo: {last_err}")


def src_coinbase():
    spot = num(get_json("https://api.coinbase.com/v2/prices/ETH-EUR/spot")["data"]["amount"])
    today = datetime.now(TZ).date()
    prev = pdate = None
    try:
        candles = get_json("https://api.exchange.coinbase.com/products/ETH-EUR/candles?granularity=86400")
        # [time, low, high, open, close, volume], neueste zuerst, UTC-Tage
        y = (datetime.now(ZoneInfo("UTC")).date() - timedelta(days=1)).isoformat()
        for c in candles:
            if datetime.fromtimestamp(c[0], ZoneInfo("UTC")).date().isoformat() == y:
                prev, pdate = float(c[4]), (today - timedelta(days=1)).isoformat()
                break
    except Exception as e:  # noqa: BLE001
        log("ETH", f"coinbase-candles: {e}")
    return {"price": spot, "date": today.isoformat(), "prev": prev, "prevDate": pdate,
            "venue": "Coinbase", "source": "Coinbase"}


def src_coingecko():
    d = get_json("https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=eur"
                 "&include_24hr_change=true")["ethereum"]
    price = float(d["eur"])
    chg = d.get("eur_24h_change")
    today = datetime.now(TZ).date()
    prev = round(price / (1 + chg / 100), 2) if chg is not None else None
    return {"price": price, "date": today.isoformat(), "prev": prev,
            "prevDate": (today - timedelta(days=1)).isoformat() if prev else None,
            "venue": "CoinGecko", "source": "CoinGecko"}


# ---------------------------------------------------------------- Ablauf
def best_of(pid, attempts, target):
    best = None
    for name, fn in attempts:
        try:
            q = fn()
            if not ok(q):
                log(pid, f"{name}: kein gültiger Kurs ({q})")
                continue
            if best is None or q["date"] > best["date"]:
                best = q
            if q["date"] >= target:
                break
        except Exception as e:  # noqa: BLE001
            log(pid, f"{name}: {type(e).__name__}: {e}")
        time.sleep(0.5)
    return best


def main():
    now = datetime.now(TZ)
    target = last_trading_day(now).isoformat()
    try:
        with open(OUT) as f:
            old = json.load(f).get("prices", {})
    except Exception:  # noqa: BLE001
        old = {}

    prices = {}
    for pid, isin in SECURITIES.items():
        q = best_of(pid, [
            ("justETF", lambda i=isin: src_justetf(i)),
            ("onvista", lambda i=isin: src_onvista(i)),
            ("Yahoo", lambda p=pid, i=isin: src_yahoo(p, i)),
        ], target)
        prices[pid] = q
    prices["ETH"] = best_of("ETH", [("Coinbase", src_coinbase), ("CoinGecko", src_coingecko)],
                            now.date().isoformat())

    out = {}
    for pid, q in prices.items():
        o = old.get(pid)
        if not q:
            if o:
                out[pid] = o
            continue
        if o and o.get("date") and q["date"] < o["date"]:
            out[pid] = o  # nie mit älterem Kurs überschreiben
            continue
        if q.get("prev") is None or not q.get("prevDate"):
            if o and o.get("date") and q["date"] > o["date"]:
                q["prev"], q["prevDate"] = o.get("price"), o.get("date")
            elif o and o.get("date") == q["date"]:
                q["prev"], q["prevDate"] = o.get("prev"), o.get("prevDate")
        q["isin"] = SECURITIES.get(pid, pid)
        for k in ("price", "prev"):
            if isinstance(q.get(k), float):
                q[k] = round(q[k], 4)
        out[pid] = q

    doc = {
        "updatedAt": now.isoformat(timespec="seconds"),
        "lastTradingDay": target,
        "prices": out,
        "errors": errors,
        "debug": debug,
    }
    with open(OUT, "w") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1, sort_keys=True)
    fresh = sum(1 for q in out.values() if q.get("date", "") >= target)
    print(f"{fresh}/{len(out)} Kurse vom {target}")
    for pid, q in sorted(out.items()):
        print(f"  {pid:7} {q.get('price')!s:>10} {q.get('date')} prev {q.get('prev')} "
              f"({q.get('prevDate')}) {q.get('source')}/{q.get('venue')}")


if __name__ == "__main__":
    main()
