"""Analyst-desk advisory layer — an optional second opinion from the
TradingAgents multi-agent LLM framework, with a graceful no-op fallback so the
pipeline never depends on it (mirrors the Kronos optional-dependency pattern in
predictor.py).

TradingAgents (https://github.com/TauricResearch/TradingAgents, Apache-2.0)
runs a simulated trading desk: fundamental / sentiment / news / technical
analysts feed a bull-vs-bear researcher debate, a trader drafts a plan, and a
risk-managed portfolio manager returns a BUY / HOLD / SELL decision with
reasoning. We run it LOCALLY on the top convergence signals and store its
verdict next to our own score, as an independent confirmation/veto and as
richer teaching material than a single-shot explanation.

Important boundaries:
  * Disabled by default. Enable with a "desk" block in config.json (see
    config.example.json) AND `pip install tradingagents`.
  * It is NOT connected to any broker. It only produces an opinion.
  * It costs LLM tokens per ticker (several agent calls + debate rounds), so it
    runs on at most `desk.max_tickers` names and caches by (ticker, date).
  * Output is non-deterministic by design (LLM sampling). Treat it as one
    advisory input, never a verdict — same caveat as the price predictions.
"""

from __future__ import annotations

import datetime as dt
import os
import re

from . import config, db

# TradingAgents maps each provider to an env var. We bridge StockPilot's own
# config keys onto the ones TradingAgents expects, so you don't keep keys twice.
_PROVIDER_ENV = {
    "google": ("GOOGLE_API_KEY", "gemini_api_key"),
    "openai": ("OPENAI_API_KEY", "openai_api_key"),
    "anthropic": ("ANTHROPIC_API_KEY", "anthropic_api_key"),
}

_graph = None
_checked = False


def _cfg() -> dict:
    """The 'desk' config block (a dict), or {} when absent."""
    block = config.load().get("desk", {})
    return block if isinstance(block, dict) else {}


def _bridge_api_key(provider: str) -> None:
    """Copy StockPilot's stored key into the env var TradingAgents reads."""
    env_name, cfg_key = _PROVIDER_ENV.get(provider, (None, None))
    if env_name and not os.environ.get(env_name):
        key = config.get(cfg_key, "")
        if key:
            os.environ[env_name] = key


def available() -> bool:
    """True only when the desk is enabled in config AND importable.

    Memoized like predictor.kronos_available(): the heavy import and any
    construction error happen once, and the rest of the pipeline degrades to a
    silent no-op if anything is missing.
    """
    global _checked, _graph
    if _checked:
        return _graph is not None
    _checked = True

    cfg = _cfg()
    if not cfg.get("enabled"):
        return False

    provider = cfg.get("provider", "google")
    _bridge_api_key(provider)
    try:
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        from tradingagents.default_config import DEFAULT_CONFIG

        ta_cfg = DEFAULT_CONFIG.copy()
        ta_cfg["llm_provider"] = provider
        ta_cfg["deep_think_llm"] = cfg.get("deep_think_llm", "gemini-2.5-flash")
        ta_cfg["quick_think_llm"] = cfg.get(
            "quick_think_llm", "gemini-2.5-flash-lite")
        ta_cfg["max_debate_rounds"] = int(cfg.get("max_debate_rounds", 1))
        if cfg.get("temperature") is not None:
            ta_cfg["temperature"] = float(cfg["temperature"])
        _graph = TradingAgentsGraph(debug=False, config=ta_cfg)
        print(f"  TradingAgents desk loaded (provider={provider})")
    except Exception as exc:  # not installed, bad key, version drift, etc.
        print(f"  TradingAgents desk unavailable ({type(exc).__name__}) — "
              "skipping advisory step")
        _graph = None
    return _graph is not None


def _normalize_action(decision) -> str:
    """Reduce a free-form decision to BUY / HOLD / SELL.

    Robust across TradingAgents versions: the decision may arrive as a plain
    string or as a structured object whose str() contains the verdict. We take
    the LAST explicit BUY/SELL/HOLD token, which is the final proposal line.
    """
    text = str(decision or "").upper()
    hits = re.findall(r"\b(BUY|SELL|HOLD)\b", text)
    return hits[-1] if hits else "HOLD"


def opinion_for(ticker: str, date: str | None = None) -> dict | None:
    """Run the desk for one ticker on one date. None if unavailable/failed."""
    if not available():
        return None
    date = date or dt.date.today().isoformat()
    try:
        _, decision = _graph.propagate(ticker, date)
    except Exception as exc:
        print(f"  {ticker}: desk run failed ({type(exc).__name__})")
        return None
    action = _normalize_action(decision)
    rationale = str(decision or "").strip()
    if len(rationale) > 4000:                       # keep the DB row sane
        rationale = rationale[:4000] + "…"
    return {"ticker": ticker, "action": action, "rationale": rationale,
            "model": "tradingagents", "as_of": date}


def run_desk(tickers: list[str], date: str | None = None) -> dict:
    """Run the desk over the top tickers and persist opinions.

    Returns a small status dict. A no-op (status='disabled') when the desk
    isn't available, so it is safe to call unconditionally from the pipeline.
    """
    if not available():
        return {"status": "disabled", "scored": 0}

    cap = int(_cfg().get("max_tickers", 5))
    targets = tickers[:cap]
    con = db.connect()
    n = 0
    for i, t in enumerate(targets, 1):
        op = opinion_for(t, date)
        if op:
            db.replace_desk_opinion(con, op)
            n += 1
            print(f"  [{i}/{len(targets)}] {t}: desk says {op['action']}")
        else:
            print(f"  [{i}/{len(targets)}] {t}: no desk opinion")
    con.commit()
    con.close()
    return {"status": "ok", "scored": n, "considered": len(targets)}
