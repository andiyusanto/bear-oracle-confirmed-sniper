"""BearEngine: 7-gate signal evaluation + anti-decoy filters.

Gate order (all must pass):
  1. UTC hour NOT in blackout set
  2. Asset regime == BEAR (from RegimeMonitor cache)
  3. Oracle direction == DOWN
  4. Time remaining within snipe window (tiered by delta strength)
  5. Chainlink delta >= min_delta_pct (DOWN direction)
  6. Binance 1-min confirms DOWN
  7. NO token best_ask in [no_price_min, no_price_max]

Anti-decoy filters (after Gate 7):
  1. Ghost-zone hard block: TTL < snipe_exit_sec
  2. Volatility damper: 5-min CL range > volatility_damper_pct
  3. Consecutive tick check: last 3 ticks all below window open
  4. Micro-rebound veto: last 2 ticks show UP recovery > rebound_veto_ratio
  5. Liquidity check: NO token depth < no_book_depth_min_usd

Shadow mode: inject a ShadowLogger at construction time to record gate-level
rejection telemetry without any code-path branching in the caller.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from core.config import CFG
from core.models import OracleState, RegimeState, Signal, Token
from feeds.prices import PriceFeeds
from feeds.regime import RegimeMonitor

log = logging.getLogger("bear.engine")


class BearEngine:
    ASSET_FILL_COOLDOWN = 10.0

    def __init__(
        self,
        feeds: PriceFeeds,
        regime: RegimeMonitor,
        shadow=None,  # Optional[ShadowLogger] — import avoided to keep circular imports out
    ) -> None:
        self.feeds = feeds
        self.regime = regime
        self._shadow = shadow
        self._traded_windows: set[str] = set()
        self._asset_fill_ts: dict[str, float] = {}

    def evaluate(self, token: Token, is_live: bool = False) -> Optional[Signal]:
        now = time.time()
        ttl = token.end_ts - now
        asset = token.asset
        delta = 0.0  # populated after Gate 3; used in shadow records throughout

        def _reject(gate: str, reason: str) -> None:
            if self._shadow:
                regime_str = self.regime.current_regime(asset).value
                self._shadow.record(token, gate, reason, ttl, delta, regime_str)

        # ── GATE 1: UTC hour blackout ─────────────────────────────────
        utc_hour = datetime.now(tz=timezone.utc).hour
        if utc_hour in CFG.blackout_hours:
            _reject("GATE_1_BLACKOUT", f"utc_hour={utc_hour}")
            return None

        # ── Not already traded this window (de-dup guard, not logged) ─
        wkey = f"{asset}_{token.window_ts}"
        if wkey in self._traded_windows:
            return None

        if now - self._asset_fill_ts.get(asset, 0.0) < self.ASSET_FILL_COOLDOWN:
            return None

        # ── GATE 2: Regime == BEAR ────────────────────────────────────
        regime_state = self.regime.current_regime(asset)
        if regime_state != RegimeState.BEAR:
            diag = self.regime.all_diagnostics(asset)
            failed = []
            if not diag["ema"]:
                failed.append("EMA")
            if not diag["funding"]:
                failed.append("funding")
            if not diag["chainlink"]:
                failed.append("CL_1h")
            reason = f"regime={regime_state.value} failed=[{','.join(failed) or 'all'}]"
            _reject("GATE_2_REGIME", reason)
            return None

        # ── GATE 3: Oracle direction == DOWN ─────────────────────────
        self.feeds.capture_opening(asset, token.window_ts)
        delta = self.feeds.oracle_delta(asset, token.window_ts)
        if delta >= 0:
            _reject("GATE_3_NOT_DOWN", f"delta={delta:.4f}%")
            return None

        abs_delta = abs(delta)

        # ── GATE 4: Tiered snipe window ───────────────────────────────
        if abs_delta >= CFG.strong_delta_pct:
            max_entry = CFG.snipe_entry_strong_sec
            tier = "STRONG"
        elif abs_delta >= CFG.normal_delta_pct:
            max_entry = CFG.snipe_entry_normal_sec
            tier = "NORMAL"
        elif abs_delta >= CFG.weak_delta_pct:
            max_entry = CFG.snipe_entry_weak_sec
            tier = "WEAK"
        else:
            _reject(
                "GATE_5_DELTA_MIN",
                f"abs_delta={abs_delta:.4f}% < min={CFG.min_delta_pct:.4f}%",
            )
            return None

        if ttl > max_entry:
            _reject(
                "GATE_4_TTL_WINDOW",
                f"ttl={ttl:.0f}s > max_entry={max_entry:.0f}s tier={tier}",
            )
            return None

        # ── GATE 5: Delta >= min_delta_pct ────────────────────────────
        if abs_delta < CFG.min_delta_pct:
            _reject(
                "GATE_5_DELTA_MIN",
                f"abs_delta={abs_delta:.4f}% < min={CFG.min_delta_pct:.4f}%",
            )
            return None

        # ── GATE 6: Binance confirms DOWN ────────────────────────────
        if not self.feeds.binance_agrees(asset, "DOWN", token.window_ts):
            bn_age = time.time() - self.feeds.bn_ts.get(asset, 0)
            if bn_age < 30.0:
                _reject(
                    "GATE_6_BINANCE",
                    f"bn_disagrees bn_age={bn_age:.0f}s delta={delta:.4f}%",
                )
                log.debug(
                    "GATE6 SKIP %s: Binance disagrees (delta=%.4f%% bn_age=%.0fs)",
                    asset,
                    delta,
                    bn_age,
                )
                return None

        # ── GATE 7: NO token price in range ──────────────────────────
        price = token.book_price
        if price < CFG.no_price_min or price > CFG.no_price_max:
            _reject(
                "GATE_7_PRICE_RANGE",
                f"price=${price:.3f} range=[${CFG.no_price_min},${CFG.no_price_max}]",
            )
            return None

        # ── ANTI-DECOY 1: Ghost-zone hard block ──────────────────────
        if ttl < CFG.snipe_exit_sec:
            _reject("DECOY_1_GHOST", f"ttl={ttl:.0f}s < {CFG.snipe_exit_sec:.0f}s")
            log.debug(
                "GHOST BLOCK %s: TTL=%.0fs < %.0fs", asset, ttl, CFG.snipe_exit_sec
            )
            return None

        # ── ANTI-DECOY 2: Volatility damper ──────────────────────────
        vol = self.feeds.five_min_range_pct(asset)
        if vol > CFG.volatility_damper_pct * 100:
            _reject(
                "DECOY_2_VOLATILITY",
                f"5m_range={vol:.4f}% > {CFG.volatility_damper_pct * 100:.4f}%",
            )
            log.debug(
                "VOL DAMP %s: 5m range=%.4f%% > %.4f%%",
                asset,
                vol,
                CFG.volatility_damper_pct * 100,
            )
            return None

        # ── ANTI-DECOY 3: Consecutive down ticks ─────────────────────
        n = CFG.consecutive_down_ticks_required
        if not self.feeds.consecutive_down_ticks(asset, token.window_ts, n):
            _reject("DECOY_3_TICKS", f"< {n} consecutive ticks below window open")
            log.debug("TICK FAIL %s: < %d consecutive ticks below open", asset, n)
            return None

        # ── ANTI-DECOY 4: Micro-rebound veto ─────────────────────────
        history = self.feeds._price_history.get(asset, [])
        if len(history) >= 2:
            opening = self.feeds.openings.get(asset, {}).get(token.window_ts, 0)
            if opening > 0:
                last2 = [p for _, p in history[-2:]]
                low = min(last2)
                drop = opening - low
                recovery = last2[-1] - low
                if drop > 0 and recovery / drop > CFG.rebound_veto_ratio:
                    pct = recovery / drop * 100
                    _reject(
                        "DECOY_4_REBOUND",
                        f"recovery={pct:.1f}% of drop > {CFG.rebound_veto_ratio * 100:.0f}%",
                    )
                    log.debug("REBOUND VETO %s: recovery=%.1f%% of drop", asset, pct)
                    return None

        # ── ANTI-DECOY 5: Liquidity check ────────────────────────────
        if token.book_depth_usd < CFG.no_book_depth_min_usd:
            _reject(
                "DECOY_5_DEPTH",
                f"depth=${token.book_depth_usd:.0f} < ${CFG.no_book_depth_min_usd:.0f}",
            )
            log.debug(
                "DEPTH SKIP %s: depth=$%.0f < $%.0f",
                asset,
                token.book_depth_usd,
                CFG.no_book_depth_min_usd,
            )
            return None

        # ── Build signal ──────────────────────────────────────────────
        opening = self.feeds.openings.get(asset, {}).get(token.window_ts, 0)
        oracle = OracleState(
            asset=asset,
            window_ts=token.window_ts,
            opening_price=opening,
            current_price=self.feeds.best_price(asset),
            delta_pct=delta,
            oracle_says="DOWN",
            binance_agrees=self.feeds.binance_agrees(asset, "DOWN", token.window_ts),
            last_update=time.time(),
        )

        ticks = self.feeds.consecutive_down_ticks(asset, token.window_ts, n)

        # Log the PASS in shadow mode — this is the would-be trade
        if self._shadow:
            regime_str = self.regime.current_regime(asset).value
            self._shadow.record(
                token,
                "PASS",
                f"delta={delta:.4f}% tier={tier} price=${price:.3f} ttl={ttl:.0f}s",
                ttl,
                delta,
                regime_str,
            )

        log.info(
            "SIGNAL %s NO @ $%.3f | delta=%.4f%% tier=%s ttl=%.0fs depth=$%.0f",
            asset,
            price,
            delta,
            tier,
            ttl,
            token.book_depth_usd,
        )

        return Signal(
            token=token,
            oracle=oracle,
            entry_price=price,
            size_usdc=CFG.stake_per_trade_usd,
            time_remaining=ttl,
            consecutive_ticks=n if ticks else 0,
            delta_tier=tier,
        )

    def mark_traded(self, asset: str, window_ts: int) -> None:
        now = time.time()
        self._traded_windows.add(f"{asset}_{window_ts}")
        self._asset_fill_ts[asset] = now
        self._traded_windows = {
            w for w in self._traded_windows if int(w.split("_")[1]) + 1800 > now
        }
