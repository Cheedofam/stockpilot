"""Trend following — GLD & USO, DAILY candles (§3.2 strategy 3, revised).

Track the 50- and 200-period EMAs of closes.
  Entry (long): golden cross — EMA50 crosses ABOVE EMA200 on the last bar.
  Exit:         death cross — EMA50 crosses back below EMA200 (long-or-flat
                by default; a short on the death cross needs allow_short).
  Trailing:     3×ATR14 trailing stop.

Timeframe note (2026-07 revision): the original brief said 4-hour candles,
but a 50/200 EMA cross on 4-hour bars needs years of intraday history to
produce even a handful of signals, and no free provider serves that
(yfinance caps 1-hour data at ~730 days). Daily bars are the classic
timeframe for the 50/200 golden cross and have unlimited history, so the
default is now "1Day"; config can still override.

The fast/slow periods are constructor parameters so the cross logic can be
unit-tested on short synthetic series; production instances use the 50/200
defaults from config.
"""

from __future__ import annotations

from ..indicators import ema
from .base import Signal, Strategy


class TrendFollowing(Strategy):
    name = "trend_following"

    def __init__(self, symbols: list[str] | None = None,
                 timeframe: str = "1Day", stop_multiple: float = 3,
                 allow_short: bool = False, fast: int = 50, slow: int = 200):
        super().__init__(symbols or ["GLD", "USO"], timeframe,
                         stop_multiple, allow_short)
        if fast >= slow:
            raise ValueError(f"fast EMA ({fast}) must be < slow EMA ({slow})")
        self.fast = int(fast)
        self.slow = int(slow)

    def min_bars(self) -> int:
        return self.slow + 1                # need a previous value to cross

    def evaluate(self, symbol: str, bars: list[dict],
                 position: dict | None = None) -> Signal | None:
        if len(bars) < self.min_bars():
            return None
        closes = self.closes(bars)
        e_fast = ema(closes, self.fast)
        e_slow = ema(closes, self.slow)
        f_now, f_prev = e_fast[-1], e_fast[-2]
        s_now, s_prev = e_slow[-1], e_slow[-2]
        if None in (f_now, f_prev, s_now, s_prev):
            return None

        golden = f_now > s_now and f_prev <= s_prev
        death = f_now < s_now and f_prev >= s_prev

        if position is not None:
            if position.get("side") == "long" and death:
                return Signal("exit", f"{symbol} death cross: EMA{self.fast} "
                              f"fell below EMA{self.slow}")
            if position.get("side") == "short" and golden:
                return Signal("exit", f"{symbol} golden cross: EMA{self.fast} "
                              f"rose above EMA{self.slow}")
            return None                     # trailing stop does the rest

        if golden:
            return Signal("buy", f"{symbol} golden cross: EMA{self.fast} "
                          f"crossed above EMA{self.slow}",
                          self.stop_multiple, trailing=True)
        if self.allow_short and death:
            return Signal("sell", f"{symbol} death cross: EMA{self.fast} "
                          f"crossed below EMA{self.slow}",
                          self.stop_multiple, trailing=True)
        return None
