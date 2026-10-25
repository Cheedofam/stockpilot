"""Broker abstraction: the engine talks to this interface, never a vendor SDK.

Adapters:
  SimulatedBroker — the default and the only one that is always available.
      Fully offline: bars are seeded by the caller (backtester, or a data
      layer in bot/main), fills are deterministic, and slippage is modeled at
      a configurable % per leg. No network, no account, no keys.
  QuestradeBroker — the Canadian live candidate (REST + OAuth 2.0). STUB.
      * Order placement may require Questrade partner-developer approval (or
        going through SnapTrade); a standard personal token reliably grants
        read access only. Confirm entitlement before any live phase.
      * Questrade rotates refresh tokens on every refresh: the adapter must
        persist the new token outside the repo (.env / secret file) or every
        restart breaks auth.
  AlpacaBroker — reference-only: Alpaca does not serve Canadian residents
      (live or paper) as of mid-2026.
  IBKRBroker — optional Canadian alternative via ib_async. STUB.

The stubs raise unconditionally at construction: live order flow is gated
behind the backtest + phased rollout (docs/CLAUDE_CODE_BUILD_PLAN_trading_bot.md
§9/§12), so no code path can reach a real broker until an adapter is actually
implemented and that rollout is complete.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Account:
    equity: float
    cash: float
    buying_power: float
    is_paper: bool = True


@dataclass
class Position:
    symbol: str
    qty: float                    # negative = short
    avg_entry: float
    last_price: float
    stop_price: float | None = None
    take_profit: float | None = None
    trail_amount: float | None = None   # $ distance; ratchets, never widens
    trail_activation: float | None = None  # ratchet arms only past this price
    strategy: str = ""

    @property
    def side(self) -> str:
        return "long" if self.qty > 0 else "short"

    @property
    def market_value(self) -> float:
        return self.qty * self.last_price

    @property
    def unrealized(self) -> float:
        return (self.last_price - self.avg_entry) * self.qty


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    status: str                   # "filled" | "rejected"
    fill_price: float = 0.0
    qty: float = 0.0
    reason: str = ""


class Broker(Protocol):
    def get_account(self) -> Account: ...
    def get_positions(self) -> list[Position]: ...
    def get_bars(self, symbol: str, timeframe: str,
                 lookback: int) -> list[dict]: ...
    def submit_bracket(self, symbol: str, qty: float, side: str,
                       stop_price: float, take_profit: float | None = None,
                       strategy: str = "") -> OrderResult: ...
    def submit_trailing_stop(self, symbol: str, qty: float, side: str,
                             trail_amount: float, strategy: str = "",
                             stop_price: float | None = None,
                             activation_price: float | None = None
                             ) -> OrderResult: ...
    def close_position(self, symbol: str, reason: str = "") -> OrderResult: ...
    def cancel_all(self) -> int: ...
    def close_all(self) -> int: ...


class SimulatedBroker:
    """Offline broker: deterministic fills, slippage per leg, protective exits.

    Price discovery is push-based: the driver (backtester or engine data
    task) calls mark(symbol, close, low, high) for every new bar, which
    updates valuations, ratchets trailing stops, and executes any protective
    stop / take-profit hit inside that bar. Orders fill immediately at the
    last marked price adjusted for slippage.
    """

    def __init__(self, starting_equity: float = 1000.0,
                 slippage_pct_per_leg: float = 0.05,
                 commission_per_trade: float = 0.0):
        self._cash = float(starting_equity)
        self._slip = float(slippage_pct_per_leg) / 100.0
        self._commission = float(commission_per_trade)
        self._positions: dict[str, Position] = {}
        self._last: dict[str, float] = {}
        self._bars: dict[str, list[dict]] = {}
        self._ids = itertools.count(1)
        self.fills: list[dict] = []          # every leg, for audit
        self.closed_trades: list[dict] = []  # round-trips, for the portfolio

    # ---- market data (seeded, offline) ------------------------------------
    def load_bars(self, symbol: str, bars: list[dict]) -> None:
        """Seed OHLCV bars: dicts with open/high/low/close/volume (+ ts)."""
        self._bars[symbol] = list(bars)

    def get_bars(self, symbol: str, timeframe: str,
                 lookback: int) -> list[dict]:
        bars = self._bars.get(symbol, [])
        return bars[-lookback:] if lookback else list(bars)

    def mark(self, symbol: str, price: float,
             low: float | None = None, high: float | None = None) -> None:
        """New bar for `symbol`: revalue, ratchet trails, fire stops/targets."""
        self._last[symbol] = float(price)
        pos = self._positions.get(symbol)
        if pos is None:
            return
        pos.last_price = float(price)
        lo = float(low if low is not None else price)
        hi = float(high if high is not None else price)

        # Trailing stops only tighten (long: up; short: down), never widen —
        # and, when an activation price is set, only start ratcheting once
        # the trade has moved through it. Until then the initial (hard)
        # stop_price protects the position without noise-driven tightening.
        if pos.trail_amount is not None:
            if pos.trail_activation is not None and (
                    (pos.qty > 0 and hi >= pos.trail_activation) or
                    (pos.qty < 0 and lo <= pos.trail_activation)):
                pos.trail_activation = None      # armed from this bar on
            if pos.trail_activation is None:
                if pos.qty > 0:
                    candidate = hi - pos.trail_amount
                    if pos.stop_price is None or candidate > pos.stop_price:
                        pos.stop_price = candidate
                else:
                    candidate = lo + pos.trail_amount
                    if pos.stop_price is None or candidate < pos.stop_price:
                        pos.stop_price = candidate

        # Protective exits, checked against the bar's range. Stop first:
        # assume the adverse move happens before the favorable one (the
        # conservative fill assumption for a backtest).
        if pos.stop_price is not None and (
                (pos.qty > 0 and lo <= pos.stop_price) or
                (pos.qty < 0 and hi >= pos.stop_price)):
            self._exit(pos, pos.stop_price, "stop")
        elif pos.take_profit is not None and (
                (pos.qty > 0 and hi >= pos.take_profit) or
                (pos.qty < 0 and lo <= pos.take_profit)):
            self._exit(pos, pos.take_profit, "take_profit")

    # ---- account ----------------------------------------------------------
    def get_account(self) -> Account:
        value = sum(p.market_value for p in self._positions.values())
        equity = self._cash + value
        return Account(equity=equity, cash=self._cash,
                       buying_power=max(self._cash, 0.0), is_paper=True)

    def get_positions(self) -> list[Position]:
        # Copies, so callers can't mutate broker state through the snapshot.
        return [Position(**vars(p)) for p in self._positions.values()]

    # ---- orders -----------------------------------------------------------
    def _fill_price(self, price: float, leg: str) -> float:
        """Slippage always hurts: pay up on buys, give up on sells."""
        return price * (1 + self._slip) if leg == "buy" \
            else price * (1 - self._slip)

    def _open(self, symbol: str, qty: float, side: str, strategy: str,
              stop_price: float | None = None,
              take_profit: float | None = None,
              trail_amount: float | None = None,
              trail_activation: float | None = None) -> OrderResult:
        oid = str(next(self._ids))
        if side not in ("buy", "sell"):
            return OrderResult(oid, "rejected", reason=f"bad side {side!r}")
        if qty <= 0:
            return OrderResult(oid, "rejected", reason="qty must be > 0")
        if symbol in self._positions:
            return OrderResult(oid, "rejected",
                               reason=f"already in a {symbol} position")
        last = self._last.get(symbol)
        if last is None:
            return OrderResult(oid, "rejected",
                               reason=f"no market price for {symbol}")
        fill = self._fill_price(last, side)
        notional = fill * qty
        if side == "buy" and notional + self._commission > self._cash:
            return OrderResult(oid, "rejected",
                               reason="insufficient buying power")
        signed = qty if side == "buy" else -qty
        self._cash -= fill * signed          # short entry adds proceeds
        self._cash -= self._commission
        self._positions[symbol] = Position(
            symbol=symbol, qty=signed, avg_entry=fill, last_price=last,
            stop_price=stop_price, take_profit=take_profit,
            trail_amount=trail_amount, trail_activation=trail_activation,
            strategy=strategy)
        self.fills.append({"order_id": oid, "symbol": symbol, "side": side,
                           "qty": qty, "price": fill, "reason": "entry"})
        return OrderResult(oid, "filled", fill_price=fill, qty=qty)

    def submit_bracket(self, symbol: str, qty: float, side: str,
                       stop_price: float, take_profit: float | None = None,
                       strategy: str = "") -> OrderResult:
        return self._open(symbol, qty, side, strategy,
                          stop_price=stop_price, take_profit=take_profit)

    def submit_trailing_stop(self, symbol: str, qty: float, side: str,
                             trail_amount: float, strategy: str = "",
                             stop_price: float | None = None,
                             activation_price: float | None = None
                             ) -> OrderResult:
        """Trailing entry. stop_price sets the initial hard stop (defaults
        to entry ∓ trail_amount, the old behavior); activation_price delays
        ratcheting until price trades through it, so entry-bar noise can't
        immediately tighten the stop into a coin flip."""
        if trail_amount <= 0:
            return OrderResult(str(next(self._ids)), "rejected",
                               reason="trail_amount must be > 0")
        stop = stop_price
        if stop is None:
            last = self._last.get(symbol)
            if last is not None:             # arm the initial stop at entry
                stop = last - trail_amount if side == "buy" \
                    else last + trail_amount
        return self._open(symbol, qty, side, strategy,
                          stop_price=stop, trail_amount=trail_amount,
                          trail_activation=activation_price)

    def _exit(self, pos: Position, ref_price: float, reason: str) -> OrderResult:
        leg = "sell" if pos.qty > 0 else "buy"
        fill = self._fill_price(ref_price, leg)
        self._cash += fill * pos.qty         # negative qty buys back
        self._cash -= self._commission
        pnl = (fill - pos.avg_entry) * pos.qty - 2 * self._commission
        oid = str(next(self._ids))
        self.fills.append({"order_id": oid, "symbol": pos.symbol, "side": leg,
                           "qty": abs(pos.qty), "price": fill,
                           "reason": reason})
        self.closed_trades.append({
            "symbol": pos.symbol, "side": pos.side, "qty": abs(pos.qty),
            "entry": pos.avg_entry, "exit": fill, "pnl": pnl,
            "reason": reason, "strategy": pos.strategy})
        del self._positions[pos.symbol]
        return OrderResult(oid, "filled", fill_price=fill, qty=abs(pos.qty))

    def close_position(self, symbol: str, reason: str = "manual") -> OrderResult:
        pos = self._positions.get(symbol)
        if pos is None:
            return OrderResult(str(next(self._ids)), "rejected",
                               reason=f"no open {symbol} position")
        return self._exit(pos, self._last[symbol], reason)

    def cancel_all(self) -> int:
        """Cancel every working protective order (stops, targets, trails)."""
        n = 0
        for pos in self._positions.values():
            for attr in ("stop_price", "take_profit", "trail_amount"):
                if getattr(pos, attr) is not None:
                    setattr(pos, attr, None)
                    n += 1
            pos.trail_activation = None      # part of the trail, not counted
        return n

    def close_all(self) -> int:
        n = 0
        for symbol in list(self._positions):
            if self.close_position(symbol, reason="flatten").status == "filled":
                n += 1
        return n


class _LiveStub:
    """Shared shape for not-yet-enabled live adapters: constructible only
    with explicit credentials, and never able to place an order."""

    name = "live-stub"
    _blocked = ("live order flow is disabled: the phased rollout "
                "(backtest -> sim -> tiny live) has not been completed")

    def __init__(self):
        raise RuntimeError(
            f"{self.name}: {self._why_unavailable()} — use SimulatedBroker")

    def _why_unavailable(self) -> str:  # pragma: no cover - overridden
        return "not configured"


class QuestradeBroker(_LiveStub):
    """Canadian live candidate. NOT IMPLEMENTED — read the caveats.

    Before this adapter can go live:
      1. ORDER ENTITLEMENT: a personal OAuth token gives account/market-data
         READ access; POST accounts/:id/orders may require Questrade
         partner-developer approval, or routing through SnapTrade. Confirm
         with Questrade before writing any order code.
      2. TOKEN ROTATION: every token refresh returns a NEW refresh token;
         persist it to .env/secret storage (never the repo) atomically.
      3. ACCOUNT GUARD: must be wired through risk_manager's registered-
         account refusal (TFSA/RRSP/etc. are never traded).
    """

    name = "questrade"

    def _why_unavailable(self) -> str:
        return ("stub adapter — Questrade order placement needs confirmed "
                "entitlement (partner-developer or SnapTrade) and a live "
                "phase approval")


class AlpacaBroker(_LiveStub):
    """Reference-only: Alpaca does not serve Canadian residents (live or
    paper) as of mid-2026. Kept as the shape of a clean REST adapter."""

    name = "alpaca"

    def _why_unavailable(self) -> str:
        return "reference-only adapter — Alpaca is unavailable in Canada"


class IBKRBroker(_LiveStub):
    """Optional Canadian alternative (ib_async). NOT IMPLEMENTED."""

    name = "ibkr"

    def _why_unavailable(self) -> str:
        return "stub adapter — IBKR support is a later, gated phase"


_ADAPTERS = {"simulated": SimulatedBroker, "questrade": QuestradeBroker,
             "alpaca": AlpacaBroker, "ibkr": IBKRBroker}


def make_broker(cfg: dict) -> Broker:
    """Build the configured broker. Anything unknown falls back to sim —
    the engine must never accidentally construct a live adapter."""
    name = str(cfg.get("broker", "simulated")).lower()
    cls = _ADAPTERS.get(name)
    if cls is None or cfg.get("mode", "sim") == "sim":
        cls = SimulatedBroker
    if cls is SimulatedBroker:
        return SimulatedBroker(
            starting_equity=float(cfg.get("starting_equity", 1000)),
            slippage_pct_per_leg=float(
                cfg.get("backtest", {}).get("slippage_pct_per_leg", 0.05))
            if isinstance(cfg.get("backtest"), dict) else 0.05,
            commission_per_trade=float(
                cfg.get("backtest", {}).get("commission_per_trade", 0.0))
            if isinstance(cfg.get("backtest"), dict) else 0.0)
    return cls()  # live stubs raise with an explanatory message
