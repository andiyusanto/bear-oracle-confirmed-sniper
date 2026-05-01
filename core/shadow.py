"""Shadow mode instrumentation: gate-level rejection logging.

ShadowLogger is injected into BearEngine at construction time.  Every gate
that rejects a signal calls logger.record(), which:
  1. Increments in-memory counters (read by dashboard in real-time)
  2. Appends to a buffer (flushed to SQLite every N records by the main loop)

Nothing in the hot evaluate() path touches SQLite directly.
"""

import time
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.database import Database
    from core.models import Token

# Canonical gate names — keep in display order
GATES = [
    "GATE_1_BLACKOUT",
    "GATE_2_REGIME",
    "GATE_3_NOT_DOWN",
    "GATE_4_TTL_WINDOW",
    "GATE_5_DELTA_MIN",
    "GATE_6_BINANCE",
    "GATE_7_PRICE_RANGE",
    "DECOY_1_GHOST",
    "DECOY_2_VOLATILITY",
    "DECOY_3_TICKS",
    "DECOY_4_REBOUND",
    "DECOY_5_DEPTH",
    "PASS",
]

GATE_LABELS = {
    "GATE_1_BLACKOUT": "Gate 1 — Blackout hour",
    "GATE_2_REGIME": "Gate 2 — Regime != BEAR",
    "GATE_3_NOT_DOWN": "Gate 3 — Oracle not DOWN",
    "GATE_4_TTL_WINDOW": "Gate 4 — TTL out of window",
    "GATE_5_DELTA_MIN": "Gate 5 — Delta too small",
    "GATE_6_BINANCE": "Gate 6 — Binance disagrees",
    "GATE_7_PRICE_RANGE": "Gate 7 — NO price out of range",
    "DECOY_1_GHOST": "Decoy 1 — Ghost zone (TTL<20s)",
    "DECOY_2_VOLATILITY": "Decoy 2 — Volatility damper",
    "DECOY_3_TICKS": "Decoy 3 — Consecutive ticks",
    "DECOY_4_REBOUND": "Decoy 4 — Micro-rebound veto",
    "DECOY_5_DEPTH": "Decoy 5 — Depth < $100",
    "PASS": "PASS — would have fired",
}


@dataclass
class ShadowRecord:
    asset: str
    token_id: str
    window_ts: int
    ttl: float
    no_price: float
    oracle_delta: float
    regime: str
    gate_blocked: str
    reason: str
    timestamp: float = field(default_factory=time.time)


class ShadowLogger:
    """Collects gate-level rejection telemetry without hitting SQLite on the hot path."""

    FLUSH_BATCH = 50  # write to DB every N records

    def __init__(self) -> None:
        self._counts: dict[str, int] = {g: 0 for g in GATES}
        self._buffer: list[ShadowRecord] = []
        self._total = 0
        self._session_start = time.time()
        # Track the last-seen values per asset for dashboard display
        self._last_delta: dict[str, float] = {}
        self._last_regime: dict[str, str] = {}

    # ── Hot-path API ─────────────────────────────────────────────────

    def record(
        self,
        token: "Token",
        gate: str,
        reason: str,
        ttl: float,
        delta: float,
        regime: str,
    ) -> None:
        """Record one gate evaluation.  Called from BearEngine.evaluate()."""
        self._counts[gate] = self._counts.get(gate, 0) + 1
        self._total += 1
        self._last_delta[token.asset] = delta
        self._last_regime[token.asset] = regime
        self._buffer.append(
            ShadowRecord(
                asset=token.asset,
                token_id=token.token_id,
                window_ts=token.window_ts,
                ttl=round(ttl, 1),
                no_price=round(token.book_price, 4),
                oracle_delta=round(delta, 6),
                regime=regime,
                gate_blocked=gate,
                reason=reason,
            )
        )

    # ── Batch flush ──────────────────────────────────────────────────

    def flush(self, db: "Database") -> int:
        """Write buffered records to SQLite.  Returns number written."""
        if not self._buffer:
            return 0
        batch = self._buffer[: self.FLUSH_BATCH]
        self._buffer = self._buffer[self.FLUSH_BATCH :]
        db.save_shadow_batch(batch)
        return len(batch)

    def flush_all(self, db: "Database") -> int:
        """Flush the entire buffer — call on shutdown."""
        total = 0
        while self._buffer:
            total += self.flush(db)
        return total

    # ── Dashboard helpers ────────────────────────────────────────────

    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    def total(self) -> int:
        return self._total

    def passes(self) -> int:
        return self._counts.get("PASS", 0)

    def session_elapsed(self) -> float:
        return time.time() - self._session_start

    def rate_per_min(self) -> float:
        elapsed = self.session_elapsed()
        return self._total / (elapsed / 60) if elapsed > 0 else 0.0

    def last_delta(self, asset: str) -> Optional[float]:
        return self._last_delta.get(asset)

    def last_regime(self, asset: str) -> str:
        return self._last_regime.get(asset, "UNKNOWN")
