"""Congress trade parsing + amount-range + ticker validation."""

from src import congress as c


def test_amount_midpoint():
    assert c.amount_midpoint("$1,001 - $15,000") == 8000.5
    assert c.amount_midpoint("$50,001 - $100,000") == 75000.5
    assert c.amount_midpoint("") == 0.0
    assert c.amount_midpoint(None) == 0.0


def test_looks_like_ticker():
    assert c.looks_like_ticker("AAPL")
    assert c.looks_like_ticker("BRK.B")
    assert not c.looks_like_ticker("--")
    assert not c.looks_like_ticker("")
    assert not c.looks_like_ticker("TOOLONGSYM")


def test_norm_purchase():
    rec = {"ticker": "nvda", "representative": "Jane Doe", "type": "Purchase",
           "amount": "$1,001 - $15,000", "transaction_date": "05/15/2026"}
    out = c._norm(rec, "house")
    assert out["ticker"] == "NVDA"
    assert out["type"] == "buy"
    assert out["trader"] == "Jane Doe"
    assert out["trade_date"] == "2026-05-15"   # MM/DD/YYYY -> ISO
    assert out["amount_usd"] == 8000.5


def test_norm_sale():
    rec = {"ticker": "MSFT", "senator": "John Roe", "type": "Sale (Full)",
           "amount": "$15,001 - $50,000", "transaction_date": "01/02/2026"}
    out = c._norm(rec, "senate")
    assert out["type"] == "sell"


def test_norm_rejects_bad_ticker_and_type():
    assert c._norm({"ticker": "--", "type": "Purchase"}, "house") is None
    assert c._norm({"ticker": "AAPL", "type": "Exchange"}, "house") is None
