"""Regime monitor: 15-minute bear-regime gating (Gate 0).

Bear regime requires ALL THREE conditions per asset:
  A) Binance 4h candle: current price < EMA(20) of last 20 closes
  B) Binance perpetual funding rate: negative for ≥ 2 of last 3 intervals
  C) Chainlink 1h net: DOWN ≥ 0.3% over last 60 minutes

Each asset is evaluated independently. Any failure → NEUTRAL (no trades).
Transitions are written to SQLite. Errors default to NEUTRAL (fail-safe).
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

import aiohttp

from core.config import CFG
from core.database import Database
from core.models import RegimeState

if TYPE_CHECKING:
    from feeds.prices import PriceFeeds

log = logging.getLogger("bear.regime")

# Binance futures REST endpoints
_KLINES_URL = "{base}/fapi/v1/klines"
_FUNDING_URL = "{base}/fapi/v1/fundingRate"

# Futures symbol mapping — USDT-M perpetuals
_SYMBOLS: dict[str, str] = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "HYPE": "HYPEUSDT",
}


class RegimeMonitor:
    """Maintains bear-regime state for each asset via a 15-minute async cycle."""

    def __init__(
        self,
        db: Database,
        feeds: "PriceFeeds",
        on_transition: Optional[Callable[..., Awaitable[None]]] = None,
    ) -> None:
        self._db = db
        self._feeds = feeds
        self._on_transition = on_transition
        # In-memory cache — updated every regime cycle, read by BearEngine
        self._cache: dict[str, RegimeState] = {
            a: RegimeState.NEUTRAL for a in CFG.assets
        }
        # Diagnostic booleans from last check — exposed for dashboard
        self._last_ema: dict[str, bool] = {a: False for a in CFG.assets}
        self._last_funding: dict[str, bool] = {a: False for a in CFG.assets}
        self._last_chainlink: dict[str, bool] = {a: False for a in CFG.assets}
        self._last_check_ts: dict[str, float] = {a: 0.0 for a in CFG.assets}
        self._running = False

    # ── Public API ───────────────────────────────────────────────────

    def current_regime(self, asset: str) -> RegimeState:
        """Current cached regime for an asset — O(1), safe to call from hot loop."""
        return self._cache.get(asset, RegimeState.NEUTRAL)

    def all_diagnostics(self, asset: str) -> dict:
        """Last-cycle pass/fail for each sub-check — used by dashboard."""
        return {
            "ema": self._last_ema.get(asset, False),
            "funding": self._last_funding.get(asset, False),
            "chainlink": self._last_chainlink.get(asset, False),
            "checked_at": self._last_check_ts.get(asset, 0.0),
        }

    async def check_regime(self, asset: str) -> RegimeState:
        """Evaluate all three bear conditions for one asset.

        Returns the new RegimeState. Writes to DB only on transitions.
        Never raises — logs errors and returns NEUTRAL on any failure.
        """
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                ema_pass, funding_pass, cl_pass = await asyncio.gather(
                    self._binance_4h_ema(session, asset),
                    self._funding_rate_negative(session, asset),
                    self._chainlink_1h_net(asset),
                )
        except Exception as exc:
            log.error(
                "Regime check error for %s: %s — defaulting to NEUTRAL", asset, exc
            )
            ema_pass = funding_pass = cl_pass = False

        self._last_ema[asset] = ema_pass
        self._last_funding[asset] = funding_pass
        self._last_chainlink[asset] = cl_pass
        self._last_check_ts[asset] = time.time()

        all_pass = ema_pass and funding_pass and cl_pass
        new_state = RegimeState.BEAR if all_pass else RegimeState.NEUTRAL
        prev_state = self._cache.get(asset, RegimeState.NEUTRAL)

        if new_state != prev_state:
            reason = self._build_reason(ema_pass, funding_pass, cl_pass)
            log.warning(
                "REGIME %s: %s → %s (%s)",
                asset,
                prev_state.value,
                new_state.value,
                reason,
            )
            await self._fire_transition(
                asset, prev_state, new_state, reason, ema_pass, funding_pass, cl_pass
            )
        else:
            log.info(
                "REGIME %s: %s (unchanged) ema=%s fund=%s cl=%s",
                asset,
                new_state.value,
                ema_pass,
                funding_pass,
                cl_pass,
            )

        return new_state

    async def run_loop(self) -> None:
        """15-minute async cycle — run as an independent asyncio task.

        Checks all assets on every cycle and updates the in-memory cache.
        The signal engine reads from the cache without waiting on this loop.
        """
        self._running = True
        log.info("RegimeMonitor started (interval=%ds)", CFG.regime_check_interval_sec)

        # Check immediately on startup so cache is populated before trading begins
        await self._check_all()

        while self._running:
            await asyncio.sleep(CFG.regime_check_interval_sec)
            await self._check_all()

    def stop(self) -> None:
        self._running = False

    # ── Internal helpers ─────────────────────────────────────────────

    async def _fire_transition(
        self,
        asset: str,
        prev_state: RegimeState,
        new_state: RegimeState,
        reason: str,
        ema_pass: bool,
        funding_pass: bool,
        cl_pass: bool,
    ) -> None:
        """Write transition to DB and fire optional callback (e.g. Telegram)."""
        self._db.log_regime_transition(
            asset=asset,
            prev_state=prev_state.value,
            new_state=new_state.value,
            reason=reason,
            ema_pass=ema_pass,
            funding_pass=funding_pass,
            chainlink_pass=cl_pass,
        )
        self._cache[asset] = new_state
        if self._on_transition:
            try:
                await self._on_transition(
                    asset=asset,
                    prev_state=prev_state.value,
                    new_state=new_state.value,
                    reason=reason,
                    ema_pass=ema_pass,
                    funding_pass=funding_pass,
                    chainlink_pass=cl_pass,
                )
            except Exception as exc:
                log.debug("on_transition callback error: %s", exc)

    async def _check_all(self) -> None:
        """Check all assets in parallel — one aiohttp session shared."""
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                tasks = [self._check_one(session, asset) for asset in CFG.assets]
                await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as exc:
            log.error("_check_all session error: %s", exc)

    async def _check_one(self, session: aiohttp.ClientSession, asset: str) -> None:
        """Check one asset using a shared session."""
        try:
            ema_pass, funding_pass, cl_pass = await asyncio.gather(
                self._binance_4h_ema(session, asset),
                self._funding_rate_negative(session, asset),
                self._chainlink_1h_net(asset),
            )
        except Exception as exc:
            log.error(
                "Regime check error for %s: %s — defaulting to NEUTRAL", asset, exc
            )
            ema_pass = funding_pass = cl_pass = False

        self._last_ema[asset] = ema_pass
        self._last_funding[asset] = funding_pass
        self._last_chainlink[asset] = cl_pass
        self._last_check_ts[asset] = time.time()

        all_pass = ema_pass and funding_pass and cl_pass
        new_state = RegimeState.BEAR if all_pass else RegimeState.NEUTRAL
        prev_state = self._cache.get(asset, RegimeState.NEUTRAL)

        if new_state != prev_state:
            reason = self._build_reason(ema_pass, funding_pass, cl_pass)
            log.warning(
                "REGIME %s: %s → %s (%s)",
                asset,
                prev_state.value,
                new_state.value,
                reason,
            )
            await self._fire_transition(
                asset, prev_state, new_state, reason, ema_pass, funding_pass, cl_pass
            )
        else:
            log.info(
                "REGIME %s: %s (unchanged) ema=%s fund=%s cl=%s",
                asset,
                new_state.value,
                ema_pass,
                funding_pass,
                cl_pass,
            )

    async def _binance_4h_ema(self, session: aiohttp.ClientSession, asset: str) -> bool:
        """True when current price is below EMA(20) of 4h closes.

        Fetches 21 completed 4h klines from Binance Futures.
        Computes EMA(20) from the first 20 closes, then checks whether
        the 21st close (most recent) is below that EMA.

        Falls back to False (fail-safe = NEUTRAL) on any API error.
        """
        symbol = _SYMBOLS.get(asset)
        if not symbol:
            return False

        url = _KLINES_URL.format(base=CFG.binance_rest)
        params = {"symbol": symbol, "interval": "4h", "limit": 21}

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    log.warning("4h klines HTTP %d for %s", resp.status, asset)
                    return False
                data = await resp.json(content_type=None)
        except Exception as exc:
            log.warning("4h klines fetch error for %s: %s", asset, exc)
            return False

        if not data or len(data) < 21:
            log.warning(
                "4h klines: insufficient data for %s (%d candles)",
                asset,
                len(data) if data else 0,
            )
            return False

        closes = [float(k[4]) for k in data]  # index 4 = close price

        # Build EMA(20) from first 20 candles; candle 21 is the current period
        k_factor = 2.0 / (CFG.ema_period_4h + 1)
        ema = closes[0]
        for price in closes[1 : CFG.ema_period_4h]:
            ema = price * k_factor + ema * (1.0 - k_factor)

        # Prefer live Chainlink price; fall back to last kline close
        current = self._feeds.best_price(asset)
        if current <= 0:
            current = closes[-1]

        result = current < ema
        log.info(
            "REGIME CHECK %s EMA: price=$%.2f ema=$%.2f → %s",
            asset,
            current,
            ema,
            "PASS" if result else "FAIL",
        )
        return result

    async def _funding_rate_negative(
        self, session: aiohttp.ClientSession, asset: str
    ) -> bool:
        """True when ≥ 2 of the last 3 funding intervals are negative.

        Binance USDT-M perpetuals settle funding every 8 hours (3/day).
        A sustained negative funding rate means shorts are being paid —
        the market is positioning bearishly.

        Falls back to False (fail-safe = NEUTRAL) on any API error.
        """
        symbol = _SYMBOLS.get(asset)
        if not symbol:
            return False

        url = _FUNDING_URL.format(base=CFG.binance_rest)
        params = {"symbol": symbol, "limit": 3}

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    log.warning("Funding rate HTTP %d for %s", resp.status, asset)
                    return False
                data = await resp.json(content_type=None)
        except Exception as exc:
            log.warning("Funding rate fetch error for %s: %s", asset, exc)
            return False

        if not data:
            log.warning("Funding rate: empty response for %s", asset)
            return False

        rates = [float(r.get("fundingRate", 0)) for r in data]
        negative_count = sum(1 for r in rates if r < CFG.funding_rate_bear_threshold)
        result = negative_count >= CFG.funding_intervals_required

        log.info(
            "REGIME CHECK %s Funding: rates=%s negative=%d/%d → %s",
            asset,
            [f"{r:.6f}" for r in rates],
            negative_count,
            CFG.funding_intervals_required,
            "PASS" if result else "FAIL",
        )
        return result

    async def _chainlink_1h_net(self, asset: str) -> bool:
        """True when Chainlink price is DOWN ≥ 0.3% over the last 60 minutes.

        Uses the price history buffer maintained by PriceFeeds, which records
        every Chainlink tick with a timestamp. Finds the price closest to
        T-60min and compares to the current best price.

        Falls back to False (fail-safe = NEUTRAL) when insufficient history.
        """
        current = self._feeds.best_price(asset)
        if current <= 0:
            log.debug("CL 1h net %s: no current price", asset)
            return False

        target_ts = time.time() - 3600.0
        past_price = self._feeds.price_at(asset, target_ts)

        if past_price <= 0:
            log.info(
                "REGIME CHECK %s CL1h: no history near T-60min (bot may have just started)",
                asset,
            )
            return False

        net_pct = (current - past_price) / past_price * 100
        result = net_pct <= CFG.chainlink_1h_net_pct_bear * 100

        log.info(
            "REGIME CHECK %s CL1h: now=$%.2f 60min_ago=$%.2f net=%.4f%% → %s",
            asset,
            current,
            past_price,
            net_pct,
            "PASS" if result else "FAIL",
        )
        return result

    @staticmethod
    def _build_reason(ema_pass: bool, funding_pass: bool, cl_pass: bool) -> str:
        """Human-readable reason string for regime log."""
        failed = []
        if not ema_pass:
            failed.append("price>EMA(20)")
        if not funding_pass:
            failed.append("funding_not_negative")
        if not cl_pass:
            failed.append("CL_1h_net_not_down")
        if not failed:
            return "all_conditions_met"
        return " | ".join(failed)

    # ── Convenience property for dashboard ──────────────────────────

    @property
    def states(self) -> dict[str, RegimeState]:
        """Snapshot of current regime per asset — for dashboard rendering."""
        return dict(self._cache)
