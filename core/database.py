"""Thread-safe SQLite database for trade and regime storage."""

import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from core.models import Trade


class Database:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                asset TEXT NOT NULL,
                direction TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                size_usdc REAL NOT NULL,
                oracle_delta REAL DEFAULT 0,
                regime_state TEXT DEFAULT 'BEAR',
                pnl REAL DEFAULT 0,
                status TEXT DEFAULT 'OPEN',
                mode TEXT DEFAULT 'PAPER',
                opened_at REAL NOT NULL,
                closed_at REAL,
                window_ts INTEGER NOT NULL,
                time_remaining REAL DEFAULT 0,
                binance_price REAL DEFAULT 0,
                chainlink_price REAL DEFAULT 0,
                opening_price REAL DEFAULT 0,
                duration_sec INTEGER DEFAULT 300,
                condition_id TEXT DEFAULT '',
                delta_tier TEXT DEFAULT 'WEAK'
            );
            CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
            CREATE INDEX IF NOT EXISTS idx_trades_opened ON trades(opened_at);
            CREATE INDEX IF NOT EXISTS idx_trades_window ON trades(window_ts);
            CREATE INDEX IF NOT EXISTS idx_trades_asset  ON trades(asset);

            CREATE TABLE IF NOT EXISTS regime_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset TEXT NOT NULL,
                prev_state TEXT NOT NULL,
                new_state TEXT NOT NULL,
                reason TEXT NOT NULL,
                ema_pass INTEGER NOT NULL,
                funding_pass INTEGER NOT NULL,
                chainlink_pass INTEGER NOT NULL,
                timestamp REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_regime_asset ON regime_log(asset);
            CREATE INDEX IF NOT EXISTS idx_regime_ts ON regime_log(timestamp);

            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                portfolio REAL NOT NULL,
                clob_balance REAL,
                reason TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS shadow_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset TEXT NOT NULL,
                token_id TEXT NOT NULL,
                window_ts INTEGER NOT NULL,
                ttl REAL NOT NULL,
                no_price REAL NOT NULL,
                oracle_delta REAL NOT NULL,
                regime TEXT NOT NULL,
                gate_blocked TEXT NOT NULL,
                reason TEXT NOT NULL,
                timestamp REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_shadow_ts ON shadow_log(timestamp);
            CREATE INDEX IF NOT EXISTS idx_shadow_gate ON shadow_log(gate_blocked);
            CREATE INDEX IF NOT EXISTS idx_shadow_asset ON shadow_log(asset);
        """)

    # ── Trade methods ────────────────────────────────────────────────

    def save_trade(self, t: Trade) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO trades VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )""",
                (
                    t.id,
                    t.asset,
                    t.direction,
                    t.side,
                    t.entry_price,
                    t.size_usdc,
                    t.oracle_delta,
                    t.regime_state,
                    t.pnl,
                    t.status,
                    t.mode,
                    t.opened_at,
                    t.closed_at,
                    t.window_ts,
                    t.time_remaining,
                    t.binance_price,
                    t.chainlink_price,
                    t.opening_price,
                    t.duration_sec,
                    t.condition_id,
                    t.delta_tier,
                ),
            )
            self.conn.commit()

    def close_trade(self, tid: str, pnl: float, status: str = "EXPIRED") -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE trades SET pnl=?, status=?, closed_at=? WHERE id=?",
                (round(pnl, 6), status, time.time(), tid),
            )
            self.conn.commit()

    def open_trades(self) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM trades WHERE status='OPEN' ORDER BY opened_at"
        )
        return self._rows(cur)

    def recent(self, n: int = 15) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM trades ORDER BY opened_at DESC LIMIT ?", (n,)
        )
        return self._rows(cur)

    def daily_pnl(self) -> float:
        ts = (
            datetime.now(tz=timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        cur = self.conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE opened_at >= ?", (ts,)
        )
        return cur.fetchone()[0]

    def daily_count(self) -> int:
        ts = (
            datetime.now(tz=timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM trades WHERE opened_at >= ?", (ts,)
        )
        return cur.fetchone()[0]

    def lifetime_stats(self) -> dict:
        cur = self.conn.execute("""
            SELECT COUNT(*),
                   COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(pnl), 0),
                   COALESCE(AVG(CASE WHEN pnl > 0 THEN pnl END), 0),
                   COALESCE(AVG(CASE WHEN pnl <= 0 THEN pnl END), 0),
                   COALESCE(MAX(pnl), 0),
                   COALESCE(MIN(pnl), 0)
            FROM trades WHERE status IN ('EXPIRED', 'CANCELLED')
        """)
        total, wins, pnl, avg_w, avg_l, max_w, max_l = cur.fetchone()
        return {
            "total": total,
            "wins": wins,
            "pnl": round(pnl, 4),
            "wr": round(wins / total * 100, 1) if total else 0.0,
            "avg_win": round(avg_w, 4),
            "avg_loss": round(avg_l, 4),
            "max_win": round(max_w, 4),
            "max_loss": round(max_l, 4),
            "expectancy": round(pnl / total, 4) if total else 0.0,
        }

    def rolling_wr(self, n: int = 20) -> Optional[float]:
        """Win rate over the last n closed trades. None if fewer than n trades."""
        cur = self.conn.execute(
            "SELECT pnl FROM trades WHERE status='EXPIRED' "
            "ORDER BY closed_at DESC LIMIT ?",
            (n,),
        )
        rows = cur.fetchall()
        if len(rows) < n:
            return None
        wins = sum(1 for (pnl,) in rows if pnl > 0)
        return wins / n

    # ── Regime log methods ───────────────────────────────────────────

    def log_regime_transition(
        self,
        asset: str,
        prev_state: str,
        new_state: str,
        reason: str,
        ema_pass: bool,
        funding_pass: bool,
        chainlink_pass: bool,
    ) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO regime_log "
                "(asset, prev_state, new_state, reason, ema_pass, funding_pass, "
                "chainlink_pass, timestamp) VALUES (?,?,?,?,?,?,?,?)",
                (
                    asset,
                    prev_state,
                    new_state,
                    reason,
                    int(ema_pass),
                    int(funding_pass),
                    int(chainlink_pass),
                    time.time(),
                ),
            )
            self.conn.commit()

    def recent_regime_log(self, n: int = 20) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM regime_log ORDER BY timestamp DESC LIMIT ?", (n,)
        )
        return self._rows(cur)

    def regime_duration(self, asset: str) -> Optional[float]:
        """Seconds the asset has been in its current state, or None if no log."""
        cur = self.conn.execute(
            "SELECT timestamp FROM regime_log WHERE asset=? ORDER BY timestamp DESC LIMIT 1",
            (asset,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return time.time() - row[0]

    # ── Portfolio snapshots ──────────────────────────────────────────

    def save_snapshot(
        self,
        portfolio: float,
        reason: str,
        clob_balance: Optional[float] = None,
    ) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO portfolio_snapshots (timestamp, portfolio, clob_balance, reason) "
                "VALUES (?,?,?,?)",
                (time.time(), round(portfolio, 4), clob_balance, reason),
            )
            self.conn.commit()

    # ── Shadow log methods ───────────────────────────────────────────

    def save_shadow_batch(self, records: list) -> None:
        """Batch-insert ShadowRecord objects.  Called by ShadowLogger.flush()."""
        with self._lock:
            self.conn.executemany(
                "INSERT INTO shadow_log "
                "(asset, token_id, window_ts, ttl, no_price, oracle_delta, "
                "regime, gate_blocked, reason, timestamp) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        r.asset,
                        r.token_id,
                        r.window_ts,
                        r.ttl,
                        r.no_price,
                        r.oracle_delta,
                        r.regime,
                        r.gate_blocked,
                        r.reason,
                        r.timestamp,
                    )
                    for r in records
                ],
            )
            self.conn.commit()

    def shadow_gate_counts(self, since_ts: float = 0.0) -> dict[str, int]:
        """Per-gate rejection counts since a timestamp (0 = all time)."""
        cur = self.conn.execute(
            "SELECT gate_blocked, COUNT(*) FROM shadow_log "
            "WHERE timestamp >= ? GROUP BY gate_blocked",
            (since_ts,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}

    def shadow_pass_samples(self, n: int = 20) -> list[dict]:
        """Most-recent PASS records — the would-be trades."""
        cur = self.conn.execute(
            "SELECT * FROM shadow_log WHERE gate_blocked='PASS' "
            "ORDER BY timestamp DESC LIMIT ?",
            (n,),
        )
        return self._rows(cur)

    def shadow_recent(self, n: int = 50) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM shadow_log ORDER BY timestamp DESC LIMIT ?", (n,)
        )
        return self._rows(cur)

    def shadow_total(self, since_ts: float = 0.0) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM shadow_log WHERE timestamp >= ?", (since_ts,)
        )
        return cur.fetchone()[0]

    # ── Helpers ──────────────────────────────────────────────────────

    def _rows(self, cur: sqlite3.Cursor) -> list[dict]:
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
