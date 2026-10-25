"""Price prediction: Kronos foundation model with silent sklearn fallback.

If config.kronos_path exists and `from model import Kronos...` works, we load
Kronos-small from HuggingFace and sample 3 paths 7 trading days ahead.
Otherwise a Ridge regression per horizon, trained on the ticker's own
history, fills in — labeled 'sklearn' so the UI can show which model ran.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from . import config, db, prices

HORIZONS = (1, 3, 7)

# Each horizon's move is sanity-capped at a multiple of the stock's OWN recent
# realized volatility, not a flat number. A sleepy utility and a parabolic
# semiconductor get very different caps, and when the model's raw output blows
# past even this generous (3-sigma) bound — which is what produced the old
# "every chip stock is -20%" artifact — we clip it AND mark the prediction
# low-confidence instead of showing a confident crash.
_VOL_SIGMAS = 3.0           # cap at a 3-sigma move over the horizon


def _realized_vol_caps(close: np.ndarray) -> dict[int, float]:
    """Per-horizon ± clip in percent, from trailing daily log-return vol."""
    series = close[-63:] if len(close) > 63 else close          # ~3 months
    if len(series) > 5:
        sigma = float(np.std(np.diff(np.log(series))))
    else:
        sigma = 0.02
    caps = {}
    for h in HORIZONS:
        cap = _VOL_SIGMAS * sigma * (h ** 0.5) * 100            # %, h-day
        # Floor and ceiling both scale with sqrt(h) so the three rows stay
        # ordered and a single number can't dominate.
        caps[h] = float(np.clip(cap, 1.5 * (h ** 0.5), 8.0 * (h ** 0.5)))
    return caps


def _apply_caps(raw_pct: dict[int, float], caps: dict[int, float]):
    """Returns (clipped pcts, did_any_clip_bind)."""
    clipped, bound = {}, False
    for h in HORIZONS:
        c = float(np.clip(raw_pct[h], -caps[h], caps[h]))
        if abs(raw_pct[h]) > caps[h] + 1e-6:
            bound = True
        clipped[h] = c
    return clipped, bound

_kronos_predictor = None
_kronos_checked = False


def kronos_available() -> bool:
    global _kronos_checked, _kronos_predictor
    if _kronos_checked:
        return _kronos_predictor is not None
    _kronos_checked = True
    kpath = Path(config.get("kronos_path", ""))
    if not kpath.exists():
        return False
    if str(kpath) not in sys.path:
        sys.path.insert(0, str(kpath))
    try:
        import torch
        from model import Kronos, KronosTokenizer, KronosPredictor
        tokenizer = KronosTokenizer.from_pretrained(
            "NeoQuasar/Kronos-Tokenizer-base")
        model = Kronos.from_pretrained("NeoQuasar/Kronos-small")
        device = "cuda:0" if torch.cuda.is_available() else "cpu"
        _kronos_predictor = KronosPredictor(model, tokenizer, device=device,
                                            max_context=512)
        print(f"  Kronos-small loaded ({device})")
    except Exception as exc:
        print(f"  Kronos unavailable ({type(exc).__name__}) — using sklearn")
        _kronos_predictor = None
    return _kronos_predictor is not None


def _direction(pct_7d: float) -> str:
    if pct_7d >= 1.0:
        return "BULLISH"
    if pct_7d <= -1.0:
        return "BEARISH"
    return "NEUTRAL"


def _predict_kronos(df) -> dict | None:
    """5 sampled paths, 7 steps ahead, median-aggregated.

    Median + low temperature keeps one tail-event sample from dragging the
    forecast to absurd values; pcts are clipped to a plausible 7-day band.
    """
    import pandas as pd

    pred_len = max(HORIZONS)
    x_df = df.tail(min(512, len(df))).reset_index(drop=True)
    feature_cols = [c for c in ("open", "high", "low", "close", "volume")
                    if c in x_df.columns]
    last_ts = x_df["timestamps"].iloc[-1]
    y_ts = pd.Series(pd.bdate_range(start=last_ts + pd.Timedelta(days=1),
                                    periods=pred_len))
    closes = []
    for _ in range(5):
        pred = _kronos_predictor.predict(
            df=x_df[feature_cols], x_timestamp=x_df["timestamps"],
            y_timestamp=y_ts, pred_len=pred_len, T=0.7, top_p=0.85,
            sample_count=1, verbose=False)
        closes.append(pred["close"].to_numpy())
    paths = np.stack(closes)                      # (5, pred_len)
    med_path = np.median(paths, axis=0)
    last = float(df["close"].iloc[-1])

    caps = _realized_vol_caps(df["close"].to_numpy(dtype=float))
    raw = {h: (med_path[h - 1] / last - 1) * 100 for h in HORIZONS}
    pcts, clip_bound = _apply_caps(raw, caps)
    # Agreement: small relative IQR across paths at the 7d point → high conf.
    spread = float((np.quantile(paths[:, -1], 0.75)
                    - np.quantile(paths[:, -1], 0.25)) / last)
    confidence = float(np.clip(0.9 - spread * 6, 0.3, 0.9))
    if clip_bound:
        confidence = min(confidence, 0.4)         # model exceeded 3-sigma
    return {"model": "Kronos-small", "last_price": round(last, 2),
            "confidence": round(confidence, 2),
            **{f"pct_{h}d": round(pcts[h], 2) for h in HORIZONS},
            **{f"target_{h}d": round(last * (1 + pcts[h] / 100), 2)
               for h in HORIZONS}}


def _predict_sklearn(df) -> dict | None:
    """Ridge regression per horizon on simple momentum features."""
    from sklearn.linear_model import Ridge

    close = df["close"].to_numpy(dtype=float)
    if len(close) < 120:
        return None

    def feats(i: int) -> list[float]:
        c = close[: i + 1]
        r = lambda n: c[-1] / c[-n - 1] - 1 if len(c) > n else 0.0
        delta = np.diff(c[-15:])
        gain = delta[delta > 0].sum()
        loss = -delta[delta < 0].sum()
        rsi = 100 - 100 / (1 + gain / loss) if loss > 0 else 70.0
        return [r(1), r(5), r(10), r(21), rsi / 100,
                float(np.std(np.diff(np.log(c[-22:]))))]

    start = 60
    idxs = list(range(start, len(close) - max(HORIZONS)))
    X = np.array([feats(i) for i in idxs])
    last = float(close[-1])
    caps = _realized_vol_caps(close)
    out = {"model": "sklearn", "last_price": round(last, 2)}
    raw, resid_ratios = {}, []
    x_now = np.array([feats(len(close) - 1)])
    for h in HORIZONS:
        y = np.array([close[i + h] / close[i] - 1 for i in idxs])
        m = Ridge(alpha=1.0).fit(X, y)
        raw[h] = float(m.predict(x_now)[0]) * 100
        resid = float(np.std(y - m.predict(X)))
        resid_ratios.append(abs(raw[h] / 100) / resid if resid > 0 else 0.0)
    pcts, clip_bound = _apply_caps(raw, caps)
    for h in HORIZONS:
        out[f"pct_{h}d"] = round(pcts[h], 2)
        out[f"target_{h}d"] = round(last * (1 + pcts[h] / 100), 2)
    # Signal-to-noise of the 7d prediction → modest confidence.
    conf = 0.35 + 0.3 * resid_ratios[-1]
    if clip_bound:
        conf = min(conf, 0.35)
    out["confidence"] = round(float(np.clip(conf, 0.3, 0.75)), 2)
    return out


def predict_ticker(ticker: str) -> dict | None:
    df = prices.fetch_history(ticker)
    if df is None:
        return None
    result = None
    if kronos_available():
        try:
            result = _predict_kronos(df)
        except Exception as exc:
            print(f"  {ticker}: Kronos failed ({type(exc).__name__}), "
                  "falling back to sklearn")
    if result is None:
        result = _predict_sklearn(df)
    if result is None:
        return None
    result["ticker"] = ticker
    result["direction"] = _direction(result["pct_7d"])
    return result


def run_predictions(tickers: list[str]) -> list[dict]:
    con = db.connect()
    results = []
    for i, t in enumerate(tickers, 1):
        p = predict_ticker(t)
        if p:
            db.replace_prediction(con, p)
            results.append(p)
            print(f"  [{i}/{len(tickers)}] {t}: {p['direction']} "
                  f"{p['pct_7d']:+.1f}% 7d ({p['model']})")
        else:
            print(f"  [{i}/{len(tickers)}] {t}: no price data")
    con.commit()
    con.close()
    return results
