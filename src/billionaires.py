"""Billionaire fund manager trades via SEC EDGAR 13F filings.

For each fund in config.json we pull the latest two 13F-HR filings, diff the
holdings, and emit NEW/ADD (buy) and TRIM/EXIT (sell) moves per ticker.
13Fs are quarterly snapshots filed up to 45 days after quarter end — the
freshest legal view of what these managers actually did.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from .congress import looks_like_ticker
from .util import cached_json, cached_text

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"
ARCHIVES = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}"
TICKER_REGISTRY = "https://www.sec.gov/files/company_tickers.json"

ADD_THRESHOLD = 0.20
TRIM_THRESHOLD = -0.20

_SUFFIXES = (
    " CORP", " CORPORATION", " INC", " INCORPORATED", " CO", " COMPANY",
    " LTD", " LIMITED", " PLC", " LP", " L P", " LLC", " HOLDINGS", " HLDGS",
    " GROUP", " GRP", " TRUST", " COM", " NEW", " DEL", " CL A", " CL B",
    " CLASS A", " CLASS B", " ADR", " ADS", " SA", " AG", " NV", " SE",
)


def normalize_issuer(name: str) -> str:
    s = re.sub(r"[^A-Z0-9 ]", " ", str(name).upper())
    s = re.sub(r"\s+", " ", s).strip()
    changed = True
    while changed:
        changed = False
        for suf in _SUFFIXES:
            if s.endswith(suf) and len(s) > len(suf) + 2:
                s = s[: -len(suf)].strip()
                changed = True
    return s


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _parse_infotable(xml_text: str) -> dict[str, dict]:
    """13F information table XML -> {cusip: {name, value, shares}}.
    Option positions (putCall) are skipped; duplicate CUSIPs aggregated."""
    holdings: dict[str, dict] = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return holdings
    for el in root.iter():
        if _localname(el.tag) != "infotable":
            continue
        row = {"name": None, "cusip": None, "value": 0.0, "shares": 0.0,
               "put_call": None}
        for child in el.iter():
            ln = _localname(child.tag)
            txt = (child.text or "").strip()
            if ln == "nameofissuer":
                row["name"] = txt
            elif ln == "cusip":
                row["cusip"] = txt.upper()
            elif ln == "value" and txt:
                try:
                    row["value"] += float(txt.replace(",", ""))
                except ValueError:
                    pass
            elif ln == "sshprnamt" and txt:
                try:
                    row["shares"] += float(txt.replace(",", ""))
                except ValueError:
                    pass
            elif ln == "putcall" and txt:
                row["put_call"] = txt
        if row["put_call"] or not row["cusip"]:
            continue
        if row["cusip"] in holdings:
            holdings[row["cusip"]]["shares"] += row["shares"]
            holdings[row["cusip"]]["value"] += row["value"]
        else:
            holdings[row["cusip"]] = {"name": row["name"],
                                      "shares": row["shares"],
                                      "value": row["value"]}
    return holdings


def _fetch_holdings(cik: int, accession: str) -> dict[str, dict]:
    acc = accession.replace("-", "")
    idx = cached_json(ARCHIVES.format(cik=cik, acc=acc) + "/index.json",
                      ttl_hours=24 * 30)
    names = [it.get("name", "") for it in
             idx.get("directory", {}).get("item", [])]
    xmls = [n for n in names if n.lower().endswith(".xml")
            and "primary_doc" not in n.lower()
            and not n.lower().startswith("xsl")]
    xmls.sort(key=lambda n: ("table" not in n.lower(), n))
    for name in xmls:
        url = ARCHIVES.format(cik=cik, acc=acc) + f"/{name}"
        try:
            text = cached_text(url, ttl_hours=24 * 30)
        except Exception:
            continue
        holdings = _parse_infotable(text)
        if holdings:
            return holdings
    return {}


def _latest_13f(cik: int, n: int = 2) -> list[dict]:
    sub = cached_json(SUBMISSIONS.format(cik=cik), ttl_hours=12)
    recent = sub.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accs = recent.get("accessionNumber", [])
    rpt = recent.get("reportDate", [])
    fdate = recent.get("filingDate", [])
    out = []
    for i, f in enumerate(forms):
        if f in ("13F-HR", "13F-HR/A"):
            out.append({"accession": accs[i],
                        "report_date": rpt[i] if i < len(rpt) else "",
                        "filing_date": fdate[i] if i < len(fdate) else ""})
        if len(out) >= n + 2:
            break
    seen: dict[str, dict] = {}
    for item in out:  # keep one filing per report period (amendments first)
        seen.setdefault(item["report_date"], item)
    return list(seen.values())[:n]


def load_ticker_registry() -> dict[str, str]:
    """normalized issuer title -> ticker, from SEC's registry."""
    data = cached_json(TICKER_REGISTRY, ttl_hours=24 * 7)
    by_name: dict[str, str] = {}
    rows = data.values() if isinstance(data, dict) else data
    for row in rows:
        ticker = str(row.get("ticker", "")).upper()
        title = str(row.get("title", ""))
        if ticker and title:
            by_name.setdefault(normalize_issuer(title), ticker)
    return by_name


