#!/usr/bin/env python3
"""
Schlaf-Dashboard Generator (v2 – vollständiges Feature-Set)
Liest alle historischen Schlafdaten und generiert schlaf_dashboard.html.

Fixes gegenüber v1:
  - Überspringt Datums-Bereichs-Dateien (z.B. Schlaf 2026.01.23-2026.02.22 Health Connect.csv)
  - SpO₂ und Puls werden aus allen Dateien nach Datum aggregiert
  - Vollständiges Dashboard: 6 Tabs, Filter, Theme-Toggle, 8 KPIs
"""

import os
import glob
import json
import csv
import base64
import re
import sys
import math
import platform
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from collections import defaultdict


# ── 1. Pfade dynamisch ermitteln ─────────────────────────────────────────────

WINDOWS_DRIVE_BASE = r'C:\Users\bjoer\Documents\Drive_Gropperstr'

def find_drive_base():
    # GitHub Actions: CSVs wurden von download_drive.py nach DRIVE_DATA_PATH geladen
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
        "Bitte den Ordner C:\\Users\\bjoer\\Documents\\Drive_Gropperstr freigeben."
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


# ── 2. Hilfsfunktionen ───────────────────────────────────────────────────────

DATE_RE       = re.compile(r'(\d{4}\.\d{2}\.\d{2})')
DATE_RANGE_RE = re.compile(r'\d{4}\.\d{2}\.\d{2}-\d{4}\.\d{2}\.\d{2}')

def is_range_file(basename):
    """True für Dateien mit Datumsbereich im Namen (z.B. 2026.01.23-2026.02.22)."""
    return bool(DATE_RANGE_RE.search(basename))

