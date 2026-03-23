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

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES            = ['https://www.googleapis.com/auth/drive.readonly']
SERVICE_ACCT_FILE = os.environ['SERVICE_ACCOUNT_FILE']
DRIVE_DATA_PATH   = os.environ.get('DRIVE_DATA_PATH', '/tmp/drive_data')

FOLDERS = {
    'Health Sync Schlaf':             os.environ['DRIVE_SCHLAF_ID'],
    'Health Sync Sauerstoffsättigung': os.environ['DRIVE_SPO2_ID'],
    'Health Sync Puls':               os.environ['DRIVE_PULS_ID'],
}


def download_folder(service, folder_id, local_dir):
    os.makedirs(local_dir, exist_ok=True)

    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields='files(id, name, mimeType)',
        pageSize=1000
    ).execute()

    files = results.get('files', [])
    print(f"  {len(files)} Dateien gefunden")

    for f in files:
        if f['mimeType'] == 'application/vnd.google-apps.folder':
            continue
        if not f['name'].endswith('.csv'):
            continue

        dest = os.path.join(local_dir, f['name'])
        request = service.files().get_media(fileId=f['id'])
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = dl.next_chunk()
        with open(dest, 'wb') as fp:
            fp.write(buf.getvalue())
        print(f"  ✓ {f['name']}")


def main():
    creds   = service_account.Credentials.from_service_account_file(
                  SERVICE_ACCT_FILE, scopes=SCOPES)
    service = build('drive', 'v3', credentials=creds, cache_discovery=False)

    for name, fid in FOLDERS.items():
        print(f"\n▶ {name} …")
        download_folder(service, fid, os.path.join(DRIVE_DATA_PATH, name))

    print('\nDownload abgeschlossen.')


if __name__ == '__main__':
    main()
