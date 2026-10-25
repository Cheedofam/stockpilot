"""Read-only portfolio tracking via SnapTrade (the API Blossom itself uses).

Blossom is a social investing app / portfolio tracker — explicitly NOT a
trading platform and NOT an income source. Its role here is limited to:
  (a) reconciliation: compare what the brokerage actually holds against the
      bot's own records, and
  (b) a unified holdings view for the dashboard.
Publishing to Blossom's feed stays manual (no public write API).

Mirrors the desk.py optional-dependency pattern: off by default in config
(bot.portfolio_tracker.enabled), silent no-op when the SDK isn't installed,
and NEVER able to place a trade — this module only ever reads holdings.

Enable (later, deliberately): `pip install snaptrade-python-sdk`, set
bot.portfolio_tracker.enabled=true, and provide SNAPTRADE_CLIENT_ID /
SNAPTRADE_CONSUMER_KEY via the environment (.env) — never in the repo.
"""

from __future__ import annotations

import os

from src import config

_client = None
_checked = False


def _cfg() -> dict:
    bot = config.load().get("bot", {})
    block = bot.get("portfolio_tracker", {}) if isinstance(bot, dict) else {}
    return block if isinstance(block, dict) else {}


def available() -> bool:
    """True only when enabled in config AND the SDK + creds are present.

    Memoized like desk.available(): one attempt, then a cheap flag. Any
    failure (not installed, no keys) degrades to a silent no-op.
    """
    global _checked, _client
    if _checked:
        return _client is not None
    _checked = True
    cfg = _cfg()
    if not cfg.get("enabled"):
        return False
    client_id = os.environ.get("SNAPTRADE_CLIENT_ID", "")
    consumer_key = os.environ.get("SNAPTRADE_CONSUMER_KEY", "")
    if not client_id or not consumer_key:
        print("  portfolio tracker enabled but SNAPTRADE_* env vars missing "
              "— skipping")
        return False
    try:
        from snaptrade_client import SnapTrade  # type: ignore
        _client = SnapTrade(client_id=client_id, consumer_key=consumer_key)
        print("  SnapTrade portfolio tracker connected (read-only)")
    except Exception as exc:
        print(f"  portfolio tracker unavailable ({type(exc).__name__}) — "
              "skipping")
        _client = None
    return _client is not None


def fetch_holdings() -> list[dict]:
    """Linked-brokerage holdings: [{symbol, qty, avg_price}]. [] when off."""
    if not available():
        return []
    try:
        # SnapTrade's exact response shape varies by SDK version; normalize
        # defensively and never let a schema change break the caller.
        out = []
        for acct in _client.account_information.list_user_accounts():
            for pos in getattr(acct, "positions", []) or []:
                out.append({
                    "symbol": str(getattr(pos, "symbol", "")),
                    "qty": float(getattr(pos, "units", 0) or 0),
                    "avg_price": float(getattr(pos, "price", 0) or 0)})
        return out
    except Exception as exc:
        print(f"  holdings fetch failed ({type(exc).__name__})")
        return []


def reconcile(bot_positions: list[dict]) -> dict:
    """Compare the bot's book against tracked brokerage holdings.

    Returns {"status": "disabled"} when the tracker is off — safe to call
    unconditionally. Mismatches are informational: the bot NEVER auto-trades
    to 'fix' a mismatch; a human looks at the report.
    """
    if not available():
        return {"status": "disabled", "mismatches": []}
    tracked = {h["symbol"]: h for h in fetch_holdings()}
    mine = {p["symbol"]: p for p in bot_positions}
    mismatches = []
    for symbol in sorted(set(tracked) | set(mine)):
        t_qty = tracked.get(symbol, {}).get("qty", 0.0)
        m_qty = mine.get(symbol, {}).get("qty", 0.0)
        if abs(t_qty - m_qty) > 1e-9:
            mismatches.append({"symbol": symbol, "tracked_qty": t_qty,
                               "bot_qty": m_qty})
    return {"status": "ok", "mismatches": mismatches,
            "tracked": len(tracked), "bot": len(mine)}
