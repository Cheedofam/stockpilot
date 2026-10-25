"""Research Desk collectors — trust-tiered inputs, fail-closed.

Gathers the day's research inputs and normalizes every one into an "item":

    {"ticker": str | "_MARKET", "source": str, "tier": 1|2|3,
     "kind": str, "summary": str, "wrapped": str, "data": dict}

`wrapped` is the summary inside personas.wrap_untrusted() delimiters — the
ONLY form that may enter an LLM prompt (docs/RESEARCH_DESK_SECURITY.md §1).
Tiers follow docs/RESEARCH_DESK_BUILD_PLAN.md §3:

    1  StockPilot's own convergence signals (13F / Form 4 / congressional)
    2  news headlines, fundamentals, model predictions, desk/analyst notes
    3  retail social (StockTwits, Reddit, Finnhub social) — corroboration only

Every external call is wrapped with a timeout and try/except: a failing
source is recorded in `errors` and skipped — never raised, never faked.
Free sources only; a paid provider would slot in as one more collector.
"""

from __future__ import annotations

import datetime as dt
import json
import re

from .. import config, db
from .personas import wrap_untrusted

TIMEOUT = 10
STOCKTWITS_URL = "https://api.stocktwits.com/api/2/streams/symbol/{t}.json"
REDDIT_URL = "https://www.reddit.com/r/{sub}/hot.json"
FINNHUB_SOCIAL_URL = "https://finnhub.io/api/v1/stock/social-sentiment"
DEFAULT_SUBREDDITS = ("stocks", "investing", "wallstreetbets")


def _cfg() -> dict:
    block = config.load().get("research", {})
    return block if isinstance(block, dict) else {}


def _watchlist() -> list[str]:
    wl = config.get("watchlist", [])
    return [t for t in wl if isinstance(t, str)] if isinstance(wl, list) else []


