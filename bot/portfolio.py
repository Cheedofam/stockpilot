"""Portfolio telemetry — the bot's audit ledger and the dashboard's source.

Three CSVs under data/bot/ (gitignored), append/upsert only:

  trades.csv        one row per ROUND TRIP: ts, symbol, side, qty, entry,
                    exit, pnl, reason, strategy. The audit trail.
  daily_pnl.csv     one row per DAY (upserted): date, equity, realized,
                    unrealized, wins, losses. Feeds the /summary endpoint.
  equity_curve.csv  one row per equity mark: ts, equity. Feeds the drawdown
                    chart; the circuit breaker keeps its own watermark in
                    state.json (risk_manager) — this file is telemetry, not
                    the safety mechanism.

Optionally mirrors trades + daily rows into data/trading.db (SQLite),
following the watchlist.db pattern: a separate database the daily pipeline
deploy never overwrites. The mirror is best-effort — CSV is the source of
truth and a mirror failure must never break trading telemetry.

All read APIs degrade to empty/zeroed results when the files don't exist
yet, so the dashboard works before the bot's first trade.
"""

from __future__ import annotations

import csv
import datetime as dt
import sqlite3
from pathlib import Path

from src import config

DEFAULT_DIR = config.DATA_DIR / "bot"
TRADING_DB_PATH = config.DATA_DIR / "trading.db"

TRADE_FIELDS = ["ts", "symbol", "side", "qty", "entry", "exit", "pnl",
                "reason", "strategy"]
DAILY_FIELDS = ["date", "equity", "realized", "unrealized", "wins", "losses"]
EQUITY_FIELDS = ["ts", "equity"]


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


class Portfolio:
    def __init__(self, directory: str | Path | None = None,
                 mirror_db: bool = False,
                 db_path: str | Path | None = None):
        self.dir = Path(directory) if directory else DEFAULT_DIR
        self.trades_path = self.dir / "trades.csv"
        self.daily_path = self.dir / "daily_pnl.csv"
        self.equity_path = self.dir / "equity_curve.csv"
        self.mirror_db = bool(mirror_db)
        self.db_path = Path(db_path) if db_path else TRADING_DB_PATH

    # ---- writes ------------------------------------------------------------
    def _append(self, path: Path, fields: list[str], row: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)

    def record_trade(self, trade: dict, ts: str | None = None) -> dict:
        """Append one closed round trip (a bot.broker closed_trades dict)."""
        row = {"ts": ts or _now(),
               "symbol": trade.get("symbol", ""),
               "side": trade.get("side", ""),
               "qty": round(float(trade.get("qty", 0)), 8),
               "entry": round(float(trade.get("entry", 0)), 6),
               "exit": round(float(trade.get("exit", 0)), 6),
               "pnl": round(float(trade.get("pnl", 0)), 6),
               "reason": trade.get("reason", ""),
               "strategy": trade.get("strategy", "")}
        self._append(self.trades_path, TRADE_FIELDS, row)
        self._mirror("bot_trades", TRADE_FIELDS, row)
        return row

    def snapshot_daily(self, equity: float, realized: float,
                       unrealized: float, wins: int, losses: int,
                       date: str | None = None) -> dict:
        """Upsert today's row — one line per calendar day, last write wins."""
        row = {"date": date or dt.date.today().isoformat(),
               "equity": round(float(equity), 2),
               "realized": round(float(realized), 2),
               "unrealized": round(float(unrealized), 2),
               "wins": int(wins), "losses": int(losses)}
        days = [d for d in self.read_daily() if d["date"] != row["date"]]
        days.append(row)
        days.sort(key=lambda d: d["date"])
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.daily_path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=DAILY_FIELDS)
            w.writeheader()
            w.writerows(days)
        self._mirror("bot_daily", DAILY_FIELDS, row, key="date")
        return row

    def record_equity(self, equity: float, ts: str | None = None) -> None:
        self._append(self.equity_path, EQUITY_FIELDS,
                     {"ts": ts or _now(), "equity": round(float(equity), 2)})

    # ---- reads (dashboard + backtest report) --------------------------------
    def _read(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        with path.open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def read_trades(self, limit: int = 0) -> list[dict]:
        rows = []
        for r in self._read(self.trades_path):
            rows.append({**r, "qty": float(r["qty"]), "entry": float(r["entry"]),
                         "exit": float(r["exit"]), "pnl": float(r["pnl"])})
        return rows[-limit:] if limit else rows

    def read_daily(self) -> list[dict]:
        rows = []
        for r in self._read(self.daily_path):
            rows.append({"date": r["date"], "equity": float(r["equity"]),
                         "realized": float(r["realized"]),
                         "unrealized": float(r["unrealized"]),
                         "wins": int(r["wins"]), "losses": int(r["losses"])})
        return rows

    def read_equity_curve(self) -> list[dict]:
        return [{"ts": r["ts"], "equity": float(r["equity"])}
                for r in self._read(self.equity_path)]

    def peak_equity(self) -> float:
        curve = self.read_equity_curve()
        return max((r["equity"] for r in curve), default=0.0)

    def summary(self) -> dict:
        """The /summary payload: aggregates over the CSVs, safe when empty."""
        trades = self.read_trades()
        daily = self.read_daily()
        wins = [t["pnl"] for t in trades if t["pnl"] > 0]
        losses = [t["pnl"] for t in trades if t["pnl"] < 0]
        gross_win, gross_loss = sum(wins), abs(sum(losses))
        out = {
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(trades) if trades else None,
            "profit_factor": (gross_win / gross_loss) if gross_loss
            else (None if not wins else float("inf")),
            "realized_pnl": round(sum(t["pnl"] for t in trades), 2),
            "equity": daily[-1]["equity"] if daily else None,
            "cumulative_return_pct": None,
            "daily_delta": None,
        }
        if daily:
            first, last = daily[0]["equity"], daily[-1]["equity"]
            if first > 0:
                out["cumulative_return_pct"] = round(
                    (last / first - 1) * 100, 4)
            if len(daily) >= 2:
                out["daily_delta"] = round(last - daily[-2]["equity"], 2)
        return out

    # ---- optional trading.db mirror -----------------------------------------
    def _mirror(self, table: str, fields: list[str], row: dict,
                key: str | None = None) -> None:
        """Best-effort SQLite mirror; never raises into the trading path."""
        if not self.mirror_db:
            return
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(self.db_path)
            cols = ", ".join(f'"{f}"' for f in fields)
            unique = f', UNIQUE("{key}")' if key else ""
            con.execute(f'CREATE TABLE IF NOT EXISTS {table} '
                        f'(id INTEGER PRIMARY KEY, {cols}{unique})')
            placeholders = ", ".join("?" for _ in fields)
            verb = "INSERT OR REPLACE" if key else "INSERT"
            con.execute(
                f'{verb} INTO {table} ({cols}) VALUES ({placeholders})',
                [row[f] for f in fields])
            con.commit()
            con.close()
        except (sqlite3.Error, OSError):
            pass  # CSV is the source of truth; the mirror is convenience
