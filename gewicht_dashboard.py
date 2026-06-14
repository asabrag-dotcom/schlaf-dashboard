#!/usr/bin/env python3
"""
Gewichts-Dashboard Generator
Liest Gewichtsdaten aus Health-Sync-CSV-Dateien und generiert
ein interaktives HTML-Dashboard, das via GitHub Pages veröffentlicht wird.
"""

import os
import glob
import json
import csv
import re
import sys
import platform
from datetime import datetime, timedelta
from statistics import mean


# ── 1. Pfade dynamisch ermitteln ─────────────────────────────────────────────

WINDOWS_DRIVE_BASE = r'C:\Users\bjoer\Documents\Drive_Gropperstr'

def find_drive_base():
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        path = os.environ.get('DRIVE_DATA_PATH', '/tmp/drive_data')
        if os.path.isdir(path):
            return path
        raise FileNotFoundError(f"DRIVE_DATA_PATH nicht gefunden: {path}")
    if platform.system() == 'Windows':
        if os.path.isdir(WINDOWS_DRIVE_BASE):
            return WINDOWS_DRIVE_BASE
    patterns = glob.glob('/sessions/*/mnt/Drive_Gropperstr')
    if patterns:
        return sorted(patterns)[-1]
    raise FileNotFoundError("Drive_Gropperstr nicht gefunden.")

DRIVE_BASE  = find_drive_base()
GEWICHT_DIR = os.path.join(DRIVE_BASE, 'Health Sync Gewicht')

if os.environ.get('GITHUB_ACTIONS') == 'true':
    OUTPUT_DIR = os.environ.get('OUTPUT_DIR', os.getcwd())
else:
    OUTPUT_DIR = os.path.join(DRIVE_BASE, 'Schlaf_Briefing')

# Körpergröße in Metern (für BMI)
HEIGHT_M = 1.82


# ── 2. CSV laden & parsen ─────────────────────────────────────────────────────

WEIGHT_COLUMNS = ['Gewicht', 'Körpergewicht', 'Gewicht (kg)', 'Weight', 'Gewicht_kg', 'value']

def find_weight_column(headers):
    for col in WEIGHT_COLUMNS:
        if col in headers:
            return col
    # Fallback: erste numerisch-klingende Spalte nach Datum/Zeit
    for h in headers:
        if h.lower() not in ('datum', 'zeit', 'datenquelle', 'source', 'date', 'time'):
            return h
    return None

def is_range_file(filename):
    return bool(re.search(r'\d{4}\.\d{2}\.\d{2}-\d{4}\.\d{2}\.\d{2}', os.path.basename(filename)))

