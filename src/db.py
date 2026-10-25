"""SQLite storage for StockPilot.

Two databases, deliberately separated:

  stocks.db    — pipeline-generated state, fully rebuilt each run and uploaded
                 to the live server (overwriting it). Tables: trades, signals,
                 predictions, news, explanations, signal_history.
  watchlist.db — user-mutable state written by the live site. The pipeline
                 NEVER overwrites this file, so watchlist edits survive the
                 twice-daily data upload.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,            -- 'congress' | 'billionaire' | 'insider'
  trader TEXT NOT NULL,            -- member / fund manager / corporate insider
  detail TEXT,                     -- chamber / fund+action / role
  ticker TEXT NOT NULL,
  type TEXT NOT NULL,              -- 'buy' | 'sell'
  amount_usd REAL,                 -- midpoint / 13F value / shares*price
  amount_range TEXT,
  trade_date TEXT,                 -- YYYY-MM-DD (13F: report period end)
  disclosure_date TEXT,            -- YYYY-MM-DD filing date
  collected_at TEXT,
  UNIQUE(source, trader, ticker, type, trade_date, amount_range)
);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(trade_date);

CREATE TABLE IF NOT EXISTS signals (
  ticker TEXT PRIMARY KEY,
  company TEXT,
  score REAL,
  buyers INTEGER,
  congress_buyers INTEGER,
  billionaire_buyers INTEGER,
  insider_buyers INTEGER,
  buyer_names TEXT,                -- JSON list
  total_buy_usd REAL,
  sells INTEGER,
  last_trade_date TEXT,
  risk TEXT,                       -- LOW | MEDIUM | HIGH
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS predictions (
  ticker TEXT PRIMARY KEY,
  direction TEXT,                  -- BULLISH | BEARISH | NEUTRAL
  confidence REAL,                 -- 0..1
  model TEXT,                      -- 'Kronos-small' | 'sklearn'
  last_price REAL,
  pct_1d REAL, pct_3d REAL, pct_7d REAL,
  target_1d REAL, target_3d REAL, target_7d REAL,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS news (
  id INTEGER PRIMARY KEY,
  ticker TEXT NOT NULL,
  headline TEXT NOT NULL,
  source TEXT,
  url TEXT,
  published TEXT,
  sentiment REAL,
  UNIQUE(ticker, headline, published)
);
CREATE INDEX IF NOT EXISTS idx_news_ticker ON news(ticker);

CREATE TABLE IF NOT EXISTS explanations (
  ticker TEXT PRIMARY KEY,
  text TEXT,
  model TEXT,
  updated_at TEXT
);

-- Advisory second opinion from the TradingAgents multi-agent desk (optional;
-- see src/desk.py). Pipeline-generated like predictions/explanations, so it
-- lives in stocks.db and is regenerated each run.
CREATE TABLE IF NOT EXISTS desk_opinions (
  ticker TEXT PRIMARY KEY,
  action TEXT,                     -- BUY | HOLD | SELL
  rationale TEXT,
  model TEXT,                      -- 'tradingagents'
  as_of TEXT,                      -- analysis date fed to the desk
  updated_at TEXT
);

-- Multi-persona equity-research notes (Goldman/Morgan Stanley/Bridgewater/
-- JPMorgan/BlackRock). One row per (ticker, persona); ticker '_PORTFOLIO' holds
-- portfolio-level personas. See src/analyst.py.
CREATE TABLE IF NOT EXISTS analyst_reports (
  ticker TEXT NOT NULL,
  persona TEXT NOT NULL,
  text TEXT,
  model TEXT,                      -- 'gemini' | 'template'
  updated_at TEXT,
  PRIMARY KEY (ticker, persona)
);

-- Daily score snapshots, the raw material for the backtest / signal validation.
CREATE TABLE IF NOT EXISTS signal_history (
  snapshot_date TEXT NOT NULL,     -- YYYY-MM-DD
  ticker TEXT NOT NULL,
  score REAL,
  buyers INTEGER,
  last_price REAL,
  PRIMARY KEY (snapshot_date, ticker)
);
CREATE INDEX IF NOT EXISTS idx_sighist_ticker ON signal_history(ticker);

-- Daily options-market positioning overlay (display + validation only; never
-- feeds scoring). One row per (ticker, date); history accumulates for IV rank
-- and the backtest join. See src/options_flow.py.
CREATE TABLE IF NOT EXISTS options_flow (
  ticker TEXT NOT NULL,
  date   TEXT NOT NULL,          -- YYYY-MM-DD
  pc_oi_ratio REAL, pc_vol_ratio REAL, atm_iv REAL, iv_rank REAL,
  unusual_volume INTEGER,        -- 0/1, NULL when unknown
  skew_label TEXT,
  spot REAL, expirations TEXT,   -- provenance
  PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_optflow_ticker ON options_flow(ticker);

-- Research Desk daily brief (advisory-only; see src/research/). One brief
-- row per day; ideas are the deterministic, risk-audited structure behind
-- it. Pipeline-generated, so both live in stocks.db and history accumulates
-- by date (like signal_history).
CREATE TABLE IF NOT EXISTS research_briefs (
  as_of TEXT PRIMARY KEY,          -- YYYY-MM-DD
  markdown TEXT,
  model TEXT,                      -- 'gemini' | 'template'
  chatter_json TEXT,               -- low-trust retail chatter (JSON list)
  pump_flags_json TEXT,            -- pump-and-dump watch (JSON list)
  errors_json TEXT,                -- sources unavailable this run
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS research_ideas (
  as_of TEXT NOT NULL,             -- YYYY-MM-DD
  rank INTEGER NOT NULL,
  ticker TEXT NOT NULL,
  kind TEXT,                       -- 'stock' | 'etf' | 'dividend'
  thesis TEXT,
  trust_tier INTEGER,              -- best (lowest) tier cited
  risk_label TEXT,                 -- Low | Medium | High
  evidence_json TEXT,              -- tier-cited evidence (JSON list)
  sizing_note TEXT,
  entry_exit TEXT,
  auditor_flags_json TEXT,         -- risk-auditor annotations (JSON list)
  PRIMARY KEY (as_of, ticker)
);
CREATE INDEX IF NOT EXISTS idx_research_ideas_ticker
  ON research_ideas(ticker);
"""

