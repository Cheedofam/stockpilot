#!/usr/bin/env python3
"""StockPilot CLI.

    python main.py collect     # congress + billionaire 13F + Form 4 -> db
    python main.py score       # convergence scoring -> signals table
    python main.py news        # Finnhub company news + Gemini sentiment
    python main.py options     # yfinance options-flow overlay -> options_flow
    python main.py predict     # Kronos (or sklearn fallback) predictions
    python main.py explain     # Gemini (or template) explanations
    python main.py desk        # TradingAgents advisory opinions (optional)
    python main.py analyst [TICKER]   # Goldman/MS/Bridgewater/JPM/BlackRock notes
    python main.py research    # Research Desk daily brief (advisory-only)
    python main.py pipeline    # all of the above, in order
    python main.py stats       # print a summary of the database
    python main.py top10       # print the 10 highest convergence signals
    python main.py backtest    # score vs forward-return validation
    python main.py digest      # email the daily digest
    python main.py dashboard   # run the Flask API on port 5001
    python main.py bot         # launch the trading engine (sim by default)
    python main.py bot --backtest   # run the 6-month strategy backtest gate
    python main.py bot --reset      # manually re-arm a tripped circuit breaker
"""

from __future__ import annotations

import argparse
import json
import sys

# Windows consoles default to cp1252, which can't print ✓/• glyphs.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

from src import config, db


def _top_tickers(limit: int | None = None) -> list[str]:
    con = db.connect()
    q = "SELECT ticker FROM signals ORDER BY score DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    out = [r["ticker"] for r in con.execute(q).fetchall()]
    con.close()
    return out


def cmd_collect(_args) -> int:
    cfg = config.load()
    con = db.connect()

    print("• Congressional trades…", flush=True)
    from src.congress import fetch_congress_trades
    c_rows, c_status = fetch_congress_trades(
        int(cfg.get("congress_window_days", 120)))
    new_c = db.insert_trades(con, c_rows)
    print(f"  {len(c_rows)} trades in window "
          f"({'+'.join(c_status['chambers'])}), {new_c} new")

    print("• Billionaire 13F moves…", flush=True)
    from src.billionaires import fetch_billionaire_trades
    b_rows, b_status = fetch_billionaire_trades(cfg.get("funds", []))
    new_b = db.insert_trades(con, b_rows)
    print(f"  {len(b_rows)} moves from {len(b_status['ok'])} funds, "
          f"{new_b} new; failed: {b_status['failed'] or 'none'}")

    # Form 4 insiders for the watchlist plus any ticker Congress just bought —
    # the freshest signal, and where a third converging source matters most.
    print("• Corporate insider Form 4 buys…", flush=True)
    from src.insiders import fetch_insider_activity
    universe = list(cfg.get("watchlist", []))
    for t in {r["ticker"] for r in c_rows if r["type"] == "buy"}:
        if t not in universe:
            universe.append(t)
    i_rows, i_status = fetch_insider_activity(universe)
    new_i = db.insert_trades(con, i_rows)
    print(f"  {i_status['rows']} open-market txns across "
          f"{i_status['tickers_with_activity']}/{i_status['tickers_checked']} "
          f"tickers, {new_i} new")
    con.close()
    return 0


def cmd_score(_args) -> int:
    print("• Convergence scoring…", flush=True)
    from src.scoring import run_scoring
    scored = run_scoring(int(config.get("max_scored_tickers", 40)))
    for s in scored[:5]:
        print(f"  {s['ticker']:<6} score {s['score']:>5}  "
              f"buyers {s['buyers']} ({s['congress_buyers']}C/"
              f"{s['billionaire_buyers']}B/{s.get('insider_buyers', 0)}I)  "
              f"sells {s['sells']}")
    print(f"  {len(scored)} tickers scored")
    return 0


def _with_watchlist(tickers: list[str]) -> list[str]:
    """Top signals plus the config watchlist (DELL, NVDA, …), deduped."""
    out = list(tickers)
    for t in config.get("watchlist", []):
        if t not in out:
            out.append(t)
    return out


