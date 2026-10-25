"""Options-market positioning overlay via yfinance option chains.

Display-only: never feeds `src.scoring`. Fetches the 2-3 nearest monthly
expirations (20-90 DTE) for the top-N convergence-score tickers, computes
put/call and IV-rank metrics, and upserts one row per (ticker, date) into
`options_flow` for the signals dashboard, the digest, and the backtest
validation join.

Normalized data shapes (metric functions never touch pandas/yfinance
directly, so they're testable without network or pandas):
  contract = {"strike": float, "openInterest": float|None,
              "volume": float|None, "impliedVolatility": float|None}
  chain    = {"expiration": "YYYY-MM-DD", "calls": [contract, ...],
              "puts": [contract, ...]}
  chains   = list[chain]  (all fetched expirations for one ticker)

Copy of the yfinance-in-a-try / normalize-to-plain-Python pattern used by
`src.prices.fetch_history`, and the `run_*` orchestrator shape used by
`src.news_sentiment.run_news`.
"""

from __future__ import annotations

import datetime as dt
import math
import time

from . import config, db

MAX_TICKERS_DEFAULT = 15      # config options_flow.max_tickers overrides
DTE_MIN, DTE_MAX = 20, 90     # expiration window (days to expiry)
MAX_EXPIRATIONS = 3           # take the 2-3 nearest monthlies in-window
ATM_PCT = 0.05                # +-5% of spot = "at the money"
THROTTLE_SEC = 1.0            # sleep between per-ticker chain fetches
UNUSUAL_RATIO = 1.0           # total option volume / total OI strictly > this
SKEW_BULLISH, SKEW_BEARISH = 0.8, 1.2   # mean-ratio bands (edges = neutral)
IV_RANK_MIN_HISTORY = 10      # prior daily snapshots required for a rank


# --------------------------------------------------------------------------
# Pure metric functions (chain data in -> scalar/None out, never raise)
# --------------------------------------------------------------------------

def _sum(contracts: list[dict], field: str) -> float:
    """Sum of a field over contracts, treating None as 0."""
    return sum((c.get(field) or 0) for c in contracts)


def pc_oi_ratio(chains: list[dict]) -> float | None:
    """total put OI / total call OI over all chains; None if call OI == 0
    or chains empty."""
    if not chains:
        return None
    call_oi = sum(_sum(c["calls"], "openInterest") for c in chains)
    put_oi = sum(_sum(c["puts"], "openInterest") for c in chains)
    if call_oi == 0:
        return None
    return round(put_oi / call_oi, 4)


def pc_vol_ratio(chains: list[dict]) -> float | None:
    """total put volume / total call volume; None if call volume == 0
    or chains empty."""
    if not chains:
        return None
    call_vol = sum(_sum(c["calls"], "volume") for c in chains)
    put_vol = sum(_sum(c["puts"], "volume") for c in chains)
    if call_vol == 0:
        return None
    return round(put_vol / call_vol, 4)


def atm_iv(chains: list[dict], spot: float | None) -> float | None:
    """Median impliedVolatility across calls+puts whose strike is within
    +-ATM_PCT of spot (ignoring None IVs). None if spot falsy or no contract
    falls in the window / no valid IV."""
    if not spot:
        return None
    lo, hi = spot * (1 - ATM_PCT), spot * (1 + ATM_PCT)
    ivs = []
    for c in chains:
        for contract in list(c.get("calls") or []) + list(c.get("puts") or []):
            strike = contract.get("strike")
            iv = contract.get("impliedVolatility")
            if strike is None or iv is None:
                continue
            if lo <= strike <= hi:
                ivs.append(iv)
    if not ivs:
        return None
    ivs.sort()
    n = len(ivs)
    mid = n // 2
    median = ivs[mid] if n % 2 else (ivs[mid - 1] + ivs[mid]) / 2
    return round(median, 4)


def unusual_volume(chains: list[dict]) -> bool | None:
    """(total option volume / total OI) > UNUSUAL_RATIO. None if total OI == 0
    or chains empty. Boundary exactly == 1.0 is False."""
    if not chains:
        return None
    total_vol = sum(_sum(c["calls"], "volume") + _sum(c["puts"], "volume")
                    for c in chains)
    total_oi = sum(_sum(c["calls"], "openInterest")
                   + _sum(c["puts"], "openInterest") for c in chains)
    if total_oi == 0:
        return None
    return (total_vol / total_oi) > UNUSUAL_RATIO