WATCHLIST_SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
  ticker TEXT PRIMARY KEY,
  added_at TEXT,
  company TEXT
);
"""


def _migrate(con: sqlite3.Connection) -> None:
    """Add columns introduced after the first deploy (idempotent)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(signals)").fetchall()}
    if "insider_buyers" not in cols:
        con.execute("ALTER TABLE signals ADD COLUMN insider_buyers INTEGER "
                    "DEFAULT 0")
        con.commit()


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def connect_watchlist() -> sqlite3.Connection:
    """Open the separate, never-overwritten watchlist database.

    On first open, migrate any rows from the legacy watchlist table that used
    to live inside stocks.db so existing entries aren't lost.
    """
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.WATCHLIST_DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(WATCHLIST_SCHEMA)
    empty = con.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0
    if empty and config.DB_PATH.exists():
        try:
            old = sqlite3.connect(config.DB_PATH)
            rows = old.execute(
                "SELECT ticker, added_at, company FROM watchlist").fetchall()
            old.close()
            con.executemany(
                "INSERT OR IGNORE INTO watchlist (ticker, added_at, company) "
                "VALUES (?,?,?)", rows)
            con.commit()
        except Exception:
            pass  # legacy table absent — nothing to migrate
    return con


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def insert_trades(con: sqlite3.Connection, rows: list[dict]) -> int:
    """Insert trades, ignoring duplicates. Returns number of NEW rows."""
    new = 0
    ts = now()
    for r in rows:
        cur = con.execute(
            """INSERT OR IGNORE INTO trades
               (source, trader, detail, ticker, type, amount_usd,
                amount_range, trade_date, disclosure_date, collected_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (r["source"], r["trader"], r.get("detail", ""), r["ticker"],
             r["type"], r.get("amount_usd"), r.get("amount_range", ""),
             r.get("trade_date", ""), r.get("disclosure_date", ""), ts))
        new += cur.rowcount
    con.commit()
    return new


def replace_signal(con: sqlite3.Connection, s: dict) -> None:
    con.execute(
        """INSERT OR REPLACE INTO signals
           (ticker, company, score, buyers, congress_buyers,
            billionaire_buyers, insider_buyers, buyer_names, total_buy_usd,
            sells, last_trade_date, risk, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (s["ticker"], s.get("company", s["ticker"]), s["score"], s["buyers"],
         s["congress_buyers"], s["billionaire_buyers"],
         s.get("insider_buyers", 0), json.dumps(s["buyer_names"]),
         s["total_buy_usd"], s["sells"], s["last_trade_date"], s["risk"],
         now()))


def snapshot_signals(con: sqlite3.Connection, signals: list[dict],
                     last_prices: dict[str, float]) -> int:
    """Record today's scores into signal_history (one row per ticker/day)."""
    today = dt.date.today().isoformat()
    n = 0
    for s in signals:
        con.execute(
            """INSERT OR REPLACE INTO signal_history
               (snapshot_date, ticker, score, buyers, last_price)
               VALUES (?,?,?,?,?)""",
            (today, s["ticker"], s["score"], s["buyers"],
             last_prices.get(s["ticker"])))
        n += 1
    con.commit()
    return n


def replace_prediction(con: sqlite3.Connection, p: dict) -> None:
    con.execute(
        """INSERT OR REPLACE INTO predictions
           (ticker, direction, confidence, model, last_price,
            pct_1d, pct_3d, pct_7d, target_1d, target_3d, target_7d,
            updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (p["ticker"], p["direction"], p["confidence"], p["model"],
         p["last_price"], p["pct_1d"], p["pct_3d"], p["pct_7d"],
         p["target_1d"], p["target_3d"], p["target_7d"], now()))


def replace_explanation(con: sqlite3.Connection, ticker: str, text: str,
                        model: str) -> None:
    con.execute(
        "INSERT OR REPLACE INTO explanations (ticker, text, model, updated_at)"
        " VALUES (?,?,?,?)", (ticker, text, model, now()))


def replace_desk_opinion(con: sqlite3.Connection, op: dict) -> None:
    con.execute(
        """INSERT OR REPLACE INTO desk_opinions
           (ticker, action, rationale, model, as_of, updated_at)
           VALUES (?,?,?,?,?,?)""",
        (op["ticker"], op["action"], op.get("rationale", ""),
         op.get("model", "tradingagents"), op.get("as_of", ""), now()))
    con.commit()


def replace_analyst_report(con: sqlite3.Connection, ticker: str, persona: str,
                           text: str, model: str) -> None:
    con.execute(
        """INSERT OR REPLACE INTO analyst_reports
           (ticker, persona, text, model, updated_at)
           VALUES (?,?,?,?,?)""",
        (ticker, persona, text, model, now()))


def replace_options_flow(con: sqlite3.Connection, row: dict) -> None:
    """Daily upsert of one ticker's options-flow snapshot (INSERT OR REPLACE
    on the (ticker, date) PK). `row` keys: ticker, date, pc_oi_ratio,
    pc_vol_ratio, atm_iv, iv_rank, unusual_volume (0/1/None), skew_label,
    spot, expirations. Commits."""
    uv = row.get("unusual_volume")
    con.execute(
        """INSERT OR REPLACE INTO options_flow
           (ticker, date, pc_oi_ratio, pc_vol_ratio, atm_iv, iv_rank,
            unusual_volume, skew_label, spot, expirations)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (row["ticker"], row["date"], row.get("pc_oi_ratio"),
         row.get("pc_vol_ratio"), row.get("atm_iv"), row.get("iv_rank"),
         int(uv) if uv is not None else None, row.get("skew_label"),
         row.get("spot"), row.get("expirations")))
    con.commit()


