"""Daily email digest of top signals, sent via Gmail SMTP."""

from __future__ import annotations

import datetime as dt
import json
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from . import config, db

DIR_COLOR = {"BULLISH": "#3fb950", "BEARISH": "#f85149", "NEUTRAL": "#d29922"}


def build_digest_html() -> tuple[str, dict]:
    con = db.connect()
    signals = con.execute(
        "SELECT * FROM signals ORDER BY score DESC LIMIT 10").fetchall()
    preds = {p["ticker"]: p for p in
             con.execute("SELECT * FROM predictions").fetchall()}
    opt = db.latest_options_flow(con)
    today = dt.date.today().isoformat()
    new_today = con.execute(
        "SELECT COUNT(*) c FROM trades WHERE collected_at >= ?",
        (today,)).fetchone()["c"]
    total_trades = con.execute(
        "SELECT COUNT(*) c FROM trades").fetchone()["c"]
    con.close()

    rows_html = ""
    for s in signals:
        p = preds.get(s["ticker"])
        direction = p["direction"] if p else "—"
        color = DIR_COLOR.get(direction, "#8b949e")
        buyers = ", ".join(json.loads(s["buyer_names"] or "[]")[:3])
        o = opt.get(s["ticker"])
        options_line = ""
        if o and (o.get("skew_label") or o.get("iv_rank") is not None
                  or o.get("unusual_volume")):
            clauses = []
            if o.get("skew_label"):
                clauses.append(f"{o['skew_label']} skew")
            if o.get("iv_rank") is not None:
                clauses.append(f"IV rank {round(o['iv_rank'])}")
            if o.get("unusual_volume"):
                clauses.append("unusual option volume")
            options_line = (f"""<div class="muted small" style="margin-top:4px;
                color:#8b949e">options: {', '.join(clauses)}</div>""")
        rows_html += f"""
        <tr>
          <td style="padding:8px;border-bottom:1px solid #30363d;
              font-weight:bold">{s['ticker']}</td>
          <td style="padding:8px;border-bottom:1px solid #30363d">
              {s['company']}</td>
          <td style="padding:8px;border-bottom:1px solid #30363d;
              color:#2ea043;font-weight:bold">{s['score']}</td>
          <td style="padding:8px;border-bottom:1px solid #30363d">
              {s['buyers']} ({buyers}){options_line}</td>
          <td style="padding:8px;border-bottom:1px solid #30363d;
              color:{color};font-weight:bold">{direction}</td>
          <td style="padding:8px;border-bottom:1px solid #30363d">
              {s['risk']}</td>
        </tr>"""

    # Research Desk section — empty string unless research.email_digest is
    # enabled AND a brief exists (advisory-only; see src/research/deliver.py).
    from .research.deliver import digest_section
    research_html = digest_section()

    html = f"""<html><body style="font-family:Segoe UI,Arial,sans-serif;
      background:#0d1117;color:#f0f6fc;padding:24px">
    <h2 style="color:#2ea043">StockPilot Daily Digest — {today}</h2>
    <p style="color:#8b949e">{new_today} trades collected today ·
       {total_trades} total tracked · top {len(signals)} convergence signals
       below.</p>
    <table style="border-collapse:collapse;width:100%;background:#161b22;
        border:1px solid #30363d;border-radius:6px">
      <tr style="color:#8b949e;text-align:left">
        <th style="padding:8px">Ticker</th><th style="padding:8px">Company</th>
        <th style="padding:8px">Score</th><th style="padding:8px">Buyers</th>
        <th style="padding:8px">7d Outlook</th><th style="padding:8px">Risk</th>
      </tr>{rows_html}
    </table>{research_html}
    <p style="color:#8b949e;font-size:12px;margin-top:16px">
      Disclosure data lags trades by days to weeks (Form 4 ≤2 business days,
      congressional PTRs ≤45 days, 13F ≤45 days after quarter end).
      Research tooling, not investment advice.</p>
    </body></html>"""
    stats = {"signals": len(signals), "new_today": new_today,
             "total_trades": total_trades}
    return html, stats


def send_alert(subject: str, body: str) -> dict:
    """Plain-text alert email (used when the daily run fails)."""
    sender = config.get("gmail_address", "")
    password = config.get("gmail_app_password", "")
    recipient = config.get("digest_recipient", sender)
    if not sender or not password:
        return {"sent": False, "error": "gmail credentials missing"}
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(sender, password)
            smtp.sendmail(sender, [recipient], msg.as_string())
    except Exception as exc:
        return {"sent": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"sent": True, "to": recipient}


def send_digest() -> dict:
    sender = config.get("gmail_address", "")
    password = config.get("gmail_app_password", "")
    recipient = config.get("digest_recipient", sender)
    if not sender or not password:
        return {"sent": False, "error": "gmail credentials missing"}

    html, stats = build_digest_html()
    msg = MIMEMultipart("alternative")
    msg["Subject"] = (f"StockPilot Digest {dt.date.today().isoformat()} — "
                      f"{stats['signals']} signals, "
                      f"{stats['new_today']} new trades")
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(sender, password)
            smtp.sendmail(sender, [recipient], msg.as_string())
    except Exception as exc:
        return {"sent": False, "error": f"{type(exc).__name__}: {exc}",
                **stats}
    return {"sent": True, "to": recipient, **stats}
