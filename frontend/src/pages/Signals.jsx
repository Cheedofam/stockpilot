import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ChevronDown, ChevronRight } from "lucide-react";
import { getSignals } from "../api.js";
import {
  Disclaimer, DirectionBadge, ModelBadge, RiskBadge, ScoreBar, fmtUsd,
} from "../components/ui.jsx";

export default function Signals() {
  const [signals, setSignals] = useState([]);
  const [error, setError] = useState("");
  const [minScore, setMinScore] = useState(0);
  const [traderType, setTraderType] = useState("any");
  const [direction, setDirection] = useState("any");
  const [open, setOpen] = useState(null);

  useEffect(() => {
    getSignals().then(setSignals)
      .catch((e) => setError(`Could not load signals: ${e.message}`));
  }, []);

  const filtered = useMemo(() => signals.filter((s) => {
    if (s.score < minScore) return false;
    if (traderType === "congress" && !s.congress_buyers) return false;
    if (traderType === "billionaire" && !s.billionaire_buyers) return false;
    if (direction !== "any" && s.direction !== direction) return false;
    return true;
  }), [signals, minScore, traderType, direction]);

  if (error) return <main className="page"><div className="error-box">{error}</div></main>;

  return (
    <main className="page">
      <h1>All Signals</h1>

      <div className="row card" style={{ marginBottom: 16 }}>
        <label className="muted small">Min score</label>
        <input
          type="number" min="0" max="100" value={minScore}
          style={{ width: 80 }}
          onChange={(e) => setMinScore(Number(e.target.value) || 0)}
        />
        <label className="muted small">Trader type</label>
        <select value={traderType} onChange={(e) => setTraderType(e.target.value)}>
          <option value="any">Any</option>
          <option value="congress">Congress</option>
          <option value="billionaire">Billionaire</option>
        </select>
        <label className="muted small">Direction</label>
        <select value={direction} onChange={(e) => setDirection(e.target.value)}>
          <option value="any">Any</option>
          <option value="BULLISH">Bullish</option>
          <option value="BEARISH">Bearish</option>
          <option value="NEUTRAL">Neutral</option>
        </select>
        <span className="muted small">{filtered.length} of {signals.length} signals</span>
      </div>

      <div className="card" style={{ overflowX: "auto" }}>
        <table>
          <thead>
            <tr>
              <th></th><th>Ticker</th><th>Company</th><th>Score</th>
              <th>Buyers</th><th>Direction</th><th>Model</th><th>Risk</th><th></th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((s) => (
              <SignalRow
                key={s.ticker} s={s}
                open={open === s.ticker}
                toggle={() => setOpen(open === s.ticker ? null : s.ticker)}
              />
            ))}
          </tbody>
        </table>
      </div>

      <Disclaimer />
    </main>
  );
}

function SignalRow({ s, open, toggle }) {
  return (
    <>
      <tr className="clickable" onClick={toggle}>
        <td>{open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}</td>
        <td className="mono" style={{ fontWeight: 700 }}>{s.ticker}</td>
        <td>{s.company}</td>
        <td><ScoreBar score={s.score} /></td>
        <td>{s.buyers}</td>
        <td><DirectionBadge direction={s.direction} /></td>
        <td><ModelBadge model={s.model} /></td>
        <td><RiskBadge risk={s.risk} /></td>
        <td>
          <Link to={`/stock/${s.ticker}`} onClick={(e) => e.stopPropagation()}>
            View →
          </Link>
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={9} style={{ background: "rgba(46,160,67,0.04)" }}>
            <strong className="small">Disclosed buyers:</strong>{" "}
            {s.buyer_names.length
              ? s.buyer_names.join(", ")
              : "none in window"}
            <span className="muted small" style={{ marginLeft: 14 }}>
              total disclosed buying ≈ {fmtUsd(s.total_buy_usd)} ·
              last trade {s.last_trade_date} · {s.sells} seller(s)
            </span>
          </td>
        </tr>
      )}
    </>
  );
}
