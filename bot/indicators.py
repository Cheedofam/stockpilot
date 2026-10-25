"""Pure technical indicators — no I/O, no state, no broker.

Every function takes plain sequences (lists of floats, or lists of bar dicts
with high/low/close/volume) and returns a list the SAME LENGTH as the input,
padded with None until enough data exists. That alignment is what lets the
strategies index "current" (-1) and "previous" (-2) values without off-by-one
bookkeeping, and it keeps the functions trivially unit-testable against
hand-computed fixtures.

Conventions (documented so the tests are the spec):
  * stddev is the POPULATION standard deviation (ddof=0), the common TA
    convention for Bollinger-style bands.
  * ema seeds with the SMA of the first `period` values, then applies the
    standard alpha = 2/(period+1) recursion.
  * atr uses Wilder smoothing: first ATR = mean of the first `period` true
    ranges, then ATR = (prev*(period-1) + TR)/period. The first bar's TR is
    high-low (no previous close).
  * donchian returns (highest_high, lowest_low) over the window INCLUDING the
    current bar; callers wanting the classic "prior N-bar high" breakout test
    compare the current close against the channel at index -2.
"""

from __future__ import annotations

import math


def sma(values: list[float], period: int) -> list[float | None]:
    """Simple moving average."""
    _check(period)
    out: list[float | None] = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


def stddev(values: list[float], period: int) -> list[float | None]:
    """Rolling population standard deviation (ddof=0)."""
    _check(period)
    out: list[float | None] = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        mean = sum(window) / period
        out[i] = math.sqrt(sum((v - mean) ** 2 for v in window) / period)
    return out


def ema(values: list[float], period: int) -> list[float | None]:
    """Exponential moving average, SMA-seeded, alpha = 2/(period+1)."""
    _check(period)
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    alpha = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = prev + alpha * (values[i] - prev)
        out[i] = prev
    return out


def true_range(bars: list[dict]) -> list[float]:
    """Per-bar true range; first bar falls back to high-low."""
    out = []
    for i, b in enumerate(bars):
        hi, lo = float(b["high"]), float(b["low"])
        if i == 0:
            out.append(hi - lo)
        else:
            prev_close = float(bars[i - 1]["close"])
            out.append(max(hi - lo, abs(hi - prev_close),
                           abs(lo - prev_close)))
    return out


def atr(bars: list[dict], period: int = 14) -> list[float | None]:
    """Average true range, Wilder-smoothed."""
    _check(period)
    trs = true_range(bars)
    out: list[float | None] = [None] * len(bars)
    if len(bars) < period:
        return out
    prev = sum(trs[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(bars)):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def donchian(bars: list[dict],
             period: int = 20) -> list[tuple[float, float] | None]:
    """(highest high, lowest low) over the trailing window incl. current bar."""
    _check(period)
    out: list[tuple[float, float] | None] = [None] * len(bars)
    for i in range(period - 1, len(bars)):
        window = bars[i - period + 1:i + 1]
        out[i] = (max(float(b["high"]) for b in window),
                  min(float(b["low"]) for b in window))
    return out


def rolling_volume_avg(volumes: list[float],
                       period: int) -> list[float | None]:
    """Rolling mean volume — an alias of sma, named for intent at call sites."""
    return sma([float(v) for v in volumes], period)


def _check(period: int) -> None:
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
