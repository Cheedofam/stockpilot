"""The engine: one async task per (strategy, symbol), everything guarded.

Startup is fail-closed, in this order (startup_checks):
  1. bot.enabled must be true (default false).
  2. bot.mode must be sim | paper | live.
  3. Outside sim: the account must be positively NON-REGISTERED
     (risk_manager.assert_non_registered — the TFSA/CRA guard).
  4. live additionally requires a RECENT PASSING backtest on disk
     (bot.backtest.has_recent_pass) — no pass, no live. Ever.
  5. A tripped circuit breaker (persisted HALTED state) refuses to start
     until `python main.py bot --reset`.
The broker factory adds its own last line of defense: live adapters are
stubs that raise at construction.

Each tick (Engine.tick — synchronous and unit-testable):
  bars ← bar_source → broker.mark (fires protective stops) → circuit
  breaker update (trip ⇒ flatten+halt) → strategy.evaluate → correlation
  filter + optional smart-money veto → risk-sized order → telemetry.
Any exception inside a tick fails CLOSED: log it, place nothing.

The default bar_source uses yfinance (network); tests inject synthetic
bars. SIGINT/Ctrl-C shuts down gracefully — flatten on exit only if
bot.flatten_on_exit is true (default: leave positions, brackets are on the
broker side anyway in sim).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time

from src import config

from . import backtest as bt
from .broker import Broker, SimulatedBroker, make_broker
from .indicators import atr
from .portfolio import Portfolio
from .risk_manager import (CircuitBreaker, assert_non_registered,
                           BotSafetyError, cap_units, correlation_block,
                           hard_stop_price, size_position)
from .strategies import Strategy, from_config

INTERVALS = {"15Min": 900, "1Hour": 3600, "4Hour": 14400, "1Day": 86400}
LOOKBACK = 320                              # bars fetched per tick


def _load_dotenv() -> None:
    """Best-effort .env loading; the engine must run without the package."""
    try:
        from dotenv import load_dotenv
        load_dotenv(config.ROOT / ".env")
    except ImportError:
        pass


def startup_checks(cfg: dict, state_path=None, result_path=None,
                   now: dt.datetime | None = None) -> tuple[bool, str]:
    """All the reasons NOT to start, in order. Returns (ok, reason)."""
    if not cfg.get("enabled"):
        return False, ("bot.enabled is false — set it in config.json after "
                       "reading docs/CLAUDE_CODE_BUILD_PLAN_trading_bot.md")
    mode = str(cfg.get("mode", "sim")).lower()
    if mode not in ("sim", "paper", "live"):
        return False, f"unknown bot.mode {mode!r} (sim | paper | live)"
    if mode != "sim":
        try:
            assert_non_registered(
                str(cfg.get("account_id", "")),
                str(cfg.get("account_type", "")),
                frozenset(cfg.get("registered_account_ids", []) or []))
        except BotSafetyError as exc:
            return False, str(exc)
    if mode == "live":
        if not bt.has_recent_pass(result_path or bt.DEFAULT_RESULT_PATH,
                                  now=now):
            return False, ("live mode requires a RECENT PASSING backtest — "
                           "run `python main.py bot --backtest` and clear "
                           "the gate first")
    breaker = CircuitBreaker(
        max_dd_pct=float(cfg.get("max_drawdown_pct", 10)),
        state_path=state_path)
    if breaker.halted:
        return False, (f"circuit breaker is HALTED ({breaker.trip_reason}) — "
                       "resume with `python main.py bot --reset` after review")
    return True, mode


def reset_breaker(cfg: dict, state_path=None,
                  current_equity: float | None = None) -> str:
    """The manual reset behind `python main.py bot --reset`."""
    breaker = CircuitBreaker(
        max_dd_pct=float(cfg.get("max_drawdown_pct", 10)),
        state_path=state_path)
    if not breaker.halted:
        return "circuit breaker is not halted — nothing to reset"
    equity = current_equity if current_equity is not None else \
        float(cfg.get("starting_equity", 1000))
    breaker.reset(equity)
    return f"circuit breaker reset; watermark re-anchored at {equity:.2f}"


def default_overlay_veto(symbol: str) -> str | None:
    """Smart-money confirm/veto: block a long if StockPilot's own desk says
    SELL for the ticker. Absent data is neutral (no veto) — the overlay
    corroborates, it never becomes a new source of conviction."""
    try:
        from src import db
        con = db.connect()
        row = con.execute("SELECT action FROM desk_opinions WHERE ticker=?",
                          (symbol,)).fetchone()
        con.close()
        if row and str(row["action"]).upper() == "SELL":
            return f"smart-money overlay: desk says SELL {symbol}"
    except Exception:
        return None                          # overlay must never break a tick
    return None


def yf_bar_source(symbol: str, timeframe: str, lookback: int) -> list[dict]:
    """Default live-data source (network). Tests always inject their own."""
    return bt.fetch_bars(symbol, timeframe)[-lookback:]


class Engine:
    """The tick loop with every dependency injected — hermetically testable."""

    def __init__(self, cfg: dict, broker: Broker, strategies: list[Strategy],
                 portfolio: Portfolio, breaker: CircuitBreaker,
                 bar_source=yf_bar_source, overlay_veto=None):
        self.cfg = cfg
        self.broker = broker
        self.strategies = strategies
        self.portfolio = portfolio
        self.breaker = breaker
        self.bar_source = bar_source
        self.overlay_veto = overlay_veto or (
            default_overlay_veto if cfg.get("smart_money_overlay")
            else (lambda s: None))
        self._bars_held: dict[str, int] = {}
        self._trades_logged = 0
        self._last_snapshot_day = ""
        self._stop = asyncio.Event()

    # -- one synchronous tick: the whole decision pipeline ------------------
    def tick(self, strategy: Strategy, symbol: str) -> str:
        try:
            return self._tick(strategy, symbol)
        except Exception as exc:            # fail closed: log, trade nothing
            print(f"  [{strategy.name}/{symbol}] tick error "
                  f"({type(exc).__name__}: {exc}) — no action", flush=True)
            return "error"

    def _tick(self, strategy: Strategy, symbol: str) -> str:
        bars = self.bar_source(symbol, strategy.timeframe, LOOKBACK)
        if not bars:
            return "no-data"                # stale/absent data: do nothing
        last = bars[-1]
        self.broker.mark(symbol, float(last["close"]),
                         low=float(last["low"]), high=float(last["high"]))
        self._log_new_trades()

        equity = self.broker.get_account().equity
        self.portfolio.record_equity(equity)
        if self.breaker.update(equity, self.broker):
            self._log_new_trades()          # the flatten fills
            return "halted"

        positions = {p.symbol: p for p in self.broker.get_positions()}
        open_pos = positions.get(symbol)
        if open_pos is not None:
            self._bars_held[symbol] = self._bars_held.get(symbol, 0) + 1
        else:
            self._bars_held[symbol] = 0

        position = None
        if open_pos is not None:
            position = {"side": open_pos.side, "entry": open_pos.avg_entry,
                        "bars_held": self._bars_held[symbol]}
        sig = strategy.evaluate(symbol, bars, position)
        status = "idle"
        if sig is not None:
            if sig.side == "exit" and open_pos is not None:
                self.broker.close_position(symbol, reason=sig.reason)
                status = "exit"
            elif sig.side in ("buy", "sell") and open_pos is None:
                status = self._enter(strategy, symbol, sig, bars, positions)
        self._log_new_trades()
        self._daily_snapshot(equity)
        return status

    def _enter(self, strategy: Strategy, symbol: str, sig, bars,
               positions) -> str:
        open_longs = {s for s, p in positions.items() if p.qty > 0}
        block = correlation_block(open_longs, symbol)
        if block:
            print(f"  [{strategy.name}/{symbol}] {block}", flush=True)
            return "blocked-correlation"
        if sig.side == "buy":
            veto = self.overlay_veto(symbol)
            if veto:
                print(f"  [{strategy.name}/{symbol}] {veto}", flush=True)
                return "blocked-overlay"
        a = atr(bars, 14)[-1]
        if not a or a <= 0:
            return "no-atr"
        acct = self.broker.get_account()
        gross = sum(abs(p.market_value)
                    for p in self.broker.get_positions())
        units = size_position(acct.equity, a, sig.stop_multiple,
                              float(self.cfg.get("risk_per_trade_pct", 1.0)))
        units = cap_units(
            units, float(bars[-1]["close"]), acct.equity,
            max_position_pct=float(self.cfg.get("max_position_pct", 25)),
            max_gross_pct=float(self.cfg.get("max_gross_pct", 100)),
            gross_exposure=gross,
            buying_power=acct.buying_power if sig.side == "buy" else None)
        if units <= 0:
            return "no-size"
        if sig.trailing:
            # Same convention as the backtester: hard stop at
            # StopMultiple×ATR (1% risk), ratchet arms after +1 ATR.
            close = float(bars[-1]["close"])
            res = self.broker.submit_trailing_stop(
                symbol, units, sig.side, trail_amount=sig.stop_multiple * a,
                strategy=strategy.name,
                stop_price=hard_stop_price(close, a, sig.stop_multiple,
                                           sig.side),
                activation_price=close + a if sig.side == "buy"
                else close - a)
        else:
            stop = hard_stop_price(float(bars[-1]["close"]), a,
                                   sig.stop_multiple, sig.side)
            res = self.broker.submit_bracket(
                symbol, units, sig.side, stop_price=stop,
                strategy=strategy.name)
        print(f"  [{strategy.name}/{symbol}] {sig.side} {units:.4f} "
              f"({res.status}) — {sig.reason}", flush=True)
        return f"entry-{res.status}"

    # -- telemetry -----------------------------------------------------------
    def _log_new_trades(self) -> None:
        trades = getattr(self.broker, "closed_trades", [])
        for trade in trades[self._trades_logged:]:
            self.portfolio.record_trade(trade)
        self._trades_logged = len(trades)

    def _daily_snapshot(self, equity: float) -> None:
        today = dt.date.today().isoformat()
        if today == self._last_snapshot_day:
            return
        trades = getattr(self.broker, "closed_trades", [])
        wins = sum(1 for t in trades if t["pnl"] > 0)
        losses = sum(1 for t in trades if t["pnl"] < 0)
        realized = sum(t["pnl"] for t in trades)
        unrealized = sum(p.unrealized for p in self.broker.get_positions())
        self.portfolio.snapshot_daily(equity, realized, unrealized,
                                      wins, losses, date=today)
        self._last_snapshot_day = today

    # -- the async loops -------------------------------------------------------
    async def _pair_loop(self, strategy: Strategy, symbol: str) -> None:
        interval = INTERVALS.get(strategy.timeframe, 3600)
        while not self._stop.is_set():
            status = self.tick(strategy, symbol)
            print(f"  ♥ {strategy.name}/{symbol}: {status} "
                  f"(next in {interval}s)", flush=True)
            if status == "halted":
                self._stop.set()
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def run_async(self) -> None:
        pairs = [(s, sym) for s in self.strategies for sym in s.symbols]
        print(f"Engine up: {len(pairs)} pair task(s), mode="
              f"{self.cfg.get('mode', 'sim')}, breaker at "
              f"{self.breaker.max_dd_pct:.0f}% dd", flush=True)
        await asyncio.gather(*(self._pair_loop(s, sym) for s, sym in pairs))

    def shutdown(self) -> None:
        self._stop.set()
        if self.cfg.get("flatten_on_exit"):
            n = self.broker.close_all()
            self._log_new_trades()
            print(f"  flattened {n} position(s) on exit", flush=True)


def run_bot(cfg: dict | None = None) -> int:
    """CLI entry: guard, build, loop. Returns an exit code."""
    _load_dotenv()
    cfg = cfg if cfg is not None else (config.load().get("bot") or {})
    ok, detail = startup_checks(cfg)
    if not ok:
        print(f"✗ bot refused to start: {detail}")
        return 1
    mode = detail
    try:
        broker = make_broker(cfg)           # sim unless explicitly not
    except RuntimeError as exc:             # live adapter stubs refuse
        print(f"✗ bot refused to start: {exc}")
        return 1
    if mode != "sim" and isinstance(broker, SimulatedBroker):
        print("  note: non-sim mode with the simulated broker — paper-like")
    engine = Engine(
        cfg, broker, from_config(cfg),
        Portfolio(mirror_db=bool(cfg.get("mirror_db"))),
        CircuitBreaker(max_dd_pct=float(cfg.get("max_drawdown_pct", 10))))
    try:
        asyncio.run(engine.run_async())
    except KeyboardInterrupt:
        print("\n  SIGINT — shutting down…")
        engine.shutdown()
    return 0