def cmd_news(_args) -> int:
    tickers = _with_watchlist(_top_tickers(20))
    print(f"• Finnhub news for {len(tickers)} tickers…", flush=True)
    from src.news_sentiment import run_news
    status = run_news(tickers)
    print(f"  {status['new_headlines']} new headlines stored")
    return 0


def cmd_options(_args) -> int:
    from src.options_flow import run_options
    status = run_options()
    if status.get("status") == "disabled":
        print("  options overlay disabled "
              "(set config 'options_flow.enabled' to enable)")
        return 0
    print(f"• Options flow for up to {status['tickers']} tickers…", flush=True)
    print(f"  stored {status['stored']} options-flow snapshots")
    return 0


def cmd_predict(_args) -> int:
    tickers = _with_watchlist(_top_tickers(15))
    print(f"• Predictions for {len(tickers)} tickers…", flush=True)
    from src import prices
    warmed = prices.prefetch_histories(tickers)   # parallel cache warm
    if warmed:
        print(f"  prefetched {warmed} price histories")
    from src.predictor import run_predictions
    run_predictions(tickers)
    return 0


def cmd_backtest(_args) -> int:
    print("• Backtest (score vs forward returns)…", flush=True)
    from src.backtest import run_backtest
    res = run_backtest()
    if res.get("status") != "ok":
        print(f"  {res.get('message', res)}")
        return 0
    print(f"  snapshots: {res['snapshots']} over {res['days']} day(s), "
          f"{res['pairs']} score→return pairs")
    for h, m in res["horizons"].items():
        if m["n"]:
            print(f"  {h}: IC={m['ic']:+.3f}  hit-rate(top quartile)="
                  f"{m['hit_rate']:.0%}  n={m['n']}")
        else:
            print(f"  {h}: not enough matured data yet")

    options = res.get("options", {})
    if options.get("snapshots"):
        for metric, horizons in options.get("metrics", {}).items():
            parts = []
            for h, m in horizons.items():
                ic = m.get("ic")
                n = m.get("n", 0)
                parts.append(f"{h} IC={ic:+.2f} (n={n})" if ic is not None
                             else f"{h} IC=n/a (n={n})")
            print(f"  options {metric}: " + "  ".join(parts))
        skew_parts = []
        for h, m in options.get("skew", {}).items():
            hr = m.get("hit_rate")
            n = m.get("n", 0)
            skew_parts.append(f"{h} hit={hr:.0%} (n={n})" if hr is not None
                              else f"{h} hit=n/a (n={n})")
        if skew_parts:
            print("  options skew hit-rate: " + "  ".join(skew_parts))
    return 0


def cmd_explain(_args) -> int:
    tickers = _top_tickers(10)
    if not tickers:
        print("  No signals yet — run `python main.py score` first.")
        return 1
    print(f"• Explanations for {len(tickers)} tickers…", flush=True)
    from src.explainer import run_explanations
    used = run_explanations(tickers)
    print(f"  models used: {used}")
    return 0


def cmd_desk(_args) -> int:
    # Advisory only; a no-op unless the desk is enabled in config AND
    # `pip install tradingagents` has been run — so it's safe in the pipeline.
    tickers = _top_tickers(int(config.get("desk", {}).get("max_tickers", 5))
                           if isinstance(config.get("desk", {}), dict) else 5)
    if not tickers:
        print("  No signals yet — run `python main.py score` first.")
        return 0
    print(f"• TradingAgents desk for up to {len(tickers)} tickers…", flush=True)
    from src.desk import run_desk
    status = run_desk(tickers)
    if status["status"] == "disabled":
        print("  desk disabled (set config 'desk.enabled' and install "
              "tradingagents to enable)")
    else:
        print(f"  {status['scored']} desk opinions stored")
    return 0


