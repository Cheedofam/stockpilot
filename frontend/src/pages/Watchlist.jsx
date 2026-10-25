import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Plus, X } from "lucide-react";
import { addToWatchlist, getWatchlist, removeFromWatchlist } from "../api.js";
import {
  Disclaimer, DirectionBadge, RiskBadge, scoreColor,
} from "../components/ui.jsx";

export default function Watchlist() {
  const [items, setItems] = useState([]);
  const [ticker, setTicker] = useState("");
  const [error, setError] = useState("");

  const refresh = () =>
    getWatchlist().then(setItems)
      .catch((e) => setError(`Could not load watchlist: ${e.message}`));

  useEffect(() => { refresh(); }, []);

  const add = async (e) => {
    e.preventDefault();
    const t = ticker.trim().toUpperCase();
    if (!/^[A-Z]{1,5}([.-][A-Z]{1,2})?$/.test(t)) {
      setError(`"${ticker}" doesn't look like a ticker symbol.`);
      return;
    }
    setError("");
    await addToWatchlist(t);
    setTicker("");
    refresh();
  };

  const remove = async (t) => {
    await removeFromWatchlist(t);
    refresh();
  };

  return (
    <main className="page">
      <h1>Watchlist</h1>

      <form className="row" onSubmit={add} style={{ marginBottom: 18 }}>
        <input
          placeholder="Add ticker (e.g. NVDA)"
          value={ticker}
          onChange={(e) => setTicker(e.target.value)}
          style={{ width: 200 }}
        />
        <button className="primary" type="submit">
          <Plus size={14} style={{ verticalAlign: "-2px" }} /> Add
        </button>
        {error && <span style={{ color: "var(--bearish)" }}>{error}</span>}
      </form>

      {items.length === 0 ? (
        <p className="muted">
          Nothing here yet — add a ticker above, or browse{" "}
          <Link to="/signals">Signals</Link> for ideas.
        </p>
      ) : (
        <div className="card-grid">
          {items.map((w) => (
            <div className="card lift" key={w.ticker}>
              <div className="row spread">
                <Link to={`/stock/${w.ticker}`} className="mono"
                      style={{ fontWeight: 700, fontSize: 18 }}>
                  {w.ticker}
                </Link>
                <button className="danger" title="Remove"
                        onClick={() => remove(w.ticker)}>
                  <X size={14} />
                </button>
              </div>
              <div className="muted small">{w.company || "—"}</div>
              <div className="row mt spread">
                <span className="mono" style={{ fontSize: 20, fontWeight: 700 }}>
                  {w.last_price != null ? `$${w.last_price.toFixed(2)}` : "—"}
                </span>
                <span style={{ color: scoreColor(w.score ?? 0),
                               fontWeight: 700, fontSize: 20 }}>
                  {w.score ?? "—"}
                </span>
              </div>
              <div className="row mt">
                <DirectionBadge direction={w.direction} />
                <RiskBadge risk={w.risk} />
              </div>
            </div>
          ))}
        </div>
      )}

      <Disclaimer />
    </main>
  );
}
