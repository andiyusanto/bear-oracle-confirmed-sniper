"""Real-time price feeds: Chainlink RTDS + Binance WebSocket.

Adapted from bull bot (oracle-confirmed-sniper/feeds/prices.py).

Bear-bot additions:
  price_at()                 — historical price at an arbitrary timestamp
  chainlink_hourly_net()     — signed % change over last 60 minutes
  consecutive_down_ticks()   — whether last N ticks were all below window open
  5-minute range()           — for volatility damper filter
"""

import asyncio
import json
import logging
import time

import websockets

from core.config import CFG

log = logging.getLogger("bear.feeds")


class PriceFeeds:
    """Dual-source price feed: Chainlink (oracle/resolution) + Binance."""

    def __init__(self) -> None:
        self.chainlink: dict[str, float] = {}
        self.binance: dict[str, float] = {}
        self.cl_ts: dict[str, float] = {}
        self.bn_ts: dict[str, float] = {}
        self.openings: dict[str, dict[int, float]] = {}  # {asset: {window_ts: price}}
        self._skipped_windows: set[tuple[str, int]] = set()
        self._running = False
        self._rtds_reconnects = 0
        self._binance_reconnects = 0

        # Rolling 10-minute price history: {asset: [(timestamp, price), ...]}
        self._price_history: dict[str, list[tuple[float, float]]] = {}

        self._rtds_last_msg_ts: float = 0.0
        self._binance_last_msg_ts: float = 0.0
        _WS_SILENCE_TIMEOUT = 60

        for a in CFG.assets:
            self.chainlink[a] = 0.0
            self.binance[a] = 0.0
            self.cl_ts[a] = 0.0
            self.bn_ts[a] = 0.0
            self.openings[a] = {}
            self._price_history[a] = []

    _WS_SILENCE_TIMEOUT = 60

    @property
    def is_ready(self) -> bool:
        return any(self.binance[a] > 0 for a in CFG.assets)

    def best_price(self, asset: str) -> float:
        """Chainlink if fresh (<30s), else Binance."""
        if self.chainlink[asset] > 0 and time.time() - self.cl_ts.get(asset, 0) < 30:
            return self.chainlink[asset]
        return self.binance.get(asset, 0.0)

    def chainlink_staleness(self, asset: str) -> float:
        return time.time() - self.cl_ts.get(asset, 0)

    # ── Opening price management ─────────────────────────────────────

    def capture_opening(self, asset: str, window_ts: int) -> None:
        if window_ts in self.openings.get(asset, {}):
            return
        if (asset, window_ts) in self._skipped_windows:
            return

        price = self.price_at(asset, float(window_ts))

        if price > 0:
            self.openings[asset][window_ts] = price
            log.info("OPEN %s $%.2f (window %d)", asset, price, window_ts)
        else:
            elapsed = time.time() - window_ts
            if elapsed > 60:
                self._skipped_windows.add((asset, window_ts))
                log.warning(
                    "OPEN %s skipped (window %d, %.0fs elapsed — opening unknowable)",
                    asset,
                    window_ts,
                    elapsed,
                )
                return
            current = self.best_price(asset)
            if current > 0:
                self.openings[asset][window_ts] = current
                log.warning(
                    "OPEN %s $%.2f (window %d, src=fallback)", asset, current, window_ts
                )

        if len(self.openings[asset]) > 30:
            for k in sorted(self.openings[asset])[:-30]:
                del self.openings[asset][k]

    def set_opening_from_gamma(
        self, asset: str, window_ts: int, gamma_price: float
    ) -> None:
        if window_ts in self.openings.get(asset, {}):
            return
        if gamma_price > 0:
            self.openings[asset][window_ts] = gamma_price
            log.info(
                "OPEN %s $%.2f (window %d, src=gamma)", asset, gamma_price, window_ts
            )

    # ── Oracle delta ─────────────────────────────────────────────────

    def oracle_delta(self, asset: str, window_ts: int) -> float:
        """Signed % delta: current oracle price vs opening price."""
        opening = self.openings.get(asset, {}).get(window_ts, 0)
        if opening <= 0:
            return 0.0
        current = self.best_price(asset)
        if current <= 0:
            return 0.0
        return (current - opening) / opening * 100

    def oracle_delta_at(self, asset: str, window_ts: int, lookback_sec: float) -> float:
        """Signed % delta at a past point vs the window opening — for momentum checks."""
        opening = self.openings.get(asset, {}).get(window_ts, 0)
        if opening <= 0:
            return 0.0
        target_ts = time.time() - lookback_sec
        past_price = self.price_at(asset, target_ts)
        if past_price <= 0:
            return 0.0
        return (past_price - opening) / opening * 100

    # ── Price history utilities (bear-bot additions) ─────────────────

    def price_at(
        self, asset: str, target_ts: float, max_gap_sec: float = 30.0
    ) -> float:
        """Price closest to target_ts within max_gap_sec, or 0.0 if none found.

        Used by:
          - capture_opening() for window boundary interpolation
          - RegimeMonitor._chainlink_1h_net() for 60-min lookback
          - oracle_delta_at() for momentum checks
        """
        history = self._price_history.get(asset, [])
        if not history:
            return 0.0
        best_price = 0.0
        best_gap = float("inf")
        for ts, price in history:
            gap = abs(ts - target_ts)
            if gap < best_gap:
                best_gap = gap
                best_price = price
        return best_price if best_gap <= max_gap_sec else 0.0

    def chainlink_hourly_net(self, asset: str) -> float:
        """Signed % change in Chainlink price over the last 60 minutes.

        Returns 0.0 when history is insufficient (e.g. bot just started).
        """
        current = self.best_price(asset)
        if current <= 0:
            return 0.0
        past = self.price_at(asset, time.time() - 3600.0)
        if past <= 0:
            return 0.0
        return (current - past) / past * 100

    def five_min_range_pct(self, asset: str) -> float:
        """High-to-low range as a % over the last 5 minutes.

        Used by the volatility damper filter: high-volatility chop collapses
        edge on NO tokens because oracle moves are transient not directional.
        Returns 0.0 when insufficient history.
        """
        now = time.time()
        cutoff = now - 300.0
        recent = [p for ts, p in self._price_history.get(asset, []) if ts >= cutoff]
        if not recent:
            return 0.0
        return (max(recent) - min(recent)) / min(recent) * 100

    def consecutive_down_ticks(self, asset: str, window_ts: int, n: int) -> bool:
        """True when the last n Chainlink ticks in history are all below window_open.

        A 'tick' here is any price record in _price_history. Only Chainlink
        data is recorded in _price_history; Binance data from _parse_binance
        is excluded so we're purely testing the oracle direction.

        Returns False when fewer than n ticks exist or no opening is captured.
        """
        opening = self.openings.get(asset, {}).get(window_ts, 0)
        if opening <= 0:
            return False
        # Most-recent n prices from Chainlink history
        history = self._price_history.get(asset, [])
        if len(history) < n:
            return False
        last_n = [p for _, p in history[-n:]]
        return all(p < opening for p in last_n)

    def binance_agrees(self, asset: str, oracle_says: str, window_ts: int = 0) -> bool:
        """True when Binance price direction matches oracle direction.

        Returns True if Binance data is stale (>30s) — CL is the authority.
        """
        bn_age = time.time() - self.bn_ts.get(asset, 0)
        if bn_age > 30:
            return True
        bn_price = self.binance.get(asset, 0)
        if bn_price <= 0:
            return True
        openings = self.openings.get(asset, {})
        if not openings:
            return True
        if window_ts and window_ts in openings:
            opening = openings[window_ts]
        else:
            opening = openings[max(openings.keys())]
        if opening <= 0:
            return True
        bn_delta = (bn_price - opening) / opening * 100
        bn_says = "UP" if bn_delta > 0 else "DOWN"
        return bn_says == oracle_says

    # ── WebSocket feeds ──────────────────────────────────────────────

    async def run_rtds(self) -> None:
        self._running = True
        while self._running:
            try:
                async with websockets.connect(
                    CFG.rtds_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    log.info("RTDS connected: %s", CFG.rtds_url)
                    self._rtds_last_msg_ts = time.time()
                    await ws.send(
                        json.dumps(
                            {
                                "action": "subscribe",
                                "subscriptions": [
                                    {
                                        "topic": "crypto_prices_chainlink",
                                        "type": "update",
                                        "filters": "",
                                    }
                                ],
                            }
                        )
                    )
                    await ws.send(
                        json.dumps(
                            {
                                "action": "subscribe",
                                "subscriptions": [
                                    {
                                        "topic": "crypto_prices",
                                        "type": "update",
                                        "filters": "",
                                    }
                                ],
                            }
                        )
                    )
                    while self._running:
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=self._WS_SILENCE_TIMEOUT
                            )
                            self._rtds_last_msg_ts = time.time()
                            self._parse_rtds(raw)
                        except asyncio.TimeoutError:
                            log.warning(
                                "RTDS: no message in %ds — reconnecting",
                                self._WS_SILENCE_TIMEOUT,
                            )
                            break
            except Exception as exc:
                if self._running:
                    self._rtds_reconnects += 1
                    delay = min(3 * (2 ** min(self._rtds_reconnects - 1, 4)), 60)
                    log.warning(
                        "RTDS disconnected (%d total): %s — reconnecting in %.0fs",
                        self._rtds_reconnects,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)

    async def run_binance(self) -> None:
        symbols = "/".join(a.lower() + "usdt@bookTicker" for a in CFG.assets)
        while self._running:
            try:
                url = f"{CFG.binance_ws}?streams={symbols}"
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=10
                ) as ws:
                    log.info("Binance WS connected")
                    self._binance_last_msg_ts = time.time()
                    while self._running:
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=self._WS_SILENCE_TIMEOUT
                            )
                            self._binance_last_msg_ts = time.time()
                            self._parse_binance(raw)
                        except asyncio.TimeoutError:
                            log.warning(
                                "Binance WS: no message in %ds — reconnecting",
                                self._WS_SILENCE_TIMEOUT,
                            )
                            break
            except Exception as exc:
                if self._running:
                    self._binance_reconnects += 1
                    log.warning(
                        "Binance disconnected (%d total): %s",
                        self._binance_reconnects,
                        exc,
                    )
                    delay = min(3 * (2 ** min(self._binance_reconnects - 1, 4)), 60)
                    await asyncio.sleep(delay)

    def _parse_rtds(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
        topic = msg.get("topic", "")
        payload = msg.get("payload", {})
        symbol = payload.get("symbol", "").lower()
        value = payload.get("value")
        if not value or float(value) <= 0:
            return
        asset = self._symbol_to_asset(symbol)
        if not asset:
            return
        fval = float(value)
        if topic == "crypto_prices_chainlink":
            log.debug("CL feed: %s=$%.2f", asset, fval)
            self.chainlink[asset] = fval
            self.cl_ts[asset] = time.time()
            self._record_price(asset, fval)  # only CL ticks go into history
        elif topic == "crypto_prices":
            self.binance[asset] = fval
            self.bn_ts[asset] = time.time()

    def _parse_binance(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
        data = msg.get("data", {})
        stream = msg.get("stream", "")
        asset = self._symbol_to_asset(stream.split("@")[0] if "@" in stream else "")
        if not asset:
            return
        bb = float(data.get("b", 0))
        ba = float(data.get("a", 0))
        if bb > 0 and ba > 0:
            mid = (bb + ba) / 2
            self.binance[asset] = mid
            self.bn_ts[asset] = time.time()
            # Binance data not recorded in _price_history — consecutive_down_ticks
            # must be pure Chainlink so the oracle tick count is meaningful.

    def _record_price(self, asset: str, price: float) -> None:
        now = time.time()
        history = self._price_history[asset]
        history.append((now, price))
        cutoff = now - 600  # keep 10-minute buffer
        self._price_history[asset] = [(t, p) for t, p in history if t > cutoff]

    @staticmethod
    def _symbol_to_asset(symbol: str) -> str:
        s = symbol.lower()
        if "btc" in s:
            return "BTC"
        if "eth" in s:
            return "ETH"
        if "sol" in s:
            return "SOL"
        if "hype" in s:
            return "HYPE"
        return ""

    def stop(self) -> None:
        self._running = False
