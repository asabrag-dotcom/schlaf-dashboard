#!/usr/bin/env python3
"""
Schlaf-Briefing Generator
Liest Schlafdaten aus lokalen Health-Sync-CSV-Dateien und generiert
ein tägliches HTML-Briefing, das via GitHub API zu GitHub Pages gepusht wird.
"""

import os
import glob
import json
import csv
import base64
import re
import sys
import platform
import urllib.request
import urllib.error
from datetime import datetime, timedelta


# ── 1. Pfade dynamisch ermitteln ─────────────────────────────────────────────

WINDOWS_DRIVE_BASE = r'C:\Users\bjoer\Documents\Drive_Gropperstr'

def find_drive_base():
    """
    Findet den Drive-Datenordner:
    - GitHub Actions: DRIVE_DATA_PATH (von download_drive.py befüllt)
    - Windows: lokaler Google-Drive-Spiegel
    - Cowork Linux-VM: dynamischer Mount-Pfad
    """
    # GitHub Actions
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        path = os.environ.get('DRIVE_DATA_PATH', '/tmp/drive_data')
        if os.path.isdir(path):
            return path
        raise FileNotFoundError(f"DRIVE_DATA_PATH nicht gefunden: {path}")
    # Windows
    if platform.system() == 'Windows':
        if os.path.isdir(WINDOWS_DRIVE_BASE):
            return WINDOWS_DRIVE_BASE
        raise FileNotFoundError(f"Ordner nicht gefunden: {WINDOWS_DRIVE_BASE}")
    # Linux (Cowork VM)
    patterns = glob.glob('/sessions/*/mnt/Drive_Gropperstr')
    if patterns:
        return sorted(patterns)[-1]
    raise FileNotFoundError(
        "Drive_Gropperstr nicht gemountet.\n"
        "Bitte den Ordner C:\\Users\\bjoer\\Documents\\Drive_Gropperstr "
        "in Cowork über 'Ordner auswählen' freigeben."
    )


DRIVE_BASE = find_drive_base()
SCHLAF_DIR = os.path.join(DRIVE_BASE, 'Health Sync Schlaf')
SPO2_DIR   = os.path.join(DRIVE_BASE, 'Health Sync Sauerstoffsättigung')
PULS_DIR   = os.path.join(DRIVE_BASE, 'Health Sync Puls')

# Output: in GitHub Actions ins Repo-Verzeichnis, sonst in Schlaf_Briefing
if os.environ.get('GITHUB_ACTIONS') == 'true':
    OUTPUT_DIR = os.environ.get('OUTPUT_DIR', os.getcwd())
else:
    OUTPUT_DIR = os.path.join(DRIVE_BASE, 'Schlaf_Briefing')

# Config nur lokal laden (nicht in GitHub Actions)
if os.environ.get('GITHUB_ACTIONS') != 'true':
    CONFIG_PATH = os.path.join(SCHLAF_DIR, 'schlaf_config.json')
    with open(CONFIG_PATH, encoding='utf-8') as f:
        CONFIG = json.load(f)
    GITHUB = CONFIG['github']
else:
    CONFIG = {}
    GITHUB = {}


# ── 2. CSV-Hilfsfunktionen ───────────────────────────────────────────────────

def find_csv_for_date(directory, date_str):
    """
    Sucht CSV-Dateien für ein bestimmtes Datum (Format: YYYY.MM.DD).
    Bevorzugt Health Connect, ignoriert Duplikate mit (1)/(2).
    """
    pattern = os.path.join(directory, f'*{date_str}*.csv')
    files = glob.glob(pattern)
    # Duplikate ausschließen
    files = [f for f in files if not re.search(r'\(\d+\)', os.path.basename(f))]
    if not files:
        return None
    # Health Connect bevorzugen
    hc = [f for f in files if 'Health Connect' in f]
    return hc[0] if hc else files[0]


