"""Signal validation: do high convergence scores actually precede higher
forward returns?

Each scoring run snapshots (date, ticker, score) into signal_history. This
module joins those snapshots to the stock's realized forward return at several
horizons (computed from the price cache) and reports, per horizon:

  IC        — Pearson correlation between score and forward return across all
              matured (snapshot, ticker) pairs. Positive = higher scores tended
              to precede higher returns. |IC| > ~0.05 is meaningful in finance.
  hit_rate  — of the pairs in the TOP score quartile on their snapshot day,
              the fraction that had a positive forward return.

A pair only counts once it has "matured" — i.e. the price cache contains a close
at least N trading days after the snapshot. Until snapshots accrue, horizons
report n=0; the machinery is correct and the numbers fill in over time.
"""

from __future__ import annotations

import json

from . import config, db

HORIZONS = {"5d": 5, "21d": 21, "63d": 63}
OPTION_METRICS = ("pc_oi_ratio", "pc_vol_ratio", "iv_rank")


def _load_closes(ticker: str) -> list[tuple[str, float]]:
    path = config.CACHE_DIR / f"prices_{ticker.upper()}.json"
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    except Exception:
        return []
    return [(r["timestamps"][:10], float(r["close"])) for r in rows]


def _forward_return(closes: list[tuple[str, float]], snap_date: str,
                    horizon: int) -> float | None:
    """Return from the first trading day >= snap_date to `horizon` bars later."""
    idx = next((i for i, (d, _) in enumerate(closes) if d >= snap_date), None)
    if idx is None or idx + horizon >= len(closes):
        return None
    base = closes[idx][1]
    fut = closes[idx + horizon][1]
    if base <= 0:
        return None
    return fut / base - 1.0


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / (sxx ** 0.5 * syy ** 0.5)


def _options_validation(con, closes_cache: dict) -> dict:
    """IC of each OPTION_METRICS field and hit-rate of skew_label vs forward
    returns, per HORIZON. Reads options_flow (date, ticker, the metric cols).
    Mirrors run_backtest's matured-pair logic. Returns:
    {'snapshots': N, 'days': D,
     'metrics': {metric: {horizon: {'ic': float|None, 'n': int}}},
     'skew': {horizon: {'hit_rate': float|None, 'n': int}}}.
    skew hit = (bullish and ret>0) or (bearish and ret<0); neutral/null rows
    excluded from the skew denominator."""
    rows = [dict(r) for r in con.execute(
        "SELECT date, ticker, pc_oi_ratio, pc_vol_ratio, iv_rank, skew_label "
        "FROM options_flow").fetchall()]
    if not rows:
        return {"snapshots": 0, "days": 0, "metrics": {}, "skew": {}}

    days = len({r["date"] for r in rows})
    by_date: dict[str, list[dict]] = {}
    for r in rows:
        by_date.setdefault(r["date"], []).append(r)

    def _closes(ticker: str):
        if ticker not in closes_cache:
            closes_cache[ticker] = _load_closes(ticker)
        return closes_cache[ticker]

    metrics_out: dict[str, dict] = {}
    for metric in OPTION_METRICS:
        horizons_out = {}
        for label, h in HORIZONS.items():
            xs, ys = [], []
            for date, day_rows in by_date.items():
                for r in day_rows:
                    val = r.get(metric)
                    if val is None:
                        continue
                    fr = _forward_return(_closes(r["ticker"]), date, h)
                    if fr is None:
                        continue
                    xs.append(val)
                    ys.append(fr)
            ic = _pearson(xs, ys)
            horizons_out[label] = {
                "ic": round(ic, 3) if ic is not None else None, "n": len(xs)}
        metrics_out[metric] = horizons_out

    skew_out = {}
    for label, h in HORIZONS.items():
        hits, n = 0, 0
        for date, day_rows in by_date.items():
            for r in day_rows:
                sk = r.get("skew_label")
                if sk not in ("bullish", "bearish"):
                    continue
                fr = _forward_return(_closes(r["ticker"]), date, h)
                if fr is None:
                    continue
                n += 1
                if (sk == "bullish" and fr > 0) or (sk == "bearish" and fr < 0):
                    hits += 1
        skew_out[label] = {
            "hit_rate": round(hits / n, 3) if n >= 1 else None, "n": n}

    return {"snapshots": len(rows), "days": days, "metrics": metrics_out,
            "skew": skew_out}


def run_backtest() -> dict:
    con = db.connect()
    snaps = [dict(r) for r in con.execute(
        "SELECT snapshot_date, ticker, score FROM signal_history").fetchall()]
    days = len({s["snapshot_date"] for s in snaps})
    if not snaps:
        con.close()
        return {"status": "empty",
                "message": "No score snapshots yet — run `score` first."}

    closes_cache: dict[str, list] = {}
    by_date: dict[str, list[dict]] = {}
    for s in snaps:
        by_date.setdefault(s["snapshot_date"], []).append(s)

    horizons_out = {}
    total_pairs = 0
    for label, h in HORIZONS.items():
        scores, rets, top_hits, top_n = [], [], 0, 0
        for date, day_rows in by_date.items():
            matured = []
            for s in day_rows:
                t = s["ticker"]
                if t not in closes_cache:
                    closes_cache[t] = _load_closes(t)
                fr = _forward_return(closes_cache[t], date, h)
                if fr is not None:
                    matured.append((s["score"], fr))
            if not matured:
                continue
            for sc, fr in matured:
                scores.append(sc)
                rets.append(fr)
            # top score quartile of THIS day
            cutoff = sorted((m[0] for m in matured), reverse=True)[
                max(0, len(matured) // 4 - 1)] if len(matured) >= 4 else None
            if cutoff is not None:
                for sc, fr in matured:
                    if sc >= cutoff:
                        top_n += 1
                        top_hits += 1 if fr > 0 else 0
        ic = _pearson(scores, rets)
        horizons_out[label] = {
            "n": len(scores),
            "ic": round(ic, 3) if ic is not None else None,
            "hit_rate": round(top_hits / top_n, 3) if top_n else None,
            "mean_return": round(sum(rets) / len(rets), 4) if rets else None,
        }
        total_pairs += len(scores)

    options_out = _options_validation(con, closes_cache)
    con.close()

    return {"status": "ok", "snapshots": len(snaps), "days": days,
            "pairs": total_pairs, "horizons": horizons_out,
            "options": options_out}
