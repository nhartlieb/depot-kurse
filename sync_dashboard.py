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
MAX_RECENT = 600


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--quotes", required=True)
    ap.add_argument("--versions", required=True)
    ap.add_argument("--out", required=True)
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
    for pid in updated:
        e = new[pid]
        doc = prices_docs.get(pid)
        if doc is None:
            add("set", "prices", pid, {"source": "Aktualisierung", "recent": [[e["date"], e["price"]]], "enc": ""})
            continue
        recent = [r for r in (doc.get("recent") or []) if isinstance(r, list) and len(r) == 2]
        if recent and recent[-1][0] == e["date"]:
            recent[-1] = [e["date"], e["price"]]
        elif not recent or recent[-1][0] < e["date"]:
            recent.append([e["date"], e["price"]])
        add("update", "prices", pid, {"recent": recent[-MAX_RECENT:]})

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