def options_iv_history(con: sqlite3.Connection, ticker: str,
                       before_date: str) -> list[float]:
    """Ascending list of this ticker's prior non-null atm_iv values with
    date < before_date (today excluded). Feeds iv_rank's ≥10-prior-snapshots
    rule."""
    rows = con.execute(
        "SELECT atm_iv FROM options_flow WHERE ticker=? AND date<? AND "
        "atm_iv IS NOT NULL ORDER BY date", (ticker, before_date)).fetchall()
    return [r["atm_iv"] for r in rows]


def latest_options_flow(con: sqlite3.Connection) -> dict[str, dict]:
    """{ticker: latest-row-dict} — the max(date) options_flow row per ticker.
    Used by the digest (the API does the same join in SQL)."""
    rows = con.execute(
        """SELECT o.* FROM options_flow o
           JOIN (SELECT ticker, MAX(date) md FROM options_flow GROUP BY ticker) m
             ON o.ticker = m.ticker AND o.date = m.md""").fetchall()
    return {r["ticker"]: dict(r) for r in rows}


def replace_research_brief(con: sqlite3.Connection, brief: dict) -> None:
    """Upsert the day's Research Desk brief (PK as_of). `brief` keys:
    as_of, markdown, model, chatter, pump_flags, errors. Commits."""
    con.execute(
        """INSERT OR REPLACE INTO research_briefs
           (as_of, markdown, model, chatter_json, pump_flags_json,
            errors_json, updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (brief["as_of"], brief.get("markdown", ""),
         brief.get("model", "template"),
         json.dumps(brief.get("chatter", [])),
         json.dumps(brief.get("pump_flags", [])),
         json.dumps(brief.get("errors", [])), now()))
    con.commit()


def replace_research_ideas(con: sqlite3.Connection, as_of: str,
                           ideas: list[dict]) -> int:
    """Replace the day's ranked ideas wholesale (delete-then-insert on
    as_of, so a re-run never leaves stale rows). Commits; returns count."""
    con.execute("DELETE FROM research_ideas WHERE as_of=?", (as_of,))
    for rank, idea in enumerate(ideas, 1):
        con.execute(
            """INSERT INTO research_ideas
               (as_of, rank, ticker, kind, thesis, trust_tier, risk_label,
                evidence_json, sizing_note, entry_exit, auditor_flags_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (as_of, rank, idea["ticker"], idea.get("kind", "stock"),
             idea.get("thesis", ""), idea.get("trust_tier"),
             idea.get("risk_label"),
             json.dumps(idea.get("evidence", [])),
             idea.get("sizing_note", ""), idea.get("entry_exit", ""),
             json.dumps(idea.get("auditor_flags", []))))
    con.commit()
    return len(ideas)


