"""Research Desk API — Blueprint under /stockpilot/api/research/*.

Read-only: the daily pipeline writes research_briefs/research_ideas into
stocks.db and the upload syncs them here; these endpoints only SELECT.
Advisory-only data — every payload carries the not-financial-advice
disclaimer so no client can render ideas without it.

    GET /latest            -> the newest brief (markdown + chatter + flags)
    GET /ideas             -> the newest brief's ranked, audited ideas
    GET /idea/<ticker>     -> one ticker's latest idea (+ its history dates)
"""

from __future__ import annotations

import re

from flask import Blueprint, jsonify

from src import db
from src.research.personas import DISCLAIMER

research_bp = Blueprint("research", __name__,
                        url_prefix="/stockpilot/api/research")

# Whitelist for the one user-supplied path param (defense in depth on top of
# parameterized SQL): real tickers are 1-10 chars of A-Z, digits, dot, dash.
_TICKER_RX = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,9}$")


def _latest(con) -> dict | None:
    return db.latest_research_brief(con)


@research_bp.get("/latest")
def latest():
    con = db.connect()
    try:
        brief = _latest(con)
        if not brief:
            return jsonify({"brief": None, "disclaimer": DISCLAIMER})
        ideas = db.research_ideas_for(con, brief["as_of"])
    finally:
        con.close()
    return jsonify({"brief": brief, "ideas": ideas,
                    "disclaimer": DISCLAIMER})


@research_bp.get("/ideas")
def ideas():
    con = db.connect()
    try:
        brief = _latest(con)
        rows = (db.research_ideas_for(con, brief["as_of"])
                if brief else [])
        as_of = brief["as_of"] if brief else None
    finally:
        con.close()
    return jsonify({"as_of": as_of, "ideas": rows,
                    "disclaimer": DISCLAIMER})


@research_bp.get("/idea/<ticker>")
def idea(ticker: str):
    if not _TICKER_RX.match(ticker or ""):
        return jsonify({"error": "invalid ticker",
                        "disclaimer": DISCLAIMER}), 400
    t = ticker.upper()
    con = db.connect()
    try:
        row = con.execute(
            "SELECT * FROM research_ideas WHERE ticker=? "
            "ORDER BY as_of DESC LIMIT 1", (t,)).fetchone()
        history = [r["as_of"] for r in con.execute(
            "SELECT as_of FROM research_ideas WHERE ticker=? "
            "ORDER BY as_of DESC LIMIT 30", (t,)).fetchall()]
    finally:
        con.close()
    if not row:
        return jsonify({"ticker": t, "idea": None,
                        "disclaimer": DISCLAIMER}), 404
    import json as _json
    d = dict(row)
    for col in ("evidence_json", "auditor_flags_json"):
        d[col.replace("_json", "")] = _json.loads(d.pop(col) or "[]")
    return jsonify({"ticker": t, "idea": d, "history": history,
                    "disclaimer": DISCLAIMER})