def skew_label(pc_oi: float | None, pc_vol: float | None) -> str | None:
    """Mean of the two P/C ratios ignoring Nones: < SKEW_BULLISH -> 'bullish',
    > SKEW_BEARISH -> 'bearish', else 'neutral'. None if BOTH ratios None.
    Band edges (0.8, 1.2) are 'neutral'."""
    vals = [v for v in (pc_oi, pc_vol) if v is not None]
    if not vals:
        return None
    mean = sum(vals) / len(vals)
    if mean < SKEW_BULLISH:
        return "bullish"
    if mean > SKEW_BEARISH:
        return "bearish"
    return "neutral"


def iv_rank(today_iv: float | None, history: list[float]) -> float | None:
    """Percentile-rank of today_iv within `history` (prior snapshots, today
    excluded): 100 * (count of history values <= today_iv) / len(history),
    rounded to 1 decimal. None if today_iv is None or
    len(history) < IV_RANK_MIN_HISTORY."""
    if today_iv is None or len(history) < IV_RANK_MIN_HISTORY:
        return None
    count = sum(1 for h in history if h <= today_iv)
    return round(100 * count / len(history), 1)


def compute_metrics(chains: list[dict], spot: float | None) -> dict:
    """Bundle the non-history metrics into
    {'pc_oi_ratio','pc_vol_ratio','atm_iv','unusual_volume','skew_label'}.
    iv_rank is added later by the collector (needs db history)."""
    oi_ratio = pc_oi_ratio(chains)
    vol_ratio = pc_vol_ratio(chains)
    return {
        "pc_oi_ratio": oi_ratio,
        "pc_vol_ratio": vol_ratio,
        "atm_iv": atm_iv(chains, spot),
        "unusual_volume": unusual_volume(chains),
        "skew_label": skew_label(oi_ratio, vol_ratio),
    }


# --------------------------------------------------------------------------
# Fetch layer (fail-soft, injectable for tests)
# --------------------------------------------------------------------------

def select_expirations(all_expirations: list[str], today: dt.date
                       ) -> list[str]:
    """Keep expirations whose DTE is in [DTE_MIN, DTE_MAX]; return the nearest
    MAX_EXPIRATIONS of them (ascending by date). Unparseable dates skipped."""
    kept = []
    for exp in all_expirations or []:
        try:
            d = dt.datetime.strptime(exp, "%Y-%m-%d").date()
        except Exception:
            continue
        dte = (d - today).days
        if DTE_MIN <= dte <= DTE_MAX:
            kept.append((d, exp))
    kept.sort(key=lambda pair: pair[0])
    return [exp for _, exp in kept[:MAX_EXPIRATIONS]]


def _clean(value):
    """None-through, NaN-through-to-None (mirrors prices.fetch_history's
    defensive column handling)."""
    if value is None:
        return None
    try:
        if isinstance(value, float) and math.isnan(value):
            return None
    except Exception:
        pass
    return value


def _normalize_side(rows) -> list[dict]:
    """yfinance calls/puts DataFrame -> list[contract]; missing columns and
    NaN become None (mirror prices.fetch_history's defensive column handling).
    Accepts a DataFrame OR a list[dict] so tests can pass plain dicts."""
    if rows is None:
        return []
    if hasattr(rows, "to_dict"):   # pandas DataFrame
        records = rows.to_dict("records")
    else:
        records = list(rows)
    out = []
    for rec in records:
        out.append({
            "strike": _clean(rec.get("strike")),
            "openInterest": _clean(rec.get("openInterest")),
            "volume": _clean(rec.get("volume")),
            "impliedVolatility": _clean(rec.get("impliedVolatility")),
        })
    return out


def _spot_from_fast_info(fast_info) -> float | None:
    if fast_info is None:
        return None
    for key in ("last_price", "lastPrice"):
        try:
            val = fast_info[key]
        except Exception:
            val = getattr(fast_info, key, None)
        if val is not None:
            try:
                return float(val)
            except Exception:
                pass
    return None


def _spot_from_history(hist) -> float | None:
    try:
        if hist is None:
            return None
        if hasattr(hist, "empty"):     # pandas DataFrame
            if hist.empty:
                return None
            for col in ("Close", "close"):
                if col in hist.columns:
                    return float(hist[col].iloc[-1])
            return None
        if isinstance(hist, list) and hist:
            row = hist[-1]
            for col in ("Close", "close"):
                if col in row:
                    return float(row[col])
        return None
    except Exception:
        return None