def latest_research_brief(con: sqlite3.Connection) -> dict | None:
    """The most recent brief row (as a dict with parsed JSON), or None."""
    r = con.execute("SELECT * FROM research_briefs ORDER BY as_of DESC "
                    "LIMIT 1").fetchone()
    if not r:
        return None
    out = dict(r)
    for col in ("chatter_json", "pump_flags_json", "errors_json"):
        out[col.replace("_json", "")] = json.loads(out.pop(col) or "[]")
    return out


def research_ideas_for(con: sqlite3.Connection,
                       as_of: str) -> list[dict]:
    """The ranked ideas for one date, JSON columns parsed."""
    rows = con.execute(
        "SELECT * FROM research_ideas WHERE as_of=? ORDER BY rank",
        (as_of,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for col in ("evidence_json", "auditor_flags_json"):
            d[col.replace("_json", "")] = json.loads(d.pop(col) or "[]")
        out.append(d)
    return out


def insert_news(con: sqlite3.Connection, rows: list[dict]) -> int:
    """Upsert headlines. New rows are inserted; existing rows get their
    sentiment refreshed (so a re-run with better scoring updates them).
    Returns the count of genuinely new headlines."""
    new = 0
    for r in rows:
        exists = con.execute(
            "SELECT 1 FROM news WHERE ticker=? AND headline=? AND published=?",
            (r["ticker"], r["headline"], r.get("published", ""))).fetchone()
        con.execute(
            """INSERT INTO news (ticker, headline, source, url, published,
                                 sentiment)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(ticker, headline, published)
               DO UPDATE SET sentiment=excluded.sentiment""",
            (r["ticker"], r["headline"], r.get("source", ""),
             r.get("url", ""), r.get("published", ""), r.get("sentiment")))
        if not exists:
            new += 1
    con.commit()
    return new
