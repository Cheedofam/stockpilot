import { NavLink, Route, Routes } from "react-router-dom";
import { TrendingUp } from "lucide-react";
import Dashboard from "./pages/Dashboard.jsx";
import Signals from "./pages/Signals.jsx";
import Stock from "./pages/Stock.jsx";
import Watchlist from "./pages/Watchlist.jsx";
import Learn from "./pages/Learn.jsx";

export default function App() {
  return (
    <>
      <nav className="topnav">
        <NavLink to="/" className="brand">
          <TrendingUp size={20} /> StockPilot
        </NavLink>
        <div className="links">
          <NavLink to="/" end>Dashboard</NavLink>
          <NavLink to="/signals">Signals</NavLink>
          <NavLink to="/watchlist">Watchlist</NavLink>
          <NavLink to="/learn">Learn</NavLink>
        </div>
      </nav>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/signals" element={<Signals />} />
        <Route path="/stock/:ticker" element={<Stock />} />
        <Route path="/watchlist" element={<Watchlist />} />
        <Route path="/learn" element={<Learn />} />
      </Routes>
    </>
  );
}