def load_weight_data():
    """Lädt alle Gewichts-CSVs und gibt eine sortierte Liste von (date, weight_kg) zurück."""
    if not os.path.isdir(GEWICHT_DIR):
        print(f"  ⚠ Ordner nicht gefunden: {GEWICHT_DIR}")
        return []

    all_csvs = glob.glob(os.path.join(GEWICHT_DIR, '*.csv'))
    measurements = {}  # date_str → weight (erste Messung des Tages)

    for filepath in all_csvs:
        if is_range_file(filepath):
            continue
        if re.search(r'\(\d+\)', os.path.basename(filepath)):
            continue

        try:
            with open(filepath, newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                headers = reader.fieldnames or []
                weight_col = find_weight_column(headers)
                if not weight_col:
                    continue

                for row in reader:
                    try:
                        weight = float(row[weight_col].replace(',', '.'))
                    except (ValueError, KeyError):
                        continue
                    if weight < 30 or weight > 300:
                        continue  # Offensichtlich ungültig

                    # Datum parsen
                    datum_raw = row.get('Datum', '')
                    try:
                        if ' ' in datum_raw:
                            dt = datetime.strptime(datum_raw.strip(), '%Y.%m.%d %H:%M:%S')
                        else:
                            dt = datetime.strptime(datum_raw.strip(), '%Y.%m.%d')
                    except ValueError:
                        continue

                    date_key = dt.strftime('%Y-%m-%d')
                    # Erste Messung des Tages behalten (Morgenmessung)
                    if date_key not in measurements:
                        measurements[date_key] = (dt, weight)
                    elif dt < measurements[date_key][0]:
                        measurements[date_key] = (dt, weight)

        except Exception as e:
            print(f"  Fehler beim Lesen von {os.path.basename(filepath)}: {e}")

    # Sortiert nach Datum zurückgeben
    result = [(k, v[1]) for k, v in sorted(measurements.items())]
    print(f"  {len(result)} Messpunkte geladen")
    return result


# ── 3. Berechnungen ───────────────────────────────────────────────────────────

def moving_average(data, window=7):
    result = []
    for i in range(len(data)):
        start = max(0, i - window + 1)
        vals = [d[1] for d in data[start:i+1]]
        result.append(round(mean(vals), 2))
    return result

def linear_regression(data):
    """Gibt (slope_per_day, intercept) zurück."""
    if len(data) < 2:
        return 0, data[0][1] if data else 0
    base_date = datetime.strptime(data[0][0], '%Y-%m-%d')
    xs = [(datetime.strptime(d[0], '%Y-%m-%d') - base_date).days for d in data]
    ys = [d[1] for d in data]
    n = len(xs)
    sx = sum(xs)
    sy = sum(ys)
    sxy = sum(x * y for x, y in zip(xs, ys))
    sxx = sum(x * x for x in xs)
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0, ys[0]
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return slope, intercept

def days_to_target(current_weight, target_weight, slope_per_day):
    if slope_per_day >= 0 or target_weight >= current_weight:
        return None
    return int((target_weight - current_weight) / slope_per_day)

def bmi(weight, height_m=HEIGHT_M):
    return round(weight / (height_m ** 2), 1)

def bmi_category(bmi_val):
    if bmi_val < 18.5:
        return "Untergewicht", "#63b3ed"
    if bmi_val < 25.0:
        return "Normalgewicht", "#4ade80"
    if bmi_val < 30.0:
        return "Übergewicht", "#fbbf24"
    return "Adipositas", "#f87171"


# ── 4. HTML generieren ────────────────────────────────────────────────────────

def generate_dashboard(data):
    if not data:
        print("  ✗ Keine Gewichtsdaten gefunden.")
        return None

    now_str      = datetime.now().strftime('%d.%m.%Y %H:%M')
    current_w    = data[-1][1]
    current_date = data[-1][0]

    # Trend (letzte 30 Tage)
    recent = [d for d in data if (datetime.strptime(data[-1][0], '%Y-%m-%d') -
                                   datetime.strptime(d[0], '%Y-%m-%d')).days <= 30]
    slope, _ = linear_regression(recent if len(recent) >= 2 else data)
    slope_30  = round(slope * 30, 2)  # Gewichtsveränderung in 30 Tagen

    # Gleitender 7-Tage-Durchschnitt
    ma7 = moving_average(data, 7)

    # Vollständige Trend-Projektion (nächste 90 Tage)
    # FIX (Juni 2026): Projektion am letzten 7-Tage-Mittel (ma7[-1]) verankern statt
    # am Regressions-Intercept. Sonst springt die Prognoselinie visuell mehrere kg
    # unter den letzten Messwert, weil die Regressionsgerade über die verrauschte
    # Punktwolke am rechten Rand systematisch tiefer läuft. Steigung bleibt gleich.
    last_date = datetime.strptime(data[-1][0], '%Y-%m-%d')
    anchor    = ma7[-1] if ma7 else current_w
    proj_dates = []
    proj_vals  = []
    for i in range(1, 91):
        pd = last_date + timedelta(days=i)
        proj_val = round(anchor + slope * i, 2)
        proj_dates.append(pd.strftime('%Y-%m-%d'))
        proj_vals.append(proj_val)

    # BMI
    bmi_val  = bmi(current_w)
    bmi_cat, bmi_color = bmi_category(bmi_val)

    # Gewichtsveränderung zu verschiedenen Zeitpunkten
    def weight_ago(days):
        target_dt = datetime.strptime(current_date, '%Y-%m-%d') - timedelta(days=days)
        candidates = [d for d in data
                     if abs((datetime.strptime(d[0], '%Y-%m-%d') - target_dt).days) <= 3]
        if not candidates:
            return None
        return min(candidates, key=lambda d: abs((datetime.strptime(d[0], '%Y-%m-%d') - target_dt).days))[1]

    w7   = weight_ago(7)
    w30  = weight_ago(30)
    w90  = weight_ago(90)

    def delta_str(ref):
        if ref is None:
            return '–'
        d = round(current_w - ref, 1)
        return f"{'▲' if d > 0 else '▼'} {abs(d)} kg"

    def delta_cls(ref):
        if ref is None:
            return 'neutral'
        d = current_w - ref
        return 'better' if d < 0 else ('worse' if d > 0 else 'neutral')

    # JSON-Daten für Charts
    dates_js   = json.dumps([d[0] for d in data])
    weights_js = json.dumps([d[1] for d in data])
    ma7_js     = json.dumps(ma7)
    proj_d_js  = json.dumps(proj_dates)
    proj_v_js  = json.dumps(proj_vals)

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Gewichts-Dashboard – Björn</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:#0d1017; --card:#161a23; --card2:#1f2330;
    --text:#e6e8ef; --muted:#7e8699; --accent:#5b8dee;
    --border:#252a38; --good:#4ade80; --warn:#fbbf24; --bad:#f87171;
    --purple:#a78bfa;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{
    background:var(--bg); color:var(--text);
    font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    max-width:1120px; margin:0 auto; padding:40px 24px 60px;
    font-feature-settings:'cv11','ss03';
    -webkit-font-smoothing:antialiased;
  }}
  header {{ margin-bottom:36px; }}
  header .eyebrow {{ font-size:0.72rem; color:var(--muted); text-transform:uppercase; letter-spacing:.12em; font-weight:600; margin-bottom:8px; }}
  header h1 {{ font-size:1.75rem; font-weight:700; letter-spacing:-0.02em; }}
  header .sub {{ color:var(--muted); font-size:0.88rem; margin-top:6px; }}
  .kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:16px; margin:28px 0 36px; }}
  .kpi {{ background:var(--card); border:1px solid var(--border); border-radius:14px; padding:18px 20px; transition:border-color .15s; }}
  .kpi:hover {{ border-color:#363c4d; }}
  .kpi-label {{ font-size:0.7rem; color:var(--muted); text-transform:uppercase; letter-spacing:.08em; font-weight:600; margin-bottom:8px; }}
  .kpi-value {{ font-size:1.5rem; font-weight:700; letter-spacing:-0.015em; font-variant-numeric:tabular-nums; }}
  .kpi-sub {{ font-size:0.75rem; color:var(--muted); margin-top:4px; }}
  .better {{ color:var(--good); }}
  .worse  {{ color:var(--bad); }}
  .neutral {{ color:var(--muted); }}
  section {{ background:var(--card); border:1px solid var(--border); border-radius:16px; padding:28px; margin-bottom:20px; }}
  section h2 {{ font-size:0.78rem; font-weight:600; color:var(--muted); text-transform:uppercase; letter-spacing:.12em; margin-bottom:20px; }}
  .chart-wrap {{ position:relative; height:420px; }}
  .settings {{ display:flex; flex-wrap:wrap; gap:28px; align-items:flex-end; margin-bottom:24px; }}
  .setting-group {{ display:flex; flex-direction:column; gap:8px; }}
  .setting-group label {{ font-size:0.72rem; color:var(--muted); text-transform:uppercase; letter-spacing:.08em; font-weight:600; }}
  .setting-group input[type=range] {{ width:200px; accent-color:var(--accent); }}
  .setting-group input[type=number] {{ background:var(--card2); border:1px solid var(--border); color:var(--text); border-radius:8px; padding:8px 12px; width:110px; font-size:0.92rem; font-family:inherit; font-variant-numeric:tabular-nums; }}
  .setting-val {{ color:var(--accent); font-weight:600; font-variant-numeric:tabular-nums; }}
  .bmi-bar {{ position:relative; height:14px; border-radius:7px; background:linear-gradient(to right, #63b3ed 0%,#63b3ed 18.5%,#4ade80 18.5%,#4ade80 25%,#fbbf24 25%,#fbbf24 30%,#f87171 30%,#f87171 100%); margin:10px 0; max-width:480px; }}
  .bmi-marker {{ position:absolute; top:-5px; width:3px; height:24px; background:#fff; border-radius:2px; transform:translateX(-50%); box-shadow:0 0 0 2px rgba(0,0,0,0.4); }}
  .bmi-labels {{ display:flex; justify-content:space-between; font-size:0.68rem; color:var(--muted); max-width:480px; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.86rem; font-variant-numeric:tabular-nums; }}
  th {{ text-align:left; padding:12px 14px; color:var(--muted); font-weight:600; font-size:0.7rem; text-transform:uppercase; letter-spacing:.08em; border-bottom:1px solid var(--border); }}
  td {{ padding:11px 14px; border-bottom:1px solid rgba(255,255,255,0.04); }}
  tr:hover td {{ background:rgba(255,255,255,0.025); }}
  .progress-bar {{ background:var(--card2); border-radius:10px; height:10px; margin-top:10px; overflow:hidden; }}
  .progress-fill {{ height:100%; border-radius:10px; background:linear-gradient(to right,var(--accent),var(--good)); transition:width .3s; }}
  footer {{ color:var(--muted); font-size:0.75rem; text-align:center; margin-top:32px; padding-top:18px; border-top:1px solid var(--border); }}
  @media(max-width:600px) {{ .settings {{ flex-direction:column; }} body {{ padding:24px 16px; }} .chart-wrap {{ height:340px; }} }}
</style>
</head>
<body>

<header>
  <div class="eyebrow">Gesundheits-Tracking</div>
  <h1>⚖️ Gewichts-Dashboard</h1>
  <div class="sub">Björn · Stand: {current_date} · Aktualisiert: {now_str}</div>
</header>

<!-- KPIs -->
<div class="kpis">
  <div class="kpi" style="border-top:3px solid var(--accent)">
    <div class="kpi-label">Aktuelles Gewicht</div>
    <div class="kpi-value" id="kpi-current">{current_w} kg</div>
    <div class="kpi-sub">{current_date}</div>
  </div>
  <div class="kpi" style="border-top:3px solid {bmi_color}">
    <div class="kpi-label">BMI</div>
    <div class="kpi-value" style="color:{bmi_color}">{bmi_val}</div>
    <div class="kpi-sub">{bmi_cat}</div>
  </div>
  <div class="kpi" style="border-top:3px solid {'var(--good)' if slope_30 < 0 else 'var(--bad)'}">
    <div class="kpi-label">Trend (30 Tage)</div>
    <div class="kpi-value {'better' if slope_30 < 0 else 'worse'}">{'+' if slope_30 > 0 else ''}{slope_30} kg</div>
    <div class="kpi-sub">Hochrechnung 30 Tage</div>
  </div>
  <div class="kpi" style="border-top:3px solid var(--muted)">
    <div class="kpi-label">vs. 7 Tage</div>
    <div class="kpi-value {delta_cls(w7)}">{delta_str(w7)}</div>
    <div class="kpi-sub">Veränderung</div>
  </div>
  <div class="kpi" style="border-top:3px solid var(--muted)">
    <div class="kpi-label">vs. 30 Tage</div>
    <div class="kpi-value {delta_cls(w30)}">{delta_str(w30)}</div>
    <div class="kpi-sub">Veränderung</div>
  </div>
  <div class="kpi" style="border-top:3px solid var(--muted)">
    <div class="kpi-label">vs. 90 Tage</div>
    <div class="kpi-value {delta_cls(w90)}">{delta_str(w90)}</div>
    <div class="kpi-sub">Veränderung</div>
  </div>
</div>

<!-- Einstellungen -->
<section>
  <h2>Einstellungen</h2>
  <div class="settings">
    <div class="setting-group">
      <label>Zielgewicht</label>
      <input type="number" id="target-input" value="85" min="50" max="150" step="0.5">
    </div>
    <div class="setting-group">
      <label>Kreatin-Abzug: <span class="setting-val" id="creatine-val">1.5 kg</span></label>
      <input type="range" id="creatine-slider" min="0" max="3" step="0.1" value="1.5">
    </div>
    <div class="setting-group">
      <label>Kreatin aktiv</label>
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;margin-top:4px">
        <input type="checkbox" id="creatine-active" checked style="width:18px;height:18px;accent-color:var(--accent)">
        <span style="font-size:0.9rem">Zweite Linie anzeigen</span>
      </label>
    </div>
  </div>

  <!-- Zielgewicht Fortschritt -->
  <div id="target-section">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <span style="font-size:0.85rem;color:var(--muted)">Fortschritt zum Zielgewicht</span>
      <span id="target-info" style="font-size:0.85rem;font-weight:600"></span>
    </div>
    <div class="progress-bar"><div class="progress-fill" id="progress-fill" style="width:0%"></div></div>
    <div id="target-days" style="font-size:0.78rem;color:var(--muted);margin-top:6px"></div>
  </div>
</section>

<!-- Hauptchart -->
<section>
  <h2>Gewichtsverlauf</h2>
  <div class="chart-wrap"><canvas id="weightChart"></canvas></div>
</section>

<!-- BMI -->
<section>
  <h2>BMI-Einordnung</h2>
  <div style="display:flex;align-items:center;gap:20px;flex-wrap:wrap">
    <div>
      <div style="font-size:2rem;font-weight:700;color:{bmi_color}">{bmi_val}</div>
      <div style="font-size:0.85rem;color:{bmi_color}">{bmi_cat}</div>
      <div style="font-size:0.78rem;color:var(--muted);margin-top:4px">Größe: {HEIGHT_M*100:.0f} cm</div>
    </div>
    <div style="flex:1;min-width:200px">
      <div class="bmi-bar">
        <div class="bmi-marker" id="bmi-marker" style="left:{min(max((bmi_val-15)/(45-15)*100,0),100):.1f}%"></div>
      </div>
      <div class="bmi-labels">
        <span>15</span><span>Unter&shy;gew.</span><span>Normal</span><span>Über&shy;gew.</span><span>Adip.</span><span>45</span>
      </div>
    </div>
  </div>
  <div id="bmi-info" style="margin-top:16px;font-size:0.85rem;color:var(--muted)">
    {_bmi_text(bmi_val, bmi_cat)}
  </div>
</section>

<!-- Letzte Messungen -->
<section>
  <h2>Letzte 20 Messungen</h2>
  <table>
    <thead><tr><th>Datum</th><th>Gewicht</th><th>BMI</th><th>7-Tage-Ø</th><th>Ohne Kreatin</th></tr></thead>
    <tbody id="hist-table"></tbody>
  </table>
</section>

<footer>Daten: Garmin / Health Connect · Automatisch generiert · {now_str}</footer>

<script>
const RAW_DATES   = {dates_js};
const RAW_WEIGHTS = {weights_js};
const MA7         = {ma7_js};
const PROJ_DATES  = {proj_d_js};
const PROJ_VALS   = {proj_v_js};
const HEIGHT_M    = {HEIGHT_M};

let chart = null;

function bmi(w) {{ return Math.round(w / (HEIGHT_M * HEIGHT_M) * 10) / 10; }}

function updateDashboard() {{
  const target     = parseFloat(document.getElementById('target-input').value) || 85;
  const creatine   = parseFloat(document.getElementById('creatine-slider').value);
  const showCreat  = document.getElementById('creatine-active').checked;
  document.getElementById('creatine-val').textContent = creatine.toFixed(1) + ' kg';

  const currentW   = RAW_WEIGHTS[RAW_WEIGHTS.length - 1];
  const creatW     = Math.round((currentW - creatine) * 10) / 10;

  // KPI aktualisieren
  document.getElementById('kpi-current').textContent = currentW + ' kg';

  // Zielgewicht
  const startW = RAW_WEIGHTS[0];
  const totalDiff = startW - target;
  const doneDiff  = startW - currentW;
  const pct = totalDiff > 0 ? Math.min(Math.max(doneDiff / totalDiff * 100, 0), 100) : 0;
  document.getElementById('progress-fill').style.width = pct.toFixed(1) + '%';

  const remaining = Math.round((currentW - target) * 10) / 10;
  document.getElementById('target-info').textContent =
    remaining > 0 ? `Noch ${{remaining}} kg bis Ziel (${{target}} kg)` :
    remaining < 0 ? `Ziel um ${{Math.abs(remaining)}} kg unterschritten ✓` : 'Ziel erreicht! ✓';

  // Projektion Tage
  let daysText = '';
  for (let i = 0; i < PROJ_VALS.length; i++) {{
    if (PROJ_VALS[i] <= target) {{
      daysText = `📅 Bei aktuellem Trend: Ziel voraussichtlich in ${{i+1}} Tagen erreicht (${{PROJ_DATES[i]}})`;
      break;
    }}
  }}
  if (!daysText && PROJ_VALS[PROJ_VALS.length-1] > target) {{
    daysText = 'ℹ️ Bei aktuellem Trend wird das Ziel innerhalb von 90 Tagen nicht erreicht.';
  }}
  document.getElementById('target-days').textContent = daysText;

  // Chart
  const withoutCreat = showCreat ? RAW_WEIGHTS.map(w => Math.round((w - creatine) * 10) / 10) : null;

  const allDates = [...RAW_DATES, ...PROJ_DATES];

  if (chart) chart.destroy();
  const ctx = document.getElementById('weightChart').getContext('2d');
  chart = new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: allDates,
      datasets: [
        {{
          label: 'Messung',
          data: [...RAW_WEIGHTS, ...Array(PROJ_DATES.length).fill(null)],
          borderColor: '#5b8dee',
          backgroundColor: 'rgba(91,141,238,0.06)',
          borderWidth: 1.25,
          pointRadius: 0,
          pointHoverRadius: 5,
          pointHoverBackgroundColor: '#5b8dee',
          pointHoverBorderColor: '#fff',
          pointHoverBorderWidth: 2,
          tension: 0.32,
          fill: true,
        }},
        {{
          label: '7-Tage-Mittel',
          data: [...MA7, ...Array(PROJ_DATES.length).fill(null)],
          borderColor: '#a78bfa',
          borderWidth: 1.5,
          pointRadius: 0,
          pointHoverRadius: 0,
          tension: 0.4,
          fill: false,
        }},
        showCreat ? {{
          label: `Ohne Kreatin (-${{creatine.toFixed(1)}} kg)`,
          data: [...withoutCreat, ...Array(PROJ_DATES.length).fill(null)],
          borderColor: '#4ade80',
          borderWidth: 1,
          borderDash: [3, 3],
          pointRadius: 0,
          tension: 0.32,
          fill: false,
        }} : null,
        {{
          label: 'Prognose (Trend)',
          data: [...Array(RAW_DATES.length).fill(null), ...PROJ_VALS],
          borderColor: '#fbbf24',
          borderWidth: 1.25,
          borderDash: [5, 3],
          pointRadius: 0,
          tension: 0.2,
          fill: false,
        }},
        {{
          label: `Ziel (${{target}} kg)`,
          data: allDates.map(() => target),
          borderColor: 'rgba(126,134,153,0.5)',
          borderWidth: 1,
          borderDash: [2, 4],
          pointRadius: 0,
          fill: false,
        }},
      ].filter(Boolean),
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{
        legend: {{
          position: 'bottom',
          labels: {{
            color: '#7e8699',
            font: {{ family: 'Inter', size: 11, weight: '500' }},
            padding: 18, boxWidth: 14, boxHeight: 2, usePointStyle: false
          }}
        }},
        tooltip: {{
          backgroundColor: '#161a23',
          borderColor: '#252a38',
          borderWidth: 1,
          titleColor: '#e6e8ef',
          bodyColor: '#7e8699',
          padding: 12,
          cornerRadius: 8,
          titleFont: {{ family: 'Inter', size: 12, weight: '600' }},
          bodyFont: {{ family: 'Inter', size: 11 }},
          callbacks: {{
            label: ctx => ` ${{ctx.dataset.label}}: ${{ctx.parsed.y !== null ? ctx.parsed.y.toFixed(1) + ' kg' : '–'}}`,
          }}
        }}
      }},
      scales: {{
        x: {{
          ticks: {{ color: '#7e8699', maxTicksLimit: 10, font: {{ family: 'Inter', size: 10 }} }},
          grid: {{ color: 'rgba(255,255,255,0.03)', drawBorder: false }},
        }},
        y: {{
          ticks: {{ color: '#7e8699', font: {{ family: 'Inter', size: 10 }}, callback: v => v + ' kg' }},
          grid: {{ color: 'rgba(255,255,255,0.05)', drawBorder: false }},
        }}
      }}
    }}
  }});

  // Tabelle
  const tbody = document.getElementById('hist-table');
  tbody.innerHTML = '';
  const last20 = RAW_DATES.slice(-20).reverse();
  last20.forEach((d, i) => {{
    const idx  = RAW_DATES.length - 1 - i;
    const w    = RAW_WEIGHTS[idx];
    const ma   = MA7[idx];
    const noC  = Math.round((w - creatine) * 10) / 10;
    const b    = bmi(w);
    tbody.innerHTML += `<tr>
      <td>${{d}}</td>
      <td><strong>${{w}} kg</strong></td>
      <td>${{b}}</td>
      <td style="color:var(--muted)">${{ma}} kg</td>
      <td style="color:#48bb78">${{noC}} kg</td>
    </tr>`;
  }});
}}

document.getElementById('target-input').addEventListener('input', updateDashboard);
document.getElementById('creatine-slider').addEventListener('input', updateDashboard);
document.getElementById('creatine-active').addEventListener('change', updateDashboard);

updateDashboard();
</script>
</body>
</html>"""


def _bmi_text(bmi_val, cat):
    if cat == 'Normalgewicht':
        return f'Dein BMI von {bmi_val} liegt im Normalbereich (18.5–24.9). Weiter so!'
    if cat == 'Übergewicht':
        return f'Dein BMI von {bmi_val} liegt im Bereich Übergewicht (25–29.9). Ein Abbau von 5–10% des Körpergewichts reduziert das kardiovaskuläre Risiko deutlich.'
    if cat == 'Adipositas':
        return f'Dein BMI von {bmi_val} liegt im Bereich Adipositas (≥30). Eine ärztliche Begleitung bei der Gewichtsreduktion wird empfohlen.'
    return f'Dein BMI von {bmi_val} liegt im Bereich Untergewicht (<18.5).'


# ── 5. Haupt-Routine ──────────────────────────────────────────────────────────

def main():
    print("▶ Gewichts-Dashboard …")

    data = load_weight_data()
    if not data:
        print("  ✗ Keine Daten – Dashboard wird nicht generiert.")
        sys.exit(0)  # Kein harter Fehler – Workflow soll weiterlaufen

    html = generate_dashboard(data)
    if not html:
        sys.exit(0)

    out_path = os.path.join(OUTPUT_DIR, 'gewicht_dashboard.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"  ✓ Gespeichert: {out_path}")


if __name__ == '__main__':
    main()
    
