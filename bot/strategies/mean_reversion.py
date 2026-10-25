"""Mean reversion — SPY & QQQ, 15-minute candles (§3.2 strategy 1).

Track the 20-period SMA and 20-period (population) SD of closes.
  Entry (long): close ≤ SMA − k·SD, k = 1.8 for both SPY and QQQ.
                (2026-07 revision: the brief said SPY 1.5, but the Phase-0
                gate autopsy showed SPY at 1.5 was profitable pre-cost and
                negative after 0.05%/leg slippage, while QQQ's tighter 1.8
                band cleared costs — fewer, deeper entries pay for their
                round trip. k stays configurable per symbol via entry_sd.)
  Target/exit:  close returns to the SMA.
  Hard stop:    2×ATR14 below entry (sized to 1% of equity — the engine
                places it via risk_manager; this module only declares the
                stop multiple).
  Time stop:    exit after `time_stop_bars` bars (default 26 ≈ one session)
                if neither target nor stop hit.
  Trend-day overlay: if today's cumulative volume exceeds 2× the average
                daily volume of the prior (up to) 10 sessions, PAUSE new
                entries — a high-volume trend day is exactly when fading a
                deviation gets run over. Existing positions are still
                managed. Bars need "ts" for this; without timestamps the
                overlay abstains (no pause).
  Shorts:       off by default (allow_short config).
"""

from __future__ import annotations

from ..indicators import sma, stddev
from .base import Signal, Strategy

DEFAULT_ENTRY_SD = {"SPY": 1.8, "QQQ": 1.8}


class MeanReversion(Strategy):
    name = "mean_reversion"

    def __init__(self, symbols: list[str] | None = None,
                 timeframe: str = "15Min", stop_multiple: float = 2,
                 allow_short: bool = False, period: int = 20,
                 entry_sd: dict[str, float] | None = None,
                 default_entry_sd: float = 1.5, time_stop_bars: int = 26,
                 trend_day_volume_mult: float = 2.0,
                 trend_day_lookback_days: int = 10):
        super().__init__(symbols or ["SPY", "QQQ"], timeframe,
                         stop_multiple, allow_short)
        self.period = int(period)
        self.entry_sd = dict(DEFAULT_ENTRY_SD if entry_sd is None else entry_sd)
        self.default_entry_sd = float(default_entry_sd)
        self.time_stop_bars = int(time_stop_bars)
        self.trend_day_volume_mult = float(trend_day_volume_mult)
        self.trend_day_lookback_days = int(trend_day_lookback_days)

    def min_bars(self) -> int:
        return self.period

    def evaluate(self, symbol: str, bars: list[dict],
                 position: dict | None = None) -> Signal | None:
        if len(bars) < self.min_bars():
            return None
        closes = self.closes(bars)
        mean = sma(closes, self.period)[-1]
        sd = stddev(closes, self.period)[-1]
        close = closes[-1]

        if position is not None:
            # Manage the open trade first: target, then time stop. (The hard
            # stop is a broker-side bracket order, not our job here.)
            if position.get("side") == "long" and close >= mean:
                return Signal("exit", f"{symbol} reverted to the "
                              f"{self.period}-bar SMA ({mean:.2f})")
            if position.get("side") == "short" and close <= mean:
                return Signal("exit", f"{symbol} reverted to the "
                              f"{self.period}-bar SMA ({mean:.2f})")
            if position.get("bars_held", 0) >= self.time_stop_bars:
                return Signal("exit", f"time stop: {self.time_stop_bars} "
                              "bars without target or stop")
            return None

        if self._trend_day_pause(bars):
            return None                     # manage-only day, no new entries

        k = float(self.entry_sd.get(symbol, self.default_entry_sd))
        band = k * sd
        if close <= mean - band:
            return Signal("buy", f"{symbol} {close:.2f} ≤ SMA−{k}·SD "
                          f"({mean - band:.2f})", self.stop_multiple)
        if self.allow_short and close >= mean + band:
            return Signal("sell", f"{symbol} {close:.2f} ≥ SMA+{k}·SD "
                          f"({mean + band:.2f})", self.stop_multiple)
        return None

    def _trend_day_pause(self, bars: list[dict]) -> bool:
        """True when today's cumulative volume marks a likely trend day."""
        by_day: dict[str, float] = {}
        order: list[str] = []
        for b in bars:
            ts = str(b.get("ts", ""))
            day = ts.split("T")[0] if ts else ""
            if not day:
                return False                # no timestamps: overlay abstains
            if day not in by_day:
                by_day[day] = 0.0
                order.append(day)
            by_day[day] += float(b.get("volume", 0))
        if len(order) < 2:
            return False                    # nothing to compare against
        today = order[-1]
        prior = order[-1 - self.trend_day_lookback_days:-1]
        avg_daily = sum(by_day[d] for d in prior) / len(prior)
        if avg_daily <= 0:
            return False
        return by_day[today] > self.trend_day_volume_mult * avg_daily
