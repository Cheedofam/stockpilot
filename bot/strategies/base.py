"""Strategy contract: bars in, Signal out, nothing else.

Strategies are PURE: no broker, no network, no clock, no mutable global
state. The engine owns execution — a strategy only looks at the bars (and,
optionally, a small dict describing its currently open position) and says
what it would do. That purity is what makes every strategy unit-testable
with synthetic bar series.

Bars are dicts with open/high/low/close/volume and optionally "ts" (ISO
timestamp), matching what SimulatedBroker.get_bars serves.

The `position` argument, when a position is open, is a plain dict:
    {"side": "long"|"short", "entry": float, "bars_held": int}
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class Signal:
    """What a strategy wants: an entry ('buy'/'sell') or an 'exit'.

    stop_multiple × ATR14 defines the hard-stop distance (and, via
    risk_manager.size_position, the position size). trailing=True asks the
    engine for a trailing stop instead of a fixed bracket.
    """

    side: str                      # "buy" | "sell" | "exit"
    reason: str
    stop_multiple: float = 0.0
    trailing: bool = False


class Strategy(ABC):
    """Base for all strategies. Subclasses set `name` and implement
    evaluate(); they must not perform any I/O."""

    name: str = "base"

    def __init__(self, symbols: list[str], timeframe: str,
                 stop_multiple: float, allow_short: bool = False):
        self.symbols = list(symbols)
        self.timeframe = timeframe
        self.stop_multiple = float(stop_multiple)
        self.allow_short = bool(allow_short)

    @abstractmethod
    def evaluate(self, symbol: str, bars: list[dict],
                 position: dict | None = None) -> Signal | None:
        """Inspect the bars; return a Signal or None (do nothing)."""

    @abstractmethod
    def min_bars(self) -> int:
        """Bars required before evaluate() can produce a signal."""

    # Shared helpers -------------------------------------------------------
    @staticmethod
    def closes(bars: list[dict]) -> list[float]:
        return [float(b["close"]) for b in bars]

    @staticmethod
    def volumes(bars: list[dict]) -> list[float]:
        return [float(b.get("volume", 0)) for b in bars]