def load_ticker_to_cik() -> dict[str, int]:
    """ticker -> SEC CIK, from the registry (used by Form 4 lookups)."""
    data = cached_json(TICKER_REGISTRY, ttl_hours=24 * 7)
    rows = data.values() if isinstance(data, dict) else data
    out: dict[str, int] = {}
    for row in rows:
        ticker = str(row.get("ticker", "")).upper()
        cik = row.get("cik_str")
        if ticker and cik is not None:
            out.setdefault(ticker, int(cik))
    return out


def classify_move(cur_shares: float | None,
                  prev_shares: float | None) -> tuple[str, str] | None:
    """Classify a holding change into (action, buy/sell). None = held steady.

    cur/prev are share counts; None means the position was absent in that 13F.
    Pure function — unit-tested in tests/test_billionaires.py.
    """
    has_cur = cur_shares is not None
    has_prev = prev_shares is not None
    cs = cur_shares or 0.0
    ps = prev_shares or 0.0
    if has_cur and not has_prev:
        return "NEW", "buy"
    if has_prev and not has_cur:
        return "EXIT", "sell"
    if ps > 0 and (cs - ps) / ps >= ADD_THRESHOLD:
        return "ADD", "buy"
    if ps > 0 and (cs - ps) / ps <= TRIM_THRESHOLD:
        return "TRIM", "sell"
    return None


def _fund_moves(fund: dict, by_name: dict[str, str]) -> list[dict]:
    """Diff one fund's latest two 13Fs into trade rows (raises on fetch error)."""
    cik = int(fund["cik"])
    manager = fund.get("manager", fund["name"])
    filings = _latest_13f(cik, n=2)
    if not filings:
        raise RuntimeError("no 13F filings")
    latest = _fetch_holdings(cik, filings[0]["accession"])
    prior = (_fetch_holdings(cik, filings[1]["accession"])
             if len(filings) > 1 else {})
    if not latest:
        raise RuntimeError("empty holdings")
    rows = []
    for cusip in set(latest) | set(prior):
        cur, old = latest.get(cusip), prior.get(cusip)
        move = classify_move((cur or {}).get("shares") if cur else None,
                             (old or {}).get("shares") if old else None)
        if move is None:
            continue
        action, kind = move
        name = (cur or old or {}).get("name", "")
        ticker = by_name.get(normalize_issuer(name))
        if not ticker or not looks_like_ticker(ticker):
            continue
        rows.append({
            "source": "billionaire", "trader": manager,
            "detail": f"{fund['name']} · {action}", "ticker": ticker,
            "type": kind,
            "amount_usd": (cur or old or {}).get("value", 0.0),
            "amount_range": action,
            "trade_date": filings[0]["report_date"],
            "disclosure_date": filings[0]["filing_date"],
        })
    return rows


def fetch_billionaire_trades(funds: list[dict],
                             max_workers: int = 5) -> tuple[list[dict], dict]:
    """Diff latest two 13Fs per fund -> trade rows (funds fetched in parallel)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    by_name = load_ticker_registry()
    rows: list[dict] = []
    ok, failed = [], []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_fund_moves, f, by_name): f for f in funds}
        for fut in as_completed(futs):
            fund = futs[fut]
            try:
                got = fut.result()
                rows.extend(got)
                ok.append(fund["name"])
            except Exception as exc:  # one fund failing shouldn't kill the run
                failed.append(f"{fund['name']} ({type(exc).__name__})")
    return rows, {"ok": ok, "failed": failed, "moves": len(rows)}
