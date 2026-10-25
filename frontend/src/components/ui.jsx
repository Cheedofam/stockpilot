// Small shared UI pieces used across pages.

export function scoreColor(score) {
  if (score >= 70) return "var(--bullish)";
  if (score >= 50) return "var(--neutral)";
  if (score >= 30) return "#db6d28"; // orange
  return "var(--muted)";
}

export function ScoreBar({ score }) {
  return (
    <div className="score-bar-wrap">
      <div className="score-bar">
        <div
          style={{ width: `${Math.min(score, 100)}%`,
                   background: scoreColor(score) }}
        />
      </div>
      <span className="score-num" style={{ color: scoreColor(score) }}>
        {score}
      </span>
    </div>
  );
}

export function DirectionBadge({ direction }) {
  if (!direction) return <span className="muted">—</span>;
  return (
    <span className={`badge ${direction.toLowerCase()}`}>{direction}</span>
  );
}

export function RiskBadge({ risk }) {
  if (!risk) return <span className="muted">—</span>;
  return <span className={`badge risk-${risk.toLowerCase()}`}>{risk}</span>;
}

export function ModelBadge({ model }) {
  if (!model) return null;
  const cls = model.toLowerCase().includes("kronos") ? "kronos" : "sklearn";
  return <span className={`badge ${cls}`}>{model}</span>;
}

export function SourceBadge({ source }) {
  return (
    <span className={`badge ${source}`}>
      {source === "congress" ? "CONGRESS" : "BILLIONAIRE"}
    </span>
  );
}

export function fmtUsd(x) {
  if (x == null) return "—";
  const a = Math.abs(x);
  if (a >= 1e9) return `$${(x / 1e9).toFixed(1)}B`;
  if (a >= 1e6) return `$${(x / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `$${(x / 1e3).toFixed(0)}K`;
  return `$${Math.round(x)}`;
}

export function SentimentBadge({ value }) {
  if (value == null) return null;
  const cls = value > 0.05 ? "bullish" : value < -0.05 ? "bearish" : "neutral";
  const label = value > 0.05 ? "positive" : value < -0.05 ? "negative" : "neutral";
  return <span className={`badge ${cls}`}>{label}</span>;
}

export function Disclaimer() {
  return (
    <p className="disclaimer">
      Disclosure data lags the underlying trades by days to weeks (Form 4 ≤2
      business days, congressional PTRs ≤45 days, 13F ≤45 days after quarter
      end). StockPilot organizes public filings for research and education —
      it is not investment advice.
    </p>
  );
}
