#!/usr/bin/env python3
"""Baut aus quotes.json die Schreibvorgänge für das Depot-Dashboard (ArtifactData-Batch).

Aufruf:
  python3 -I sync_dashboard.py --db DIR --quotes quotes.json --versions versions.json --out OUTDIR

DIR enthält die mit ArtifactData (out_dir) gespeicherten Dokumente:
  DIR/positions/*.json, DIR/quotes/latest.json, DIR/history/*.json, DIR/prices/*.json
versions.json: {"quotes/latest": 81, "history/2026-11-11": 2, "prices/A1JJTD": 42, ...}
Ergebnis: OUTDIR/writes.json (Liste für action "batch", Feld writes) und die Datendateien.
Schreibt selbst nichts in die Datenbank.
"""
import argparse
import glob
import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
MAX_POINTS = 48
MAX_RECENT = 2500
ENC_KEEP_RECENT_DAYS = 60


def load(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_dir(d):
    out = {}
    for p in glob.glob(os.path.join(d, "*.json")):
        doc = load(p)
        if isinstance(doc, dict):
            out[os.path.basename(p)[:-5]] = doc
    return out


def prev_weekday(d):
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def encode(rows):
    """[[YYYY-MM-DD, v], ...] -> 'start|0:v,dd:v,...' (dd = Tage seit dem vorigen Punkt)."""
    from datetime import date as _d
    # ausdünnen wie die übrigen Reihen: älter als 5 Jahre monatlich, 1–5 Jahre wöchentlich, danach täglich
    last = _d.fromisoformat(rows[-1][0])
    thin, keep_prev = [], None
    for i, (d_, v) in enumerate(rows):
        dd = _d.fromisoformat(d_)
        age = (last - dd).days
        gap = 30 if age > 5 * 365 else 7 if age > 365 else 1
        if keep_prev is None or (dd - keep_prev).days >= gap or i == len(rows) - 1:
            thin.append((d_, v))
            keep_prev = dd
    rows = thin
    out, prev = [], None
    for d_, v in rows:
        dd = _d.fromisoformat(d_)
        out.append(f"{0 if prev is None else (dd - prev).days}:{round(float(v), 4):g}")
        prev = dd
    return rows[0][0] + "|" + ",".join(out)


def enc_last_day(enc):
    if not enc or "|" not in enc:
        return None
    from datetime import date as _d
    d0, body = enc.split("|", 1)
    d = _d.fromisoformat(d0)
    for part in body.split(","):
        if ":" in part:
            d += timedelta(days=int(part.split(":")[0]))
    return d.isoformat()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--quotes", required=True)
    ap.add_argument("--versions", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--history", help="history.json mit Tagesschlusskursen (optional)")
    ap.add_argument("--intraday", help="intraday.json mit 30-Minuten-Kursen (optional)")
    ap.add_argument("--stocks", help="stocks.json mit Kursen der Fondsbestandteile (optional)")
    a = ap.parse_args()

    now = datetime.now(TZ)
    today = now.date()
    ltd = today if (today.weekday() < 5 and now.hour >= 9) else prev_weekday(today)
    stale_limit = prev_weekday(prev_weekday(ltd)).isoformat()  # älter als 2 Handelstage

    versions = load(a.versions, {}) or {}
    positions = load_dir(os.path.join(a.db, "positions"))
    latest = load(os.path.join(a.db, "quotes", "latest.json"), {}) or {}
    history = load_dir(os.path.join(a.db, "history"))
    prices_docs = load_dir(os.path.join(a.db, "prices"))
    gh = (load(a.quotes, {}) or {}).get("prices", {})

    old = dict(latest.get("prices") or {})
    new = dict(old)
    updated, warnings = [], []
    for pid in positions:
        q, o = gh.get(pid), old.get(pid) or {}
        if not q or not q.get("price") or not q.get("date"):
            continue
        if o.get("date") and q["date"] < o["date"]:
            continue
        if (o.get("date") == q["date"] and o.get("price") == q["price"]
                and o.get("prev") == q.get("prev")):
            continue
        if o.get("price") and abs(q["price"] / o["price"] - 1) > 0.15:
            warnings.append(f"{pid}: Kurs {q['price']} weicht >15 % von {o['price']} ab, nicht übernommen")
            continue
        e = {"price": q["price"], "date": q["date"], "venue": q.get("venue") or q.get("source")}
        if q.get("prev") is not None and q.get("prevDate"):
            e["prev"], e["prevDate"] = q["prev"], q["prevDate"]
        elif o.get("date") and q["date"] > o["date"]:
            e["prev"], e["prevDate"] = o.get("price"), o.get("date")
        else:
            e["prev"], e["prevDate"] = o.get("prev"), o.get("prevDate")
        new[pid] = e
        updated.append(pid)

    for pid in positions:
        d = (new.get(pid) or {}).get("date")
        if pid == "ETH" and today.weekday() >= 5:
            continue
        if not d or d < stale_limit:
            warnings.append(f"{pid}: Kurs vom {d or '–'} (älter als 2 Handelstage)")

    # Depotwert, Einstand, Bestände
    value = cost = 0.0
    pos = {}
    for pid, p in positions.items():
        sh = float(p.get("shares") or 0)
        pos[pid] = sh
        cost += float(p.get("cost") or 0)
        pr = (new.get(pid) or {}).get("price")
        if pr:
            value += sh * pr
    value, cost = round(value, 2), round(cost, 2)

    tkey = today.isoformat()
    hist_today = history.get(tkey)
    before = sorted(k for k in history if k < tkey)
    prev_doc = history[before[-1]] if before else None

    def price(pid):
        return (new.get(pid) or {}).get("price") or 0

    if hist_today and "flow" in hist_today:
        flow = float(hist_today.get("flow") or 0)
        old_pos = hist_today.get("pos") or {}
        if old_pos != pos:
            flow += sum((pos.get(i, 0) - old_pos.get(i, 0)) * price(i) for i in set(pos) | set(old_pos))
    elif prev_doc and prev_doc.get("pos"):
        pp = prev_doc["pos"]
        flow = sum((pos.get(i, 0) - pp.get(i, 0)) * price(i) for i in set(pos) | set(pp))
    else:
        flow = 0.0
    flow = round(flow, 2)

    points = list((hist_today or {}).get("points") or [])
    points.append({"t": now.isoformat(timespec="seconds"), "value": value})
    points = points[-MAX_POINTS:]
    hdoc = {"date": tkey, "value": value, "cost": cost, "pos": pos, "flow": flow, "points": points}
    if hist_today and "flowNote" in hist_today:
        hdoc["flowNote"] = hist_today["flowNote"]

    os.makedirs(a.out, exist_ok=True)
    out = os.path.abspath(a.out)
    writes = []

    def add(op, coll, doc_id, data):
        fp = os.path.join(out, f"{coll}__{doc_id}.json")
        with open(fp, "w") as f:
            json.dump(data, f, ensure_ascii=False)
        w = {"op": op, "collection": coll, "doc_id": doc_id, "file_path": fp}
        v = versions.get(f"{coll}/{doc_id}")
        if v:
            w["if_version"] = int(v)
        writes.append(w)

    add("set", "quotes", "latest", {"updatedAt": now.isoformat(timespec="seconds"), "prices": new})
    add("set", "history", tkey, hdoc)
    hist_series = ((load(a.history, {}) or {}).get("series") or {}) if a.history else {}
    for pid in positions:
        e = new.get(pid) if pid in updated else None
        series = [r for r in (hist_series.get(pid) or []) if isinstance(r, list) and len(r) == 2 and r[1]]
        if e is None and not series:
            continue
        doc = prices_docs.get(pid)
        enc = (doc or {}).get("enc") or ""
        data = {}
        cur = {r[0]: r[1] for r in ((doc or {}).get("recent") or []) if isinstance(r, list) and len(r) == 2}
        new_enc = None
        if not enc and len(series) > 30:
            # Fehlende Langfrist-Historie einmalig aus den Tagesschlusskursen bilden
            cut = (today - timedelta(days=ENC_KEEP_RECENT_DAYS)).isoformat()
            old_part = [r for r in series if r[0] < cut]
            if len(old_part) > 10:
                new_enc = encode(old_part)
                enc_end = old_part[-1][0]
        if new_enc is None:
            enc_end = enc_last_day(enc)
        # Tagesschlusskurse füllen Lücken und ersetzen Zwischenstände vergangener Tage
        for d_, v in series:
            if d_ < tkey and (enc_end is None or d_ > enc_end):
                cur[d_] = v
        if e:
            cur[e["date"]] = e["price"]
        recent = sorted([d_, v] for d_, v in cur.items() if enc_end is None or d_ > enc_end)[-MAX_RECENT:]
        old_recent = sorted([r[0], r[1]] for r in ((doc or {}).get("recent") or []) if isinstance(r, list) and len(r) == 2)
        if new_enc is not None:
            data["enc"] = new_enc
            data["source"] = ((doc or {}).get("source") or "Aktualisierung") + " + Tagesschlusskurse (GitHub)"
        if recent != old_recent or data:
            data["recent"] = recent
        if not data:
            continue
        if doc is None:
            add("set", "prices", pid, {"source": "Aktualisierung", "enc": data.get("enc", ""), "recent": recent})
        else:
            add("update", "prices", pid, data)

    # Intraday-Kurse (Wochen- und Tagesansicht), kompakt: "t0|dt:v,..." mit t in Minuten (Unix/60)
    if a.intraday:
        idoc = load(a.intraday, {}) or {}
        enc_i = {}
        for pid, rows in (idoc.get("series") or {}).items():
            rows = [r for r in rows if isinstance(r, list) and len(r) == 2 and r[1]]
            if not rows:
                continue
            t0, prev, parts = rows[0][0] // 60, None, []
            for t, v in rows:
                m = t // 60
                parts.append(f"{0 if prev is None else m - prev}:{v:g}")
                prev = m
            enc_i[pid] = f"{t0}|" + ",".join(parts)
        if enc_i:
            old_i = load(os.path.join(a.db, "intraday", "latest.json"), None)
            add("set", "intraday", "latest", {"asOf": idoc.get("asOf"), "series": enc_i, "src": idoc.get("src") or {}})
            if old_i is None:
                versions.pop("intraday/latest", None)
                writes[-1].pop("if_version", None)
            elif "if_version" not in writes[-1]:
                print("FEHLER: version fehlt in versions.json für intraday/latest")

    # Durchschau: Gewichte aus den Factsheets mit der Kursentwicklung seit dem Stichtag fortschreiben
    # w_heute = w_Stichtag × (Aktie heute / Aktie Stichtag) / (Fonds heute / Fonds Stichtag), alles in EUR
    if a.stocks:
        st = (load(a.stocks, {}) or {}).get("eur") or {}

        def at(rows, day):
            best = None
            for d_, v in rows:
                if d_ <= day:
                    best = v
                else:
                    break
            return best

        lt, base = {}, {}
        for pid, p in positions.items():
            hold, d0 = p.get("holdings") or [], p.get("holdingsAsOf")
            if not hold or not d0 or d0 == "-":
                continue
            fser = [r for r in (hist_series.get(pid) or []) if isinstance(r, list) and len(r) == 2]
            f0, f1 = at(fser, d0), (new.get(pid) or {}).get("price")
            if not f0 or not f1:
                continue
            rf = f1 / f0
            hw = []
            for h in hold:
                srow = st.get(h.get("n"))
                s0 = at(srow, d0) if srow else None
                w = h["w"] * (srow[-1][1] / s0) / rf if s0 else h["w"]
                hw.append({"n": h["n"], "w": round(w, 3), "w0": h["w"]})
            lt[pid], base[pid] = hw, d0
        if lt:
            old_l = load(os.path.join(a.db, "lookthrough", "latest.json"), None)
            add("set", "lookthrough", "latest", {"asOf": tkey, "base": base, "pos": lt,
                                                 "method": "Factsheet-Gewichte mit Kursentwicklung fortgeschrieben"})
            if old_l is None:
                writes[-1].pop("if_version", None)
            elif "if_version" not in writes[-1]:
                print("FEHLER: version fehlt in versions.json für lookthrough/latest")

    missing = [f"{w['collection']}/{w['doc_id']}" for w in writes if "if_version" not in w
               and ((w["collection"] == "quotes" and latest) or (w["collection"] == "history" and hist_today)
                    or (w["collection"] == "prices" and w["doc_id"] in prices_docs))]
    with open(os.path.join(out, "writes.json"), "w") as f:
        json.dump(writes, f, ensure_ascii=False, indent=1)

    print(f"Aktualisiert: {len(updated)} ({', '.join(updated) or '–'}); value {value}; flow {flow}")
    print(f"Schreibvorgänge: {len(writes)} -> {os.path.join(out, 'writes.json')}")
    if missing:
        print("FEHLER: version fehlt in versions.json für " + ", ".join(missing))
    for w in warnings:
        print("WARNUNG: " + w)


if __name__ == "__main__":
    main()
