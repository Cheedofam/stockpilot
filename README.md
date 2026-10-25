# StockPilot

Automated stock-intelligence platform: it tracks **disclosed** smart-money
activity — US House congressional trades, billionaire fund-manager 13F filings,
and corporate-insider Form 4 buys — scores tickers by how many distinct buyers
are *converging*, forecasts price with the Kronos ML model (sklearn fallback),
explains each signal with Gemini, and serves it all on a React dashboard.

> Research tooling, not investment advice. Every signal is a **disclosure**,
> filed days to weeks after the trade.

## Layout

```
main.py              CLI (collect / score / news / predict / explain /
                     pipeline / stats / backtest / digest / dashboard)
daily_update.py      orchestrates the twice-daily automated run
run_daily.bat        Task Scheduler wrapper
deploy_pythonanywhere.py   backend upload + reload helper
src/                 collectors, scoring, predictor, explainer, backtest
dashboard/app.py     Flask API (routes under /stockpilot)
frontend/            React + Vite site (deployed to Vercel)
tests/               pytest unit tests (hermetic, no network)
```

## Setup

```powershell
pip install -r requirements.txt
copy config.example.json config.json   # then fill in your keys
python main.py pipeline                 # collect -> score -> news -> predict -> explain
python main.py dashboard                # Flask API on :5001
```

Secrets come from environment variables first (e.g. `FINNHUB_API_KEY`), then
`config.json`. `config.json` is git-ignored — never commit it.

## Convergence score

A 0–100 score per ticker that rises when multiple **distinct** smart-money
buyers converge recently:

* +14 per congress buyer, +18 per billionaire fund, +16 per corporate insider
  (max 3 of each counted)
* +10 bonus per additional trader *type* present (cross-source agreement)
* up to +20 from total disclosed buy dollars; −8 per distinct seller
* every component decays by `exp(−age/45d)` on the **disclosure** date

## Signal validation

`python main.py backtest` joins the daily score snapshots in `signal_history`
to realized forward returns (5/21/63 trading days) and reports the information
coefficient and top-quartile hit rate. It needs a few days of snapshots to
mature before the numbers mean anything — that's by design.

## Data sources

| Signal | Source | Freshness |
|--------|--------|-----------|
| Congressional trades | House Stock Watcher mirror (GitHub) | ≤45 days |
| Billionaire 13Fs | SEC EDGAR | ≤45 days after quarter end |
| Corporate insiders | SEC EDGAR Form 4 | ≤2 business days |
| Company news | Finnhub company-news | hours |
| Prices | yfinance | end-of-day |

### Known data gap — Senate coverage

Congressional data is **House-only**. The free Senate Stock Watcher feed went
offline in 2024 and no maintained free Senate source exists as of mid-2026.
Restoring Senate coverage requires a paid API (Quiver Quantitative or Unusual
Whales). The collector in `src/congress.py` already merges multiple chambers —
add a Senate adapter there once a key is available.

## Tests

```powershell
python -m pytest
```

Hermetic: scoring math, the 13F NEW/ADD/TRIM/EXIT differ, congress parsing,
keyword sentiment, and the realized-vol prediction clip. CI runs them on push
(`.github/workflows/ci.yml`) once the repo has a GitHub remote.
