#!/usr/bin/env python3
"""Daily automated run for StockPilot.

Sequence:
  1. pipeline   — collect → score → news → predict → explain (refreshes the db)
  2. upload     — push the SQLite db + price cache to PythonAnywhere
  3. reload     — restart the live web app so the site serves fresh data
  4. digest     — email the top-10 signals to the configured recipient

Scheduled by Windows Task Scheduler via run_daily.bat. Each step is wrapped so
one failure is logged but does not abort the rest; the script exits non-zero if
anything failed, so Task Scheduler's "Last Run Result" flags it.

Run it by hand any time:  python daily_update.py
"""

from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# UTF-8 console so ✓/• and company names never crash on Windows cp1252.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

from src import config


def _hr(title: str) -> None:
    print(f"\n{'=' * 60}\n{title}  ({dt.datetime.now():%Y-%m-%d %H:%M:%S})\n"
          f"{'=' * 60}", flush=True)


def step_pipeline() -> None:
    from main import cmd_pipeline
    cmd_pipeline(None)


def step_upload_and_reload() -> None:
    """Push data files to PythonAnywhere and reload the web app.

    Data only — never touches the shared WSGI dispatcher. Throttled because
    the Files API rate-limits at ~40 requests/minute (HTTP 429).
    """
    import requests

    user = config.get("pythonanywhere_username")
    token = config.get("pythonanywhere_token")
    api = f"https://www.pythonanywhere.com/api/v0/user/{user}"
    headers = {"Authorization": f"Token {token}"}
    remote_root = f"/home/{user}/stockpilot"

    files: list[tuple[Path, str]] = [
        (ROOT / "data" / "stocks.db", f"{remote_root}/data/stocks.db"),
    ]
    for p in sorted((ROOT / "data" / "cache").glob("prices_*.json")):
        files.append((p, f"{remote_root}/data/cache/{p.name}"))

    print(f"Uploading {len(files)} files to {remote_root} …", flush=True)
    for local, remote in files:
        for attempt in range(6):
            resp = requests.post(f"{api}/files/path{remote}", headers=headers,
                                 files={"content": local.read_bytes()},
                                 timeout=120)
            if resp.status_code == 429:        # rate limited — back off
                time.sleep(20)
                continue
            resp.raise_for_status()
            break
        else:
            raise RuntimeError(f"gave up uploading {remote} after retries")
        time.sleep(1.6)

    print("Reloading web app …", flush=True)
    resp = requests.post(f"{api}/webapps/{user}.pythonanywhere.com/reload/",
                         headers=headers, timeout=180)
    resp.raise_for_status()
    print(f"✓ Live site reloaded: "
          f"https://{user.lower()}.pythonanywhere.com/stockpilot/api/stats")


def step_digest() -> None:
    from src.digest import send_digest
    result = send_digest()
    if result.get("sent"):
        print(f"✓ Digest emailed to {result['to']} "
              f"({result.get('signals', '?')} signals)")
    else:
        raise RuntimeError(f"digest not sent: {result.get('error')}")


STEPS = [
    ("PIPELINE", step_pipeline),
    ("UPLOAD + RELOAD", step_upload_and_reload),
    ("DIGEST", step_digest),
]


def main() -> int:
    started = dt.datetime.now()
    print(f"\n########## StockPilot daily run — {started:%Y-%m-%d %H:%M:%S} "
          f"##########", flush=True)
    failures: list[str] = []
    for name, fn in STEPS:
        _hr(name)
        try:
            fn()
        except Exception as exc:                       # log, keep going
            failures.append(name)
            print(f"✗ {name} FAILED: {type(exc).__name__}: {exc}", flush=True)
            import traceback
            traceback.print_exc()

    elapsed = (dt.datetime.now() - started).total_seconds()
    if failures:
        print(f"\n########## DONE with errors in {elapsed:.0f}s — "
              f"failed: {', '.join(failures)} ##########", flush=True)
        # Surface the failure by email instead of only logging it.
        try:
            from src.digest import send_alert
            send_alert(
                f"⚠ StockPilot daily run FAILED ({', '.join(failures)})",
                f"The daily run at {started:%Y-%m-%d %H:%M} failed at: "
                f"{', '.join(failures)}.\n\nCheck the log on the machine for "
                f"the traceback.")
        except Exception as exc:
            print(f"(could not send failure alert: {exc})", flush=True)
        return 1
    print(f"\n########## DONE — all steps OK in {elapsed:.0f}s ##########",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
