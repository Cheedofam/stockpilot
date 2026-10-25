"""Research Desk delivery — persist the brief, render the email section.

Storage goes to stocks.db (research_briefs / research_ideas), which the
existing daily upload already syncs to the live site, so the dashboard's
/stockpilot/api/research/* endpoints serve it with no extra plumbing.

Email: the digest gains a Research Desk section ONLY when
`research.email_digest` is true (default OFF — Phase 2 flips it, see
docs/RESEARCH_DESK_BUILD_PLAN.md §9). Nothing here sends mail itself; it
renders HTML for digest.py to embed, so there is exactly one mail path in
the codebase.
"""

from __future__ import annotations

import html as _html

from .. import config, db

RISK_COLOR = {"Low": "#3fb950", "Medium": "#d29922", "High": "#f85149"}
TIER_BADGE = {1: "T1 · institutional", 2: "T2 · professional",
              3: "T3 · social (low trust)"}

DISCLAIMER_HTML = (
    "Educational research from StockPilot's Research Desk — <b>not "
    "financial advice</b>. Disclosures lag trades by days to weeks; verify "
    "everything in Questrade before acting. Every decision is yours.")


def _cfg() -> dict:
    block = config.load().get("research", {})
    return block if isinstance(block, dict) else {}


def email_enabled() -> bool:
    """Phase gate: the research email section is off unless explicitly
    enabled in config (research.email_digest)."""
    return bool(_cfg().get("email_digest", False))


def store_brief(result: dict, con=None) -> dict:
    """Persist build_brief() output; returns a small status dict."""
    own = con is None
    if own:
        con = db.connect()
    try:
        db.replace_research_brief(con, result)
        n = db.replace_research_ideas(con, result["as_of"],
                                      result.get("ideas", []))
    finally:
        if own:
            con.close()
    return {"as_of": result["as_of"], "ideas": n,
            "model": result.get("model")}


def _esc(text) -> str:
    """All idea/chatter text originated in untrusted sources — escape it
    before it goes anywhere near HTML."""
    return _html.escape(str(text if text is not None else ""))


def render_email_section(brief: dict | None, ideas: list[dict]) -> str:
    """The Research Desk block for the digest email (HTML fragment).

    Renders from the STRUCTURED ideas (not the LLM markdown): every row
    shows the risk label, trust-tier badge, thesis and entry/exit note, the
    chatter/pump sections stay visibly separated, and the disclaimer is
    always present.
    """
    if not brief:
        return ""
    rows = ""
    for idea in ideas:
        color = RISK_COLOR.get(idea.get("risk_label"), "#8b949e")
        badge = TIER_BADGE.get(idea.get("trust_tier"), "T?")
        flags = idea.get("auditor_flags") or []
        flags_html = ""
        if flags:
            flags_html = ("<div style='color:#d29922;font-size:11px;"
                          "margin-top:2px'>auditor: "
                          + _esc("; ".join(flags)) + "</div>")
        rows += f"""
        <tr>
          <td style="padding:8px;border-bottom:1px solid #30363d;
              font-weight:bold">{_esc(idea['ticker'])}</td>
          <td style="padding:8px;border-bottom:1px solid #30363d;
              color:{color};font-weight:bold">{_esc(idea.get('risk_label'))}</td>
          <td style="padding:8px;border-bottom:1px solid #30363d;
              color:#8b949e;font-size:11px">{_esc(badge)}</td>
          <td style="padding:8px;border-bottom:1px solid #30363d">
              {_esc(idea.get('thesis'))}
              <div style="color:#8b949e;font-size:11px;margin-top:2px">
                {_esc(idea.get('entry_exit'))}</div>{flags_html}</td>
        </tr>"""

    chatter_html = ""
    if brief.get("chatter"):
        lines = "".join(
            f"<li>{_esc(c.get('ticker'))} ({_esc(c.get('source'))}): "
            f"{_esc(c.get('summary'))}</li>"
            for c in brief["chatter"][:6])
        chatter_html = f"""
    <h4 style="color:#8b949e;margin-bottom:4px">Retail chatter (low-trust)
    </h4>
    <p style="color:#8b949e;font-size:11px;margin:0 0 6px">Context only —
      never a recommendation.</p>
    <ul style="color:#8b949e;font-size:12px">{lines}</ul>"""

    pump_html = ""
    if brief.get("pump_flags"):
        lines = "".join(
            f"<li><b>{_esc(p.get('ticker'))}</b>: {_esc(p.get('reason'))}"
            f"</li>" for p in brief["pump_flags"][:6])
        pump_html = f"""
    <h4 style="color:#f85149;margin-bottom:4px">⚠ Pump-and-dump watch</h4>
    <ul style="color:#f85149;font-size:12px">{lines}</ul>"""

    body = rows or ("<tr><td colspan='4' style='padding:8px;color:#8b949e'>"
                    "No ideas met the evidence bar today (Tier-1/2 support "
                    "required).</td></tr>")
    return f"""
    <h3 style="color:#2ea043;margin-top:28px">Research Desk — {_esc(brief.get('as_of'))}</h3>
    <table style="border-collapse:collapse;width:100%;background:#161b22;
        border:1px solid #30363d;border-radius:6px">
      <tr style="color:#8b949e;text-align:left">
        <th style="padding:8px">Ticker</th><th style="padding:8px">Risk</th>
        <th style="padding:8px">Trust</th><th style="padding:8px">Thesis</th>
      </tr>{body}
    </table>{chatter_html}{pump_html}
    <p style="color:#8b949e;font-size:12px;margin-top:12px">
      {DISCLAIMER_HTML}</p>"""


def digest_section() -> str:
    """The fragment digest.py embeds — empty string unless enabled AND a
    brief exists (fail closed, never breaks the digest)."""
    try:
        if not email_enabled():
            return ""
        con = db.connect()
        try:
            brief = db.latest_research_brief(con)
            ideas = (db.research_ideas_for(con, brief["as_of"])
                     if brief else [])
        finally:
            con.close()
        return render_email_section(brief, ideas)
    except Exception:
        return ""
