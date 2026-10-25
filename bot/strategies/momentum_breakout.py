"""Momentum breakout — BTC/USD, 1-hour candles (§3.2 strategy 2).

Track the 20-period Donchian channel.
  Entry (long): close breaks above the PRIOR 20-period high AND the bar's
                volume ≥ 1.5× the 20-period average volume (measured up to
                the prior bar, so the breakout bar can't inflate its own
                confirmation).
  Downside:     close below the prior 20-period low liquidates longs
                (and may open a short when allow_short).
  Trailing:     2×ATR14 trailing stop, ratcheted by the broker.

24/7 asset: the engine loop, not the strategy, deals with continuous
sessions. If the live broker can't trade BTC, disable this strategy in
config rather than mapping it onto something else.
"""

from __future__ import annotations

from ..indicators import donchian, rolling_volume_avg
from .base import Signal, Strategy


class MomentumBreakout(Strategy):
    name = "momentum_breakout"

    def __init__(self, symbols: list[str] | None = None,
                 timeframe: str = "1Hour", stop_multiple: float = 2,
                 allow_short: bool = False, period: int = 20,
                 volume_mult: float = 1.5):
        super().__init__(symbols or ["BTC/USD"], timeframe,
                         stop_multiple, allow_short)
        self.period = int(period)
        self.volume_mult = float(volume_mult)

    def min_bars(self) -> int:
        return self.period + 1              # prior channel + breakout bar

    def evaluate(self, symbol: str, bars: list[dict],
                 position: dict | None = None) -> Signal | None:
        if len(bars) < self.min_bars():
            return None
        channel = donchian(bars, self.period)
        prior_high, prior_low = channel[-2]  # channel EXCLUDING current bar
        close = float(bars[-1]["close"])

        if position is not None:
            if position.get("side") == "long" and close < prior_low:
                return Signal("exit", f"{symbol} broke below the prior "
                              f"{self.period}-bar low ({prior_low:.2f})")
            if position.get("side") == "short" and close > prior_high:
                return Signal("exit", f"{symbol} broke above the prior "
                              f"{self.period}-bar high ({prior_high:.2f})")
            return None                     # trailing stop does the rest

        vol_avg = rolling_volume_avg(self.volumes(bars), self.period)[-2]
        volume_ok = vol_avg is not None and vol_avg > 0 and \
            float(bars[-1].get("volume", 0)) >= self.volume_mult * vol_avg

        if close > prior_high and volume_ok:
            return Signal("buy", f"{symbol} {close:.2f} broke the "
                          f"{self.period}-bar high ({prior_high:.2f}) on "
                          f"≥{self.volume_mult}× volume",
                          self.stop_multiple, trailing=True)
        if self.allow_short and close < prior_low and volume_ok:
            return Signal("sell", f"{symbol} {close:.2f} broke the "
                          f"{self.period}-bar low ({prior_low:.2f}) on "
                          f"≥{self.volume_mult}× volume",
                          self.stop_multiple, trailing=True)
        return None
