"""Backtester — replay months of bars through the SimulatedBroker.

This is THE GATE: `live` mode must refuse to start unless a recent PASSING
result exists on disk (data/bot/backtest_result.json). The gate thresholds
come from config `bot.backtest` (min_profit_factor, max_drawdown_pct,
min_trades).

Structure:
  replay_pair()          one (strategy, symbol) pair over a bar list — the
                         hermetic core, no network. Slippage (0.05%/leg by
                         default) and commission are modeled by the
                         SimulatedBroker itself.
  run_backtest_offline() replays every configured pair from a caller-
                         provided {symbol: bars} dict and aggregates
                         metrics + the PASS/FAIL verdict. Also hermetic.
  run_backtest()         the real thing: fetches ~6 months of bars per
                         timeframe via yfinance (network!), then calls
                         run_backtest_offline and persists the result.

Aggregation is deliberately conservative: each pair runs on its own equity
sleeve; the overall max drawdown is the WORST pair's, profit factor / win
rate come from the combined trade list, and CAGR/Sharpe are sleeve
averages. The cross-pair correlation filter is an engine-level control
(bot/main) and is not modeled here.

Data-source honesty: yfinance caps 15-minute history at ~60 days, so the
mean-reversion sleeve backtests on a shorter window than the 1-hour/4-hour
sleeves; the report records the actual window per pair.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

from src import config

from .broker import SimulatedBroker
from .indicators import atr
from .risk_manager import cap_units, hard_stop_price, size_position
from .strategies import Strategy, from_config

DEFAULT_RESULT_PATH = config.DATA_DIR / "bot" / "backtest_result.json"

# Rough bars-per-year, per timeframe, for Sharpe annualization and CAGR.
BARS_PER_YEAR = {"15Min": 26 * 252, "1Hour": 24 * 365, "4Hour": 2 * 252,
                 "1Day": 252}
DEFAULT_GATE = {"min_profit_factor": 1.0, "max_drawdown_pct": 15.0,
                "min_trades": 20, "min_trades_per_sleeve": 10,
                "walk_forward": True, "validation_fraction": 1 / 3}


# ---------------------------------------------------------------------------
# Hermetic core
# ---------------------------------------------------------------------------

def replay_pair(strategy: Strategy, symbol: str, bars: list[dict],
                starting_equity: float = 1000.0,
                slippage_pct_per_leg: float = 0.05,
                commission_per_trade: float = 0.0,
                risk_pct: float = 1.0, max_position_pct: float = 25.0,
                max_gross_pct: float = 100.0, warmup: int = 0) -> dict:
    """Replay one (strategy, symbol) pair. Returns trades + equity curve.

    The first `warmup` bars are history-only: they warm the indicators
    (EMA-200 etc.) but never trade and never count in the metrics — so a
    slow strategy's warm-up doesn't silently eat the evaluation window.
    """
    broker = SimulatedBroker(starting_equity=starting_equity,
                             slippage_pct_per_leg=slippage_pct_per_leg,
                             commission_per_trade=commission_per_trade)
    warmup = max(0, min(int(warmup), len(bars)))
    lookback = max(strategy.min_bars() + 10, 320)
    curve: list[float] = []
    bars_held = 0
    trades_tagged = 0

    for i, b in enumerate(bars):
        broker.mark(symbol, float(b["close"]), low=float(b["low"]),
                    high=float(b["high"]))
        if i < warmup:
            continue                        # history only: no trading yet
        open_pos = {p.symbol: p for p in broker.get_positions()}.get(symbol)
        if open_pos is not None:
            bars_held += 1
        else:
            bars_held = 0

        window = bars[max(0, i + 1 - lookback):i + 1]
        position = None
        if open_pos is not None:
            position = {"side": open_pos.side, "entry": open_pos.avg_entry,
                        "bars_held": bars_held}
        sig = strategy.evaluate(symbol, window, position)

        if sig is not None:
            if sig.side == "exit" and open_pos is not None:
                broker.close_position(symbol, reason=sig.reason)
            elif sig.side in ("buy", "sell") and open_pos is None:
                a = atr(window, 14)[-1]
                if a and a > 0:
                    acct = broker.get_account()
                    units = size_position(acct.equity, a, sig.stop_multiple,
                                          risk_pct)
                    units = cap_units(
                        units, float(b["close"]), acct.equity,
                        max_position_pct=max_position_pct,
                        max_gross_pct=max_gross_pct,
                        buying_power=acct.buying_power
                        if sig.side == "buy" else None)
                    if units > 0:
                        if sig.trailing:
                            # Initial hard stop at StopMultiple×ATR (the 1%
                            # risk anchor); ratcheting arms only after +1 ATR
                            # in favor, so entry-bar noise can't strangle the
                            # trade (post-mortem: all 88 BTC exits were the
                            # immediately-armed trail).
                            close = float(b["close"])
                            broker.submit_trailing_stop(
                                symbol, units, sig.side,
                                trail_amount=sig.stop_multiple * a,
                                strategy=strategy.name,
                                stop_price=hard_stop_price(
                                    close, a, sig.stop_multiple, sig.side),
                                activation_price=close + a
                                if sig.side == "buy" else close - a)
                        else:
                            stop = hard_stop_price(
                                float(b["close"]), a, sig.stop_multiple,
                                sig.side)
                            broker.submit_bracket(
                                symbol, units, sig.side, stop_price=stop,
                                strategy=strategy.name)
        curve.append(broker.get_account().equity)
        # Tag round trips closed on this bar with their eval-window index —
        # the walk-forward split needs to know WHEN each trade happened.
        for t in broker.closed_trades[trades_tagged:]:
            t["bar_index"] = i - warmup
        trades_tagged = len(broker.closed_trades)

    broker.close_all()                      # flatten at the end of the window
    for t in broker.closed_trades[trades_tagged:]:
        t["bar_index"] = max(0, len(bars) - warmup - 1)
    if curve:
        curve[-1] = broker.get_account().equity
    return {"strategy": strategy.name, "symbol": symbol,
            "timeframe": strategy.timeframe, "bars": len(bars) - warmup,
            "warmup": warmup,
            "trades": list(broker.closed_trades), "equity_curve": curve,
            "starting_equity": starting_equity,
            "final_equity": broker.get_account().equity}


def max_drawdown_pct(curve: list[float]) -> float:
    peak, worst = float("-inf"), 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak * 100)
    return worst


def sharpe_ratio(curve: list[float], bars_per_year: float) -> float:
    rets = [curve[i] / curve[i - 1] - 1 for i in range(1, len(curve))
            if curve[i - 1] > 0]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return 0.0
    return mean / sd * math.sqrt(bars_per_year)


def cagr_pct(start: float, end: float, years: float) -> float:
    if start <= 0 or years <= 0:
        return 0.0
    if end <= 0:
        return -100.0
    return ((end / start) ** (1 / years) - 1) * 100


def _profit_factor(trades: list[dict]) -> float | None:
    gross_win = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    if gross_loss:
        return gross_win / gross_loss
    return None if not gross_win else float("inf")


def _pf_ok(pf: float | None, minimum: float) -> bool:
    return pf is not None and (pf == float("inf") or pf >= minimum)


def pair_metrics(result: dict, gate: dict | None = None) -> dict:
    """Metrics for one sleeve, PLUS its own PASS/FAIL verdict.

    Per-sleeve gating (2026-07 upgrade) stops one bad strategy from holding
    the passing ones hostage: each sleeve must clear the PF/drawdown
    thresholds on at least min_trades_per_sleeve trades, AND — when the
    walk-forward check is on and the validation segment (the last
    validation_fraction of the eval window) has enough trades — the
    validation segment's PF must clear the threshold too. That keeps any
    future parameter tuning honest by construction: an edge tuned onto the
    early window must still show up in the segment it wasn't fitted to.
    A validation segment with fewer than 5 trades is reported as
    'inconclusive' and the full-window thresholds decide alone.
    """
    g = {**DEFAULT_GATE, **(gate or {})}
    trades = result["trades"]
    wins = [t for t in trades if t["pnl"] > 0]
    pf = _profit_factor(trades)
    bpy = BARS_PER_YEAR.get(result["timeframe"], 252)
    years = result["bars"] / bpy if bpy else 0

    reasons: list[str] = []
    if len(trades) < g["min_trades_per_sleeve"]:
        reasons.append(f"only {len(trades)} trades < "
                       f"{g['min_trades_per_sleeve']} sleeve minimum")
    if not _pf_ok(pf, g["min_profit_factor"]):
        reasons.append(f"profit factor "
                       f"{'n/a' if pf is None else round(pf, 3)} < "
                       f"{g['min_profit_factor']}")
    dd = max_drawdown_pct(result["equity_curve"])
    if dd > g["max_drawdown_pct"]:
        reasons.append(f"max drawdown {dd:.1f}% > {g['max_drawdown_pct']}%")

    # Walk-forward validation segment.
    validation: dict = {"verdict": "off"}
    if g.get("walk_forward", True):
        split = int(result["bars"] * (1 - g["validation_fraction"]))
        val_trades = [t for t in trades if t.get("bar_index", 0) >= split]
        val_pf = _profit_factor(val_trades)
        validation = {"split_bar": split, "trades": len(val_trades),
                      "profit_factor": val_pf, "verdict": "inconclusive"}
        if len(val_trades) >= 5:
            if _pf_ok(val_pf, g["min_profit_factor"]):
                validation["verdict"] = "pass"
            else:
                validation["verdict"] = "fail"
                reasons.append(
                    f"validation segment PF "
                    f"{'n/a' if val_pf is None else round(val_pf, 3)} < "
                    f"{g['min_profit_factor']} over {len(val_trades)} trades")

    return {
        "strategy": result["strategy"], "symbol": result["symbol"],
        "timeframe": result["timeframe"], "bars": result["bars"],
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) if trades else None,
        "profit_factor": pf,
        "total_return_pct": (result["final_equity"] /
                             result["starting_equity"] - 1) * 100,
        "cagr_pct": cagr_pct(result["starting_equity"],
                             result["final_equity"], years),
        "max_drawdown_pct": dd,
        "sharpe": sharpe_ratio(result["equity_curve"], bpy),
        "passed": not reasons,
        "fail_reasons": reasons,
        "validation": validation,
    }


def evaluate_gate(overall: dict, gate: dict | None = None) -> tuple[bool, list[str]]:
    """PASS/FAIL against the configured thresholds; reasons for any FAIL."""
    g = {**DEFAULT_GATE, **(gate or {})}
    reasons = []
    pf = overall.get("profit_factor")
    if pf is None or (pf != float("inf") and pf < g["min_profit_factor"]):
        reasons.append(f"profit factor {pf if pf is not None else 'n/a'} < "
                       f"{g['min_profit_factor']}")
    if overall.get("max_drawdown_pct", 100.0) > g["max_drawdown_pct"]:
        reasons.append(f"max drawdown {overall['max_drawdown_pct']:.1f}% > "
                       f"{g['max_drawdown_pct']}%")
    if overall.get("trades", 0) < g["min_trades"]:
        reasons.append(f"only {overall.get('trades', 0)} trades < "
                       f"{g['min_trades']} minimum")
    return (not reasons), reasons


def run_backtest_offline(bot_cfg: dict, bars_by_symbol: dict[str, list[dict]],
                         result_path: str | Path | None = None,
                         warmup_by_symbol: dict[str, int] | None = None) -> dict:
    """Replay every configured (strategy, symbol) pair from provided bars.

    Hermetic: no network, no clock beyond the report timestamp. Pass
    result_path=None to skip persisting (tests); the real run persists so
    the live-mode guard can find it. warmup_by_symbol marks how many leading
    bars per symbol are indicator warm-up (excluded from trading/metrics).
    """
    warmup_by_symbol = warmup_by_symbol or {}
    strategies = from_config(bot_cfg)
    starting = float(bot_cfg.get("starting_equity", 1000))
    bt_cfg = bot_cfg.get("backtest", {}) if isinstance(
        bot_cfg.get("backtest"), dict) else {}
    slip = float(bt_cfg.get("slippage_pct_per_leg", 0.05))
    commission = float(bt_cfg.get("commission_per_trade", 0.0))

    pairs = []
    all_trades: list[dict] = []
    for strat in strategies:
        for symbol in strat.symbols:
            bars = bars_by_symbol.get(symbol)
            if not bars:
                continue
            res = replay_pair(
                strat, symbol, bars, starting_equity=starting,
                slippage_pct_per_leg=slip, commission_per_trade=commission,
                risk_pct=float(bot_cfg.get("risk_per_trade_pct", 1.0)),
                max_position_pct=float(bot_cfg.get("max_position_pct", 25)),
                max_gross_pct=float(bot_cfg.get("max_gross_pct", 100)),
                warmup=int(warmup_by_symbol.get(symbol, 0)))
            pairs.append(pair_metrics(res, bt_cfg))
            all_trades.extend(res["trades"])

    wins = [t for t in all_trades if t["pnl"] > 0]
    losses = [t for t in all_trades if t["pnl"] < 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    overall = {
        "trades": len(all_trades),
        "win_rate": len(wins) / len(all_trades) if all_trades else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss
        else (None if not wins else float("inf")),
        # Conservative: the worst sleeve's drawdown gates the whole result.
        "max_drawdown_pct": max((p["max_drawdown_pct"] for p in pairs),
                                default=0.0),
        "cagr_pct": (sum(p["cagr_pct"] for p in pairs) / len(pairs))
        if pairs else 0.0,
        "sharpe": (sum(p["sharpe"] for p in pairs) / len(pairs))
        if pairs else 0.0,
    }
    passed, reasons = evaluate_gate(overall, bt_cfg)
    # Per-sleeve verdicts (2026-07): EVERY enabled sleeve must pass. A
    # failing strategy is disabled explicitly in config
    # (bot.assets.<name>.enabled = false) — a deliberate human action, never
    # a silent auto-drop — and the gate re-runs on what remains.
    for p in pairs:
        if not p["passed"]:
            passed = False
            reasons.append(f"sleeve {p['strategy']}/{p['symbol']} failed: "
                           + "; ".join(p["fail_reasons"]))
    result = {"passed": passed, "fail_reasons": reasons,
              "at": dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
              "slippage_pct_per_leg": slip,
              "commission_per_trade": commission,
              "starting_equity_per_sleeve": starting,
              "overall": overall, "pairs": pairs}
    if result_path is not None:
        save_result(result, result_path)
    return result


# ---------------------------------------------------------------------------
# Result persistence — what the live-mode guard reads
# ---------------------------------------------------------------------------

def save_result(result: dict, path: str | Path = DEFAULT_RESULT_PATH) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # float('inf') serializes as bare Infinity (invalid strict JSON) — store
    # the sentinel string "inf" instead so any reader can parse the file.
    txt = json.dumps(result, indent=1, default=str).replace(
        "Infinity", '"inf"')
    p.write_text(txt, encoding="utf-8")


def latest_result(path: str | Path = DEFAULT_RESULT_PATH) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def has_recent_pass(path: str | Path = DEFAULT_RESULT_PATH,
                    max_age_days: int = 30,
                    now: dt.datetime | None = None) -> bool:
    """The live-mode guard: a PASSING result newer than max_age_days."""
    res = latest_result(path)
    if not res or not res.get("passed"):
        return False
    try:
        at = dt.datetime.strptime(res["at"], "%Y-%m-%dT%H:%M:%S")
    except (KeyError, ValueError):
        return False
    now = now or dt.datetime.now()
    return (now - at) <= dt.timedelta(days=max_age_days)


# ---------------------------------------------------------------------------
# The real run (network: yfinance) — not exercised by unit tests
# ---------------------------------------------------------------------------

YF_SYMBOL = {"BTC/USD": "BTC-USD"}
# yfinance interval + PROVIDER HISTORY CAP (days) per strategy timeframe.
YF_INTERVAL = {"15Min": ("15m", 59), "1Hour": ("1h", 730),
               "4Hour": ("1h", 730), "1Day": ("1d", 3650)}
# Conservative bars/day for warm-up budgeting (equity regular hours; 24/7
# assets simply fetch a little extra warm-up, which is harmless).
BARS_PER_DAY = {"15Min": 26, "1Hour": 7, "4Hour": 2, "1Day": 252 / 365}


def fetch_days(timeframe: str, months: float, min_bars: int) -> int:
    """Calendar days to request: the evaluation window PLUS the strategy's
    indicator warm-up, capped at what the provider can actually serve."""
    _, cap = YF_INTERVAL.get(timeframe, ("1d", 3650))
    eval_days = int(months * 30.5)
    warmup_days = math.ceil(min_bars / BARS_PER_DAY.get(timeframe, 1.0))
    return min(cap, eval_days + warmup_days)


def fetch_bars(symbol: str, timeframe: str,
               days: int | None = None) -> list[dict]:
    """Bars via yfinance (15m capped at ~60 days by Yahoo; 1h at ~730 days).

    4Hour series are resampled from 1-hour bars. Returns bar dicts in the
    shape the strategies expect. NETWORK — never called from unit tests.
    """
    import pandas as pd
    import yfinance as yf

    interval, cap = YF_INTERVAL.get(timeframe, ("1d", 3650))
    days = min(days or cap, cap)
    ticker = YF_SYMBOL.get(symbol, symbol)
    df = yf.Ticker(ticker).history(period=f"{days}d", interval=interval,
                                   auto_adjust=False)
    if df is None or df.empty:
        return []
    if timeframe == "4Hour":
        df = df.resample("4h").agg({"Open": "first", "High": "max",
                                    "Low": "min", "Close": "last",
                                    "Volume": "sum"}).dropna()
    bars = []
    for ts, row in df.iterrows():
        bars.append({"ts": pd.Timestamp(ts).isoformat(),
                     "open": float(row["Open"]), "high": float(row["High"]),
                     "low": float(row["Low"]), "close": float(row["Close"]),
                     "volume": float(row["Volume"])})
    return bars


def run_backtest(bot_cfg: dict | None = None,
                 result_path: str | Path = DEFAULT_RESULT_PATH) -> dict:
    """Fetch real bars and run the full gate. Prints a report."""
    bot_cfg = bot_cfg if bot_cfg is not None else \
        (config.load().get("bot") or {})
    strategies = from_config(bot_cfg)
    bt_cfg = bot_cfg.get("backtest", {}) if isinstance(
        bot_cfg.get("backtest"), dict) else {}
    months = float(bt_cfg.get("months", 6))
    bars_by_symbol: dict[str, list[dict]] = {}
    warmup_by_symbol: dict[str, int] = {}
    for strat in strategies:
        for symbol in strat.symbols:
            if symbol in bars_by_symbol:
                continue
            days = fetch_days(strat.timeframe, months, strat.min_bars())
            print(f"  fetching {symbol} ({strat.timeframe}, {days}d incl. "
                  f"{strat.min_bars()}-bar warmup)…", flush=True)
            bars = fetch_bars(symbol, strat.timeframe, days=days)
            bars_by_symbol[symbol] = bars
            # Only mark warm-up if there's an evaluation window left after it.
            warmup_by_symbol[symbol] = strat.min_bars() \
                if len(bars) > strat.min_bars() + 10 else 0
            print(f"    {len(bars)} bars "
                  f"({len(bars) - warmup_by_symbol[symbol]} evaluated)")
    result = run_backtest_offline(bot_cfg, bars_by_symbol, result_path,
                                  warmup_by_symbol)
    print_report(result)
    return result


def print_report(result: dict) -> None:
    print("\nBACKTEST REPORT")
    print("─" * 72)
    for p in result["pairs"]:
        pf = p["profit_factor"]
        pf_s = "inf" if pf == float("inf") else \
            ("n/a" if pf is None else f"{pf:.2f}")
        wr = p["win_rate"]
        val = p.get("validation", {})
        val_s = val.get("verdict", "off")
        verdict = "PASS" if p.get("passed") else "fail"
        print(f"  {p['strategy']:<18} {p['symbol']:<8} {p['timeframe']:<6} "
              f"trades {p['trades']:>3}  win {(wr or 0):>5.0%}  PF {pf_s:>5} "
              f" ret {p['total_return_pct']:>+7.2f}%  dd {p['max_drawdown_pct']:>5.2f}%"
              f"  sharpe {p['sharpe']:>+5.2f}  val:{val_s:<12} [{verdict}]")
    o = result["overall"]
    pf = o["profit_factor"]
    pf_s = "inf" if pf == float("inf") else ("n/a" if pf is None else f"{pf:.2f}")
    print("─" * 72)
    print(f"  OVERALL: trades {o['trades']}, win "
          f"{(o['win_rate'] or 0):.0%}, PF {pf_s}, worst-sleeve dd "
          f"{o['max_drawdown_pct']:.2f}%, avg CAGR {o['cagr_pct']:+.2f}%, "
          f"avg sharpe {o['sharpe']:+.2f}")
    print(f"  slippage {result['slippage_pct_per_leg']}%/leg, commission "
          f"${result['commission_per_trade']}/trade")
    if result["passed"]:
        print("  GATE: PASS ✓")
    else:
        print("  GATE: FAIL ✗ — " + "; ".join(result["fail_reasons"]))
