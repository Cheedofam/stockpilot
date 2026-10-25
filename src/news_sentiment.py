"""Company news via Finnhub's company-news endpoint.

Headlines are scored for sentiment with Gemini (one batched call per ticker),
falling back to a transparent keyword lexicon when Gemini is unavailable or the
response can't be parsed — so scoring never hard-fails and works offline.

GET https://finnhub.io/api/v1/company-news?symbol=T&from=...&to=...&token=...
"""

from __future__ import annotations

import datetime as dt
import json
import re

from . import config, db
from .util import cached_json

# Prefer the "lite" models — far higher free-tier quota, plenty for headline
# scoring. Fall back to the full flash models, then to the keyword lexicon.
GEMINI_MODELS = ["gemini-2.5-flash-lite", "gemini-2.0-flash-lite",
                 "gemini-2.5-flash", "gemini-2.0-flash"]

POSITIVE = {
    "beat", "beats", "surge", "surges", "soar", "soars", "rally", "rallies",
    "gain", "gains", "jump", "jumps", "record", "upgrade", "upgrades",
    "raised", "raises", "strong", "growth", "profit", "profits", "buyback",
    "outperform", "bullish", "wins", "win", "approval", "approved",
    "expands", "expansion", "partnership", "breakthrough", "tops",
    "exceeds", "boost", "boosts", "upbeat", "optimistic", "dividend",
}
NEGATIVE = {
    "miss", "misses", "fall", "falls", "drop", "drops", "plunge", "plunges",
    "slump", "slumps", "cut", "cuts", "downgrade", "downgrades", "weak",
    "loss", "losses", "lawsuit", "probe", "investigation", "recall",
    "bearish", "warning", "warns", "layoff", "layoffs", "fraud", "decline",
    "declines", "tumble", "tumbles", "fears", "risk", "risks", "concern",
    "concerns", "halt", "halts", "delays", "delay", "scrutiny", "fine",
    "fined", "bankruptcy", "default", "selloff", "crash",
}
_WORD_RE = re.compile(r"[a-z']+")


def keyword_sentiment(text: str) -> float:
    """-1..1 from positive/negative keyword counts."""
    words = _WORD_RE.findall((text or "").lower())
    if not words:
        return 0.0
    pos = sum(1 for w in words if w in POSITIVE)
    neg = sum(1 for w in words if w in NEGATIVE)
    if pos + neg == 0:
        return 0.0
    return round((pos - neg) / (pos + neg), 3)


def gemini_sentiment(headlines: list[str]) -> list[float] | None:
    """Score a batch of headlines from -1 (very negative) to +1 (very positive)
    in a single Gemini call. Returns None if unavailable/unparseable so the
    caller can fall back to the keyword lexicon."""
    key = config.get("gemini_api_key", "")
    if not key or not headlines:
        return None
    try:
        import google.generativeai as genai
        genai.configure(api_key=key)
    except Exception:
        return None
    numbered = "\n".join(f"{i}. {h}" for i, h in enumerate(headlines))
    prompt = (
        "Score each financial news headline's sentiment for the company it "
        "concerns, from -1.0 (very bearish) to 1.0 (very bullish), 0 for "
        "neutral. Return ONLY a JSON array of numbers, one per headline, in "
        f"order. {len(headlines)} headlines:\n{numbered}")
    for model_name in GEMINI_MODELS:
        try:
            model = genai.GenerativeModel(model_name)
            txt = (model.generate_content(prompt).text or "").strip()
            m = re.search(r"\[.*\]", txt, re.DOTALL)
            if not m:
                continue
            arr = json.loads(m.group(0))
            if isinstance(arr, list) and len(arr) == len(headlines):
                return [max(-1.0, min(1.0, float(x))) for x in arr]
        except Exception:
            continue
    return None


def fetch_company_news(ticker: str, window_days: int = 14,
                       max_items: int = 20) -> list[dict]:
    key = config.get("finnhub_api_key", "")
    if not key:
        return []
    today = dt.date.today()
    frm = (today - dt.timedelta(days=window_days)).isoformat()
    url = ("https://finnhub.io/api/v1/company-news"
           f"?symbol={ticker}&from={frm}&to={today.isoformat()}&token={key}")
    try:
        data = cached_json(url, ttl_hours=6, min_interval=1.1)
    except Exception:
        return []
    rows = []
    for rec in (data or [])[:max_items]:
        headline = (rec.get("headline") or "").strip()
        if not headline:
            continue
        published = ""
        if rec.get("datetime"):
            published = dt.datetime.fromtimestamp(
                rec["datetime"], dt.timezone.utc).date().isoformat()
        rows.append({
            "ticker": ticker, "headline": headline,
            "source": rec.get("source") or "", "url": rec.get("url") or "",
            "published": published,
            # Keyword score is the always-available baseline; run_news upgrades
            # the whole batch with Gemini in as few calls as possible.
            "sentiment": keyword_sentiment(
                f"{headline}. {rec.get('summary', '')}"),
        })
    return rows


def run_news(tickers: list[str]) -> dict:
    """Fetch news for all tickers, upgrade sentiment with Gemini in a few
    batched calls (keyword baseline if quota is exhausted), then store."""
    window = int(config.get("news_window_days", 14))
    all_rows: list[dict] = []
    for t in tickers:
        all_rows.extend(fetch_company_news(t, window))

    # Upgrade sentiment for the whole run in chunks — minimizes Gemini calls so
    # we stay inside the free-tier quota (and don't starve the explain step).
    # Space the chunks out to respect the free tier's per-minute rate limit.
    import time
    gemini_used = False
    chunks = [all_rows[i:i + 150] for i in range(0, len(all_rows), 150)]
    for n, chunk in enumerate(chunks):
        if n:
            time.sleep(5)
        scores = gemini_sentiment([r["headline"] for r in chunk])
        if scores is None:
            break  # quota gone — keep keyword scores for the rest
        gemini_used = True
        for r, sc in zip(chunk, scores):
            r["sentiment"] = round(sc, 3)

    con = db.connect()
    total_new = db.insert_news(con, all_rows)
    con.close()
    return {"tickers": len(tickers), "new_headlines": total_new,
            "sentiment_model": "gemini" if gemini_used else "keyword"}
