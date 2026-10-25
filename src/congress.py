"""Congressional stock trade disclosures (STOCK Act PTRs).

Primary source: the actively maintained house-stock-watcher-data mirror on
GitHub (scraped from the House Clerk's official PTR PDFs, updated daily).
The original Senate/House Stock Watcher S3 buckets went offline in ~2024 and
are kept only as fallbacks. No maintained Senate feed exists as of mid-2026,
so coverage is House-only.

Disclosures lag the trade by up to 45 days — that lag is structural.
"""

from __future__ import annotations

import datetime as dt
import re

from .util import cached_json

SENATE_URLS = [
    "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json",
]
HOUSE_URLS = [
    "https://raw.githubusercontent.com/TattooedHead/house-stock-watcher-data/main/data/all_transactions.json",
    "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json",
]

_AMOUNT_RE = re.compile(r"\$?([\d,]+(?:\.\d+)?)")
VALID_TICKER_RE = re.compile(r"^[A-Z]{1,5}([.-][A-Z]{1,2})?$")


def looks_like_ticker(t) -> bool:
    return bool(t) and bool(VALID_TICKER_RE.match(str(t).strip().upper()))


def amount_midpoint(text: str | None) -> float:
    """'$1,001 - $15,000' -> 8000.5 ; unknown -> 0."""
    if not text:
        return 0.0
    nums = [float(m.replace(",", "")) for m in _AMOUNT_RE.findall(str(text))]
    nums = [n for n in nums if n > 0]
    return sum(nums) / len(nums) if nums else 0.0


def _norm(rec: dict, chamber: str) -> dict | None:
    ticker = (rec.get("ticker") or rec.get("symbol") or "").strip().upper()
    if not looks_like_ticker(ticker):
        return None
    member = (rec.get("senator") or rec.get("representative")
              or rec.get("member") or rec.get("name") or "Unknown")
    ttype = str(rec.get("type") or rec.get("transaction_type") or "").lower()
    if "purchase" in ttype or ttype == "buy":
        kind = "buy"
    elif "sale" in ttype or ttype == "sell":
        kind = "sell"
    else:
        return None
    amount_text = str(rec.get("amount") or rec.get("amount_range") or "")
    date = str(rec.get("transaction_date") or rec.get("date") or "")
    if "/" in date:
        try:
            date = dt.datetime.strptime(date, "%m/%d/%Y").date().isoformat()
        except ValueError:
            return None
    disclosed = str(rec.get("disclosure_date") or rec.get("ptr_date") or "")
    if "/" in disclosed:
        try:
            disclosed = dt.datetime.strptime(
                disclosed, "%m/%d/%Y").date().isoformat()
        except ValueError:
            disclosed = ""
    return {
        "source": "congress",
        "trader": str(member).strip(),
        "detail": chamber,
        "ticker": ticker,
        "type": kind,
        "amount_usd": amount_midpoint(amount_text),
        "amount_range": amount_text,
        "trade_date": date[:10],
        "disclosure_date": disclosed[:10],
    }


def _fetch(urls: list[str], chamber: str) -> list[dict]:
    for url in urls:
        try:
            data = cached_json(url, ttl_hours=6, min_interval=0.5, timeout=90)
        except Exception:
            continue
        if isinstance(data, dict):
            data = data.get("transactions") or data.get("data") or []
        rows = [n for rec in data if (n := _norm(rec, chamber))]
        if rows:
            return rows
    return []


def fetch_congress_trades(window_days: int = 120) -> tuple[list[dict], dict]:
    """Returns (trades within window, status), newest first."""
    rows = _fetch(SENATE_URLS, "senate") + _fetch(HOUSE_URLS, "house")
    cutoff = (dt.date.today() - dt.timedelta(days=window_days)).isoformat()
    today = dt.date.today().isoformat()
    # Upper bound too: source PDFs occasionally contain future-dated typos.
    rows = [r for r in rows if cutoff <= r["trade_date"] <= today]
    rows.sort(key=lambda r: r["trade_date"], reverse=True)
    chambers = {r["detail"] for r in rows}
    return rows, {"trades": len(rows), "chambers": sorted(chambers) or ["none"]}
