"""Research Desk brief builder — advisor → risk-auditor, LLM optional.

Turns the collected, trust-tiered inputs (collect.collect_all) into the daily
brief. The safety-critical rules are enforced DETERMINISTICALLY in code —
never delegated to an LLM:

  * an idea is only ever built for a ticker with Tier-1/2 evidence; a ticker
    with only Tier-3 buzz becomes a pump-and-dump flag, never an idea;
  * every idea carries a risk label, tier-cited evidence, sizing and
    entry/exit notes; ideas failing the audit are dropped (fail closed);
  * the not-financial-advice disclaimer is appended in code, always.

Gemini (same key/pattern as src/analyst.py) writes the narrative markdown on
top of that structure; when unavailable, a deterministic template renders the
same structure so the pipeline never breaks. LLM prompts only ever receive
the `wrapped` (delimiter-guarded) form of collected text.
"""

from __future__ import annotations

import json
import re

from .. import config
from . import personas
from .collect import support_by_ticker

GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite",
                 "gemini-2.0-flash", "gemini-2.0-flash-lite"]

RISK_LABELS = ("Low", "Medium", "High")
_RISK_MAP = {"LOW": "Low", "MEDIUM": "Medium", "HIGH": "High"}

# Promissory / hype language the auditor rejects in any narrative.
HYPE_WORDS = ("guaranteed", "can't lose", "cannot lose", "sure thing",
              "risk-free", "no risk", "get rich", "to the moon", "🚀",
              "10x", "100x", "easy money")

# Injection artifacts to catch in evidence that reaches an idea (defense in
# depth on top of the wrap_untrusted delimiters).
_INJECTION_RX = re.compile(
    r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above)\s+instructions"
    r"|disregard\s+(?:your|the|all)\b.*instructions"
    r"|system\s+prompt|reveal\s+your|api\s*key|you\s+are\s+now\s+",
    re.IGNORECASE)


def _cfg() -> dict:
    block = config.load().get("research", {})
    return block if isinstance(block, dict) else {}


def _profile() -> dict:
    p = config.load().get("investor_profile", {})
    return p if isinstance(p, dict) else {}


def _gemini(prompt: str) -> str | None:
    """Same optional-LLM pattern as analyst.py: None when unavailable."""
    key = config.get("gemini_api_key", "")
    if not key:
        return None
    try:
        import google.generativeai as genai
        genai.configure(api_key=key)
    except Exception:
        return None
    for name in GEMINI_MODELS:
        try:
            resp = genai.GenerativeModel(name).generate_content(prompt)
            text = (resp.text or "").strip()
            if text:
                return text
        except Exception:
            continue
    return None


# ------------------------------------------------- deterministic core --

