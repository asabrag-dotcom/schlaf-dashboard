#!/usr/bin/env python3
"""
Google Drive → lokaler Ordner (für GitHub Actions)
Lädt alle CSV-Dateien aus den drei Gesundheitsdaten-Ordnern herunter.
 
Umgebungsvariablen:
  SERVICE_ACCOUNT_FILE  – Pfad zur Service-Account-JSON
  DRIVE_DATA_PATH       – Zielordner (Standard: /tmp/drive_data)
  DRIVE_SCHLAF_ID       – Folder-ID: Health Sync Schlaf
  DRIVE_SPO2_ID         – Folder-ID: Health Sync Sauerstoffsättigung
  DRIVE_PULS_ID         – Folder-ID: Health Sync Puls
"""
 
import os
import io
import sys
import json
import re
import base64
import time
import random
 
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from googleapiclient.errors import HttpError
 
SCOPES          = ['https://www.googleapis.com/auth/drive.readonly']
DRIVE_DATA_PATH = os.environ.get('DRIVE_DATA_PATH', '/tmp/drive_data')
 
# Retry-Konfiguration für transiente Google-API-Fehler
MAX_RETRIES     = 4          # bis zu 4 Versuche gesamt (1 initial + 3 Retries)
RETRY_STATUS    = {429, 500, 502, 503, 504}  # was gilt als "transient"
BACKOFF_BASE_S  = 1.0        # Startwartezeit; verdoppelt sich pro Retry
 
 
def with_retries(fn, *, what='API-Aufruf'):
    """
    Führt fn() aus und wiederholt bei transienten HTTP-Fehlern (429/5xx).
    Exponential Backoff mit leichtem Jitter, damit paralleles Retry nicht synchron feuert.
    Bei nicht-transienten Fehlern sofortiges Weitergeben. Nach MAX_RETRIES letzter Fehler.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except HttpError as e:
            status = getattr(e.resp, 'status', None)
            try:
                status = int(status)
            except (TypeError, ValueError):
                status = None
 
            if status not in RETRY_STATUS or attempt >= MAX_RETRIES:
                raise
 
            wait = BACKOFF_BASE_S * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            print(f"    ⟳ {what}: HTTP {status}, Versuch {attempt}/{MAX_RETRIES}, warte {wait:.1f}s …")
            time.sleep(wait)
 
 
def parse_sa_info(raw: str) -> dict:
    """
    Versucht Service-Account-JSON zu parsen.
    Unterstützt: Base64-kodiert, reines JSON, JSON mit unescapten Zeilenumbrüchen.
    """
    raw = raw.strip()
 
    # 1. Base64-dekodiert?
    if not raw.startswith('{'):
        try:
            decoded = base64.b64decode(raw).decode('utf-8')
            return json.loads(decoded)
        except Exception:
            pass
 
    # 2. Direktes JSON
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
 
    # 3. Echte Zeilenumbrüche im private_key reparieren
    try:
        fixed = re.sub(
            r'("private_key"\s*:\s*")(.*?)(?="[,\s]*\n?\s*"(?:client_email|client_id))',
            lambda m: m.group(1) + m.group(2).replace('\n', '\\n').replace('\r', ''),
            raw, flags=re.DOTALL
        )
        return json.loads(fixed)
    except Exception:
        pass
 
    raise ValueError(
        "Service-Account-JSON konnte nicht geparst werden.\n"
        "Bitte das Secret als Base64 speichern:\n"
        "  PowerShell: [Convert]::ToBase64String([IO.File]::ReadAllBytes('pfad\\zur\\datei.json')) | clip"
    )
 
 
def load_credentials():
    """Lädt Service-Account-Credentials aus Umgebungsvariable."""
    sa_raw = os.environ.get('GDRIVE_SERVICE_ACCOUNT', '')
    if sa_raw:
        info = parse_sa_info(sa_raw)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
 
    # Fallback: aus Datei
    sa_file = os.environ.get('SERVICE_ACCOUNT_FILE', '')
    if sa_file and os.path.exists(sa_file):
        with open(sa_file, encoding='utf-8') as f:
            info = parse_sa_info(f.read())
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
 
    raise EnvironmentError("Keine Service-Account-Credentials gefunden.")
 
FOLDERS = {
    'Health Sync Schlaf':             os.environ['DRIVE_SCHLAF_ID'],
    'Health Sync Sauerstoffsättigung': os.environ['DRIVE_SPO2_ID'],
    'Health Sync Puls':               os.environ['DRIVE_PULS_ID'],
    'Health Sync Gewicht':            os.environ.get('DRIVE_GEWICHT_ID', ''),
}
# Leere Folder-IDs überspringen (optionale Ordner)
FOLDERS = {k: v for k, v in FOLDERS.items() if v}
 
 
def download_folder(service, folder_id, local_dir):
    os.makedirs(local_dir, exist_ok=True)
 
    # Ordnerinhalt listen — mit Retry gegen transiente Fehler
    results = with_retries(
        lambda: service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields='files(id, name, mimeType)',
            pageSize=1000
        ).execute(),
        what='files.list'
    )
 
    files = results.get('files', [])
    print(f"  {len(files)} Dateien gefunden")
 
    fehler = []
    for f in files:
        if f['mimeType'] == 'application/vnd.google-apps.folder':
            continue
        if not f['name'].endswith('.csv'):
            continue
 
        dest = os.path.join(local_dir, f['name'])
 
        try:
            # Kompletten Datei-Download mit Retry umhüllen. Bei Chunk-Fehler
            # verwerfen wir das Buffer und starten den Download neu — sicherer
            # als mitten in einem MediaIoBaseDownload weiterzumachen.
            def _lade():
                request = service.files().get_media(fileId=f['id'])
                buf = io.BytesIO()
                dl = MediaIoBaseDownload(buf, request)
                done = False
                while not done:
                    _, done = dl.next_chunk()
                return buf.getvalue()
 
            data = with_retries(_lade, what=f'download {f["name"]}')
 
            with open(dest, 'wb') as fp:
                fp.write(data)
            print(f"  ✓ {f['name']}")
 
        except HttpError as e:
            # Nach allen Retries immer noch tot — Datei überspringen,
            # aber nicht den ganzen Lauf killen. Andere Dateien und
            # Folgeschritte (Schlaf-Dashboard, Gewicht, Push) sollen laufen.
            status = getattr(e.resp, 'status', '?')
            print(f"  ✗ {f['name']} nicht ladbar (HTTP {status}), überspringe.")
            fehler.append((f['name'], status))
 
    if fehler:
        print(f"  ⚠ {len(fehler)} Datei(en) konnten nicht geladen werden — Lauf geht trotzdem weiter.")
 
 
def main():
    creds   = load_credentials()
    service = build('drive', 'v3', credentials=creds, cache_discovery=False)
 
    for name, fid in FOLDERS.items():
        print(f"\n▶ {name} …")
        download_folder(service, fid, os.path.join(DRIVE_DATA_PATH, name))
 
    print('\nDownload abgeschlossen.')
 
 
if __name__ == '__main__':
    main()