def cmd_analyst(args) -> int:
    # `analyst NVDA` → per-ticker notes (screen / DCF / earnings).
    # `analyst`      → portfolio-level notes (risk framework / construction).
    ticker = (getattr(args, "ticker", None) or "").upper() or None
    from src.analyst import run_analyst
    if ticker:
        personas = ["goldman_screener", "ms_dcf", "jpm_earnings"]
        print(f"• Analyst notes for {ticker}…", flush=True)
    else:
        personas = ["bridgewater_risk", "blackrock_portfolio"]
        print("• Portfolio-level analyst notes…", flush=True)
    status = run_analyst(ticker, personas)
    print(f"  stored {len(status['personas'])} report(s) {status['models']}")
    return 0


def cmd_research(_args) -> int:
    # Research Desk: collect (trust-tiered, fail-closed) → brief (advisor →
    # risk-auditor, Gemini or template) → store. ADVISORY ONLY — this can
    # never place an order; the email section stays off until
    # research.email_digest is enabled (Phase 2).
    cfg = config.load().get("research", {})
    if isinstance(cfg, dict) and not cfg.get("enabled", True):
        print("  research desk disabled (set config 'research.enabled')")
        return 0
    print("• Research Desk: collecting trust-tiered inputs…", flush=True)
    from src.research.collect import collect_all
    collected = collect_all()
    tiers = [i["tier"] for i in collected["items"]]
    print(f"  {len(collected['items'])} items across "
          f"{len(collected['universe'])} tickers "
          f"(T1:{tiers.count(1)} T2:{tiers.count(2)} T3:{tiers.count(3)}); "
          f"{len(collected['errors'])} source error(s)")

    print("• Building the brief (advisor → risk-auditor)…", flush=True)
    from src.research.brief import build_brief
    result = build_brief(collected)
    print(f"  {len(result['ideas'])} audited idea(s), "
          f"{len(result['chatter'])} chatter item(s), "
          f"{len(result['pump_flags'])} pump-and-dump flag(s) "
          f"[{result['model']}]")

    from src.research.deliver import store_brief, email_enabled
    status = store_brief(result)
    print(f"  stored brief {status['as_of']} with {status['ideas']} ideas")
    if not email_enabled():
        print("  email digest section: OFF (enable research.email_digest "
              "for Phase 2)")
    return 0


def cmd_pipeline(args) -> int:
    for step in (cmd_collect, cmd_score, cmd_news, cmd_options, cmd_predict,
                 cmd_explain, cmd_desk):
        rc = step(args)
        if rc not in (0, None) and step is cmd_collect:
            return rc
    print("✓ Pipeline complete. Try: python main.py stats")
    return 0


def cmd_stats(_args) -> int:
    con = db.connect()
    n = lambda q, *a: con.execute(q, a).fetchone()[0]
    total = n("SELECT COUNT(*) FROM trades")
    congress = n("SELECT COUNT(*) FROM trades WHERE source='congress'")
    billionaire = n("SELECT COUNT(*) FROM trades WHERE source='billionaire'")
    insider = n("SELECT COUNT(*) FROM trades WHERE source='insider'")
    import datetime as dt
    today = dt.date.today().isoformat()
    new_today = n("SELECT COUNT(*) FROM trades WHERE collected_at >= ?", today)
    signals = n("SELECT COUNT(*) FROM signals")
    preds = n("SELECT COUNT(*) FROM predictions")
    news = n("SELECT COUNT(*) FROM news")
    snaps = n("SELECT COUNT(DISTINCT snapshot_date) FROM signal_history")

    print("STOCKPILOT STATS")
    print("─" * 52)
    print(f"  trades tracked         : {total} "
          f"({congress} congress / {billionaire} billionaire / "
          f"{insider} insider)")
    print(f"  collected today        : {new_today}")
    print(f"  active signals         : {signals}")
    print(f"  predictions            : {preds}")
    print(f"  news headlines         : {news}")
    print(f"  history snapshots      : {snaps} day(s)")
    print()
    print("TOP 5 CONVERGENCE SIGNALS")
    print("─" * 52)
    rows = con.execute(
        """SELECT s.*, p.direction, p.pct_7d, p.model FROM signals s
           LEFT JOIN predictions p ON p.ticker = s.ticker
           ORDER BY s.score DESC LIMIT 5""").fetchall()
    for r in rows:
        buyers = ", ".join(json.loads(r["buyer_names"] or "[]")[:3])
        direction = r["direction"] or "—"
        print(f"  {r['ticker']:<6} {r['score']:>5}  {direction:<8} "
              f"risk {r['risk']:<7} buyers: {buyers}")
    con.close()
    return 0