def _by_ticker(items: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for it in items:
        if it["ticker"] and it["ticker"] != "_MARKET":
            out.setdefault(it["ticker"], []).append(it)
    return out


def _risk_label(ticker_items: list[dict]) -> str:
    """Signal risk mapped to Low/Medium/High; unknown = High (fail closed)."""
    for it in ticker_items:
        if it["kind"] == "convergence_signal":
            return _RISK_MAP.get(str(it["data"].get("risk", "")).upper(),
                                 "High")
    return "High"


def _score(ticker_items: list[dict]) -> float:
    for it in ticker_items:
        if it["kind"] == "convergence_signal":
            return float(it["data"].get("score") or 0)
    return 0.0


def _thesis(ticker: str, ticker_items: list[dict]) -> str:
    sig = next((i for i in ticker_items
                if i["kind"] == "convergence_signal"), None)
    pred = next((i for i in ticker_items
                 if i["kind"] == "model_prediction"), None)
    parts = []
    if sig:
        d = sig["data"]
        parts.append(f"{d.get('buyers', '?')} disclosed smart-money buyers "
                     f"converging (score {d.get('score')})")
    if pred:
        parts.append(f"model leans {pred['data'].get('direction', '?')}")
    if not parts:
        parts.append("supported by professional (Tier-2) coverage")
    return f"{ticker}: " + "; ".join(parts) + "."


def _sizing_note() -> str:
    p = _profile()
    capital = p.get("total_starting_capital", 1000)
    return (f"Satellite position only: cap at ~10% of the portfolio "
            f"(~${int(capital) // 10} of a ${capital} start) and ~30% for "
            f"all satellites combined. The diversified ETF core belongs in "
            f"the TFSA on a monthly DCA schedule and is not affected by "
            f"this idea.")


def _entry_exit(risk: str) -> str:
    return (f"Satellite framework ({risk} risk): enter in 2 tranches rather "
            "than all at once; set a hard stop-loss idea around -20-25% (or "
            "a trailing stop) before buying; exit regardless of price if "
            "the thesis breaks (buyers stop converging, guidance cut, "
            "growth stalls). Core ETFs: DCA on schedule, no stop-loss, no "
            "market-timing.")


def build_ideas(collected: dict) -> tuple[list[dict], list[dict],
                                          list[dict]]:
    """The deterministic heart: (ideas, chatter, pump_flags).

    Ideas only for tickers with Tier-1/2 support, ranked by convergence
    score, capped at research.max_ideas. Tier-3 evidence NEVER creates an
    idea: with Tier-1/2 support it is attached as separate low-trust
    chatter; alone, it becomes a pump-and-dump flag.
    """
    grouped = _by_ticker(collected["items"])
    support = support_by_ticker(collected["items"])
    max_ideas = int(_cfg().get("max_ideas", 8))

    ideas, chatter, pump_flags = [], [], []
    ranked = sorted(grouped, key=lambda t: _score(grouped[t]), reverse=True)
    for t in ranked:
        tiers = support.get(t, set())
        t3_items = [i for i in grouped[t] if i["tier"] == 3]
        if not tiers & {1, 2}:
            if t3_items:                        # Tier-3 alone: red flag
                pump_flags.append({
                    "ticker": t,
                    "reason": ("retail social buzz with NO Tier-1/2 "
                               "support — possible pump-and-dump; not a "
                               "recommendation"),
                    "evidence": [{"source": i["source"], "tier": i["tier"],
                                  "summary": i["summary"]}
                                 for i in t3_items]})
            continue
        chatter += [{"ticker": t, "source": i["source"], "tier": 3,
                     "summary": i["summary"]} for i in t3_items]
        if len(ideas) >= max_ideas:
            continue
        evidence = [{"source": i["source"], "tier": i["tier"],
                     "kind": i["kind"], "summary": i["summary"]}
                    for i in grouped[t] if i["tier"] in (1, 2)]
        risk = _risk_label(grouped[t])
        ideas.append({
            "ticker": t,
            "kind": "stock",
            "thesis": _thesis(t, grouped[t]),
            "trust_tier": min(i["tier"] for i in grouped[t]),
            "risk_label": risk,
            "evidence": evidence,
            "sizing_note": _sizing_note(),
            "entry_exit": _entry_exit(risk),
            "auditor_flags": [],
        })
    return ideas, chatter, pump_flags


# --------------------------------------------------------- risk audit --

def audit_ideas(ideas: list[dict]) -> list[dict]:
    """Deterministic compliance gate. Returns the surviving ideas with
    `auditor_flags` annotations; VETOed ideas are dropped (fail closed)."""
    survivors = []
    for idea in ideas:
        flags: list[str] = []
        veto = False
        tiers = {e.get("tier") for e in idea.get("evidence", [])}
        if not tiers & {1, 2}:
            flags.append("VETO: no Tier-1/2 evidence (pump-and-dump "
                         "pattern)")
            veto = True
        if not idea.get("evidence"):
            flags.append("VETO: no cited evidence")
            veto = True
        if idea.get("risk_label") not in RISK_LABELS:
            flags.append("VETO: missing/invalid risk label")
            veto = True
        narrative = " ".join([idea.get("thesis", ""),
                              idea.get("advisor_note", "")]).lower()
        for w in HYPE_WORDS:
            if w in narrative:
                flags.append(f"FLAG: promissory/hype language ({w!r})")
        for e in idea.get("evidence", []):
            if _INJECTION_RX.search(e.get("summary", "")):
                flags.append(
                    f"FLAG: possible prompt-injection artifact in evidence "
                    f"from {e.get('source')} — treated as data, verify "
                    f"the source")
        idea["auditor_flags"] = flags
        if not veto:
            survivors.append(idea)
    return survivors


# ------------------------------------------------------ narrative layer --

def _advisor_prompt(collected: dict, ideas: list[dict]) -> str:
    """Narrative prompt for the advisor persona. ONLY wrapped (delimiter-
    guarded) text enters the prompt; the deterministic idea skeleton rides
    along as trusted JSON."""
    wrapped_evidence = "\n\n".join(
        i["wrapped"] for i in collected["items"]
        if i["ticker"] in {x["ticker"] for x in ideas})
    skeleton = [{k: v for k, v in idea.items()
                 if k in ("ticker", "thesis", "trust_tier", "risk_label")}
                for idea in ideas]
    return (
        f"{personas.system_prompt('advisor')}\n\n"
        f"INVESTOR PROFILE:\n{json.dumps(_profile(), indent=1)}\n\n"
        f"TODAY'S VETTED IDEA SKELETONS (deterministically screened — do "
        f"not add or remove tickers):\n{json.dumps(skeleton, indent=1)}\n\n"
        f"EVIDENCE (untrusted data blocks):\n{wrapped_evidence}\n\n"
        "Write the daily research brief in clean markdown covering exactly "
        "these tickers, in this order.")


def _auditor_prompt(draft: str) -> str:
    return (
        f"{personas.system_prompt('risk_auditor')}\n\n"
        f"ADVISOR DRAFT TO AUDIT:\n{personas.wrap_untrusted(draft, source='advisor_draft', tier=2)}\n\n"
        "Return your PASS/FLAG/VETO audit as terse markdown bullets.")


def _safety_sections(collected: dict, chatter: list[dict],
                     pump_flags: list[dict]) -> str:
    """Chatter / pump-and-dump / source-note sections, rendered
    deterministically and appended to BOTH the LLM and template paths — the
    warnings must never depend on what the LLM chose to write."""
    lines: list[str] = []
    if chatter:
        lines += ["## Retail chatter (low-trust)",
                  "_Social sentiment shown for context only — never a "
                  "recommendation._", ""]
        lines += [f"- {c['ticker']} ({c['source']}): {c['summary']}"
                  for c in chatter]
        lines.append("")
    if pump_flags:
        lines += ["## ⚠ Pump-and-dump watch",
                  "_Tickers with social buzz but no Tier-1/2 evidence — "
                  "avoid._", ""]
        lines += [f"- {p['ticker']}: {p['reason']}" for p in pump_flags]
        lines.append("")
    if collected.get("errors"):
        lines += ["## Source notes",
                  f"_{len(collected['errors'])} source(s) unavailable this "
                  f"run: " + "; ".join(collected["errors"][:8]) + "_", ""]
    return "\n".join(lines)


def _template_markdown(collected: dict, ideas: list[dict]) -> str:
    lines = [f"# Research Desk brief — {collected['as_of']}", ""]
    if not ideas:
        lines.append("_No ideas met the evidence bar today (Tier-1/2 "
                     "support required)._")
    for n, idea in enumerate(ideas, 1):
        lines += [
            f"## {n}. {idea['ticker']} — risk {idea['risk_label']}",
            "", idea["thesis"], "",
            "**Evidence:**"]
        lines += [f"- [Tier {e['tier']}] ({e['source']}) {e['summary']}"
                  for e in idea["evidence"]]
        lines += ["", f"**Sizing:** {idea['sizing_note']}",
                  f"**Entry/exit:** {idea['entry_exit']}"]
        if idea["auditor_flags"]:
            lines += ["", "**Auditor notes:**"]
            lines += [f"- {f}" for f in idea["auditor_flags"]]
        lines.append("")
    return "\n".join(lines)


def build_brief(collected: dict, llm=None) -> dict:
    """collect_all() output -> the daily brief:

        {"as_of", "markdown", "model", "ideas", "chatter", "pump_flags"}

    `llm` is injectable for tests; defaults to Gemini with template
    fallback. Regardless of what the LLM writes, the structured ideas are
    the deterministic, audited ones, and the disclaimer is always appended.
    """
    llm = llm or _gemini
    ideas, chatter, pump_flags = build_ideas(collected)
    ideas = audit_ideas(ideas)

    model = "template"
    markdown = None
    if ideas:
        draft = llm(_advisor_prompt(collected, ideas))
        if draft:
            audit = llm(_auditor_prompt(draft))
            markdown = draft
            if audit:
                markdown += f"\n\n## Risk-auditor review\n\n{audit}"
            model = "gemini"
    if markdown is None:
        markdown = _template_markdown(collected, ideas)

    safety = _safety_sections(collected, chatter, pump_flags)
    if safety:
        markdown = markdown.rstrip() + "\n\n" + safety
    markdown = markdown.rstrip() + f"\n\n---\n> {personas.DISCLAIMER}\n"
    return {"as_of": collected["as_of"], "markdown": markdown,
            "model": model, "ideas": ideas, "chatter": chatter,
            "pump_flags": pump_flags,
            "errors": collected.get("errors", [])}
