"""Price history via yfinance, cached to data/cache/prices_<T>.json.

fetch_history()  — OHLCV DataFrame for the predictor
fetch_meta()     — company name + annualized volatility (for risk labels)
chart_series()   — downsampled close series for the dashboard chart
"""

from __future__ import annotations

import json
import math
import time

from . import config

CACHE_TTL_HOURS = 12


def _cache_path(ticker: str):
    return config.CACHE_DIR / f"prices_{ticker.upper()}.json"


def prefetch_histories(tickers: list[str], max_workers: int = 8) -> int:
    """Warm the price cache for many tickers in parallel (yfinance has no hard
    rate limit). Subsequent fetch_history() calls then hit disk. Returns the
    count successfully fetched."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    todo = []
    for t in tickers:
        p = _cache_path(t)
        fresh = (p.exists()
                 and (time.time() - p.stat().st_mtime) < CACHE_TTL_HOURS * 3600)
        if not fresh:
            todo.append(t)
    if not todo:
        return 0
    ok = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(fetch_history, t): t for t in todo}
        for fut in as_completed(futs):
            try:
                if fut.result() is not None:
                    ok += 1
            except Exception:
                pass
    return ok


def fetch_history(ticker: str, period: str = "2y"):
    """Daily OHLCV DataFrame (lowercase columns + 'timestamps'), or None."""
    import pandas as pd
    import yfinance as yf

    path = _cache_path(ticker)
    if path.exists() and (time.time() - path.stat().st_mtime
                          ) < CACHE_TTL_HOURS * 3600:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            df = pd.DataFrame(raw["rows"])
            df["timestamps"] = pd.to_datetime(df["timestamps"])
            return df
        except Exception:
            pass

    df = yf.download(ticker, period=period, interval="1d", auto_adjust=True,
                     progress=False)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)
    cols = [c for c in ("open", "high", "low", "close", "volume")
            if c in df.columns]
    df = df[cols].dropna().reset_index()
    df = df.rename(columns={df.columns[0]: "timestamps"})
    if len(df) < 60:
        return None

    out = df.copy()
    out["timestamps"] = out["timestamps"].dt.strftime("%Y-%m-%d")
    path.write_text(json.dumps({"rows": out.to_dict("records")}),
                    encoding="utf-8")
    df["timestamps"] = pd.to_datetime(df["timestamps"])
    return df


def fetch_meta(tickers: list[str]) -> dict[str, dict]:
    """{ticker: {name, ann_vol, last_price}} — name via Finnhub profile
    (fast), volatility from cached yfinance history."""
    from .util import cached_json

    key = config.get("finnhub_api_key", "")
    out: dict[str, dict] = {}
    for t in tickers:
        info: dict = {}
        if key:
            try:
                prof = cached_json(
                    "https://finnhub.io/api/v1/stock/profile2"
                    f"?symbol={t}&token={key}",
                    ttl_hours=24 * 7, min_interval=1.1)
                if prof.get("name"):
                    info["name"] = prof["name"]
            except Exception:
                pass
        df = fetch_history(t)
        if df is not None:
            daily = df["close"].pct_change().dropna()
            if len(daily) >= 30:
                info["ann_vol"] = float(daily.tail(63).std() * math.sqrt(252))
            info["last_price"] = round(float(df["close"].iloc[-1]), 2)
        info.setdefault("name", t)
        out[t] = info
    return out


def chart_series(ticker: str, max_points: int = 180) -> dict | None:
    df = fetch_history(ticker)
    if df is None:
        return None
    step = max(1, len(df) // max_points)
    sampled = df.iloc[::step]
    if sampled.index[-1] != df.index[-1]:
        import pandas as pd
        sampled = pd.concat([sampled, df.iloc[[-1]]])
    return {
        "dates": [d.strftime("%Y-%m-%d") for d in sampled["timestamps"]],
        "close": [round(float(v), 4) for v in sampled["close"]],
    }
