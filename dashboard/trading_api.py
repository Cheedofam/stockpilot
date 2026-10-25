"""Trading telemetry API — Blueprint under /stockpilot/api/trading/*.

Read endpoints serve the bot's CSV telemetry (bot.portfolio) and the
persisted circuit-breaker state; they never need a broker connection and
degrade gracefully before the bot's first run. /positions is the exception:
it can only reflect a live/paper broker — under the default SimulatedBroker
the engine's positions live in another process's memory, so the endpoint
says exactly that instead of pretending.

The two admin actions are the remote kill switch:
  POST /halt   -> persist HALTED (the engine stops on its next tick;
                  flattening happens engine-side, where the broker lives)
  POST /reset  -> clear a halt and re-anchor the watermark
Both are fail-closed: they require an X-Admin-Token header matching the
BOT_ADMIN_TOKEN env var / bot_admin_token config value, and refuse when no
token is configured at all.
"""

from __future__ import annotations

import hmac

from flask import Blueprint, jsonify, request

from src import config
from bot.broker import SimulatedBroker, make_broker
from bot.portfolio import Portfolio
from bot.risk_manager import CircuitBreaker

trading_bp = Blueprint("trading", __name__,
                       url_prefix="/stockpilot/api/trading")


def _bot_cfg() -> dict:
    cfg = config.load().get("bot", {})
    return cfg if isinstance(cfg, dict) else {}


# Module-level factories so tests can monkeypatch state locations.
def get_portfolio() -> Portfolio:
    return Portfolio()


def get_breaker() -> CircuitBreaker:
    return CircuitBreaker(
        max_dd_pct=float(_bot_cfg().get("max_drawdown_pct", 10)))


def _admin_ok() -> bool:
    """Fail closed: no configured token means NO admin access, not open."""
    expected = str(config.get("bot_admin_token", "") or "")
    supplied = request.headers.get("X-Admin-Token", "")
    return bool(expected) and hmac.compare_digest(expected, supplied)


@trading_bp.get("/summary")
def summary():
    data = get_portfolio().summary()
    # float('inf') would serialize as a bare `Infinity` token — legal for
    # Python's json but not RFC 8259, and browsers' JSON.parse rejects it
    # (axios then silently hands the frontend a raw string). Use the same
    # "inf" sentinel as bot/backtest.py's saved results.
    if data.get("profit_factor") == float("inf"):
        data["profit_factor"] = "inf"
    return jsonify(data)


@trading_bp.get("/history")
def history():
    try:
        limit = max(0, min(int(request.args.get("limit", 50)), 500))
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    rows = get_portfolio().read_trades(limit=limit)
    rows.reverse()                          # newest first for the ledger
    return jsonify(rows)


@trading_bp.get("/positions")
def positions():
    cfg = _bot_cfg()
    try:
        broker = make_broker(cfg)
    except RuntimeError as exc:             # live stub: report, don't crash
        return jsonify({"positions": [], "simulated": False,
                        "note": str(exc)})
    if isinstance(broker, SimulatedBroker):
        return jsonify({
            "positions": [], "simulated": True,
            "note": "simulated broker — positions live in the engine "
                    "process; see /history and /summary for telemetry"})
    out = [{"symbol": p.symbol, "qty": p.qty, "side": p.side,
            "avg_entry": p.avg_entry, "last_price": p.last_price,
            "unrealized": p.unrealized,
            "unrealized_pct": (p.unrealized / (abs(p.qty) * p.avg_entry)
                               * 100) if p.qty and p.avg_entry else 0.0}
           for p in broker.get_positions()]
    return jsonify({"positions": out, "simulated": False, "note": ""})


@trading_bp.get("/status")
def status():
    cfg = _bot_cfg()
    breaker = get_breaker()
    portfolio = get_portfolio()
    curve = portfolio.read_equity_curve()
    equity = curve[-1]["equity"] if curve else None
    return jsonify({
        "enabled": bool(cfg.get("enabled", False)),
        "mode": str(cfg.get("mode", "sim")),
        "broker": str(cfg.get("broker", "simulated")),
        "halted": breaker.halted,
        "trip_reason": breaker.trip_reason,
        "peak_equity": breaker.peak_equity or None,
        "equity": equity,
        "drawdown_pct": (round(breaker.drawdown_pct(equity), 4)
                         if equity is not None else None),
        "max_drawdown_pct": breaker.max_dd_pct,
        "last_heartbeat": curve[-1]["ts"] if curve else None,
    })


@trading_bp.post("/halt")
def halt():
    if not _admin_ok():
        return jsonify({"error": "admin token required"}), 403
    breaker = get_breaker()
    already = breaker.halted
    if not already:
        # No broker here — persist the halt; the engine (which owns the
        # broker) sees it on its next breaker.update and stops/flattens.
        breaker.trip(broker=None, reason="manual halt via dashboard")
    return jsonify({"ok": True, "halted": True,
                    "already_halted": already})


@trading_bp.post("/reset")
def reset():
    if not _admin_ok():
        return jsonify({"error": "admin token required"}), 403
    breaker = get_breaker()
    if not breaker.halted:
        return jsonify({"ok": True, "halted": False,
                        "note": "breaker was not halted"})
    curve = get_portfolio().read_equity_curve()
    equity = curve[-1]["equity"] if curve else \
        float(_bot_cfg().get("starting_equity", 1000))
    breaker.reset(equity)
    return jsonify({"ok": True, "halted": False,
                    "watermark": equity})
