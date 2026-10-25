"""Realized-volatility prediction clip."""

import numpy as np

from src import predictor as p


def test_caps_scale_with_volatility():
    rng = np.random.default_rng(0)
    calm = 100 * np.exp(np.cumsum(rng.normal(0, 0.005, 200)))   # low vol
    wild = 100 * np.exp(np.cumsum(rng.normal(0, 0.06, 200)))    # high vol
    calm_caps = p._realized_vol_caps(calm)
    wild_caps = p._realized_vol_caps(wild)
    for h in p.HORIZONS:
        assert wild_caps[h] > calm_caps[h]


def test_caps_ordered_by_horizon():
    caps = p._realized_vol_caps(100 * np.exp(np.cumsum(
        np.random.default_rng(1).normal(0, 0.02, 200))))
    assert caps[1] < caps[3] < caps[7]


def test_apply_caps_binds_and_flags():
    caps = {1: 5.0, 3: 8.0, 7: 12.0}
    pcts, bound = p._apply_caps({1: -40, 3: -40, 7: -40}, caps)
    assert bound is True
    assert pcts[1] == -5.0 and pcts[3] == -8.0 and pcts[7] == -12.0


def test_apply_caps_passes_through_when_within_bounds():
    caps = {1: 5.0, 3: 8.0, 7: 12.0}
    pcts, bound = p._apply_caps({1: 2.0, 3: -3.0, 7: 4.0}, caps)
    assert bound is False
    assert pcts == {1: 2.0, 3: -3.0, 7: 4.0}


def test_direction_thresholds():
    assert p._direction(5.0) == "BULLISH"
    assert p._direction(-5.0) == "BEARISH"
    assert p._direction(0.3) == "NEUTRAL"
