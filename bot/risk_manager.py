"""Risk management — the code that is allowed to say NO.

Four independent guards, in the order the engine consults them:

  1. Account guard (Canada/CRA): the bot trades a NON-REGISTERED account
     only. Active trading inside a TFSA (or any registered account) risks
     CRA business-income reclassification with retroactive penalties, so
     assert_non_registered() hard-refuses registered account types AND
     fails closed on anything it cannot positively identify.
  2. Position sizing (the §3.1 convention): the hard stop sits at
     StopMultiple × ATR14 from entry, and the position is sized so that
     being stopped there loses exactly risk_pct (default 1%) of equity:
         units = (equity × risk_pct/100) / (stop_multiple × atr)
     A 1-ATR adverse move therefore costs risk_pct / stop_multiple.
  3. Correlation filter: data-driven rules blocking a new entry when a set
     of correlated longs is already open (default: SPY+QQQ longs block new
     BTC/USD longs).
  4. Circuit breaker: >10% drawdown from peak historic equity → flatten,
     cancel, HALT. The HALTED flag and the peak-equity watermark persist to
     disk so a restart cannot bypass the halt; only an explicit manual
     reset (CLI --reset or the dashboard admin endpoint) resumes trading.

Everything here is deterministic and broker-free except CircuitBreaker.trip,
which takes the broker as an argument — so all of it unit-tests hermetically.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from src import config


class BotSafetyError(RuntimeError):
    """A hard safety refusal — never catch-and-continue past one of these."""


# ---------------------------------------------------------------------------
# 1. Account guard (TFSA / registered accounts are never traded)
# ---------------------------------------------------------------------------

REGISTERED_TYPES = frozenset({
    "tfsa", "rrsp", "srrsp", "rrif", "resp", "fhsa", "lira", "lrsp", "lif",
    "rdsp", "dpsp", "registered"})
ALLOWED_TYPES = frozenset({"margin", "cash", "non-registered",
                           "nonregistered", "non_registered"})


def assert_non_registered(account_id: str, account_type: str,
                          registered_ids: set[str] | frozenset[str] = frozenset()
                          ) -> None:
    """Refuse to arm the bot against anything but a non-registered account.

    Fail-closed: an unknown or empty account_type raises too — the operator
    must positively declare "margin" or "cash" in config, not leave it blank.
    """
    atype = (account_type or "").strip().lower()
    if atype in REGISTERED_TYPES:
        raise BotSafetyError(
            f"account {account_id or '?'} is a REGISTERED ({atype.upper()}) "
            "account — the bot never trades registered accounts (CRA "
            "business-income risk). Use a non-registered margin/cash account.")
    if atype not in ALLOWED_TYPES:
        raise BotSafetyError(
            f"account type {account_type!r} is not positively non-registered "
            f"— set bot.account_type to one of {sorted(ALLOWED_TYPES)}.")
    if account_id and account_id in registered_ids:
        raise BotSafetyError(
            f"account id {account_id} is on the known-registered list — "
            "refusing to trade it regardless of its declared type.")


# ---------------------------------------------------------------------------
# 2. Position sizing (§3.1: full stop = exactly risk_pct of equity)
# ---------------------------------------------------------------------------

def size_position(equity: float, atr: float, stop_multiple: float,
                  risk_pct: float = 1.0) -> float:
    """Units such that a stop-out at stop_multiple×ATR loses risk_pct% equity.

    The invariant (asserted in tests):
        units * stop_multiple * atr == equity * risk_pct / 100
    """
    if equity <= 0:
        raise ValueError(f"equity must be > 0, got {equity}")
    if atr <= 0:
        raise ValueError(f"atr must be > 0, got {atr}")
    if stop_multiple <= 0:
        raise ValueError(f"stop_multiple must be > 0, got {stop_multiple}")
    if not 0 < risk_pct <= 5:
        raise ValueError(f"risk_pct out of sane range (0, 5]: {risk_pct}")
    return (equity * risk_pct / 100.0) / (stop_multiple * atr)


def hard_stop_price(entry: float, atr: float, stop_multiple: float,
                    side: str) -> float:
    """The protective stop at StopMultiple×ATR from entry, side-aware."""
    dist = stop_multiple * atr
    if side == "buy":
        return entry - dist
    if side == "sell":
        return entry + dist
    raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")


def cap_units(units: float, price: float, equity: float,
              max_position_pct: float = 25.0, max_gross_pct: float = 100.0,
              gross_exposure: float = 0.0,
              buying_power: float | None = None,
              fractional: bool = True) -> float:
    """Shrink (never grow) a sized position to respect the exposure caps.

    Caps applied, tightest wins: single-position notional ≤ max_position_pct%
    of equity; total gross exposure ≤ max_gross_pct% of equity; notional ≤
    available buying power. Non-fractional instruments floor to whole units.
    Returns 0.0 when no compliant size exists.
    """
    if price <= 0:
        raise ValueError(f"price must be > 0, got {price}")
    capped = min(
        units,
        (equity * max_position_pct / 100.0) / price,
        max(equity * max_gross_pct / 100.0 - gross_exposure, 0.0) / price)
    if buying_power is not None:
        capped = min(capped, buying_power / price)
    if not fractional:
        capped = float(int(capped))
    return max(capped, 0.0)


# ---------------------------------------------------------------------------
# 3. Correlation filter (data-driven)
# ---------------------------------------------------------------------------

# Rule: when ALL of `if_long_open` are held long, block new longs in `block`.
DEFAULT_CORRELATION_RULES: list[dict] = [
    {"if_long_open": ["SPY", "QQQ"], "block": ["BTC/USD"]},
]


def correlation_block(open_long_symbols: set[str] | list[str],
                      candidate: str,
                      rules: list[dict] | None = None) -> str | None:
    """Reason string if `candidate` is blocked by an active rule, else None."""
    held = set(open_long_symbols)
    for rule in (DEFAULT_CORRELATION_RULES if rules is None else rules):
        trigger = set(rule.get("if_long_open", []))
        if candidate in rule.get("block", []) and trigger and \
                trigger.issubset(held):
            return (f"correlation filter: {candidate} blocked while long "
                    f"{' + '.join(sorted(trigger))}")
    return None


# ---------------------------------------------------------------------------
# 4. Circuit breaker (persistent, manual-reset-only)
# ---------------------------------------------------------------------------

DEFAULT_STATE_PATH = config.DATA_DIR / "bot" / "state.json"


class CircuitBreaker:
    """Systemic kill switch: >max_dd_pct drawdown from peak equity halts all.

    State (halted flag, peak watermark, trip reason) lives in a JSON file so
    a process restart resumes exactly as halted/armed as it left off. There
    is deliberately NO automatic reset path.
    """

    def __init__(self, max_dd_pct: float = 10.0,
                 state_path: str | Path | None = None):
        if not 0 < max_dd_pct < 100:
            raise ValueError(f"max_dd_pct out of range (0, 100): {max_dd_pct}")
        self.max_dd_pct = float(max_dd_pct)
        self.state_path = Path(state_path) if state_path else DEFAULT_STATE_PATH
        state = self._load()
        self.halted: bool = bool(state.get("halted", False))
        self.peak_equity: float = float(state.get("peak_equity", 0.0))
        self.trip_reason: str = state.get("trip_reason", "")

    # -- persistence --------------------------------------------------------
    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"halted": self.halted, "peak_equity": self.peak_equity,
                   "trip_reason": self.trip_reason,
                   "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        tmp.replace(self.state_path)      # atomic-ish: never a torn file

    # -- the breaker --------------------------------------------------------
    def drawdown_pct(self, equity: float) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - equity) / self.peak_equity * 100)

    def check(self, equity: float) -> bool:
        """Would this equity trip the breaker? (No side effects.)"""
        return self.drawdown_pct(equity) > self.max_dd_pct

    def update(self, equity: float, broker=None) -> bool:
        """New equity mark: raise the watermark or trip. Returns halted.

        The watermark only ever rises. If drawdown exceeds the limit and a
        broker is provided, trip() flattens and cancels everything.

        Also honors an EXTERNALLY persisted halt (the dashboard kill switch
        writes HALTED to the same state file from another process): on first
        detection the engine flattens + cancels once, then stays halted.
        """
        if self.halted:
            return True
        state = self._load()
        if state.get("halted"):
            self.halted = True
            self.trip_reason = state.get("trip_reason", "external halt")
            if broker is not None:
                broker.cancel_all()
                broker.close_all()
            return True
        if equity > self.peak_equity:
            self.peak_equity = equity
            self._save()
            return False
        if self.check(equity):
            self.trip(broker, equity)
            return True
        return False

    def trip(self, broker=None, equity: float | None = None,
             reason: str | None = None) -> None:
        """Halt + persist, THEN flatten + cancel. Manual reset required.

        The halt is written to disk before any broker call: the exact moment
        a breaker trips is when a broker API is likeliest to be misbehaving,
        and an exception mid-flatten must never leave the on-disk state
        un-halted (a restart would silently resume trading past the limit).
        """
        self.halted = True
        if reason is not None:
            self.trip_reason = reason
        else:
            dd = f"{self.drawdown_pct(equity):.1f}%" \
                if equity is not None else "?"
            self.trip_reason = (f"drawdown {dd} from peak "
                                f"{self.peak_equity:.2f} exceeded "
                                f"{self.max_dd_pct:.0f}% limit")
        self._save()
        if broker is not None:
            # Cancel working orders first so nothing re-arms during the
            # flatten, then close every position at market. Any exception
            # propagates (fail-closed) — but the halt is already on disk.
            broker.cancel_all()
            broker.close_all()

    def reset(self, current_equity: float) -> None:
        """Explicit manual reset: clear the halt and re-anchor the watermark.

        Re-anchoring matters — keeping the old peak would re-trip on the very
        next update. Only the CLI (--reset) and the admin endpoint call this.
        """
        self.halted = False
        self.trip_reason = ""
        self.peak_equity = float(current_equity)
        self._save()
