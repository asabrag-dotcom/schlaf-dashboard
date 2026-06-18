#!/usr/bin/env python3
"""
Garmin-Aktivitaeten -> activities.json (fuer Polaris, Bereich Gesundheit > Sport).

Liest die kleinen Aktivitaets-Zusammenfassungs-CSVs aus dem Garmin/Health-Sync-Ordner
(z. B. "TRAINING 2026.02.18 19_37_18 Garmin.csv") und buendelt sie zu EINER activities.json.
Diese JSON wird neben die bestehende kpis.json veroeffentlicht (GitHub Pages); Polaris
liest sie per URL (lib/activities.ts, ENV ACTIVITIES_URL).

Aufruf:
    python garmin_activities_to_json.py --input ./aktivitaeten --output ./public/activities.json

Erkennung: Eine Datei gilt als Aktivitaets-CSV, wenn ihre Kopfzeile "Aktivitätstyp" enthaelt.
Puls-/RHF-/Schlaf-CSVs werden so automatisch ignoriert.
"""
import argparse
import csv
import io
import json
import os
from datetime import date, datetime

HEADER_MARKER = "Aktivitätstyp"


def parse_dt(raw: str):
    """'2026.02.18 19:37:18' -> ('2026-02-18', '19:37'). Tolerant gegenueber Varianten."""
    raw = (raw or "").strip()
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
        except ValueError:
            continue
    # nur Datum?
    for fmt in ("%Y.%m.%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d"), None
        except ValueError:
            continue
    return None, None


def to_int(v):
    try:
        return int(float(str(v).strip().replace(",", ".")))
    except (ValueError, TypeError):
        return 0


def to_float(v):
    try:
        return round(float(str(v).strip().replace(",", ".")), 3)
    except (ValueError, TypeError):
        return 0.0


def read_activity(path: str):
    with io.open(path, encoding="utf-8-sig", newline="") as f:
        head = f.readline()
        if HEADER_MARKER not in head:
            return None  # keine Aktivitaets-CSV
        f.seek(0)
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        # Spaltennamen koennen nachgestellte Leerzeichen haben -> normalisieren
        row = { (k or "").strip(): (v or "").strip() for k, v in r.items() }
        datum, zeit = parse_dt(row.get("Datum", ""))
        if not datum:
            continue
        name = row.get("Aktivitätsname", "")
        # Unsichtbare Zeichen (Zero-Width-Space, BOM) aus der Quelle entfernen
        name = name.replace("\u200b", "").replace("\ufeff", "").strip()
        out.append({
            "datum": datum,
            "zeit": zeit,
            "typ": row.get("Aktivitätstyp", "") or "Sonstiges",
            "name": None if name in ("", "null", "None") else name,
            "quelle": row.get("Quell-App", "") or "",
            "dauer_gesamt_s": to_int(row.get("Verstrichene Zeit")),
            "dauer_aktiv_s": to_int(row.get("Aktive Zeit")),
            "distanz_km": to_float(row.get("Entfernung (km)")),
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="Ordner mit den Aktivitaets-CSVs")
    ap.add_argument("--output", default="activities.json", help="Zielpfad der activities.json")
    args = ap.parse_args()

    acts = []
    for root, _dirs, files in os.walk(args.input):
        for fn in files:
            if not fn.lower().endswith(".csv"):
                continue
            try:
                parsed = read_activity(os.path.join(root, fn))
            except Exception as e:  # noqa: BLE001
                print(f"  uebersprungen ({fn}): {e}")
                parsed = None
            if parsed:
                acts.extend(parsed)

    # Deduplizieren nach (datum, zeit, typ); neueste zuerst
    seen = {}
    for a in acts:
        seen[(a["datum"], a["zeit"], a["typ"])] = a
    activities = sorted(seen.values(), key=lambda a: (a["datum"], a["zeit"] or ""), reverse=True)

    payload = {"stand": date.today().isoformat(), "aktivitaeten": activities}
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with io.open(args.output, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"{len(activities)} Aktivitaeten -> {args.output}")


if __name__ == "__main__":
    main()