def load_csv(filepath):
    rows = []
    with open(filepath, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


# ── 3. Schlafdaten verarbeiten ───────────────────────────────────────────────

def parse_sleep(rows):
    """Berechnet alle KPIs aus den Schlaf-CSV-Zeilen."""
    if not rows:
        return None

    durations = {'light': 0, 'deep': 0, 'rem': 0, 'awake': 0}
    awake_count = 0

    for row in rows:
        stage = row.get('Schlafstadium', '').lower().strip()
        try:
            secs = int(row.get('Durée en secondes', 0))
        except ValueError:
            continue
        if stage in durations:
            durations[stage] += secs
        if stage == 'awake':
            awake_count += 1

    sleep_secs = durations['light'] + durations['deep'] + durations['rem']
    total_secs = sleep_secs + durations['awake']
    if sleep_secs == 0:
        return None

    # Schlafbeginn / -ende
    start_dt = end_dt = None
    try:
        first = rows[0]
        last  = rows[-1]
        fmt = '%Y.%m.%d %H:%M:%S'
        start_dt = datetime.strptime(f"{first['Datum']} {first['Zeit']}", fmt)
        last_start = datetime.strptime(f"{last['Datum']} {last['Zeit']}", fmt)
        end_dt = last_start + timedelta(seconds=int(last.get('Durée en secondes', 0)))
    except Exception:
        pass

    efficiency  = round(sleep_secs / total_secs * 100, 1) if total_secs else 0
    deep_pct    = round(durations['deep'] / sleep_secs * 100, 1)
    rem_pct     = round(durations['rem']  / sleep_secs * 100, 1)

    def fmt_dur(s):
        h, m = divmod(int(s) // 60, 60)
        return f"{h}h {m:02d}m"

    return {
        'duration_min': sleep_secs / 60,
        'duration_str': fmt_dur(sleep_secs),
        'efficiency':   efficiency,
        'deep_min':     durations['deep'] / 60,
        'deep_pct':     deep_pct,
        'deep_str':     fmt_dur(durations['deep']),
        'rem_min':      durations['rem'] / 60,
        'rem_pct':      rem_pct,
        'rem_str':      fmt_dur(durations['rem']),
        'awake_count':  awake_count,
        'start_time':   start_dt.strftime('%H:%M') if start_dt else '?',
        'end_time':     end_dt.strftime('%H:%M')   if end_dt   else '?',
        'start_dt':     start_dt,
        'end_dt':       end_dt,
    }


def parse_spo2(rows, sleep_start, sleep_end):
    """SpO₂-Durchschnitt und -Minimum während des Schlafs."""
    if not rows:
        return None, None

    # Spaltennamen ermitteln
    spo2_col = None
    for key in rows[0].keys():
        if any(x in key.lower() for x in ['sat', 'ox', 'sauerstoff']):
            spo2_col = key
            break
    if not spo2_col:
        return None, None

    values = []
    for row in rows:
        try:
            val = float(row[spo2_col])
        except (ValueError, KeyError):
            continue
        if sleep_start and sleep_end:
            try:
                ts = datetime.strptime(f"{row['Datum']} {row['Zeit']}", '%Y.%m.%d %H:%M:%S')
                window_end = sleep_end + timedelta(hours=1)
                if not (sleep_start <= ts <= window_end):
                    continue
            except Exception:
                pass
        values.append(val)

    if not values:
        return None, None
    return round(sum(values) / len(values), 1), round(min(values), 1)


def get_resting_hr(rows):
    """Ruhepuls = niedrigster Wert aus der RHF-CSV."""
    values = []
    for row in rows:
        try:
            values.append(int(float(row.get('Puls', 0))))
        except (ValueError, KeyError):
            pass
    return min(values) if values else None


# ── 4. Qualitätsbewertung & Empfehlungen ────────────────────────────────────

def quality_badge(m):
    score = 0
    if m['duration_min'] >= 420: score += 1
    elif m['duration_min'] >= 360: score += 0.5
    if m['efficiency'] >= 90: score += 1
    elif m['efficiency'] >= 85: score += 0.5
    if 15 <= m['deep_pct'] <= 30: score += 1
    elif m['deep_pct'] > 10: score += 0.5
    if m['rem_pct'] >= 20: score += 1
    elif m['rem_pct'] >= 10: score += 0.5
    if m['awake_count'] <= 2: score += 1
    elif m['awake_count'] <= 4: score += 0.5
    ratio = score / 5
    if ratio >= 0.85: return "Sehr gut",    "#48bb78"
    if ratio >= 0.65: return "Gut",         "#48bb78"
    if ratio >= 0.40: return "Befriedigend","#ed8936"
    return "Schlecht", "#fc8181"


def befund(m, spo2_avg, spo2_min):
    issues, oks = [], []
    if m['duration_min'] < 360:
        issues.append(f"⚠️ Deutlich verkürzte Schlafdauer ({m['duration_str']}; Empfehlung: 7–9 Std.)")
    elif m['duration_min'] < 420:
        issues.append(f"⚠️ Verkürzte Schlafdauer ({m['duration_str']}; Empfehlung: 7–9 Std.)")
    else:
        oks.append(f"✓ Ausreichende Schlafdauer ({m['duration_str']})")

    if m['efficiency'] < 85:
        issues.append(f"⚠️ Niedrige Schlafeffizienz ({m['efficiency']}%; Norm: ≥85%)")
    else:
        oks.append(f"✓ Hohe Schlafeffizienz ({m['efficiency']}%)")

    if m['deep_pct'] < 10:
        issues.append(f"⚠️ Stark reduzierter Tiefschlaf ({m['deep_pct']}%; Norm: 15–25%)")
    elif m['deep_pct'] < 15:
        issues.append(f"⚠️ Reduzierter Tiefschlaf ({m['deep_pct']}%; Norm: 15–25%)")
    else:
        oks.append(f"✓ Ausreichender Tiefschlaf ({m['deep_pct']}%)")

    if m['rem_pct'] < 10:
        issues.append(f"⚠️ REM-Schlaf stark reduziert ({m['rem_pct']}%; Norm: 20–25%) – beeinträchtigt emotionale Verarbeitung und kognitive Leistung")
    elif m['rem_pct'] < 20:
        issues.append(f"⚠️ REM-Schlaf reduziert ({m['rem_pct']}%; Norm: 20–25%) – beeinträchtigt emotionale Verarbeitung und kognitive Leistung")
    else:
        oks.append(f"✓ Ausreichender REM-Schlaf ({m['rem_pct']}%)")

    if spo2_min is not None:
        if spo2_min < 90:
            issues.append(f"⚠️ SpO₂-Minima unter 90% ({spo2_min}%) – kurzfristige Entsättigungen, Abklärung empfohlen")
        elif spo2_min < 93:
            issues.append(f"⚠️ SpO₂-Minima leicht erniedrigt ({spo2_min}%)")

    if m['awake_count'] > 5:
        issues.append(f"⚠️ Häufige Wachphasen ({m['awake_count']} Episoden)")

    return issues, oks


def recommendations(m, spo2_min):
    recs = []
    if m['rem_pct'] < 20:
        recs.append({
            'title': '💡 REM-Schlaf fördern',
            'text':  f"Ihre REM-Phasen waren mit {m['rem_pct']}% reduziert. REM tritt vor allem in den frühen Morgenstunden auf. Vermeiden Sie Alkohol und spätes Essen – beides unterdrückt den REM-Schlaf nachweislich. Regelmäßige Aufwachzeiten stabilisieren den REM-Anteil."
        })
    if spo2_min is not None and spo2_min < 90:
        recs.append({
            'title': '💡 Schlafbezogene Atmungsstörung ausschließen',
            'text':  f"Wiederholte SpO₂-Minima unter 90% (heute: {spo2_min}%) können auf eine Schlafapnoe hindeuten. Ich empfehle eine Abklärung beim HNO-Arzt oder Pneumologen, ggf. eine ambulante Polygraphie."
        })
    if m['duration_min'] < 420:
        recs.append({
            'title': '💡 Schlafdauer verlängern',
            'text':  f"Ihre Schlafdauer von {m['duration_str']} liegt unter der empfohlenen Schlafdauer von 7–9 Stunden. Versuchen Sie, früher ins Bett zu gehen oder den Wecker etwas zu verzögern."
        })
    if m['deep_pct'] < 15:
        recs.append({
            'title': '💡 Tiefschlaf verbessern',
            'text':  "Regelmäßige körperliche Aktivität (nicht kurz vor dem Schlafengehen), Alkoholverzicht und eine konstante Schlafzeit fördern den Tiefschlaf."
        })
    if not recs:
        recs.append({
            'title': '💡 Schlafqualität aufrechterhalten',
            'text':  "Ihre Schlafdaten zeigen eine gute Schlafqualität. Behalten Sie Ihre Schlafgewohnheiten bei: konstante Schlaf- und Aufwachzeiten, dunkles und kühles Schlafzimmer."
        })
    return recs


# ── 5. Historische Nächte laden ──────────────────────────────────────────────

def load_history(n=14):
    all_csvs = glob.glob(os.path.join(SCHLAF_DIR, '*.csv'))
    # Datum extrahieren, Duplikate ausschließen
    date_map = {}
    for f in all_csvs:
        base = os.path.basename(f)
        if re.search(r'\(\d+\)', base):
            continue
        if 'config' in base.lower():
            continue
        m = re.search(r'(\d{4}\.\d{2}\.\d{2})', base)
        if not m:
            continue
        ds = m.group(1)
        # Health Connect bevorzugen
        if ds not in date_map or 'Health Connect' in base:
            date_map[ds] = f

    nights = []
    for ds in sorted(date_map.keys(), reverse=True)[:n]:
        rows = load_csv(date_map[ds])
        met  = parse_sleep(rows)
        if not met:
            continue

        s2f = find_csv_for_date(SPO2_DIR, ds)
        spo2_avg = spo2_min = None
        if s2f:
            spo2_avg, spo2_min = parse_spo2(load_csv(s2f), met['start_dt'], met['end_dt'])

        pf = find_csv_for_date(PULS_DIR, ds)
        hr = None
        if pf:
            hr = get_resting_hr(load_csv(pf))

        try:
            date_fmt = datetime.strptime(ds, '%Y.%m.%d').strftime('%Y-%m-%d')
        except Exception:
            date_fmt = ds

        nights.append({'date': date_fmt, 'metrics': met, 'spo2_avg': spo2_avg, 'spo2_min': spo2_min, 'hr': hr})
    return nights


# ── 6. HTML generieren ───────────────────────────────────────────────────────

def generate_html(date_label, m, spo2_avg, spo2_min, hr, nights):
    badge_text, badge_color = quality_badge(m)
    issues, oks  = befund(m, spo2_avg, spo2_min)
    recs         = recommendations(m, spo2_min)
    now_str      = datetime.now().strftime('%d.%m.%Y %H:%M')

    # Befund-Liste
    befund_html = ''.join(f'<li class="issue">{i}</li>' for i in issues)
    befund_html += ''.join(f'<li class="ok">{o}</li>' for o in oks)

    apnoe_html = ''
    if spo2_min is not None and spo2_min < 90:
        apnoe_html = (
            "<div class='apnoe-warning'>⚠️ <strong>Hinweis:</strong> "
            "Wiederholte Entsättigungen (SpO₂ &lt; 90%) können auf eine "
            "schlafbezogene Atmungsstörung (z.B. obstruktive Schlafapnoe) hinweisen. "
            "Eine schlafmedizinische Abklärung wird empfohlen.</div>"
        )

    # Trend 7 Nächte (nights[0] = heute, also nights[1:8] = letzte 7)
    recent = nights[1:8] if len(nights) > 1 else []
    if recent:
        def avg(lst): return sum(lst) / len(lst) if lst else 0
        a_dur  = avg([n['metrics']['duration_min'] for n in recent])
        a_eff  = avg([n['metrics']['efficiency']   for n in recent])
        a_deep = avg([n['metrics']['deep_pct']     for n in recent])
        a_rem  = avg([n['metrics']['rem_pct']      for n in recent])
        a_spo2_vals = [n['spo2_avg'] for n in recent if n['spo2_avg']]
        a_spo2 = avg(a_spo2_vals) if a_spo2_vals else None

        def dcls(val, ref, higher=True):
            d = val - ref
            if abs(d) < 0.5: return 'neutral'
            return 'better' if (d > 0) == higher else 'worse'

        def dstr(val, ref):
            d = val - ref
            return f"{'↑' if d >= 0 else '↓'} {abs(round(d, 1))}"

        trend_rows = (
            f"<tr><td>Schlafdauer (min)</td><td>{round(m['duration_min'],1)}</td>"
            f"<td>{round(a_dur,1)}</td>"
            f"<td class='{dcls(m['duration_min'],a_dur)}'>{dstr(m['duration_min'],a_dur)}</td></tr>"

            f"<tr><td>Effizienz</td><td>{m['efficiency']}%</td>"
            f"<td>{round(a_eff,1)}%</td>"
            f"<td class='{dcls(m['efficiency'],a_eff)}'>{dstr(m['efficiency'],a_eff)}%</td></tr>"

            f"<tr><td>Tiefschlaf</td><td>{m['deep_pct']}%</td>"
            f"<td>{round(a_deep,1)}%</td>"
            f"<td class='{dcls(m['deep_pct'],a_deep)}'>{dstr(m['deep_pct'],a_deep)}%</td></tr>"

            f"<tr><td>REM-Anteil</td><td>{m['rem_pct']}%</td>"
            f"<td>{round(a_rem,1)}%</td>"
            f"<td class='{dcls(m['rem_pct'],a_rem)}'>{dstr(m['rem_pct'],a_rem)}%</td></tr>"
        )
        if spo2_avg and a_spo2:
            trend_rows += (
                f"<tr><td>SpO₂ Ø</td><td>{spo2_avg}%</td>"
                f"<td>{round(a_spo2,1)}%</td>"
                f"<td class='{dcls(spo2_avg,a_spo2)}'>{dstr(spo2_avg,a_spo2)}%</td></tr>"
            )
    else:
        trend_rows = '<tr><td colspan="4" style="color:var(--muted)">Nicht genug historische Daten</td></tr>'

    # Empfehlungen
    recs_html = ''.join(
        f'<div class="rec-card"><div class="rec-title">{r["title"]}</div>'
        f'<div class="rec-text">{r["text"]}</div></div>'
        for r in recs
    )

    # 14-Nächte-Tabelle
    def eff_badge(e):
        cls = 'badge-good' if e >= 90 else ('badge-warn' if e >= 85 else 'badge-bad')
        return f'<span class="badge {cls}">{e}%</span>'

    hist_rows = ''
    for n in nights:
        nm = n['metrics']
        s  = f"{n['spo2_avg']}% / {n['spo2_min']}%" if n['spo2_avg'] else '–'
        h  = str(n['hr']) if n['hr'] else '–'
        hist_rows += (
            f"<tr><td>{n['date']}</td><td>{nm['duration_str']}</td>"
            f"<td>{nm['deep_pct']}%</td><td>{nm['rem_pct']}%</td>"
            f"<td>{eff_badge(nm['efficiency'])}</td><td>{s}</td><td>{h}</td></tr>"
        )

    spo2_disp  = f"{spo2_avg}% / {spo2_min}%" if spo2_avg else '–'
    spo2_color = '#48bb78' if (spo2_min is None or spo2_min >= 93) else ('#ed8936' if spo2_min >= 90 else '#fc8181')
    hr_disp    = f"{hr} bpm" if hr else '–'
    hr_sub     = f"Ø {hr} bpm" if hr else '–'

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Schlaf-Briefing {date_label} – Björn</title>
<style>
  :root {{
    --bg:#0f1117; --card:#1a1d27; --card2:#22263a;
    --text:#e8eaf0; --muted:#8892a4; --accent:#5b8dee;
    --border:#2d3148; --good:#48bb78; --warn:#ed8936; --bad:#fc8181;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; max-width:900px; margin:0 auto; padding:24px 16px; }}
  header {{ margin-bottom:24px; }}
  header h1 {{ font-size:1.5rem; font-weight:700; }}
  header .sub {{ color:var(--muted); font-size:0.85rem; margin-top:4px; }}
  .note-badge {{ display:inline-block; padding:6px 18px; border-radius:20px; font-size:1rem; font-weight:700; color:#fff; margin-top:10px; background:{badge_color}; }}
  .kpis {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(140px,1fr)); gap:12px; margin:20px 0; }}
  .kpi {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:14px; }}
  .kpi-label {{ font-size:0.7rem; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; margin-bottom:5px; }}
  .kpi-value {{ font-size:1.25rem; font-weight:700; }}
  .kpi-sub {{ font-size:0.7rem; color:var(--muted); margin-top:3px; }}
  section {{ background:var(--card); border:1px solid var(--border); border-radius:12px; padding:20px; margin-bottom:16px; }}
  section h2 {{ font-size:0.95rem; font-weight:700; color:var(--accent); text-transform:uppercase; letter-spacing:.05em; margin-bottom:14px; }}
  ul {{ list-style:none; padding:0; display:flex; flex-direction:column; gap:8px; }}
  li.issue {{ color:var(--bad); font-size:0.9rem; }}
  li.ok    {{ color:var(--good); font-size:0.9rem; }}
  .rec-card {{ background:var(--card2); border-radius:8px; padding:14px; margin-bottom:10px; }}
  .rec-title {{ font-weight:700; font-size:0.9rem; margin-bottom:6px; }}
  .rec-text {{ font-size:0.85rem; color:var(--muted); line-height:1.5; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.82rem; }}
  th {{ text-align:left; padding:8px 10px; color:var(--muted); font-weight:600; font-size:0.72rem; text-transform:uppercase; border-bottom:1px solid var(--border); }}
  td {{ padding:8px 10px; border-bottom:1px solid var(--border); }}
  tr:hover td {{ background:var(--card2); }}
  td.better {{ color:var(--good); }}
  td.worse  {{ color:var(--bad); }}
  td.neutral{{ color:var(--muted); }}
  .badge {{ display:inline-block; padding:2px 7px; border-radius:8px; font-size:0.72rem; font-weight:600; }}
  .badge-good {{ background:rgba(72,187,120,.2); color:var(--good); }}
  .badge-warn {{ background:rgba(237,137,54,.2);  color:var(--warn); }}
  .badge-bad  {{ background:rgba(252,129,129,.2); color:var(--bad); }}
  .apnoe-warning {{ background:rgba(252,129,129,.1); border:1px solid var(--bad); border-radius:8px; padding:14px; margin-top:12px; font-size:0.88rem; color:var(--bad); }}
  footer {{ color:var(--muted); font-size:0.75rem; text-align:center; margin-top:24px; padding-top:12px; border-top:1px solid var(--border); }}
</style>
</head>
<body>
<header>
  <h1>🌙 Schlaf-Briefing</h1>
  <div class="sub">Björn · Nacht vom {date_label} · Erstellt am {now_str}</div>
  <div class="note-badge">{badge_text}</div>
</header>

<div class="kpis">
  <div class="kpi" style="border-top:3px solid var(--accent)">
    <div class="kpi-label">Schlafdauer</div>
    <div class="kpi-value">{m['duration_str']}</div>
    <div class="kpi-sub">{m['start_time']} – {m['end_time']}</div>
  </div>
  <div class="kpi" style="border-top:3px solid #5b8dee">
    <div class="kpi-label">Effizienz</div>
    <div class="kpi-value">{m['efficiency']}%</div>
    <div class="kpi-sub">Norm: ≥85%</div>
  </div>
  <div class="kpi" style="border-top:3px solid #3d63dd">
    <div class="kpi-label">Tiefschlaf</div>
    <div class="kpi-value">{m['deep_str']}</div>
    <div class="kpi-sub">{m['deep_pct']}% (Norm: 15–25%)</div>
  </div>
  <div class="kpi" style="border-top:3px solid #9f7aea">
    <div class="kpi-label">REM-Schlaf</div>
    <div class="kpi-value">{m['rem_str']}</div>
    <div class="kpi-sub">{m['rem_pct']}% (Norm: 20–25%)</div>
  </div>
  <div class="kpi" style="border-top:3px solid #48bb78">
    <div class="kpi-label">Wach-Episoden</div>
    <div class="kpi-value">{m['awake_count']}</div>
    <div class="kpi-sub">Unterbrechungen</div>
  </div>
  <div class="kpi" style="border-top:3px solid {spo2_color}">
    <div class="kpi-label">SpO₂ Ø / Min</div>
    <div class="kpi-value">{spo2_disp}</div>
    <div class="kpi-sub">Sauerstoffsättigung</div>
  </div>
  <div class="kpi" style="border-top:3px solid #ed8936">
    <div class="kpi-label">Ruhepuls (Nacht)</div>
    <div class="kpi-value">{hr_disp}</div>
    <div class="kpi-sub">{hr_sub}</div>
  </div>
</div>

<section>
  <h2>1. Befund der letzten Nacht</h2>
  <ul>{befund_html}</ul>
  {apnoe_html}
</section>

<section>
  <h2>2. Trend der letzten 7 Nächte</h2>
  <table>
    <thead><tr><th>Metrik</th><th>Heute</th><th>7-Tage-Ø</th><th>Differenz</th></tr></thead>
    <tbody>{trend_rows}</tbody>
  </table>
</section>

<section>
  <h2>3. Empfehlungen für heute</h2>
  {recs_html}
</section>

<section>
  <h2>4. Letzte 14 Nächte im Überblick</h2>
  <table>
    <thead>
      <tr><th>Datum</th><th>Schlaf</th><th>Tief%</th><th>REM%</th><th>Effizienz</th><th>SpO₂ Ø/Min</th><th>Ruhepuls</th></tr>
    </thead>
    <tbody>{hist_rows}</tbody>
  </table>
</section>

<footer>Daten: Garmin / Health Connect · Automatisch generiert · {now_str}</footer>
</body>
</html>"""


# ── 7. GitHub Push ────────────────────────────────────────────────────────────

def push_to_github(file_path, repo_path):
    token = GITHUB['token']
    user  = GITHUB['user']
    repo  = GITHUB['repo']

    with open(file_path, 'rb') as f:
        content = base64.b64encode(f.read()).decode()

    url     = f'https://api.github.com/repos/{user}/{repo}/contents/{repo_path}'
    headers = {
        'Authorization': f'token {token}',
        'Accept':        'application/vnd.github.v3+json',
        'Content-Type':  'application/json',
        'User-Agent':    'schlaf-briefing-bot',
    }

    # Bestehende SHA holen
    sha = None
    req = urllib.request.Request(url, headers=headers, method='GET')
    try:
        with urllib.request.urlopen(req) as resp:
            sha = json.loads(resp.read()).get('sha')
    except urllib.error.HTTPError:
        pass

    body = {'message': f'Update {datetime.now().strftime("%Y-%m-%d %H:%M")}', 'content': content}
    if sha:
        body['sha'] = sha

    data = json.dumps(body).encode('utf-8')
    req  = urllib.request.Request(url, data=data, headers=headers, method='PUT')
    try:
        with urllib.request.urlopen(req):
            print(f"  ✓ Gepusht: {repo_path}")
    except urllib.error.HTTPError as e:
        print(f"  ✗ Fehler bei {repo_path}: {e.code} {e.reason}")


# ── 8. Haupt-Routine ──────────────────────────────────────────────────────────

def main():
    today = datetime.now()
    date_dot  = today.strftime('%Y.%m.%d')
    date_dash = today.strftime('%Y-%m-%d')

    print(f"▶ Schlaf-Briefing für {date_dash} …")

    # Schlafdaten suchen (heute, sonst gestern)
    sleep_file = find_csv_for_date(SCHLAF_DIR, date_dot)
    if not sleep_file:
        yesterday = today - timedelta(days=1)
        date_dot  = yesterday.strftime('%Y.%m.%d')
        date_dash = yesterday.strftime('%Y-%m-%d')
        sleep_file = find_csv_for_date(SCHLAF_DIR, date_dot)

    if not sleep_file:
        print(f"  ✗ Keine Schlafdaten für {date_dash} gefunden. Abbruch.")
        sys.exit(1)

    print(f"  Schlaf: {os.path.basename(sleep_file)}")
    metrics = parse_sleep(load_csv(sleep_file))
    if not metrics:
        print("  ✗ Fehler beim Verarbeiten der Schlafdaten.")
        sys.exit(1)

    # SpO₂
    spo2_avg = spo2_min = None
    spo2_file = find_csv_for_date(SPO2_DIR, date_dot)
    if spo2_file:
        print(f"  SpO₂:  {os.path.basename(spo2_file)}")
        spo2_avg, spo2_min = parse_spo2(load_csv(spo2_file), metrics['start_dt'], metrics['end_dt'])
    else:
        print("  SpO₂:  keine Datei gefunden")

    # Puls
    hr = None
    puls_file = find_csv_for_date(PULS_DIR, date_dot)
    if puls_file:
        print(f"  Puls:  {os.path.basename(puls_file)}")
        hr = get_resting_hr(load_csv(puls_file))
    else:
        print("  Puls:  keine Datei gefunden")

    # Historische Daten
    print("  Lade historische Daten …")
    nights = load_history(14)
    # Heutige Nacht vorne einhängen
    nights.insert(0, {'date': date_dash, 'metrics': metrics, 'spo2_avg': spo2_avg, 'spo2_min': spo2_min, 'hr': hr})

    # HTML generieren & speichern
    html     = generate_html(date_dash, metrics, spo2_avg, spo2_min, hr, nights)
    out_name = f'schlaf_briefing_{date_dash}.html'
    out_path = os.path.join(OUTPUT_DIR, out_name)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"  Gespeichert: {out_path}")

    # latest.json schreiben – Index-Seite liest diese Datei für den Link
    latest_path = os.path.join(OUTPUT_DIR, 'latest.json')
    with open(latest_path, 'w', encoding='utf-8') as f:
        json.dump({'briefing': out_name, 'date': date_dash}, f)

    # GitHub Push – nur außerhalb von GitHub Actions
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        print(f"\n✅ Fertig! (GitHub Actions übernimmt den Push)")
        print(f"   Briefing: {out_name}")
    else:
        print("▶ GitHub Push …")
        try:
            push_to_github(out_path, out_name)
            push_to_github(latest_path, 'latest.json')
            index_path = os.path.join(OUTPUT_DIR, 'index.html')
            if os.path.exists(index_path):
                push_to_github(index_path, 'index.html')
            pages_url = GITHUB.get('pages_url', '')
            print(f"\n✅ Fertig! {pages_url}/{out_name}")
        except Exception as e:
            print(f"  ℹ GitHub-Push nicht möglich (Netzwerk geblockt): {e}")
            print(f"\n✅ HTML generiert: {out_path}")
            print(f"   Push-Schritt wird vom Task-Prompt via Chrome übernommen.")


if __name__ == '__main__':
    main()
