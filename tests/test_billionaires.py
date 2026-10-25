"""13F holdings diff -> NEW/ADD/TRIM/EXIT classification + XML parsing."""

from src import billionaires as b


def test_new_position():
    assert b.classify_move(1000, None) == ("NEW", "buy")


def test_exit_position():
    assert b.classify_move(None, 1000) == ("EXIT", "sell")


def test_add_above_threshold():
    # +25% > 20% ADD threshold
    assert b.classify_move(1250, 1000) == ("ADD", "buy")


def test_trim_below_threshold():
    # -25% < -20% TRIM threshold
    assert b.classify_move(750, 1000) == ("TRIM", "sell")


def test_held_steady_is_none():
    assert b.classify_move(1050, 1000) is None       # +5%, inside band
    assert b.classify_move(900, 1000) is None        # -10%, inside band


def test_normalize_issuer_strips_suffixes():
    assert b.normalize_issuer("NVIDIA CORP") == "NVIDIA"
    assert b.normalize_issuer("Apple Inc.") == "APPLE"
    assert b.normalize_issuer("Alphabet Inc. Class A") == "ALPHABET"


def test_parse_infotable_skips_options_and_aggregates():
    xml = """<?xml version="1.0"?>
    <informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
      <infoTable>
        <nameOfIssuer>NVIDIA CORP</nameOfIssuer>
        <cusip>67066G104</cusip>
        <value>1000000</value>
        <shrsOrPrnAmt><sshPrnamt>5000</sshPrnamt></shrsOrPrnAmt>
      </infoTable>
      <infoTable>
        <nameOfIssuer>NVIDIA CORP</nameOfIssuer>
        <cusip>67066G104</cusip>
        <value>500000</value>
        <shrsOrPrnAmt><sshPrnamt>2000</sshPrnamt></shrsOrPrnAmt>
      </infoTable>
      <infoTable>
        <nameOfIssuer>SOME OPTION</nameOfIssuer>
        <cusip>11111X999</cusip>
        <value>9999</value>
        <shrsOrPrnAmt><sshPrnamt>100</sshPrnamt></shrsOrPrnAmt>
        <putCall>Put</putCall>
      </infoTable>
    </informationTable>"""
    holdings = b._parse_infotable(xml)
    assert "67066G104" in holdings
    # two rows for the same CUSIP aggregate
    assert holdings["67066G104"]["shares"] == 7000
    # option position skipped
    assert "11111X999" not in holdings
