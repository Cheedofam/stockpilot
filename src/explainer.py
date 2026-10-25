"""Plain-English signal explanations via Gemini, with a deterministic
template fallback so `explain` always produces output.

Sections (the React Stock page renders these):
  ## Signal summary / ## Who is buying / ## Prediction context
  ## Risks / ## What to watch
"""

from __future__ import annotations

import json

from . import config, db

GEMINI_MODELS = ["gemini-2.5-flash-lite", "gemini-2.0-flash-lite",
                 "gemini-2.5-flash", "gemini-2.0-flash"]


def _gemini_explain(context: dict) -> str | None:
    key = config.get("gemini_api_key", "")
    if not key:
        return None
    try:
        import google.generativeai as genai
        genai.configure(api_key=key)
    except Exception:
        return None
    prompt = (
        "You are an educational assistant for a student-focused stock "
        "research tool. Using ONLY the JSON facts below, write a short "
        "markdown explanation with exactly these sections:\n"
        "## Signal summary\n## Who is buying\n## Prediction context\n"
        "## Risks\n## What to watch\n\n"
        "Rules: 2-3 sentences per section, no hype, mention that disclosure "
        "data lags by days to weeks, and that nothing is investment advice. "
        "Note whether the prediction came from the Kronos model or a simple "
        "sklearn fallback.\n\nFACTS:\n" + json.dumps(context, indent=1))
    for model_name in GEMINI_MODELS:
        try:
            model = genai.GenerativeModel(model_name)
            resp = model.generate_content(prompt)
            text = (resp.text or "").strip()
            if text:
                return text
        except Exception:
            continue
    return None


def _template_explain(c: dict) -> str:
    t = c["ticker"]
    buyers = c.get("buyer_names", [])
    who = ", ".join(buyers[:4]) + ("…" if len(buyers) > 4 else "")
    pred = c.get("prediction") or {}
    direction = pred.get("direction", "NEUTRAL")
    model = pred.get("model", "n/a")
    parts = [
        "## Signal summary",
        f"{t} has a convergence score of {c.get('score', '?')} based on "
        f"{c.get('buyers', 0)} distinct disclosed buyer(s) "
        f"({c.get('congress_buyers', 0)} congressional, "
        f"{c.get('billionaire_buyers', 0)} billionaire funds). Disclosures "
        "lag the actual trades by days to weeks, so this reflects past "
        "positioning, not real-time activity.",
        "## Who is buying",
        (f"Recent disclosed buyers: {who}." if buyers
         else "No individual buyers in the current window."),
        "## Prediction context",
        (f"The {model} model projects a {direction.lower()} move of "
         f"{pred.get('pct_7d', 0):+.1f}% over 7 trading days "
         f"(confidence {pred.get('confidence', 0):.0%}). Short-horizon "
         "forecasts are noisy; treat this as one input, not a verdict."
         if pred else "No model prediction is available for this ticker."),
        "## Risks",
        f"Risk level is rated {c.get('risk', 'MEDIUM')} from recent price "
        "volatility. Insiders and politicians also lose money, and by the "
        "time a trade is disclosed the price may already reflect it.",
        "## What to watch",
        "Watch for follow-on filings (more buyers strengthens the signal), "
        "the next 13F cycle, and earnings dates. This is research tooling, "
        "not investment advice.",
    ]
    return "\n".join(parts)


def explain_ticker(ticker: str) -> tuple[str, str]:
    """Returns (markdown_text, model_label)."""
    con = db.connect()
    sig = con.execute("SELECT * FROM signals WHERE ticker=?",
                      (ticker,)).fetchone()
    pred = con.execute("SELECT * FROM predictions WHERE ticker=?",
                       (ticker,)).fetchone()
    news = con.execute(
        "SELECT headline, sentiment FROM news WHERE ticker=? "
        "ORDER BY published DESC LIMIT 5", (ticker,)).fetchall()
    con.close()

    context = {"ticker": ticker}
    if sig:
        context.update({k: sig[k] for k in
                        ("score", "buyers", "congress_buyers",
                         "billionaire_buyers", "total_buy_usd", "risk")})
        context["buyer_names"] = json.loads(sig["buyer_names"] or "[]")
    if pred:
        context["prediction"] = {k: pred[k] for k in
                                 ("direction", "confidence", "model",
                                  "pct_1d", "pct_3d", "pct_7d")}
    if news:
        context["recent_headlines"] = [
            {"headline": n["headline"], "sentiment": n["sentiment"]}
            for n in news]

    text = _gemini_explain(context)
    model = "gemini"
    if not text:
        text = _template_explain(context)
        model = "template"
    return text, model


def run_explanations(tickers: list[str]) -> dict:
    con = db.connect()
    used = {}
    for i, t in enumerate(tickers, 1):
        text, model = explain_ticker(t)
        db.replace_explanation(con, t, text, model)
        used[model] = used.get(model, 0) + 1
        print(f"  [{i}/{len(tickers)}] {t} ({model})")
    con.commit()
    con.close()
    return used