def fetch_chains(ticker: str, yf_module=None, today: dt.date | None = None
                 ) -> tuple[list[dict], float | None, list[str]]:
    """Return (chains, spot, expirations_used). Uses yfinance.Ticker:
    `.options` for expirations, `.option_chain(exp)` for calls/puts, spot from
    fast_info last price (fallback: 1d history close). Any exception ->
    ([], None, []). yf_module defaults to a lazy `import yfinance`."""
    today = today or dt.date.today()
    try:
        if yf_module is None:
            import yfinance as yf_module
        t = yf_module.Ticker(ticker)
        spot = _spot_from_fast_info(getattr(t, "fast_info", None))
        if spot is None:
            spot = _spot_from_history(t.history(period="1d"))
        exps = select_expirations(list(t.options or []), today)
        chains = []
        for exp in exps:
            oc = t.option_chain(exp)
            chains.append({
                "expiration": exp,
                "calls": _normalize_side(getattr(oc, "calls", None)),
                "puts": _normalize_side(getattr(oc, "puts", None)),
            })
        return chains, spot, exps
    except Exception:
        return [], None, []


def collect_options_flow(tickers: list[str], yf_module=None,
                         sleep=None, today: dt.date | None = None
                         ) -> list[dict]:
    """For each ticker: fetch_chains, compute_metrics, then iv_rank from
    db.options_iv_history(con, ticker, today). Sleep THROTTLE_SEC between
    tickers (injectable `sleep`, default time.sleep). Per-ticker try/except:
    on failure append an all-null row (ticker/date/spot=None) — never raise.
    Returns list of row dicts ready for db.replace_options_flow."""
    sleep_fn = sleep or time.sleep
    today = today or dt.date.today()
    today_iso = today.isoformat()
    con = db.connect()
    rows = []
    try:
        for i, ticker in enumerate(tickers):
            if i:
                sleep_fn(THROTTLE_SEC)
            try:
                chains, spot, exps = fetch_chains(ticker, yf_module=yf_module,
                                                  today=today)
                metrics = compute_metrics(chains, spot)
                history = db.options_iv_history(con, ticker, today_iso)
                row = {
                    "ticker": ticker, "date": today_iso,
                    "pc_oi_ratio": metrics["pc_oi_ratio"],
                    "pc_vol_ratio": metrics["pc_vol_ratio"],
                    "atm_iv": metrics["atm_iv"],
                    "iv_rank": iv_rank(metrics["atm_iv"], history),
                    "unusual_volume": metrics["unusual_volume"],
                    "skew_label": metrics["skew_label"],
                    "spot": round(spot, 4) if spot is not None else None,
                    "expirations": ",".join(exps),
                }
            except Exception:
                row = {
                    "ticker": ticker, "date": today_iso,
                    "pc_oi_ratio": None, "pc_vol_ratio": None,
                    "atm_iv": None, "iv_rank": None,
                    "unusual_volume": None, "skew_label": None,
                    "spot": None, "expirations": "",
                }
            rows.append(row)
    finally:
        con.close()
    return rows


def run_options(max_tickers: int | None = None) -> dict:
    """Read config `options_flow` block. If enabled is False -> return
    {'status': 'disabled'} and write nothing. Else pick top-N tickers by score
    from the signals table (default config options_flow.max_tickers or
    MAX_TICKERS_DEFAULT), collect, upsert each via db.replace_options_flow,
    return {'status':'ok', 'tickers': N, 'stored': count}."""
    cfg = config.get("options_flow", {})
    if not isinstance(cfg, dict) or not cfg:
        # Absent block: the doc ships config.example.json with enabled=true,
        # so a missing block behaves the same as the shipped default.
        cfg = {"enabled": True, "max_tickers": MAX_TICKERS_DEFAULT}
    if not cfg.get("enabled", True):
        return {"status": "disabled"}

    n = int(max_tickers or cfg.get("max_tickers", MAX_TICKERS_DEFAULT))
    con = db.connect()
    tickers = [r["ticker"] for r in con.execute(
        "SELECT ticker FROM signals ORDER BY score DESC LIMIT ?",
        (n,)).fetchall()]
    con.close()

    rows = collect_options_flow(tickers)

    con = db.connect()
    stored = 0
    for row in rows:
        db.replace_options_flow(con, row)
        stored += 1
    con.close()
    return {"status": "ok", "tickers": len(tickers), "stored": stored}
