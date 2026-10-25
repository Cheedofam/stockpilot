"""The three §3.2 strategies + a config-driven factory.

Strategies stay pure (bars in, Signal out); from_config() is the one place
that maps the `bot.assets` config block onto strategy instances, so the
engine and the backtester build identical strategy sets.
"""

from __future__ import annotations

from .base import Signal, Strategy
from .mean_reversion import MeanReversion
from .momentum_breakout import MomentumBreakout
from .trend_following import TrendFollowing

STRATEGY_CLASSES: dict[str, type[Strategy]] = {
    MeanReversion.name: MeanReversion,
    MomentumBreakout.name: MomentumBreakout,
    TrendFollowing.name: TrendFollowing,
}


def from_config(bot_cfg: dict) -> list[Strategy]:
    """Build the enabled strategies from a `bot` config block.

    Each entry in bot.assets maps a strategy name to its symbols, timeframe
    and stop_multiple; unknown names are skipped (fail-safe: a typo disables
    a strategy rather than trading something unintended). An asset block
    with "enabled": false is skipped too.
    """
    out: list[Strategy] = []
    assets = bot_cfg.get("assets", {})
    if not isinstance(assets, dict):
        return out
    for name, spec in assets.items():
        cls = STRATEGY_CLASSES.get(name)
        if cls is None or not isinstance(spec, dict):
            continue
        if spec.get("enabled", True) is False:
            continue
        kwargs = {}
        if spec.get("symbols"):
            kwargs["symbols"] = list(spec["symbols"])
        if spec.get("timeframe"):
            kwargs["timeframe"] = str(spec["timeframe"])
        if spec.get("stop_multiple") is not None:
            kwargs["stop_multiple"] = float(spec["stop_multiple"])
        if spec.get("allow_short") is not None:
            kwargs["allow_short"] = bool(spec["allow_short"])
        out.append(cls(**kwargs))
    return out


__all__ = ["Signal", "Strategy", "MeanReversion", "MomentumBreakout",
           "TrendFollowing", "STRATEGY_CLASSES", "from_config"]
