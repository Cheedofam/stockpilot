"""Corporate insider trades from SEC EDGAR Form 4 (the third converging source).

Form 4 must be filed within 2 business days of an insider's trade, so it is the
*freshest* disclosure signal. We count only open-market purchases (code P) and
sales (code S) — option grants, automatic awards, gifts, and derivative
positions are routine compensation noise and are excluded. This is why we go to
EDGAR directly rather than Finnhub's free feed, which omits the transaction code
and price entirely.

No API key needed — only the polite SEC_USER_AGENT header (see util.py).
"""

from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET

from .billionaires import load_ticker_to_cik
from .congress import looks_like_ticker
from .util import cached_json, cached_text

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"
ARCHIVES = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}"


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _text(el) -> str:
    return (el.text or "").strip() if el is not None else ""


def _find(el, path_locals: list[str]):
    cur = el
    for want in path_locals:
        nxt = None
        for child in cur:
            if _localname(child.tag) == want.lower():
                nxt = child
                break
        if nxt is None:
            return None
        cur = nxt
    return cur


def _parse_form4(xml_text: str) -> dict | None:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    if _localname(root.tag) != "ownershipdocument":
        return None

    owner = _find(root, ["reportingOwner", "reportingOwnerId", "rptOwnerName"])
    title = _find(root, ["reportingOwner", "reportingOwnerRelationship",
                         "officerTitle"])
    is_dir = _find(root, ["reportingOwner", "reportingOwnerRelationship",
                          "isDirector"])
    role = _text(title) or ("Director" if _text(is_dir) in ("1", "true")
                            else "Insider")

    txns = []
    for el in root.iter():
        if _localname(el.tag) != "nonderivativetransaction":
            continue
        code = _text(_find(el, ["transactionCoding", "transactionCode"])).upper()
        if code not in ("P", "S"):
            continue
        shares = _text(_find(el, ["transactionAmounts", "transactionShares",
                                  "value"]))
        price = _text(_find(el, ["transactionAmounts",
                                 "transactionPricePerShare", "value"]))
        date = _text(_find(el, ["transactionDate", "value"]))
        try:
            sh = float(shares.replace(",", "")) if shares else 0.0
            px = float(price.replace(",", "")) if price else 0.0
        except ValueError:
            continue
        txns.append({"code": code, "shares": sh, "price": px, "date": date})
    if not txns:
        return None
    return {"owner": _text(owner) or "Unknown", "role": role, "txns": txns}


def fetch_insider_trades(ticker: str, cik: int, *, window_days: int = 90,
                         max_filings: int = 12) -> list[dict]:
    """Recent open-market Form 4 buys/sells for one ticker, as trade rows."""
    cutoff = (dt.date.today() - dt.timedelta(days=window_days)).isoformat()
    try:
        sub = cached_json(SUBMISSIONS.format(cik=cik), ttl_hours=12)
    except Exception:
        return []
    recent = sub.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accs = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])

    picked = []
    for i, f in enumerate(forms):
        if f != "4":
            continue
        if i < len(dates) and dates[i] < cutoff:
            break  # submissions are reverse-chronological
        picked.append((accs[i], dates[i] if i < len(dates) else ""))
        if len(picked) >= max_filings:
            break

    rows: list[dict] = []
    for acc_dashed, filing_date in picked:
        acc = acc_dashed.replace("-", "")
        base = ARCHIVES.format(cik=cik, acc=acc)
        try:
            idx = cached_json(base + "/index.json", ttl_hours=24 * 30)
        except Exception:
            continue
        names = [it.get("name", "") for it in
                 idx.get("directory", {}).get("item", [])]
        xmls = [n for n in names if n.lower().endswith(".xml")
                and not n.lower().startswith("xsl")]
        parsed = None
        for name in xmls:
            try:
                parsed = _parse_form4(cached_text(base + f"/{name}",
                                                  ttl_hours=24 * 30))
            except Exception:
                parsed = None
            if parsed:
                break
        if not parsed:
            continue
        for t in parsed["txns"]:
            usd = t["shares"] * t["price"]
            rows.append({
                "source": "insider",
                "trader": parsed["owner"],
                "detail": parsed["role"],
                "ticker": ticker,
                "type": "buy" if t["code"] == "P" else "sell",
                "amount_usd": usd,
                "amount_range": f"{int(t['shares'])}@{t['price']:.2f}",
                "trade_date": (t["date"] or filing_date)[:10],
                "disclosure_date": filing_date[:10],
            })
    return rows


def fetch_insider_activity(tickers: list[str], *, window_days: int = 90,
                           max_workers: int = 6) -> tuple[list[dict], dict]:
    """Fetch Form 4 buys/sells for a list of tickers (parallelized)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    t2c = load_ticker_to_cik()
    pairs = [(t, t2c[t]) for t in tickers
             if looks_like_ticker(t) and t in t2c]
    rows: list[dict] = []
    ok = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(fetch_insider_trades, t, c, window_days=window_days): t
                for t, c in pairs}
        for fut in as_completed(futs):
            try:
                got = fut.result()
            except Exception:
                got = []
            if got:
                ok += 1
            rows.extend(got)
    return rows, {"tickers_checked": len(pairs), "tickers_with_activity": ok,
                  "rows": len(rows)}
