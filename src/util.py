"""Shared HTTP helpers: rate-limited session with SEC-compliant User-Agent
and a small on-disk TTL cache (UTF-8 everywhere — Windows-safe)."""

from __future__ import annotations

import hashlib
import json
import threading
import time

import requests

from . import config

config.CACHE_DIR.mkdir(parents=True, exist_ok=True)

_session: requests.Session | None = None
_last_call: dict[str, float] = {}
# Per-host spacing is shared across threads (we now fetch SEC concurrently),
# so the wait-then-stamp must be atomic or threads under-space and risk a ban.
_rate_lock = threading.Lock()


def session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": config.get("sec_user_agent",
                                     "StockPilot research tool"),
            "Accept-Encoding": "gzip, deflate",
        })
        _session = s
    return _session


def get(url: str, *, min_interval: float = 0.0, timeout: int = 30, **kw):
    """GET with thread-safe per-host politeness spacing (SEC limit 10 req/s)."""
    host = url.split("/")[2]
    if min_interval:
        with _rate_lock:
            wait = _last_call.get(host, 0) + min_interval - time.time()
            if wait > 0:
                time.sleep(wait)
            _last_call[host] = time.time()
    resp = session().get(url, timeout=timeout, **kw)
    if not min_interval:
        _last_call[host] = time.time()
    return resp


def cached_json(url: str, ttl_hours: float = 24.0, *,
                min_interval: float = 0.11, **kw):
    key = hashlib.sha1(url.encode()).hexdigest()[:24]
    path = config.CACHE_DIR / f"{key}.json"
    if path.exists() and (time.time() - path.stat().st_mtime) < ttl_hours * 3600:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    resp = get(url, min_interval=min_interval, **kw)
    resp.raise_for_status()
    data = resp.json()
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def cached_text(url: str, ttl_hours: float = 24.0, *,
                min_interval: float = 0.11, **kw) -> str:
    key = hashlib.sha1(url.encode()).hexdigest()[:24]
    path = config.CACHE_DIR / f"{key}.txt"
    if path.exists() and (time.time() - path.stat().st_mtime) < ttl_hours * 3600:
        return path.read_text(encoding="utf-8")
    resp = get(url, min_interval=min_interval, **kw)
    resp.raise_for_status()
    path.write_text(resp.text, encoding="utf-8")
    return resp.text
