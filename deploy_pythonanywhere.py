#!/usr/bin/env python3
"""Upload the StockPilot backend to PythonAnywhere and reload the web app.

    python deploy_pythonanywhere.py

Uploads dashboard/, src/, config.json, wsgi.py, requirements.txt, the SQLite
db, and the price cache to /home/<user>/stockpilot/, installs a dispatcher
WSGI that routes /stockpilot/* to this app and everything else to the
existing JobPilot app, then reloads the site via the API.
"""

from __future__ import annotations

import sys
from pathlib import Path

import requests

from src import config

ROOT = Path(__file__).resolve().parent
USER = config.get("pythonanywhere_username")
TOKEN = config.get("pythonanywhere_token")
API = f"https://www.pythonanywhere.com/api/v0/user/{USER}"
HEADERS = {"Authorization": f"Token {TOKEN}"}
REMOTE_ROOT = f"/home/{USER}/stockpilot"
DOMAIN = f"{USER.lower()}.pythonanywhere.com"

# Dispatcher: both JobPilot and StockPilot define `dashboard.app`, so import
# StockPilot first, purge the colliding module names, then import JobPilot.
WSGI_DISPATCHER = f"""\
import os
import sys

STOCKPILOT = '{REMOTE_ROOT}'
JOBPILOT = '/home/{USER}/jobpilot'

sys.path.insert(0, STOCKPILOT)
from dashboard.app import app as stockpilot_app
for name in list(sys.modules):
    if name in ('dashboard', 'src') or name.startswith(('dashboard.', 'src.')):
        del sys.modules[name]
sys.path.remove(STOCKPILOT)

if JOBPILOT not in sys.path:
    sys.path.insert(0, JOBPILOT)
os.chdir(JOBPILOT)
from dashboard.app import app as jobpilot_app


def application(environ, start_response):
    if environ.get('PATH_INFO', '').startswith('/stockpilot'):
        return stockpilot_app(environ, start_response)
    return jobpilot_app(environ, start_response)
"""


def upload(local: Path, remote: str) -> None:
    url = f"{API}/files/path{remote}"
    resp = requests.post(url, headers=HEADERS,
                         files={"content": local.read_bytes()}, timeout=120)
    resp.raise_for_status()
    print(f"  {local.relative_to(ROOT)} -> {remote}")


def main() -> int:
    files: list[tuple[Path, str]] = [
        (ROOT / "config.json", f"{REMOTE_ROOT}/config.json"),
        (ROOT / "wsgi.py", f"{REMOTE_ROOT}/wsgi.py"),
        (ROOT / "requirements.txt", f"{REMOTE_ROOT}/requirements.txt"),
        (ROOT / "main.py", f"{REMOTE_ROOT}/main.py"),
        (ROOT / "data" / "stocks.db", f"{REMOTE_ROOT}/data/stocks.db"),
    ]
    for p in sorted((ROOT / "src").glob("*.py")):
        files.append((p, f"{REMOTE_ROOT}/src/{p.name}"))
    for p in sorted((ROOT / "dashboard").glob("*.py")):
        files.append((p, f"{REMOTE_ROOT}/dashboard/{p.name}"))
    for p in sorted((ROOT / "data" / "cache").glob("prices_*.json")):
        files.append((p, f"{REMOTE_ROOT}/data/cache/{p.name}"))

    print(f"Uploading {len(files)} files to {REMOTE_ROOT} …")
    for local, remote in files:
        upload(local, remote)

    print("Updating WSGI dispatcher …")
    wsgi_path = f"/var/www/{USER.lower()}_pythonanywhere_com_wsgi.py"
    resp = requests.post(f"{API}/files/path{wsgi_path}", headers=HEADERS,
                         files={"content": WSGI_DISPATCHER.encode()},
                         timeout=60)
    resp.raise_for_status()

    print("Reloading web app …")
    resp = requests.post(f"{API}/webapps/{DOMAIN}/reload/", headers=HEADERS,
                         timeout=120)
    resp.raise_for_status()
    print(f"✓ Reloaded. Try: https://{DOMAIN}/stockpilot/api/stats")
    return 0


if __name__ == "__main__":
    sys.exit(main())
