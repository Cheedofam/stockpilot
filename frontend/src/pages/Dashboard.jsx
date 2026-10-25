import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Activity, Newspaper, Target, Trophy } from "lucide-react";
import { getSignals, getStats, getTrades } from "../api.js";
import {
  Disclaimer, DirectionBadge, RiskBadge, ScoreBar, SourceBadge, fmtUsd,
} from "../components/ui.jsx";

export default function Dashboard() {
  const [stats, setStats] = useState(null);
  const [signals, setSignals] = useState([]);
  const [trades, setTrades] = useState([]);
  const [error, setError] = useState("");

  useEffect(() => {
    Promise.all([getStats(), getSignals(), getTrades(15)])
      .then(([s, sig, tr]) => {
        setStats(s);
        setSignals(sig.slice(0, 10));
        setTrades(tr);
      })
      .catch((e) => setError(`Could not reach the StockPilot API: ${e.message}`));
  }, []);

  if (error) return <main className="page"><div className="error-box">{error}</div></main>;

  const cards = stats ? [
    { icon: <Activity size={15} />, label: "Active Signals", value: stats.active_signals },
    { icon: <Newspaper size={15} />, label: "New Trades Today", value: stats.new_trades_today },
    { icon: <Target size={15} />, label: "Tickers Tracked", value: stats.tickers_tracked },
    { icon: <Trophy size={15} />, label: "Top Score", value: stats.top_score },
  ] : [];

  return (
    <main className="page">
      <h1>Dashboard</h1>
      <p className="page-sub">
        Where disclosed smart money is converging — congressional filings,
        billionaire 13Fs, and Kronos model forecasts.
        {stats?.last_updated && (
          <span className="small"> Updated {new Date(stats.last_updated).toLocaleString()}.</span>
        )}
      </p>

      <div className="stat-grid">
        {cards.map((c) => (
          <div className="card stat-card lift" key={c.label}>
            <div className="label">{c.icon}{c.label}</div>
            <div className="value">{c.value}</div>
          </div>
        ))}
      </div>

      <h2>Top Convergence Signals</h2>
      <div className="card" style={{ overflowX: "auto" }}>
        <table>
          <thead>
            <tr>
              <th>Ticker</th><th>Company</th><th>Score</th><th>Buyers</th>
              <th>Kronos Direction</th><th>Risk</th><th></th>
            </tr>
          </thead>
          <tbody>
            {signals.map((s) => (
              <tr key={s.ticker}>
                <td className="mono" style={{ fontWeight: 700 }}>{s.ticker}</td>
                <td>{s.company}</td>
                <td><ScoreBar score={s.score} /></td>
                <td>{s.buyers} <span className="muted small">
                  ({s.congress_buyers}C / {s.billionaire_buyers}B
                  {s.insider_buyers ? ` / ${s.insider_buyers}I` : ""})</span></td>
                <td><DirectionBadge direction={s.direction} /></td>
                <td><RiskBadge risk={s.risk} /></td>
                <td><Link to={`/stock/${s.ticker}`}>View →</Link></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2>Recent Insider Trades</h2>
      <div className="card">
        {trades.map((t) => (
          <div className="feed-row" key={t.id}>
            <SourceBadge source={t.source} />
            <strong>{t.trader}</strong>
            <span className={t.type === "buy" ? "badge bullish" : "badge bearish"}>
              {t.type === "buy" ? "bought" : "sold"}
            </span>
            <Link to={`/stock/${t.ticker}`} className="mono">{t.ticker}</Link>
            <span className="muted">
              {t.source === "billionaire"
                ? `${fmtUsd(t.amount_usd)} position (${t.amount_range})`
                : t.amount_range}
            </span>
            <span className="muted small">{t.trade_date}</span>
          </div>
        ))}
      </div>

      <Disclaimer />
    </main>
  );
}
