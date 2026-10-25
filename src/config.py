"""Configuration + project paths.

Values are read from environment variables first (e.g. FINNHUB_API_KEY),
falling back to config.json. This lets secrets live in the environment in
production while config.json stays convenient for local dev — and keeps the
plaintext file out of version control (see .gitignore).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "stocks.db"
# User-mutable watchlist lives in its OWN database so the daily pipeline
# upload (which overwrites stocks.db) never clobbers it.
WATCHLIST_DB_PATH = DATA_DIR / "watchlist.db"

_config: dict | None = None

# Keys whose JSON values are containers, not scalars — never overridden by env.
_NON_SCALAR = {"funds", "watchlist"}


def load() -> dict:
    global _config
    if _config is None:
        if CONFIG_PATH.exists():
            _config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        else:
            _config = {}
    return _config


def get(key: str, default=None):
    """Env var (KEY uppercased) wins over config.json for scalar settings."""
    if key not in _NON_SCALAR:
        env = os.environ.get(key.upper())
        if env is not None:
            return env
    return load().get(key, default)
