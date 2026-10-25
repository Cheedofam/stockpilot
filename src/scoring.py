"""Convergence scoring: how many distinct smart-money buyers are converging
on a ticker, dollar-weighted and decayed by disclosure age.

Three trader types now count toward convergence:
  +14 per distinct congress buyer    (max 3 counted)
  +18 per distinct billionaire buyer  (max 3 counted)
  +16 per distinct corporate insider  (max 3 counted; Form 4 open-market buys)
  +10 convergence bonus PER distinct type beyond the first
      (2 types → +10, all 3 → +20: cross-source agreement is the real signal)
  +up to 20 from total buy dollars    (tanh-scaled, $5M ~ +15)
  -8  per distinct seller             (max 3 counted)
  every buy component is multiplied by exp(-age/45d) recency decay, keyed off
  the DISCLOSURE date (when the trade became public), not the trade date.

Risk label: annualized volatility of the ticker if price data is available,
else a neutral default.
"""

from __future__ import annotations

import datetime as dt
import json
import math

from . import db

# Per-buyer points and the convergence bonus per extra trader type.
_PTS = {"congress": 14.0, "billionaire": 18.0, "insider": 16.0}
_TYPE_BONUS = 10.0


def _decay(date_str: str, half_life_days: float = 45.0) -> float:
    try:
        age = (dt.date.today() - dt.date.fromisoformat(date_str[:10])).days
    except ValueError:
        age = 90
    return math.exp(-max(age, 0) / half_life_days)


def score_ticker(trades: list[dict]) -> dict:
    """trades: rows from the trades table for one ticker."""
    buyers_by_type: dict[str, dict[str, float]] = {
        "congress": {}, "billionaire": {}, "insider": {}}
    sellers: set[str] = set()
    total_buy = 0.0
    last_date = ""
    for t in trades:
        d = t["trade_date"] or ""
        last_date = max(last_date, d)
        w = _decay(t["disclosure_date"] or d)
        if t["type"] == "buy":
            total_buy += (t["amount_usd"] or 0.0) * w
            bucket = buyers_by_type.get(t["source"])
            if bucket is not None:
                bucket[t["trader"]] = max(bucket.get(t["trader"], 0.0), w)
        else:
            sellers.add(t["trader"])

    s = 0.0
    for src, bucket in buyers_by_type.items():
        top = sorted(bucket.values(), reverse=True)[:3]
        s += _PTS[src] * sum(top)
    distinct_types = sum(1 for b in buyers_by_type.values() if b)
    if distinct_types > 1:
        s += _TYPE_BONUS * (distinct_types - 1)
    s += 20 * math.tanh(total_buy / 5e6)
    s -= 8 * min(len(sellers), 3)
    score = round(max(0.0, min(100.0, s)), 1)

    all_buyers = sorted(set().union(*[set(b) for b in buyers_by_type.values()]))
    return {
        "score": score,
        "buyers": len(all_buyers),
        "congress_buyers": len(buyers_by_type["congress"]),
        "billionaire_buyers": len(buyers_by_type["billionaire"]),
        "insider_buyers": len(buyers_by_type["insider"]),
        "buyer_names": all_buyers,
        "total_buy_usd": round(total_buy),
        "sells": len(sellers),
        "last_trade_date": last_date,
    }


def risk_label(ann_vol: float | None, score: float) -> str:
    if ann_vol is not None:
        return "HIGH" if ann_vol > 0.45 else "MEDIUM" if ann_vol > 0.25 else "LOW"
    return "MEDIUM"


def run_scoring(max_tickers: int = 40) -> list[dict]:
    """Score every ticker with buy activity; persist to signals + history."""
    con = db.connect()
    rows = [dict(r) for r in con.execute("SELECT * FROM trades").fetchall()]
    by_ticker: dict[str, list[dict]] = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(r)

    scored = []
    for ticker, trades in by_ticker.items():
        if not any(t["type"] == "buy" for t in trades):
            continue
        s = score_ticker(trades)
        s["ticker"] = ticker
        scored.append(s)
    scored.sort(key=lambda s: s["score"], reverse=True)
    scored = scored[:max_tickers]

    # company names + volatility-based risk from cached prices
    from . import prices
    tickers = [s["ticker"] for s in scored]
    meta = prices.fetch_meta(tickers)
    for s in scored:
        m = meta.get(s["ticker"], {})
        s["company"] = m.get("name", s["ticker"])
        s["risk"] = risk_label(m.get("ann_vol"), s["score"])
        db.replace_signal(con, s)
    con.commit()

    # prune signals for tickers that fell out of the ranking
    keep = json.dumps(tickers)
    con.execute(
        "DELETE FROM signals WHERE ticker NOT IN (SELECT value FROM json_each(?))",
        (keep,))
    con.commit()

    # snapshot today's scores for the backtest / signal-validation loop
    last_prices = {t: meta.get(t, {}).get("last_price") for t in tickers}
    db.snapshot_signals(con, scored, last_prices)
    con.close()
    return scored
