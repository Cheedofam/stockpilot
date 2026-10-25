"""Keyword sentiment fallback (the offline path)."""

from src import news_sentiment as ns


def test_positive_headline():
    assert ns.keyword_sentiment("Company beats estimates, shares surge") > 0


def test_negative_headline():
    assert ns.keyword_sentiment("Company misses, faces lawsuit and layoffs") < 0


def test_neutral_or_unknown_is_zero():
    assert ns.keyword_sentiment("Company to present at conference") == 0.0
    assert ns.keyword_sentiment("") == 0.0


def test_bounded_range():
    s = ns.keyword_sentiment("surge beats record gains rally upgrade strong")
    assert -1.0 <= s <= 1.0