def _get_json(url: str, params: dict | None = None,
              headers: dict | None = None):
    """GET a JSON document; None on ANY failure (timeout, HTTP, parse)."""
    try:
        import requests
        base = {"User-Agent": config.get(
            "sec_user_agent", "StockPilot research tool")}
        base.update(headers or {})
        resp = requests.get(url, params=params, headers=base, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def _item(ticker: str, source: str, tier: int, kind: str, summary: str,
          data: dict | None = None) -> dict:
    summary = " ".join(str(summary).split())        # collapse whitespace
    return {"ticker": ticker, "source": source, "tier": tier, "kind": kind,
            "summary": summary,
            "wrapped": wrap_untrusted(summary, source=source, tier=tier),
            "data": data or {}}


# ------------------------------------------------------------- universe --

def build_universe(con) -> list[str]:
    """Top-N convergence signals plus the config watchlist, deduped."""
    top_n = int(_cfg().get("universe_top_n", 20))
    rows = con.execute(
        "SELECT ticker FROM signals ORDER BY score DESC LIMIT ?",
        (top_n,)).fetchall()
    out = [r["ticker"] for r in rows]
    for t in _watchlist():
        if t not in out:
            out.append(t)
    return out


# ------------------------------------------------- Tier 1: own DB signals --

def collect_signals(con, universe: list[str]) -> list[dict]:
    """Convergence signals — the aggregation of the primary disclosures."""
    items = []
    for t in universe:
        r = con.execute("SELECT * FROM signals WHERE ticker=?",
                        (t,)).fetchone()
        if not r:
            continue
        names = ", ".join(json.loads(r["buyer_names"] or "[]")[:5])
        summary = (
            f"{t}: convergence score {r['score']}, {r['buyers']} distinct "
            f"buyers ({r['congress_buyers']} congress / "
            f"{r['billionaire_buyers']} billionaire funds / "
            f"{r['insider_buyers']} corporate insiders), "
            f"{r['sells']} sellers, total disclosed buys "
            f"${(r['total_buy_usd'] or 0):,.0f}, risk {r['risk']}. "
            f"Buyers: {names or 'n/a'}.")
        items.append(_item(t, "stockpilot_signals", 1, "convergence_signal",
                           summary,
                           {"score": r["score"], "buyers": r["buyers"],
                            "risk": r["risk"]}))
    return items


# ----------------------------------------- Tier 2: internal derived notes --

def collect_derived(con, universe: list[str]) -> list[dict]:
    """Predictions, desk opinions and analyst notes already in stocks.db."""
    items = []
    for t in universe:
        p = con.execute("SELECT * FROM predictions WHERE ticker=?",
                        (t,)).fetchone()
        if p:
            items.append(_item(
                t, "stockpilot_predictor", 2, "model_prediction",
                f"{t}: model {p['model']} leans {p['direction']} "
                f"(confidence {p['confidence']}), last price "
                f"{p['last_price']}, 7-day move {p['pct_7d']}%.",
                {"direction": p["direction"], "confidence": p["confidence"]}))
        d = con.execute("SELECT * FROM desk_opinions WHERE ticker=?",
                        (t,)).fetchone()
        if d:
            items.append(_item(
                t, "tradingagents_desk", 2, "desk_opinion",
                f"{t}: desk verdict {d['action']}. "
                f"{(d['rationale'] or '')[:400]}",
                {"action": d["action"]}))
        for a in con.execute(
                "SELECT persona, text FROM analyst_reports WHERE ticker=?",
                (t,)).fetchall():
            items.append(_item(
                t, "stockpilot_analyst", 2, "analyst_note",
                f"{t} ({a['persona']}): {(a['text'] or '')[:400]}",
                {"persona": a["persona"]}))
    return items


# ----------------------------------------------------- Tier 2: news (DB) --

def collect_news(con, universe: list[str], per_ticker: int = 5) -> list[dict]:
    """Recent Finnhub headlines already collected by the daily pipeline."""
    items = []
    for t in universe:
        for n in con.execute(
                "SELECT headline, source, published, sentiment FROM news "
                "WHERE ticker=? ORDER BY published DESC LIMIT ?",
                (t, per_ticker)).fetchall():
            senti = (f" (sentiment {n['sentiment']:+.2f})"
                     if n["sentiment"] is not None else "")
            items.append(_item(
                t, "finnhub_news", 2, "news_headline",
                f"{t}: {n['headline']} [{n['source']}, "
                f"{n['published']}]{senti}",
                {"published": n["published"], "sentiment": n["sentiment"]}))
    return items


# ------------------------------------------------ Tier 2: fundamentals --

def _fast_info(ticker: str) -> dict | None:
    """yfinance fast_info as a plain dict; None on any failure. Split out so
    tests can stub it (no network in tests)."""
    try:
        import yfinance as yf
        fi = yf.Ticker(ticker).fast_info
        return {"last_price": getattr(fi, "last_price", None),
                "market_cap": getattr(fi, "market_cap", None),
                "year_high": getattr(fi, "year_high", None),
                "year_low": getattr(fi, "year_low", None)}
    except Exception:
        return None


def collect_fundamentals(universe: list[str],
                         errors: list[str]) -> list[dict]:
    cap = int(_cfg().get("max_fundamentals_tickers", 10))
    items = []
    for t in universe[:cap]:
        info = _fast_info(t)
        if not info:
            errors.append(f"yfinance:{t}: unavailable")
            continue

        def _fmt(v, money=False):
            if v is None:
                return "unknown"
            return f"${v:,.0f}" if money else f"{v:,.2f}"

        items.append(_item(
            t, "yfinance", 2, "fundamentals",
            f"{t}: last price {_fmt(info['last_price'])}, market cap "
            f"{_fmt(info['market_cap'], money=True)}, 52-week range "
            f"{_fmt(info['year_low'])}-{_fmt(info['year_high'])}.",
            info))
    return items


# -------------------------------------------- Tier 3: retail social buzz --

def collect_stocktwits(universe: list[str], errors: list[str]) -> list[dict]:
    cap = int(_cfg().get("max_social_tickers", 10))
    items = []
    for t in universe[:cap]:
        doc = _get_json(STOCKTWITS_URL.format(t=t))
        if not doc or "messages" not in doc:
            errors.append(f"stocktwits:{t}: unavailable")
            continue
        msgs = doc.get("messages") or []
        bulls = bears = 0
        samples: list[str] = []
        for m in msgs:
            senti = (((m.get("entities") or {}).get("sentiment") or {})
                     .get("basic") or "")
            bulls += senti == "Bullish"
            bears += senti == "Bearish"
            if len(samples) < 3 and m.get("body"):
                samples.append(str(m["body"])[:200])
        items.append(_item(
            t, "stocktwits", 3, "social_stocktwits",
            f"{t}: {len(msgs)} recent StockTwits messages, {bulls} tagged "
            f"bullish / {bears} bearish. Samples: "
            + " | ".join(samples),
            {"messages": len(msgs), "bullish": bulls, "bearish": bears}))
    return items


def collect_reddit(universe: list[str], errors: list[str]) -> list[dict]:
    subs = _cfg().get("reddit_subreddits") or list(DEFAULT_SUBREDDITS)
    titles: list[tuple[str, str]] = []          # (subreddit, title)
    for sub in subs:
        doc = _get_json(REDDIT_URL.format(sub=sub), params={"limit": 25})
        try:
            children = doc["data"]["children"]
        except (TypeError, KeyError):
            errors.append(f"reddit:r/{sub}: unavailable")
            continue
        for c in children:
            title = ((c.get("data") or {}).get("title") or "")
            if title:
                titles.append((sub, title))

    items = []
    for t in universe:
        rx = re.compile(rf"(?:\${re.escape(t)}\b|\b{re.escape(t)}\b)")
        hits = [(sub, ti) for sub, ti in titles
                if rx.search(ti.upper() if t.isupper() else ti)]
        if not hits:
            continue
        sample = " | ".join(ti[:150] for _, ti in hits[:3])
        items.append(_item(
            t, "reddit", 3, "social_reddit",
            f"{t}: mentioned in {len(hits)} hot post title(s) across "
            f"r/{', r/'.join(sorted({s for s, _ in hits}))}. "
            f"Samples: {sample}",
            {"mentions": len(hits)}))
    return items


def collect_finnhub_social(universe: list[str],
                           errors: list[str]) -> list[dict]:
    key = config.get("finnhub_api_key", "")
    if not key:
        errors.append("finnhub_social: no api key configured")
        return []
    cap = int(_cfg().get("max_social_tickers", 10))
    items = []
    for t in universe[:cap]:
        doc = _get_json(FINNHUB_SOCIAL_URL,
                        params={"symbol": t, "token": key})
        if not isinstance(doc, dict):
            errors.append(f"finnhub_social:{t}: unavailable")
            continue
        mentions = pos = neg = 0
        for platform in ("reddit", "twitter"):
            for row in (doc.get(platform) or [])[:50]:
                mentions += int(row.get("mention") or 0)
                pos += int(row.get("positiveMention") or 0)
                neg += int(row.get("negativeMention") or 0)
        if mentions:
            items.append(_item(
                t, "finnhub_social", 3, "social_finnhub",
                f"{t}: {mentions} social mentions tracked by Finnhub, "
                f"{pos} positive / {neg} negative.",
                {"mentions": mentions, "positive": pos, "negative": neg}))
    return items


# -------------------------------------------------------------- assembly --

def support_by_ticker(items: list[dict]) -> dict[str, set[int]]:
    """{ticker: set of tiers with evidence} — feeds the never-Tier-3-alone
    rule and the pump-and-dump screen in brief.py."""
    out: dict[str, set[int]] = {}
    for it in items:
        if it["ticker"] and it["ticker"] != "_MARKET":
            out.setdefault(it["ticker"], set()).add(int(it["tier"]))
    return out


def collect_all(con=None) -> dict:
    """Run every collector, fail-closed, and return the day's inputs:

        {"as_of", "universe", "items", "errors"}

    A source that errors contributes nothing but an `errors` entry; the
    result is always structurally complete.
    """
    own = con is None
    if own:
        con = db.connect()
    errors: list[str] = []
    try:
        universe = build_universe(con)
        items: list[dict] = []
        items += collect_signals(con, universe)
        items += collect_derived(con, universe)
        items += collect_news(con, universe)
        items += collect_fundamentals(universe, errors)

        social = _cfg().get("social_sources",
                            ["stocktwits", "reddit", "finnhub_social"])
        if "stocktwits" in social:
            items += collect_stocktwits(universe, errors)
        if "reddit" in social:
            items += collect_reddit(universe, errors)
        if "finnhub_social" in social:
            items += collect_finnhub_social(universe, errors)
    finally:
        if own:
            con.close()
    return {"as_of": dt.date.today().isoformat(), "universe": universe,
            "items": items, "errors": errors}