def cmd_top10(_args) -> int:
    con = db.connect()
    rows = con.execute(
        """SELECT s.ticker, s.company, s.score, s.buyers,
                  s.congress_buyers, s.billionaire_buyers, s.insider_buyers,
                  s.risk, p.direction
           FROM signals s
           LEFT JOIN predictions p ON p.ticker = s.ticker
           ORDER BY s.score DESC
           LIMIT 10""").fetchall()
    if not rows:
        print("  No signals yet — run `python main.py score` first.")
        con.close()
        return 1

    print("TOP 10 CONVERGENCE SIGNALS")
    print("─" * 64)
    print(f"  {'#':>2}  {'TICKER':<6} {'SCORE':>5}  "
          f"{'SOURCES':<9} {'RISK':<7} {'BIAS':<8} COMPANY")
    for i, r in enumerate(rows, start=1):
        sources = (f"{r['congress_buyers']}C/"
                   f"{r['billionaire_buyers']}B/{r['insider_buyers']}I")
        company = (r["company"] or r["ticker"])[:28]
        print(f"  {i:>2}  {r['ticker']:<6} {r['score']:>5}  "
              f"{sources:<9} {(r['risk'] or '—'):<7} "
              f"{(r['direction'] or '—'):<8} {company}")
    con.close()
    return 0


def cmd_digest(_args) -> int:
    print("• Sending digest…", flush=True)
    from src.digest import send_digest
    result = send_digest()
    if result.get("sent"):
        print(f"  ✓ sent to {result['to']} "
              f"({result['signals']} signals)")
        return 0
    print(f"  ✗ failed: {result.get('error')}")
    return 1


def cmd_dashboard(_args) -> int:
    from dashboard.app import app
    app.run(host="127.0.0.1", port=5001, debug=False)
    return 0


def cmd_bot(args) -> int:
    # The trading engine: simulation-first, off by default (bot.enabled),
    # and guarded — see bot/main.startup_checks for the refusal chain.
    bot_cfg = config.load().get("bot") or {}
    if getattr(args, "reset", False):
        print("• Circuit breaker manual reset…", flush=True)
        from bot.main import reset_breaker
        print(f"  {reset_breaker(bot_cfg)}")
        return 0
    if getattr(args, "backtest", False):
        print("• Strategy backtest (fetches bars via yfinance)…", flush=True)
        from bot.backtest import run_backtest
        result = run_backtest(bot_cfg)
        return 0 if result["passed"] else 1
    print("• Launching trading engine…", flush=True)
    from bot.main import run_bot
    return run_bot(bot_cfg)


COMMANDS = {
    "collect": cmd_collect, "score": cmd_score, "news": cmd_news,
    "options": cmd_options,
    "predict": cmd_predict, "explain": cmd_explain, "desk": cmd_desk,
    "pipeline": cmd_pipeline, "stats": cmd_stats, "backtest": cmd_backtest,
    "digest": cmd_digest, "dashboard": cmd_dashboard, "top10": cmd_top10,
    "analyst": cmd_analyst, "bot": cmd_bot, "research": cmd_research,
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="StockPilot")
    ap.add_argument("command", choices=sorted(COMMANDS))
    ap.add_argument("ticker", nargs="?", default=None,
                    help="optional ticker (used by the `analyst` command)")
    ap.add_argument("--reset", action="store_true",
                    help="bot: manually re-arm a tripped circuit breaker")
    ap.add_argument("--backtest", action="store_true",
                    help="bot: run the 6-month strategy backtest gate")
    args = ap.parse_args(argv)
    return COMMANDS[args.command](args) or 0


if __name__ == "__main__":
    sys.exit(main())
