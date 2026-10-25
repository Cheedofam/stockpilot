"""StockPilot Flask API.

All routes are prefixed /stockpilot to coexist with JobPilot on the same
PythonAnywhere account. CORS is open to Vercel + local Vite dev.

    python main.py dashboard      ->  http://localhost:5001/stockpilot/api/stats
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask, jsonify, request

from src import config, db
# Import everything the routes need at module load: the WSGI dispatcher purges
# 'src' from sys.modules after importing this app (so JobPilot gets a clean
# import), which would break any lazy `from src...` done at request time.
from src.backtest import run_backtest
from dashboard.trading_api import trading_bp, _admin_ok
from dashboard.research_api import research_bp

app = Flask(__name__)
app.register_blueprint(trading_bp)
app.register_blueprint(research_bp)

try:
    from flask_cors import CORS
    CORS(app, origins=["https://*.vercel.app", "http://localhost:5173"])
except ImportError:  # PythonAnywhere may not have flask-cors installed
    @app.after_request
    def _cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = \
            "GET, POST, DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp

P = "/stockpilot"

# Whitelist for user-supplied tickers (path params): letter/underscore
# first (underscore covers the '_PORTFOLIO' analyst pseudo-ticker), then
# letters/digits/dot/dash. Defense in depth on top of parameterized SQL —
# and mandatory for /chart, where the ticker lands in a filename
# (path-traversal guard).
_TICKER_RX = re.compile(r"^[A-Za-z_][A-Za-z0-9.\-]{0,10}$")
WATCHLIST_MAX_ROWS = 200


def _valid_ticker(ticker: str) -> str | None:
    """The normalized ticker, or None if malformed."""
    t = (ticker or "").upper()
    return t if _TICKER_RX.match(t) else None


def _int_arg(name: str, default: int, cap: int) -> int:
    """Parse an integer query param defensively (400s handled by caller)."""
    try:
        return min(int(request.args.get(name, default)), cap)
    except (TypeError, ValueError):
        return default


def _rows(query: str, *args) -> list[dict]:
    con = db.connect()
    out = [dict(r) for r in con.execute(query, args).fetchall()]
    con.close()
    return out


def _signal_dict(r: dict) -> dict:
    r["buyer_names"] = json.loads(r.get("buyer_names") or "[]")
    return r


@app.get(P + "/api/stats")
def stats():
    con = db.connect()
    n = lambda q, *a: con.execute(q, a).fetchone()[0]
    today = dt.date.today().isoformat()
    out = {
        "active_signals": n("SELECT COUNT(*) FROM signals"),
        "new_trades_today": n(
            "SELECT COUNT(*) FROM trades WHERE collected_at >= ?", today),
        "tickers_tracked": n("SELECT COUNT(DISTINCT ticker) FROM trades"),
        "total_trades": n("SELECT COUNT(*) FROM trades"),
        "top_score": n("SELECT COALESCE(MAX(score), 0) FROM signals"),
        "last_updated": (con.execute(
            "SELECT MAX(updated_at) FROM signals").fetchone()[0] or ""),
    }
    con.close()
    return jsonify(out)


@app.get(P + "/api/signals")
def signals():
    rows = _rows("""
        SELECT s.*, p.direction, p.confidence, p.model, p.pct_7d, p.last_price,
               of.pc_oi_ratio, of.pc_vol_ratio, of.atm_iv, of.iv_rank,
               of.unusual_volume, of.skew_label
        FROM signals s
        LEFT JOIN predictions p ON p.ticker = s.ticker
        LEFT JOIN (
            SELECT o.* FROM options_flow o
            JOIN (SELECT ticker, MAX(date) md FROM options_flow GROUP BY ticker) m
              ON o.ticker = m.ticker AND o.date = m.md
        ) of ON of.ticker = s.ticker
        ORDER BY s.score DESC""")
    return jsonify([_signal_dict(r) for r in rows])


@app.get(P + "/api/signal/<ticker>")
def signal(ticker: str):
    ticker = ticker.upper()
    sig = _rows("SELECT * FROM signals WHERE ticker=?", ticker)
    pred = _rows("SELECT * FROM predictions WHERE ticker=?", ticker)
    trades = _rows("SELECT * FROM trades WHERE ticker=? "
                   "ORDER BY trade_date DESC LIMIT 50", ticker)
    news = _rows("SELECT * FROM news WHERE ticker=? "
                 "ORDER BY published DESC LIMIT 5", ticker)
    expl = _rows("SELECT * FROM explanations WHERE ticker=?", ticker)
    desk = _rows("SELECT * FROM desk_opinions WHERE ticker=?", ticker)
    analyst = _rows("SELECT persona, text, model, updated_at FROM "
                    "analyst_reports WHERE ticker=? ORDER BY persona", ticker)
    if not sig and not trades:
        return jsonify({"error": f"{ticker} not tracked"}), 404
    return jsonify({
        "ticker": ticker,
        "signal": _signal_dict(sig[0]) if sig else None,
        "prediction": pred[0] if pred else None,
        "trades": trades,
        "news": news,
        "explanation": expl[0] if expl else None,
        "desk": desk[0] if desk else None,
        "analyst": analyst,
    })


@app.get(P + "/api/trades")
def trades():
    limit = _int_arg("limit", 50, 200)
    rows = _rows("SELECT * FROM trades ORDER BY trade_date DESC LIMIT ?",
                 limit)
    return jsonify(rows)


@app.get(P + "/api/prediction/<ticker>")
def prediction(ticker: str):
    rows = _rows("SELECT * FROM predictions WHERE ticker=?", ticker.upper())
    if not rows:
        return jsonify({"error": "no prediction"}), 404
    return jsonify(rows[0])


@app.get(P + "/api/desk/<ticker>")
def desk_opinion(ticker: str):
    rows = _rows("SELECT * FROM desk_opinions WHERE ticker=?", ticker.upper())
    if not rows:
        return jsonify({"error": "no desk opinion"}), 404
    return jsonify(rows[0])


@app.get(P + "/api/analyst/<ticker>")
def analyst_reports(ticker: str):
    # ticker '_PORTFOLIO' returns the portfolio-level personas.
    rows = _rows("SELECT persona, text, model, updated_at FROM analyst_reports "
                 "WHERE ticker=? ORDER BY persona", ticker.upper())
    return jsonify(rows)


@app.get(P + "/api/chart/<ticker>")
def chart(ticker: str):
    # Serve from the price cache written by the pipeline — no yfinance
    # dependency on the server (PythonAnywhere can't fetch Yahoo anyway).
    # The ticker lands in a FILENAME here, so the whitelist is mandatory
    # (blocks ../ and \ traversal), not just hygiene.
    t = _valid_ticker(ticker)
    if not t:
        return jsonify({"error": "invalid ticker"}), 400
    path = config.CACHE_DIR / f"prices_{t}.json"
    if not path.exists():
        return jsonify({"error": "no price data"}), 404
    rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    step = max(1, len(rows) // 180)
    sampled = rows[::step]
    if sampled[-1] is not rows[-1]:
        sampled.append(rows[-1])
    return jsonify({
        "dates": [r["timestamps"] for r in sampled],
        "close": [round(float(r["close"]), 4) for r in sampled],
    })


@app.get(P + "/api/news/<ticker>")
def ticker_news(ticker: str):
    rows = _rows("SELECT * FROM news WHERE ticker=? "
                 "ORDER BY published DESC LIMIT 10", ticker.upper())
    return jsonify(rows)


@app.get(P + "/api/watchlist")
def watchlist_get():
    # Watchlist lives in its own DB; enrich with signal/prediction data from
    # stocks.db in Python (the two databases are intentionally separate).
    wcon = db.connect_watchlist()
    wl = [dict(r) for r in wcon.execute(
        "SELECT ticker, added_at, company FROM watchlist "
        "ORDER BY added_at").fetchall()]
    wcon.close()
    if not wl:
        return jsonify([])
    tickers = [w["ticker"] for w in wl]
    placeholders = ",".join("?" * len(tickers))
    sig = {r["ticker"]: r for r in _rows(
        f"SELECT ticker, score, risk, company FROM signals "
        f"WHERE ticker IN ({placeholders})", *tickers)}
    pred = {r["ticker"]: r for r in _rows(
        f"SELECT ticker, direction, last_price, confidence FROM predictions "
        f"WHERE ticker IN ({placeholders})", *tickers)}
    out = []
    for w in wl:
        s, p = sig.get(w["ticker"], {}), pred.get(w["ticker"], {})
        out.append({
            "ticker": w["ticker"], "added_at": w["added_at"],
            "company": s.get("company") or w.get("company") or w["ticker"],
            "score": s.get("score"), "risk": s.get("risk"),
            "direction": p.get("direction"), "last_price": p.get("last_price"),
            "confidence": p.get("confidence"),
        })
    return jsonify(out)


@app.post(P + "/api/watchlist/<ticker>")
def watchlist_add(ticker: str):
    # Creator-only: the watchlist is the site owner's data. Mutations
    # require the same admin token as the trading kill switch (fail-closed:
    # no configured token -> nobody writes). Reads stay public.
    if not _admin_ok():
        return jsonify({"error": "admin token required"}), 403
    # Defense in depth even for the owner: whitelist the format, only
    # accept tickers StockPilot already tracks, and cap total rows.
    t = _valid_ticker(ticker)
    if not t:
        return jsonify({"error": "invalid ticker"}), 400
    known = _rows("SELECT 1 FROM trades WHERE ticker=? LIMIT 1", t) or \
        _rows("SELECT 1 FROM signals WHERE ticker=? LIMIT 1", t)
    if not known:
        return jsonify({"error": f"{t} is not tracked by StockPilot"}), 404
    con = db.connect_watchlist()
    count = con.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
    if count >= WATCHLIST_MAX_ROWS:
        con.close()
        return jsonify({"error": "watchlist is full"}), 409
    con.execute("INSERT OR IGNORE INTO watchlist (ticker, added_at) "
                "VALUES (?, ?)", (t, db.now()))
    con.commit()
    con.close()
    return jsonify({"ok": True, "ticker": t})


@app.delete(P + "/api/watchlist/<ticker>")
def watchlist_remove(ticker: str):
    if not _admin_ok():
        return jsonify({"error": "admin token required"}), 403
    t = _valid_ticker(ticker)
    if not t:
        return jsonify({"error": "invalid ticker"}), 400
    con = db.connect_watchlist()
    con.execute("DELETE FROM watchlist WHERE ticker=?", (t,))
    con.commit()
    con.close()
    return jsonify({"ok": True, "ticker": t})


@app.get(P + "/api/backtest")
def backtest():
    return jsonify(run_backtest())


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001)
