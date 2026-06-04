#!/usr/bin/env python3
"""Exportiert kpis.json (Schlaf) für das Polaris-Gesundheitsmodul.
Wiederverwendet die Auswertung aus process_health_data.py — keine doppelte Logik."""
import os, json
from datetime import datetime
import process_health_data as ph


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def main():
    nights = ph.load_all_nights()
    spo2 = ph.load_spo2_by_date()
    pulse = ph.load_pulse_by_date()
    if not nights:
        print("Keine Schlafdaten — kpis.json wird nicht erzeugt.")
        return

    latest = nights[-1]
    m = latest["metrics"]
    s2 = spo2.get(latest["date"], {})
    p = pulse.get(latest["date"], {})

    # einfache Gesamtbewertung (analog Briefing-Logik)
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
            "datum": n["date"],
            "dauer_min": round(nm["duration_min"]),
            "tief_pct": nm["deep_pct"],
            "rem_pct": nm["rem_pct"],
            "effizienz_pct": nm["efficiency"],
            "spo2_avg": ns2.get("avg"),
            "spo2_min": ns2.get("min"),
            "ruhepuls": npd.get("resting"),
        })
    naechte_14.reverse()  # neueste zuerst

    best_rem = max(nights, key=lambda n: n["metrics"]["rem_min"])
    longest = max(nights, key=lambda n: n["metrics"]["duration_min"])
    shortest = min(nights, key=lambda n: n["metrics"]["duration_min"])

    out = {
        "stand": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "schlaf": {
            "datum": latest["date"],
            "bewertung": bewertung,
            "dauer_min": round(m["duration_min"]),
            "effizienz_pct": m["efficiency"],
            "tief_min": round(m["deep_min"]),
            "tief_pct": m["deep_pct"],
            "rem_min": round(m["rem_min"]),
            "rem_pct": m["rem_pct"],
            "wach_episoden": m["awake_count"],
            "spo2_avg": s2.get("avg"),
            "spo2_min": s2.get("min"),
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
        },
        # "gewicht": {...}  ← folgt, sobald ich gewicht_dashboard.py habe
    }

    out_path = os.path.join(ph.OUTPUT_DIR, "kpis.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"kpis.json geschrieben: {out_path}")


if __name__ == "__main__":
    main()