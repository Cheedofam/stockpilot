"""Convergence scoring math — the heart of the product."""

import datetime as dt

from src import scoring


def _trade(source, trader, ticker="AAA", type="buy", amount=1_000_000,
           days_ago=1):
    d = (dt.date.today() - dt.timedelta(days=days_ago)).isoformat()
    return {"source": source, "trader": trader, "ticker": ticker,
            "type": type, "amount_usd": amount, "trade_date": d,
            "disclosure_date": d}


def test_no_buyers_scores_low():
    s = scoring.score_ticker([_trade("congress", "X", type="sell")])
    assert s["buyers"] == 0
    assert s["sells"] == 1
    assert s["score"] <= 10


def test_single_congress_buyer():
    s = scoring.score_ticker([_trade("congress", "Sen. A")])
    assert s["congress_buyers"] == 1
    assert s["billionaire_buyers"] == 0
    assert s["buyers"] == 1
    assert 0 < s["score"] <= 100


def test_convergence_bonus_scales_with_distinct_types():
    """Three types converging should beat one type, all else equal."""
    one = scoring.score_ticker([
        _trade("congress", "A"), _trade("congress", "B")])
    three = scoring.score_ticker([
        _trade("congress", "A"), _trade("billionaire", "Buffett"),
        _trade("insider", "CEO")])
    assert three["score"] > one["score"]
    assert three["congress_buyers"] == 1
    assert three["billionaire_buyers"] == 1
    assert three["insider_buyers"] == 1
    assert three["buyers"] == 3


def test_distinct_buyers_deduped_by_name():
    s = scoring.score_ticker([
        _trade("congress", "Sen. A"), _trade("congress", "Sen. A")])
    assert s["congress_buyers"] == 1


def test_recency_decay_lowers_old_trades():
    fresh = scoring.score_ticker([_trade("billionaire", "F", days_ago=1)])
    stale = scoring.score_ticker([_trade("billionaire", "F", days_ago=300)])
    assert fresh["score"] > stale["score"]


def test_sellers_reduce_score():
    buys_only = scoring.score_ticker([
        _trade("congress", "A"), _trade("billionaire", "B")])
    with_sellers = scoring.score_ticker([
        _trade("congress", "A"), _trade("billionaire", "B"),
        _trade("insider", "S1", type="sell"),
        _trade("insider", "S2", type="sell")])
    assert with_sellers["score"] < buys_only["score"]
    assert with_sellers["sells"] == 2


def test_score_clamped_to_100():
    many = [_trade(src, f"{src}{i}", amount=50_000_000)
            for src in ("congress", "billionaire", "insider")
            for i in range(5)]
    s = scoring.score_ticker(many)
    assert s["score"] <= 100.0