def load_csv(filepath):
    rows = []
    try:
        with open(filepath, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
    except Exception as e:
        print(f"  Warnung: {filepath}: {e}")
    return rows

def fmt_dur(s):
    """Sekunden → 'Xh YYm'."""
    h, m = divmod(int(s) // 60, 60)
    return f"{h}h {m:02d}m"

def fmt_min(m):
    """Minuten → 'Xh YYm'."""
    h = int(m) // 60
    mn = int(m) % 60
    return f"{h}h {mn:02d}m"

def pearson_r(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs)/n; my = sum(ys)/n
    num   = sum((x-mx)*(y-my) for x,y in zip(xs,ys))
    denom = math.sqrt(sum((x-mx)**2 for x in xs) * sum((y-my)**2 for y in ys))
    return round(num/denom, 3) if denom else None

def percentile(sorted_vals, p):
    """p-tes Perzentil einer sortierten Liste."""
    if not sorted_vals:
        return None
    idx = (p/100) * (len(sorted_vals)-1)
    lo, hi = int(idx), min(int(idx)+1, len(sorted_vals)-1)
    return sorted_vals[lo] + (sorted_vals[hi]-sorted_vals[lo])*(idx-lo)


# ── 3. Schlafdaten laden ──────────────────────────────────────────────────────

def split_first_sleep_session(rows, gap_hours=4):
    """Gibt nur die erste Schlaf-Session zurück (bis zur ersten Lücke > gap_hours).
    Das verhindert, dass Garmin-Exporte mit mehreren aufeinander folgenden Nächten
    als eine einzige, zu lange Nacht gezählt werden.
    """
    if not rows:
        return rows

    def parse_ts(row):
        datum = row.get('Datum', '')
        zeit  = row.get('Zeit', '')
        # Garmin: Datum = '2025.02.20 02:35:00'  (schon vollständig)
        # Health Connect: Datum = '2026.01.25 00:00:00', Zeit = '00:00:00'
        for fmt in ('%Y.%m.%d %H:%M:%S', '%Y.%m.%d'):
            try:
                return datetime.strptime(datum, fmt)
            except Exception:
                pass
        # Fallback: Datum + Zeit
        try:
            return datetime.strptime(f"{datum[:10]} {zeit}", '%Y.%m.%d %H:%M:%S')
        except Exception:
            return None

    # Sortieren nach Startzeitstempel
    timed = [(parse_ts(r), r) for r in rows]
    timed = [(t, r) for t, r in timed if t is not None]
    if not timed:
        return rows

    timed.sort(key=lambda x: x[0])
    session = [timed[0][1]]

    for i in range(1, len(timed)):
        prev_ts, prev_row = timed[i-1]
        curr_ts, curr_row = timed[i]
        # Berücksichtige Dauer des vorherigen Eintrags
        try:
            prev_dur = timedelta(seconds=int(prev_row.get('Durée en secondes', 0)))
        except ValueError:
            prev_dur = timedelta(0)
        gap = curr_ts - (prev_ts + prev_dur)
        if gap.total_seconds() > gap_hours * 3600:
            break  # Neue Schlafnacht → Session hier beenden
        session.append(curr_row)

    return session


def parse_sleep(rows):
    if not rows:
        return None

    # Trenne bei langen Unterbrechungen (mehrere Nächte in einer Datei)
    rows = split_first_sleep_session(rows)

    d = {'light': 0, 'deep': 0, 'rem': 0, 'awake': 0}
    awake_count = 0
    for row in rows:
        stage = row.get('Schlafstadium', '').lower().strip()
        try:
            secs = int(row.get('Durée en secondes', 0))
        except ValueError:
            continue
        if stage in d:
            d[stage] += secs
        if stage == 'awake':
            awake_count += 1

    sleep_secs = d['light'] + d['deep'] + d['rem']
    total_secs = sleep_secs + d['awake']
    if sleep_secs == 0:
        return None

    # Sanity-Check: mehr als 16h Schlaf → korrupte Datei
    if sleep_secs > 16 * 3600:
        return None

    start_dt = end_dt = None
    try:
        fmt = '%Y.%m.%d %H:%M:%S'
        start_dt = datetime.strptime(f"{rows[0]['Datum']} {rows[0]['Zeit']}", fmt)
        last_start = datetime.strptime(f"{rows[-1]['Datum']} {rows[-1]['Zeit']}", fmt)
        end_dt = last_start + timedelta(seconds=int(rows[-1].get('Durée en secondes', 0)))
    except Exception:
        pass

    efficiency = round(sleep_secs / total_secs * 100, 1) if total_secs else 0

    return {
        'duration_min':  round(sleep_secs / 60, 1),
        'duration_h':    round(sleep_secs / 3600, 2),
        'duration_str':  fmt_dur(sleep_secs),
        'efficiency':    efficiency,
        'deep_min':      round(d['deep']  / 60, 1),
        'deep_pct':      round(d['deep']  / sleep_secs * 100, 1),
        'rem_min':       round(d['rem']   / 60, 1),
        'rem_pct':       round(d['rem']   / sleep_secs * 100, 1),
        'light_min':     round(d['light'] / 60, 1),
        'light_pct':     round(d['light'] / sleep_secs * 100, 1),
        'awake_min':     round(d['awake'] / 60, 1),
        'awake_count':   awake_count,
        'start_time':    start_dt.strftime('%H:%M') if start_dt else '?',
        'end_time':      end_dt.strftime('%H:%M')   if end_dt   else '?',
        'start_dt':      start_dt,
        'end_dt':        end_dt,
    }


def file_priority(basename):
    """
    Priorität für Dateiauswahl pro Datum (höher = bevorzugt):
    3 – Health Connect mit echter Uhrzeit (z.B. '12 34 56 Health Connect')
    2 – Garmin
    1 – Health Connect mit '00 00 00' (Mitternachts-Export, teils korrupte Dauer in März 2026+)
    """
    if 'Health Connect' in basename:
        if '00 00 00' in basename:
            return 1   # Mitternachts-Export → niedrigste Priorität
        return 3       # Echter Zeitstempel → höchste Priorität
    if 'Garmin' in basename:
        return 2
    return 1


def load_all_nights():
    all_csvs = glob.glob(os.path.join(SCHLAF_DIR, '*.csv'))
    # date_map: ds → (priority, filepath)
    date_map = {}

    for f in all_csvs:
        base = os.path.basename(f)

        # Überspringen: Bereichsdateien (30-Tage-Exporte)
        if is_range_file(base):
            continue
        # Überspringen: Duplikate mit (1),(2)... und Non-Sleep-Dateien
        if re.search(r'\(\d+\)', base) or 'config' in base.lower():
            continue

        m = DATE_RE.search(base)
        if not m:
            continue
        ds = m.group(1)  # z.B. '2025.06.08'

        prio = file_priority(base)
        if ds not in date_map or prio > date_map[ds][0]:
            date_map[ds] = (prio, f)

    nights = []
    for ds in sorted(date_map.keys()):
        rows = load_csv(date_map[ds][1])
        met  = parse_sleep(rows)
        if not met:
            continue

        try:
            date_fmt = datetime.strptime(ds, '%Y.%m.%d').strftime('%Y-%m-%d')
        except Exception:
            date_fmt = ds

        nights.append({
            'date':    date_fmt,
            'metrics': met,
        })

    return nights


# ── 4. SpO₂-Daten aggregieren (aus allen Dateien, nach Datum) ────────────────

def load_spo2_by_date():
    """Gibt dict {date_str: {avg, min, max}} zurück."""
    by_date = defaultdict(list)

    for f in glob.glob(os.path.join(SPO2_DIR, '*.csv')):
        rows = load_csv(f)
        spo2_col = None
        for row in rows:
            if spo2_col is None:
                spo2_col = next(
                    (k for k in row.keys() if any(x in k.lower() for x in ['sat', 'ox', 'sauerstoff'])),
                    None
                )
            if not spo2_col:
                continue
            try:
                val  = float(row[spo2_col])
                date = row.get('Datum', '')[:10].replace('.', '-')  # '2026.01.23...' → '2026-01-23'
                if len(date) == 10 and '-' in date:
                    by_date[date].append(val)
            except (ValueError, KeyError):
                continue

    result = {}
    for date, vals in by_date.items():
        if vals:
            result[date] = {
                'avg': round(sum(vals)/len(vals), 1),
                'min': round(min(vals), 1),
                'max': round(max(vals), 1),
                'std': round(math.sqrt(sum((v-sum(vals)/len(vals))**2 for v in vals)/len(vals)), 2),
            }
    return result


# ── 5. Puls-Daten aggregieren (aus allen Dateien, nach Datum) ─────────────────

def load_pulse_by_date():
    """Gibt dict {date_str: {avg, min, max, resting}} zurück.
    resting = 10. Perzentil der Nacht-Messwerte (als Ruhepuls-Schätzung).
    """
    by_date = defaultdict(list)

    for f in glob.glob(os.path.join(PULS_DIR, '*.csv')):
        rows = load_csv(f)
        for row in rows:
            try:
                val  = int(float(row.get('Puls', 0)))
                date = row.get('Datum', '')[:10].replace('.', '-')
                if val > 0 and len(date) == 10 and '-' in date:
                    by_date[date].append(val)
            except (ValueError, KeyError):
                continue

    result = {}
    for date, vals in by_date.items():
        if vals:
            sv = sorted(vals)
            result[date] = {
                'avg':     round(sum(vals)/len(vals), 1),
                'min':     sv[0],
                'max':     sv[-1],
                'resting': round(percentile(sv, 10)),
            }
    return result


# ── 6. Monatsdaten berechnen ──────────────────────────────────────────────────

def compute_monthly(nights, spo2_by_date, pulse_by_date):
    by_month = defaultdict(list)
    for n in nights:
        month = n['date'][:7]  # '2025-06'
        by_month[month].append(n)

    result = []
    for month in sorted(by_month.keys()):
        ns = by_month[month]
        avg_sleep = round(sum(n['metrics']['duration_min'] for n in ns) / len(ns), 1)
        avg_deep  = round(sum(n['metrics']['deep_min']     for n in ns) / len(ns), 1)
        avg_rem   = round(sum(n['metrics']['rem_min']      for n in ns) / len(ns), 1)
        avg_eff   = round(sum(n['metrics']['efficiency']   for n in ns) / len(ns), 1)

        spo2_vals = [spo2_by_date[n['date']]['avg'] for n in ns if n['date'] in spo2_by_date]
        pulse_vals= [pulse_by_date[n['date']]['resting'] for n in ns if n['date'] in pulse_by_date]

        result.append({
            'month':               month,
            'nights':              len(ns),
            'avg_sleep':           avg_sleep,
            'avg_deep':            avg_deep,
            'avg_rem':             avg_rem,
            'avg_efficiency':      avg_eff,
            'avg_spo2':            round(sum(spo2_vals)/len(spo2_vals), 1) if spo2_vals else None,
            'avg_resting_pulse':   round(sum(pulse_vals)/len(pulse_vals), 1) if pulse_vals else None,
        })
    return result


# ── 7. KPIs berechnen ────────────────────────────────────────────────────────

def compute_kpis(nights, spo2_by_date, pulse_by_date):
    if not nights:
        return {}

    # Beste REM-Nacht
    best_rem = max(nights, key=lambda n: n['metrics']['rem_min'])
    # Längste / kürzeste Nacht
    longest  = max(nights, key=lambda n: n['metrics']['duration_min'])
    shortest = min(nights, key=lambda n: n['metrics']['duration_min'])

    # Durchschnittswerte
    def avg(lst): return round(sum(x for x in lst if x is not None) /
                               max(1, sum(1 for x in lst if x is not None)), 1)

    avg_eff   = avg([n['metrics']['efficiency']  for n in nights])
    avg_deep  = avg([n['metrics']['deep_min']    for n in nights])
    avg_rem   = avg([n['metrics']['rem_min']     for n in nights])
    avg_spo2  = avg([spo2_by_date[n['date']]['avg']     for n in nights if n['date'] in spo2_by_date])
    avg_pulse = avg([pulse_by_date[n['date']]['resting'] for n in nights if n['date'] in pulse_by_date])

    return {
        'best_rem_str':   best_rem['metrics']['rem_min'] and fmt_min(best_rem['metrics']['rem_min']),
        'best_rem_date':  best_rem['date'],
        'longest_str':    fmt_min(longest['metrics']['duration_min']),
        'longest_date':   longest['date'],
        'shortest_str':   fmt_min(shortest['metrics']['duration_min']),
        'shortest_date':  shortest['date'],
        'avg_eff':        f"{avg_eff}%",
        'avg_deep':       fmt_min(avg_deep),
        'avg_rem':        fmt_min(avg_rem),
        'avg_spo2':       f"{avg_spo2}%" if avg_spo2 else '–',
        'avg_pulse':      f"{avg_pulse} bpm" if avg_pulse else '–',
    }


# ── 8. Korrelationsdaten ──────────────────────────────────────────────────────

def compute_corr(nights, spo2_by_date, pulse_by_date):
    eff_spo2 = [{'x': n['metrics']['efficiency'], 'y': spo2_by_date[n['date']]['avg']}
                for n in nights if n['date'] in spo2_by_date]
    rem_spo2 = [{'x': n['metrics']['rem_min'], 'y': spo2_by_date[n['date']]['avg']}
                for n in nights if n['date'] in spo2_by_date]
    deep_pulse = [{'x': n['metrics']['deep_min'], 'y': pulse_by_date[n['date']]['resting']}
                  for n in nights if n['date'] in pulse_by_date]

    r1 = pearson_r([p['x'] for p in eff_spo2],  [p['y'] for p in eff_spo2])
    r2 = pearson_r([p['x'] for p in rem_spo2],  [p['y'] for p in rem_spo2])
    r3 = pearson_r([p['x'] for p in deep_pulse],[p['y'] for p in deep_pulse])

    return {
        'eff_spo2':    eff_spo2,
        'rem_spo2':    rem_spo2,
        'deep_pulse':  deep_pulse,
        'r_eff_spo2':  r1,
        'r_rem_spo2':  r2,
        'r_deep_pulse':r3,
    }


# ── 9. HTML generieren ───────────────────────────────────────────────────────

def generate_dashboard(nights, spo2_by_date, pulse_by_date):
    now_str = datetime.now().strftime('%d.%m.%Y %H:%M')
    kpis    = compute_kpis(nights, spo2_by_date, pulse_by_date)
    monthly = compute_monthly(nights, spo2_by_date, pulse_by_date)
    corr    = compute_corr(nights, spo2_by_date, pulse_by_date)

    date_range = f"{nights[0]['date']} bis {nights[-1]['date']}" if nights else '–'
    all_months = sorted({n['date'][:7] for n in nights})

    # SLEEP_DATA für JavaScript
    sleep_js_rows = []
    for n in nights:
        m = n['metrics']
        sleep_js_rows.append({
            'date':             n['date'],
            'total_sleep_min':  m['duration_min'],
            'deep_min':         m['deep_min'],
            'light_min':        m['light_min'],
            'rem_min':          m['rem_min'],
            'awake_min':        m['awake_min'],
            'deep_pct':         m['deep_pct'],
            'light_pct':        m['light_pct'],
            'rem_pct':          m['rem_pct'],
            'awake_pct':        round(m['awake_min'] / (m['duration_min'] + m['awake_min']) * 100, 1)
                                    if (m['duration_min'] + m['awake_min']) > 0 else 0,
            'efficiency':       m['efficiency'],
            'sleep_start':      m['start_time'],
            'wake_time':        m['end_time'],
            'wake_episodes':    m['awake_count'],
            'sleep_start_hour': (lambda t: (lambda h,mn: h+mn/60)(*map(int,t.split(':'))))(m['start_time'])
                                    if m['start_time'] != '?' else None,
            'wake_hour':        (lambda t: (lambda h,mn: h+mn/60)(*map(int,t.split(':'))))(m['end_time'])
                                    if m['end_time'] != '?' else None,
        })

    # Tabellen-Zeilen (alle Nächte, neueste zuerst)
    def eff_badge(e):
        cls = 'badge-good' if e >= 90 else ('badge-warn' if e >= 85 else 'badge-bad')
        return f'<span class="badge {cls}">{e}%</span>'

    table_rows = ''
    for n in reversed(nights):
        nm  = n['metrics']
        s2  = spo2_by_date.get(n['date'])
        p   = pulse_by_date.get(n['date'])
        s2s = f"{s2['avg']}% / {s2['min']}%" if s2 else '–'
        hrs = f"{p['resting']} bpm"           if p  else '–'
        table_rows += (
            f"<tr>"
            f"<td>{n['date']}</td>"
            f"<td>{nm['duration_str']}</td>"
            f"<td>{nm['deep_pct']}%</td>"
            f"<td>{nm['rem_pct']}%</td>"
            f"<td>{nm['light_pct']}%</td>"
            f"<td>{eff_badge(nm['efficiency'])}</td>"
            f"<td>{s2s}</td>"
            f"<td>{hrs}</td>"
            f"<td>{nm['awake_count']}</td>"
            f"</tr>\n"
        )

    # JSON-Blobs
    sleep_data_json   = json.dumps(sleep_js_rows)
    spo2_data_json    = json.dumps({k: {'avg': v['avg'], 'min': v['min']} for k,v in spo2_by_date.items()})
    pulse_data_json   = json.dumps(pulse_by_date)
    monthly_data_json = json.dumps(monthly)
    corr_data_json    = json.dumps(corr)
    all_months_json   = json.dumps(all_months)

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Schlaf-Dashboard – Björn</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root {{
    --bg: #0f1117; --card: #1a1d27; --card2: #22263a;
    --text: #e8eaf0; --muted: #8892a4; --accent: #5b8dee;
    --deep: #3d63dd; --light: #63b3ed; --rem: #9f7aea; --awake: #fc8181;
    --spo2: #48bb78; --pulse: #ed8936; --border: #2d3148; --tab-active: #5b8dee;
  }}
  [data-theme="light"] {{
    --bg: #f0f2f8; --card: #ffffff; --card2: #f7f9ff;
    --text: #1a1d27; --muted: #5a6478; --border: #d0d7e8;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; min-height: 100vh; transition: background 0.2s, color 0.2s; }}

  header {{ background: var(--card); border-bottom: 1px solid var(--border); padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }}
  header h1 {{ font-size: 1.4rem; font-weight: 700; }}
  header .subtitle {{ color: var(--muted); font-size: 0.85rem; margin-top: 2px; }}
  .header-right {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
  .theme-toggle {{ background: var(--card2); border: 1px solid var(--border); color: var(--text); padding: 6px 14px; border-radius: 20px; cursor: pointer; font-size: 0.85rem; transition: all 0.2s; }}
  .theme-toggle:hover {{ background: var(--border); }}
  .filter-group {{ display: flex; align-items: center; gap: 8px; font-size: 0.85rem; color: var(--muted); }}
  .filter-group select {{ background: var(--card2); border: 1px solid var(--border); color: var(--text); padding: 5px 10px; border-radius: 8px; font-size: 0.82rem; cursor: pointer; }}

  .kpis {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 12px; padding: 20px 24px; }}
  .kpi {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; }}
  .kpi-label {{ font-size: 0.72rem; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; margin-bottom: 6px; }}
  .kpi-value {{ font-size: 1.4rem; font-weight: 700; }}
  .kpi-sub {{ font-size: 0.72rem; color: var(--muted); margin-top: 3px; }}

  .tabs {{ display: flex; padding: 0 24px; border-bottom: 1px solid var(--border); overflow-x: auto; }}
  .tab {{ padding: 12px 18px; cursor: pointer; font-size: 0.88rem; color: var(--muted); border-bottom: 3px solid transparent; white-space: nowrap; transition: all 0.2s; user-select: none; }}
  .tab:hover {{ color: var(--text); }}
  .tab.active {{ color: var(--tab-active); border-bottom-color: var(--tab-active); font-weight: 600; }}
  .tab-content {{ display: none; padding: 20px 24px; }}
  .tab-content.active {{ display: block; }}

  .charts-grid   {{ display: grid; grid-template-columns: 2fr 1fr; gap: 16px; }}
  .charts-grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  .charts-grid-3 {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; }}
  @media (max-width: 900px) {{
    .charts-grid, .charts-grid-2, .charts-grid-3 {{ grid-template-columns: 1fr; }}
  }}

  .chart-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; margin-bottom: 0; }}
  .chart-card h3 {{ font-size: 0.88rem; color: var(--muted); margin-bottom: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .04em; }}
  .chart-wrapper {{ position: relative; }}
  .gap-top {{ margin-top: 16px; }}

  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  th {{ text-align: left; padding: 8px 12px; color: var(--muted); font-weight: 600; font-size: 0.75rem; text-transform: uppercase; border-bottom: 1px solid var(--border); white-space: nowrap; }}
  td {{ padding: 8px 12px; border-bottom: 1px solid var(--border); }}
  tr:hover td {{ background: var(--card2); }}
  .table-wrap {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; overflow: auto; }}

  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 0.72rem; font-weight: 600; }}
  .badge-good {{ background: rgba(72,187,120,.2); color: #48bb78; }}
  .badge-warn {{ background: rgba(252,129,74,.2);  color: #ed8936; }}
  .badge-bad  {{ background: rgba(252,129,129,.2); color: #fc8181; }}

  .corr-label {{ font-size: 0.78rem; color: var(--muted); text-align: center; margin-top: 6px; }}
  .no-data {{ color: var(--muted); font-size: 0.9rem; text-align: center; padding: 40px; }}
  footer {{ color: var(--muted); font-size: 0.75rem; text-align: center; padding: 24px; border-top: 1px solid var(--border); margin-top: 8px; }}
</style>
</head>
<body data-theme="dark">

<header>
  <div>
    <h1>🌙 Schlaf-Dashboard</h1>
    <div class="subtitle">Björn · {len(nights)} Nächte · {date_range}</div>
  </div>
  <div class="header-right">
    <div class="filter-group">
      <label>Monat:</label>
      <select id="monthFilter">
        <option value="all">Alle</option>
      </select>
    </div>
    <div class="filter-group">
      <label>Min. Schlaf:</label>
      <select id="minSleepFilter">
        <option value="180">3 Std.</option>
        <option value="240">4 Std.</option>
        <option value="300" selected>5 Std.</option>
        <option value="360">6 Std.</option>
      </select>
    </div>
    <button class="theme-toggle" onclick="toggleTheme()">☀️ Hell</button>
  </div>
</header>

<!-- KPIs -->
<div class="kpis">
  <div class="kpi" style="border-top:3px solid var(--rem)">
    <div class="kpi-label">Beste REM-Nacht</div>
    <div class="kpi-value">{kpis.get('best_rem_str','–')}</div>
    <div class="kpi-sub">{kpis.get('best_rem_date','–')}</div>
  </div>
  <div class="kpi" style="border-top:3px solid #48bb78">
    <div class="kpi-label">Längste Nacht</div>
    <div class="kpi-value">{kpis.get('longest_str','–')}</div>
    <div class="kpi-sub">{kpis.get('longest_date','–')}</div>
  </div>
  <div class="kpi" style="border-top:3px solid #fc8181">
    <div class="kpi-label">Kürzeste Nacht</div>
    <div class="kpi-value">{kpis.get('shortest_str','–')}</div>
    <div class="kpi-sub">{kpis.get('shortest_date','–')}</div>
  </div>
  <div class="kpi" style="border-top:3px solid var(--accent)">
    <div class="kpi-label">Ø Effizienz</div>
    <div class="kpi-value">{kpis.get('avg_eff','–')}</div>
    <div class="kpi-sub">Schlafeffizienz</div>
  </div>
  <div class="kpi" style="border-top:3px solid var(--deep)">
    <div class="kpi-label">Ø Tiefschlaf</div>
    <div class="kpi-value">{kpis.get('avg_deep','–')}</div>
    <div class="kpi-sub">pro Nacht</div>
  </div>
  <div class="kpi" style="border-top:3px solid var(--rem)">
    <div class="kpi-label">Ø REM-Schlaf</div>
    <div class="kpi-value">{kpis.get('avg_rem','–')}</div>
    <div class="kpi-sub">pro Nacht</div>
  </div>
  <div class="kpi" style="border-top:3px solid var(--pulse)">
    <div class="kpi-label">Ø Ruhepuls</div>
    <div class="kpi-value">{kpis.get('avg_pulse','–')}</div>
    <div class="kpi-sub">Nachts (10. Pztl.)</div>
  </div>
  <div class="kpi" style="border-top:3px solid var(--spo2)">
    <div class="kpi-label">Ø SpO₂</div>
    <div class="kpi-value">{kpis.get('avg_spo2','–')}</div>
    <div class="kpi-sub">Sauerstoffsättigung</div>
  </div>
</div>

<!-- Tabs -->
<div class="tabs">
  <div class="tab active" data-tab="schlafphasen">Schlafphasen</div>
  <div class="tab" data-tab="trends">Trends</div>
  <div class="tab" data-tab="regelmaessigkeit">Regelmäßigkeit</div>
  <div class="tab" data-tab="spo2puls">SpO₂ &amp; Puls</div>
  <div class="tab" data-tab="korrelation">Korrelation</div>
  <div class="tab" data-tab="monatsvergleich">Monatsvergleich</div>
  <div class="tab" data-tab="tabelle">Alle Nächte</div>
</div>

<!-- TAB 1: Schlafphasen -->
<div class="tab-content active" id="tab-schlafphasen">
  <div class="charts-grid">
    <div class="chart-card">
      <h3>Schlafphasen pro Nacht (gestapelt)</h3>
      <div class="chart-wrapper" style="height:320px"><canvas id="chartPhasenBar"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>Durchschn. Schlafphasen-Verteilung</h3>
      <div class="chart-wrapper" style="height:320px"><canvas id="chartPhasenDonut"></canvas></div>
    </div>
  </div>
</div>

<!-- TAB 2: Trends -->
<div class="tab-content" id="tab-trends">
  <div style="display:flex;flex-direction:column;gap:16px">
    <div class="chart-card">
      <h3>Schlafdauer &amp; Effizienz (7-Tage gleitender Ø)</h3>
      <div class="chart-wrapper" style="height:260px"><canvas id="chartTrendDauer"></canvas></div>
    </div>
    <div class="charts-grid-2">
      <div class="chart-card">
        <h3>REM-Schlaf (min) mit 7T-Ø</h3>
        <div class="chart-wrapper" style="height:220px"><canvas id="chartTrendREM"></canvas></div>
      </div>
      <div class="chart-card">
        <h3>Tiefschlaf (min) mit 7T-Ø</h3>
        <div class="chart-wrapper" style="height:220px"><canvas id="chartTrendDeep"></canvas></div>
      </div>
    </div>
  </div>
</div>

<!-- TAB 3: Regelmäßigkeit -->
<div class="tab-content" id="tab-regelmaessigkeit">
  <div style="display:flex;flex-direction:column;gap:16px">
    <div class="chart-card">
      <h3>Einschlaf- und Aufwachzeiten</h3>
      <div class="chart-wrapper" style="height:260px"><canvas id="chartZeiten"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>Wach-Episoden pro Nacht</h3>
      <div class="chart-wrapper" style="height:200px"><canvas id="chartWachEpisoden"></canvas></div>
    </div>
  </div>
</div>

<!-- TAB 4: SpO₂ & Puls -->
<div class="tab-content" id="tab-spo2puls">
  <div style="display:flex;flex-direction:column;gap:16px">
    <div class="chart-card">
      <h3>SpO₂ – Ø und Minimum (%)</h3>
      <div class="chart-wrapper" style="height:260px"><canvas id="chartSpo2"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>Puls – Ø, Minimum und Ruhepuls (bpm)</h3>
      <div class="chart-wrapper" style="height:260px"><canvas id="chartPuls"></canvas></div>
    </div>
  </div>
</div>

<!-- TAB 5: Korrelation -->
<div class="tab-content" id="tab-korrelation">
  <div class="charts-grid-3">
    <div class="chart-card">
      <h3>Effizienz vs. SpO₂</h3>
      <div class="chart-wrapper" style="height:280px"><canvas id="chartCorrEffSpo2"></canvas></div>
      <div class="corr-label" id="r_eff_spo2_label"></div>
    </div>
    <div class="chart-card">
      <h3>REM vs. SpO₂</h3>
      <div class="chart-wrapper" style="height:280px"><canvas id="chartCorrRemSpo2"></canvas></div>
      <div class="corr-label" id="r_rem_spo2_label"></div>
    </div>
    <div class="chart-card">
      <h3>Tiefschlaf vs. Ruhepuls</h3>
      <div class="chart-wrapper" style="height:280px"><canvas id="chartCorrDeepPulse"></canvas></div>
      <div class="corr-label" id="r_deep_pulse_label"></div>
    </div>
  </div>
</div>

<!-- TAB 6: Monatsvergleich -->
<div class="tab-content" id="tab-monatsvergleich">
  <div style="display:flex;flex-direction:column;gap:16px">
    <div class="charts-grid-2">
      <div class="chart-card">
        <h3>Ø Schlafdauer pro Monat</h3>
        <div class="chart-wrapper" style="height:260px"><canvas id="chartMonthBar"></canvas></div>
      </div>
      <div class="chart-card">
        <h3>Ø Tiefschlaf &amp; REM pro Monat</h3>
        <div class="chart-wrapper" style="height:260px"><canvas id="chartMonthPhases"></canvas></div>
      </div>
    </div>
    <div class="chart-card">
      <h3>Monatsübersicht</h3>
      <table>
        <thead><tr>
          <th>Monat</th><th>Nächte</th><th>Ø Schlaf</th><th>Ø Tiefschlaf</th>
          <th>Ø REM</th><th>Ø Effizienz</th><th>Ø SpO₂</th><th>Ø Ruhepuls</th>
        </tr></thead>
        <tbody id="monthTableBody"></tbody>
      </table>
    </div>
  </div>
</div>

<!-- TAB 7: Alle Nächte -->
<div class="tab-content" id="tab-tabelle">
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>Datum</th><th>Dauer</th><th>Tief%</th><th>REM%</th>
        <th>Leicht%</th><th>Effizienz</th><th>SpO₂ Ø/Min</th>
        <th>Ruhepuls</th><th>Wach</th>
      </tr></thead>
      <tbody>{table_rows}</tbody>
    </table>
  </div>
</div>

<footer>Daten: Garmin / Health Connect · Aktualisiert: {now_str} · {len(nights)} Nächte</footer>

<script>
const SLEEP_DATA   = {sleep_data_json};
const SPO2_DATA    = {spo2_data_json};
const PULSE_DATA   = {pulse_data_json};
const MONTHLY_DATA = {monthly_data_json};
const CORR_DATA    = {corr_data_json};
const ALL_MONTHS   = {all_months_json};

const COLORS = {{
  deep: '#3d63dd', light: '#63b3ed', rem: '#9f7aea', awake: '#fc8181',
  spo2: '#48bb78', pulse: '#ed8936', accent: '#5b8dee',
}};

let activeCharts = {{}};
let currentFilter = {{ month: 'all', minSleep: 300 }};

// ── Theme ────────────────────────────────────────────────────────────────────
function toggleTheme() {{
  const body = document.body;
  const btn  = document.querySelector('.theme-toggle');
  if (body.dataset.theme === 'dark') {{
    body.dataset.theme = 'light'; btn.textContent = '🌙 Dunkel';
  }} else {{
    body.dataset.theme = 'dark';  btn.textContent = '☀️ Hell';
  }}
  redrawAll();
}}

// ── Month filter ─────────────────────────────────────────────────────────────
const monthSel = document.getElementById('monthFilter');
ALL_MONTHS.forEach(m => {{
  const opt = document.createElement('option');
  opt.value = m; opt.textContent = formatMonth(m);
  monthSel.appendChild(opt);
}});
monthSel.addEventListener('change', e => {{ currentFilter.month = e.target.value; redrawAll(); }});
document.getElementById('minSleepFilter').addEventListener('change', e => {{
  currentFilter.minSleep = parseInt(e.target.value); redrawAll();
}});

// ── Helpers ──────────────────────────────────────────────────────────────────
function formatMonth(m) {{
  const [y, mo] = m.split('-');
  return ['Jan','Feb','Mär','Apr','Mai','Jun','Jul','Aug','Sep','Okt','Nov','Dez'][parseInt(mo)-1] + ' ' + y;
}}
function fmtH(min) {{
  const h = Math.floor(min/60), m = Math.round(min%60);
  return h + 'h ' + String(m).padStart(2,'0') + 'm';
}}
function getFilteredData() {{
  return SLEEP_DATA.filter(s => {{
    if (currentFilter.month !== 'all' && !s.date.startsWith(currentFilter.month)) return false;
    if (s.total_sleep_min < currentFilter.minSleep) return false;
    return true;
  }});
}}
function destroyChart(id) {{
  if (activeCharts[id]) {{ activeCharts[id].destroy(); delete activeCharts[id]; }}
}}
function getGridColor() {{
  return document.body.dataset.theme === 'light' ? 'rgba(0,0,0,0.08)' : 'rgba(255,255,255,0.08)';
}}
function getTextColor() {{
  return document.body.dataset.theme === 'light' ? '#5a6478' : '#8892a4';
}}
function chartDefaults() {{
  return {{
    plugins: {{ legend: {{ labels: {{ color: getTextColor(), boxWidth: 12, font: {{ size: 11 }} }} }} }},
    scales: {{
      x: {{ ticks: {{ color: getTextColor(), maxTicksLimit: 10, font:{{size:10}} }}, grid: {{ color: getGridColor() }} }},
      y: {{ ticks: {{ color: getTextColor(), font:{{size:10}} }}, grid: {{ color: getGridColor() }} }}
    }}
  }};
}}
function computeMA(arr, w) {{
  return arr.map((_, i) => {{
    const chunk = arr.slice(Math.max(0,i-w+1), i+1).filter(v=>v!=null);
    return chunk.length ? Math.round(chunk.reduce((a,b)=>a+b,0)/chunk.length*10)/10 : null;
  }});
}}

// ── Tab 1: Schlafphasen ──────────────────────────────────────────────────────
function drawPhasenBar(data) {{
  destroyChart('chartPhasenBar');
  const ctx = document.getElementById('chartPhasenBar').getContext('2d');
  const defs = chartDefaults();
  activeCharts['chartPhasenBar'] = new Chart(ctx, {{
    type: 'bar',
    data: {{
      labels: data.map(d => d.date.slice(5)),
      datasets: [
        {{ label:'Tiefschlaf',  data: data.map(d=>d.deep_min),  backgroundColor: COLORS.deep,  stack:'s' }},
        {{ label:'Leichtschlaf',data: data.map(d=>d.light_min), backgroundColor: COLORS.light, stack:'s' }},
        {{ label:'REM',         data: data.map(d=>d.rem_min),   backgroundColor: COLORS.rem,   stack:'s' }},
        {{ label:'Wach',        data: data.map(d=>d.awake_min), backgroundColor: COLORS.awake, stack:'s' }},
      ]
    }},
    options: {{
      responsive:true, maintainAspectRatio:false,
      plugins: defs.plugins,
      scales: {{
        x: {{ stacked:true, ...defs.scales.x }},
        y: {{ stacked:true, ...defs.scales.y, title:{{ display:true, text:'Minuten', color:getTextColor() }} }}
      }}
    }}
  }});
}}

function drawPhasenDonut(data) {{
  destroyChart('chartPhasenDonut');
  const ctx = document.getElementById('chartPhasenDonut').getContext('2d');
  const n = data.length || 1;
  const aD = data.reduce((s,d)=>s+d.deep_min,0)/n;
  const aL = data.reduce((s,d)=>s+d.light_min,0)/n;
  const aR = data.reduce((s,d)=>s+d.rem_min,0)/n;
  const aA = data.reduce((s,d)=>s+d.awake_min,0)/n;
  activeCharts['chartPhasenDonut'] = new Chart(ctx, {{
    type: 'doughnut',
    data: {{
      labels: ['Tiefschlaf','Leichtschlaf','REM','Wach'],
      datasets: [{{ data:[aD,aL,aR,aA], backgroundColor:[COLORS.deep,COLORS.light,COLORS.rem,COLORS.awake], borderWidth:2, borderColor:'transparent' }}]
    }},
    options: {{
      responsive:true, maintainAspectRatio:false,
      plugins: {{
        legend: {{ position:'bottom', labels:{{ color:getTextColor(), font:{{size:11}} }} }},
        tooltip: {{ callbacks: {{ label: c => ' ' + fmtH(c.raw) + ' (' + Math.round(c.raw/(aD+aL+aR+aA)*100) + '%)' }} }}
      }}
    }}
  }});
}}

// ── Tab 2: Trends ────────────────────────────────────────────────────────────
function drawTrendDauer(data) {{
  destroyChart('chartTrendDauer');
  const ctx = document.getElementById('chartTrendDauer').getContext('2d');
  const defs = chartDefaults();
  const maT  = computeMA(data.map(d=>d.total_sleep_min), 7);
  activeCharts['chartTrendDauer'] = new Chart(ctx, {{
    type:'bar',
    data: {{
      labels: data.map(d=>d.date.slice(5)),
      datasets: [
        {{ label:'Schlafdauer (min)', data:data.map(d=>d.total_sleep_min), backgroundColor:COLORS.accent+'88', yAxisID:'y' }},
        {{ label:'7T-Ø Dauer', data:maT, type:'line', borderColor:COLORS.accent, borderWidth:2, pointRadius:0, yAxisID:'y', tension:0.4 }},
        {{ label:'Effizienz (%)', data:data.map(d=>d.efficiency), type:'line', borderColor:COLORS.rem, borderWidth:2, pointRadius:0, yAxisID:'y2', tension:0.4, borderDash:[5,3] }},
      ]
    }},
    options: {{
      responsive:true, maintainAspectRatio:false,
      plugins: defs.plugins,
      scales: {{
        x: defs.scales.x,
        y:  {{ ...defs.scales.y, position:'left',  title:{{ display:true, text:'Minuten', color:getTextColor() }} }},
        y2: {{ ...defs.scales.y, position:'right', min:0, max:100, grid:{{ drawOnChartArea:false }}, title:{{ display:true, text:'%', color:getTextColor() }} }}
      }}
    }}
  }});
}}

function drawTrendLine(canvasId, label, values, dates, color) {{
  destroyChart(canvasId);
  const ctx  = document.getElementById(canvasId).getContext('2d');
  const ma   = computeMA(values, 7);
  const defs = chartDefaults();
  activeCharts[canvasId] = new Chart(ctx, {{
    type:'bar',
    data: {{
      labels: dates.map(d=>d.slice(5)),
      datasets: [
        {{ label, data:values, backgroundColor:color+'88', yAxisID:'y' }},
        {{ label:'7T-Ø', data:ma, type:'line', borderColor:color, borderWidth:2, pointRadius:0, yAxisID:'y', tension:0.4 }},
      ]
    }},
    options: {{
      responsive:true, maintainAspectRatio:false,
      plugins: defs.plugins,
      scales: {{ x:defs.scales.x, y:{{ ...defs.scales.y, title:{{ display:true, text:'Minuten', color:getTextColor() }} }} }}
    }}
  }});
}}

// ── Tab 3: Regelmäßigkeit ────────────────────────────────────────────────────
function drawZeiten(data) {{
  destroyChart('chartZeiten');
  const ctx = document.getElementById('chartZeiten').getContext('2d');
  function toH(t) {{
    if (!t) return null;
    const [h,m] = t.split(':').map(Number);
    let v = h + m/60;
    if (v >= 18) v -= 24;
    return Math.round(v*100)/100;
  }}
  const defs = chartDefaults();
  activeCharts['chartZeiten'] = new Chart(ctx, {{
    type:'line',
    data: {{
      labels: data.map(d=>d.date.slice(5)),
      datasets: [
        {{ label:'Einschlafzeit', data:data.map(d=>toH(d.sleep_start)), borderColor:COLORS.rem, tension:0.3, pointRadius:3, fill:false }},
        {{ label:'Aufwachzeit',   data:data.map(d=>d.wake_hour), borderColor:COLORS.spo2, tension:0.3, pointRadius:3, fill:false }},
      ]
    }},
    options: {{
      responsive:true, maintainAspectRatio:false,
      plugins: {{
        ...defs.plugins,
        tooltip:{{ callbacks:{{ label:c => c.dataset.label+': '+(c.raw<0?(24+c.raw).toFixed(1):c.raw.toFixed(1))+' Uhr' }} }}
      }},
      scales: {{
        x: defs.scales.x,
        y: {{ ...defs.scales.y, ticks:{{ color:getTextColor(), callback:v=>(v<0?24+v:v).toFixed(0)+':00' }} }}
      }}
    }}
  }});
}}

function drawWachEpisoden(data) {{
  destroyChart('chartWachEpisoden');
  const ctx  = document.getElementById('chartWachEpisoden').getContext('2d');
  const defs = chartDefaults();
  activeCharts['chartWachEpisoden'] = new Chart(ctx, {{
    type:'bar',
    data: {{
      labels: data.map(d=>d.date.slice(5)),
      datasets:[{{ label:'Wach-Episoden', data:data.map(d=>d.wake_episodes), backgroundColor:COLORS.awake+'aa' }}]
    }},
    options: {{
      responsive:true, maintainAspectRatio:false,
      plugins:defs.plugins,
      scales:{{ x:defs.scales.x, y:{{ ...defs.scales.y, ticks:{{ stepSize:1, color:getTextColor() }} }} }}
    }}
  }});
}}

// ── Tab 4: SpO₂ & Puls ──────────────────────────────────────────────────────
function drawSpo2(data) {{
  destroyChart('chartSpo2');
  const ctx  = document.getElementById('chartSpo2').getContext('2d');
  const defs = chartDefaults();
  activeCharts['chartSpo2'] = new Chart(ctx, {{
    type:'line',
    data: {{
      labels: data.map(d=>d.date.slice(5)),
      datasets: [
        {{ label:'Ø SpO₂',  data:data.map(d=>SPO2_DATA[d.date]?.avg??null), borderColor:COLORS.spo2, backgroundColor:COLORS.spo2+'33', tension:0.3, fill:true, pointRadius:3, spanGaps:true }},
        {{ label:'Min SpO₂',data:data.map(d=>SPO2_DATA[d.date]?.min??null), borderColor:COLORS.awake, borderDash:[4,3], tension:0.3, pointRadius:2, spanGaps:true }},
      ]
    }},
    options: {{
      responsive:true, maintainAspectRatio:false,
      plugins:defs.plugins,
      scales:{{ x:defs.scales.x, y:{{ ...defs.scales.y, min:75, max:100, title:{{ display:true, text:'%', color:getTextColor() }} }} }}
    }}
  }});
}}

function drawPuls(data) {{
  destroyChart('chartPuls');
  const ctx  = document.getElementById('chartPuls').getContext('2d');
  const defs = chartDefaults();
  activeCharts['chartPuls'] = new Chart(ctx, {{
    type:'line',
    data: {{
      labels: data.map(d=>d.date.slice(5)),
      datasets: [
        {{ label:'Ø Puls',               data:data.map(d=>PULSE_DATA[d.date]?.avg??null),     borderColor:COLORS.pulse,  backgroundColor:COLORS.pulse+'33', tension:0.3, fill:true, pointRadius:2, spanGaps:true }},
        {{ label:'Min Puls',             data:data.map(d=>PULSE_DATA[d.date]?.min??null),     borderColor:COLORS.accent, borderDash:[4,3], tension:0.3, pointRadius:2, spanGaps:true }},
        {{ label:'Ruhepuls (10. Pztl.)', data:data.map(d=>PULSE_DATA[d.date]?.resting??null), borderColor:'#e53e3e',     borderWidth:2, borderDash:[2,2], tension:0.3, pointRadius:3, spanGaps:true }},
      ]
    }},
    options: {{
      responsive:true, maintainAspectRatio:false,
      plugins:defs.plugins,
      scales:{{ x:defs.scales.x, y:{{ ...defs.scales.y, title:{{ display:true, text:'bpm', color:getTextColor() }} }} }}
    }}
  }});
}}

// ── Tab 5: Korrelation ───────────────────────────────────────────────────────
function drawCorrelation(canvasId, corrData, xLabel, yLabel) {{
  destroyChart(canvasId);
  if (!corrData || corrData.length === 0) return;
  const ctx  = document.getElementById(canvasId).getContext('2d');
  const defs = chartDefaults();
  activeCharts[canvasId] = new Chart(ctx, {{
    type:'scatter',
    data:{{ datasets:[{{ label:'', data:corrData, backgroundColor:COLORS.accent+'99', pointRadius:5 }}] }},
    options:{{
      responsive:true, maintainAspectRatio:false,
      plugins:{{ legend:{{ display:false }} }},
      scales:{{
        x:{{ ...defs.scales.x, title:{{ display:true, text:xLabel, color:getTextColor() }} }},
        y:{{ ...defs.scales.y, title:{{ display:true, text:yLabel, color:getTextColor() }} }},
      }}
    }}
  }});
}}

// ── Tab 6: Monatsvergleich ───────────────────────────────────────────────────
function drawMonthBar() {{
  destroyChart('chartMonthBar');
  const ctx  = document.getElementById('chartMonthBar').getContext('2d');
  const defs = chartDefaults();
  activeCharts['chartMonthBar'] = new Chart(ctx, {{
    type:'bar',
    data:{{
      labels: MONTHLY_DATA.map(m=>formatMonth(m.month)),
      datasets:[{{ label:'Ø Schlafdauer (min)', data:MONTHLY_DATA.map(m=>m.avg_sleep), backgroundColor:COLORS.accent }}]
    }},
    options:{{
      responsive:true, maintainAspectRatio:false,
      plugins:defs.plugins,
      scales:{{ x:defs.scales.x, y:{{ ...defs.scales.y, title:{{ display:true, text:'Minuten', color:getTextColor() }} }} }}
    }}
  }});
}}

function drawMonthPhases() {{
  destroyChart('chartMonthPhases');
  const ctx  = document.getElementById('chartMonthPhases').getContext('2d');
  const defs = chartDefaults();
  activeCharts['chartMonthPhases'] = new Chart(ctx, {{
    type:'bar',
    data:{{
      labels: MONTHLY_DATA.map(m=>formatMonth(m.month)),
      datasets:[
        {{ label:'Tiefschlaf', data:MONTHLY_DATA.map(m=>m.avg_deep), backgroundColor:COLORS.deep, stack:'s' }},
        {{ label:'REM',        data:MONTHLY_DATA.map(m=>m.avg_rem),  backgroundColor:COLORS.rem,  stack:'s' }},
      ]
    }},
    options:{{
      responsive:true, maintainAspectRatio:false,
      plugins:defs.plugins,
      scales:{{
        x:{{ stacked:true, ...defs.scales.x }},
        y:{{ stacked:true, ...defs.scales.y, title:{{ display:true, text:'Minuten', color:getTextColor() }} }}
      }}
    }}
  }});
}}

function buildMonthTable() {{
  const tbody = document.getElementById('monthTableBody');
  tbody.innerHTML = '';
  MONTHLY_DATA.forEach(m => {{
    const eff = m.avg_efficiency;
    const cls = eff >= 85 ? 'badge-good' : eff >= 75 ? 'badge-warn' : 'badge-bad';
    const tr  = document.createElement('tr');
    tr.innerHTML = `
      <td><strong>${{formatMonth(m.month)}}</strong></td>
      <td>${{m.nights}}</td>
      <td>${{fmtH(m.avg_sleep)}}</td>
      <td>${{fmtH(m.avg_deep)}}</td>
      <td>${{fmtH(m.avg_rem)}}</td>
      <td><span class="badge ${{cls}}">${{eff}}%</span></td>
      <td>${{m.avg_spo2 ? m.avg_spo2 + '%' : '–'}}</td>
      <td>${{m.avg_resting_pulse ? m.avg_resting_pulse + ' bpm' : '–'}}</td>
    `;
    tbody.appendChild(tr);
  }});
}}

// ── Redraw ───────────────────────────────────────────────────────────────────
function redrawAll() {{
  const activeTab = document.querySelector('.tab.active')?.dataset.tab;
  drawForTab(activeTab);
}}

function drawForTab(tabName, data) {{
  if (!data) data = getFilteredData();
  if (!data.length) return;
  const dates = data.map(d=>d.date);
  switch(tabName) {{
    case 'schlafphasen':
      drawPhasenBar(data); drawPhasenDonut(data); break;
    case 'trends':
      drawTrendDauer(data);
      drawTrendLine('chartTrendREM',  'REM (min)',        data.map(d=>d.rem_min),  dates, COLORS.rem);
      drawTrendLine('chartTrendDeep', 'Tiefschlaf (min)', data.map(d=>d.deep_min), dates, COLORS.deep);
      break;
    case 'regelmaessigkeit':
      drawZeiten(data); drawWachEpisoden(data); break;
    case 'spo2puls':
      drawSpo2(data); drawPuls(data); break;
    case 'korrelation':
      drawCorrelation('chartCorrEffSpo2',  CORR_DATA.eff_spo2,   'Effizienz (%)', 'SpO₂ (%)');
      drawCorrelation('chartCorrRemSpo2',  CORR_DATA.rem_spo2,   'REM (min)',     'SpO₂ (%)');
      drawCorrelation('chartCorrDeepPulse',CORR_DATA.deep_pulse, 'Tiefschlaf (min)', 'Ruhepuls (bpm)');
      document.getElementById('r_eff_spo2_label').textContent   = CORR_DATA.r_eff_spo2   != null ? 'Pearson r = ' + CORR_DATA.r_eff_spo2   : '';
      document.getElementById('r_rem_spo2_label').textContent   = CORR_DATA.r_rem_spo2   != null ? 'Pearson r = ' + CORR_DATA.r_rem_spo2   : '';
      document.getElementById('r_deep_pulse_label').textContent = CORR_DATA.r_deep_pulse != null ? 'Pearson r = ' + CORR_DATA.r_deep_pulse : '';
      break;
    case 'monatsvergleich':
      drawMonthBar(); drawMonthPhases(); buildMonthTable(); break;
  }}
}}

// ── Tab switching ────────────────────────────────────────────────────────────
document.querySelectorAll('.tab').forEach(tab => {{
  tab.addEventListener('click', () => {{
    document.querySelectorAll('.tab').forEach(t  => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
    drawForTab(tab.dataset.tab);
  }});
}});

// ── Initial draw ─────────────────────────────────────────────────────────────
drawForTab('schlafphasen');
</script>
</body>
</html>"""


# ── 10. GitHub Push ───────────────────────────────────────────────────────────

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
        'User-Agent':    'schlaf-dashboard-bot',
    }

    sha = None
    try:
        req = urllib.request.Request(url, headers=headers, method='GET')
        with urllib.request.urlopen(req) as resp:
            sha = json.loads(resp.read()).get('sha')
    except urllib.error.HTTPError:
        pass

    body = {'message': f'Update {datetime.now().strftime("%Y-%m-%d %H:%M")}', 'content': content}
    if sha:
        body['sha'] = sha

    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method='PUT')
    try:
        with urllib.request.urlopen(req):
            print(f"  OK Gepusht: {repo_path}")
    except urllib.error.HTTPError as e:
        print(f"  FEHLER bei {repo_path}: {e.code} {e.reason}")


# ── 11. Haupt-Routine ─────────────────────────────────────────────────────────

def main():
    print("Schlaf-Dashboard v2 wird generiert ...")

    nights       = load_all_nights()
    spo2_by_date = load_spo2_by_date()
    pulse_by_date= load_pulse_by_date()

    print(f"  {len(nights)} Nächte geladen")
    print(f"  {len(spo2_by_date)} Tage SpO2-Daten")
    print(f"  {len(pulse_by_date)} Tage Pulsdaten")

    if not nights:
        print("  FEHLER: Keine Schlafdaten gefunden.")
        sys.exit(1)

    html     = generate_dashboard(nights, spo2_by_date, pulse_by_date)
    out_path = os.path.join(OUTPUT_DIR, 'schlaf_dashboard.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"  Gespeichert: {out_path}")

    # GitHub Push — nur außerhalb von GitHub Actions (dort übernimmt git den Push)
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        print(f"\nFertig! (GitHub Actions übernimmt den Push)")
    else:
        print("GitHub Push ...")
        try:
            push_to_github(out_path, 'schlaf_dashboard.html')
            index_path = os.path.join(OUTPUT_DIR, 'index.html')
            if os.path.exists(index_path):
                push_to_github(index_path, 'index.html')
            print(f"\nFertig! {GITHUB.get('pages_url','')}/schlaf_dashboard.html")
        except Exception as e:
            print(f"  GitHub-Push nicht möglich (Netzwerk geblockt): {e}")
            print(f"\nHTML generiert: {out_path}")


if __name__ == '__main__':
    main()
