#!/usr/bin/env python3
"""Exportiert kpis.json (Schlaf + Gewicht) für das Polaris-Gesundheitsmodul.
Wiederverwendet die Auswertung aus process_health_data.py und gewicht_dashboard.py — keine doppelte Logik."""
import os, json
from datetime import datetime, timedelta
import process_health_data as ph
import gewicht_dashboard as gw

ZIEL_KG = 85.0        # Standard-Zielgewicht (wie im Gewichts-Dashboard)
KREATIN_ABZUG = 1.5   # Standard-Kreatin-Abzug (wie im Gewichts-Dashboard)


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def build_schlaf():
    nights = ph.load_all_nights()
    spo2 = ph.load_spo2_by_date()
    pulse = ph.load_pulse_by_date()
    if not nights:
        return None

    latest = nights[-1]
    m = latest["metrics"]
    s2 = spo2.get(latest["date"], {})
    p = pulse.get(latest["date"], {})

    score = 0
    score += m["duration_min"] >= 420
    score += m["efficiency"] >= 85
    score += 15 <= m["deep_pct"] <= 25
    score += m["rem_pct"] >= 18
    score += s2.get("min", 100) >= 90
    bewertung = "Gut" if score >= 4 else ("Mittel" if score >= 2 else "Schlecht")

    naechte_14 = []
    for n in nights[-14:]:
        nm = n["metrics"]
        ns2 = spo2.get(n["date"], {})
        npd = pulse.get(n["date"], {})
        naechte_14.append({
            "datum": n["date"], "dauer_min": round(nm["duration_min"]),
            "tief_pct": nm["deep_pct"], "rem_pct": nm["rem_pct"],
            "effizienz_pct": nm["efficiency"],
            "spo2_avg": ns2.get("avg"), "spo2_min": ns2.get("min"),
            "ruhepuls": npd.get("resting"),
        })
    naechte_14.reverse()

    best_rem = max(nights, key=lambda n: n["metrics"]["rem_min"])
    longest = max(nights, key=lambda n: n["metrics"]["duration_min"])
    shortest = min(nights, key=lambda n: n["metrics"]["duration_min"])

    return {
        "datum": latest["date"], "bewertung": bewertung,
        "dauer_min": round(m["duration_min"]), "effizienz_pct": m["efficiency"],
        "tief_min": round(m["deep_min"]), "tief_pct": m["deep_pct"],
        "rem_min": round(m["rem_min"]), "rem_pct": m["rem_pct"],
        "wach_episoden": m["awake_count"],
        "spo2_avg": s2.get("avg"), "spo2_min": s2.get("min"),
        "ruhepuls": p.get("resting"),
        "naechte_14": naechte_14,
        "gesamt": {
            "naechte": len(nights),
            "zeitraum": f"{nights[0]['date']} bis {nights[-1]['date']}",
            "eff_avg_pct": _avg([n["metrics"]["efficiency"] for n in nights]),
            "tief_avg_min": round(_avg([n["metrics"]["deep_min"] for n in nights]) or 0),
            "rem_avg_min": round(_avg([n["metrics"]["rem_min"] for n in nights]) or 0),
            "ruhepuls_avg": _avg([pulse[n["date"]]["resting"] for n in nights if n["date"] in pulse]),
            "spo2_avg": _avg([spo2[n["date"]]["avg"] for n in nights if n["date"] in spo2]),
            "beste_rem_min": round(best_rem["metrics"]["rem_min"]),
            "laengste_min": round(longest["metrics"]["duration_min"]),
            "kuerzeste_min": round(shortest["metrics"]["duration_min"]),
        },
    }


def build_gewicht():
    data = gw.load_weight_data()  # sortiert: [(datum, kg)]
    if not data:
        return None

    current_w = data[-1][1]
    current_date = data[-1][0]
    last_dt = datetime.strptime(current_date, "%Y-%m-%d")

    recent = [d for d in data if (last_dt - datetime.strptime(d[0], "%Y-%m-%d")).days <= 30]
    slope, _ = gw.linear_regression(recent if len(recent) >= 2 else data)
    slope_30 = round(slope * 30, 2)
    ma7 = gw.moving_average(data, 7)

    def weight_ago(days):
        target = last_dt - timedelta(days=days)
        cands = [d for d in data if abs((datetime.strptime(d[0], "%Y-%m-%d") - target).days) <= 3]
        if not cands:
            return None
        return min(cands, key=lambda d: abs((datetime.strptime(d[0], "%Y-%m-%d") - target).days))[1]

    def delta(ref):
        return round(current_w - ref, 1) if ref is not None else None

    messungen = []
    for (d, w), ma in zip(data[-20:], ma7[-20:]):
        messungen.append({
            "datum": d, "kg": round(w, 1), "bmi": gw.bmi(w),
            "avg7": round(ma, 1), "ohne_kreatin": round(w - KREATIN_ABZUG, 1),
        })
    messungen.reverse()  # neueste zuerst

    bmi_val = gw.bmi(current_w)
    bmi_klasse, _color = gw.bmi_category(bmi_val)

    return {
        "datum": current_date, "kg": round(current_w, 1),
        "bmi": bmi_val, "bmi_klasse": bmi_klasse, "groesse_cm": round(gw.HEIGHT_M * 100),
        "ziel_kg": ZIEL_KG, "rest_kg": round(current_w - ZIEL_KG, 1), "kreatin_abzug": KREATIN_ABZUG,
        "trend_30_kg": slope_30,
        "vs_7t": delta(weight_ago(7)), "vs_30t": delta(weight_ago(30)), "vs_90t": delta(weight_ago(90)),
        "messungen": messungen,
    }


def main():
    schlaf = build_schlaf()
    gewicht = build_gewicht()
    if not schlaf and not gewicht:
        print("Keine Daten — kpis.json wird nicht erzeugt.")
        return

    out = {"stand": datetime.now().strftime("%Y-%m-%d %H:%M")}
    if schlaf:
        out["schlaf"] = schlaf
    if gewicht:
        out["gewicht"] = gewicht

    out_path = os.path.join(ph.OUTPUT_DIR, "kpis.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"kpis.json geschrieben: {out_path}")


if __name__ == "__main__":
    main()
