"""NO-token market discovery via Gamma API slug lookup.

Adapted from bull bot's markets.py.  Key differences:
  - Only the NO token per market is surfaced (outcome == "No", token index 1)
  - book_price tracks best_ask on the NO token (taker entry price)
  - book_depth_usd tracks ask-side liquidity depth ≥ $200 (anti-decoy filter 5)
  - spread is computed for optional future spread gate
"""

import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import aiohttp

from core.config import CFG
from core.models import Token

_POLYGON_RPCS = [
    "https://rpc.ankr.com/polygon",
    "https://polygon-mainnet.public.blastapi.io",
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon-rpc.com",
]

_CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
_CTF_SLOT_ABI = [
    {
        "name": "getOutcomeSlotCount",
        "type": "function",
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    }
]

log = logging.getLogger("bear.markets")

try:
    from py_clob_client_v2.client import ClobClient

    HAS_CLOB = True
except ImportError:
    HAS_CLOB = False

try:
    from py_clob_client_v2.constants import POLYGON
except ImportError:
    POLYGON = 137


class MarketDiscovery:
    def __init__(self, price_feeds=None) -> None:
        self.tokens: dict[str, Token] = {}
        self._last_discovery = 0.0
        # (price, depth_usd, spread, timestamp)
        self._book_cache: dict[str, tuple[float, float, float, float]] = {}
        self._executor = ThreadPoolExecutor(max_workers=6)
        self._price_feeds = price_feeds

        self._clob: Optional[object] = None
        if HAS_CLOB:
            try:
                self._clob = ClobClient(host=CFG.clob_host, chain_id=POLYGON)
            except Exception as exc:
                log.warning("Failed to init read-only ClobClient: %s", exc)

        self._cid_valid: dict[str, bool] = {}
        self._w3 = None

    def needs_refresh(self) -> bool:
        return time.time() - self._last_discovery > CFG.discovery_interval

    def _get_w3(self):
        if self._w3 and self._w3.is_connected():
            return self._w3
        try:
            from web3 import Web3

            for rpc in _POLYGON_RPCS:
                try:
                    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))
                    if w3.is_connected():
                        self._w3 = w3
                        return w3
                except Exception:
                    continue
        except ImportError:
            pass
        return None

    def _validate_condition_sync(self, cid_hex: str) -> bool:
        if not cid_hex:
            return True
        if not cid_hex.startswith("0x"):
            cid_hex = "0x" + cid_hex
        w3 = self._get_w3()
        if not w3:
            return True
        try:
            from web3 import Web3 as _W3

            ctf = w3.eth.contract(
                address=_W3.to_checksum_address(_CTF_ADDRESS),
                abi=_CTF_SLOT_ABI,
            )
            slots = ctf.functions.getOutcomeSlotCount(bytes.fromhex(cid_hex[2:])).call()
            if slots == 0:
                log.warning(
                    "[PRE-ENTRY] conditionId %s not registered (slots=0) — excluded",
                    cid_hex[:18],
                )
                return False
            return True
        except Exception as exc:
            log.debug(
                "conditionId %s validation error: %s — fail-open", cid_hex[:18], exc
            )
            return True

    async def discover(self) -> None:
        """Find active NO-token markets using deterministic slug lookup."""
        now = time.time()
        now_int = int(now)
        found: dict[str, Token] = {}

        try:
            async with aiohttp.ClientSession() as session:
                for asset_l in [a.lower() for a in CFG.assets]:
                    asset_u = asset_l.upper()
                    for dur_label, dur_sec in CFG.durations:
                        current_ts = now_int - (now_int % dur_sec)
                        for offset in [0, dur_sec, dur_sec * 2]:
                            wts = current_ts + offset
                            end_ts = float(wts + dur_sec)
                            if end_ts < now:
                                continue
                            slug = f"{asset_l}-updown-{dur_label}-{wts}"
                            tokens = await self._fetch_slug_with_retry(
                                session, slug, asset_u, end_ts, wts, dur_label
                            )
                            found.update(tokens)
        except Exception as exc:
            log.error("Discovery failed: %s", exc)

        # On-chain conditionId validation
        loop = asyncio.get_running_loop()
        new_cids = {
            tok.conditionId
            for tok in found.values()
            if tok.conditionId and tok.conditionId not in self._cid_valid
        }
        for cid in new_cids:
            valid = await loop.run_in_executor(
                self._executor, self._validate_condition_sync, cid
            )
            self._cid_valid[cid] = valid

        zombie_cids = {cid for cid, ok in self._cid_valid.items() if not ok}
        if zombie_cids:
            before = len(found)
            found = {
                tid: tok
                for tid, tok in found.items()
                if not tok.conditionId or tok.conditionId not in zombie_cids
            }
            removed = before - len(found)
            if removed:
                log.warning(
                    "[PRE-ENTRY] Excluded %d token(s) for zombie conditionId(s)",
                    removed,
                )

        now2 = time.time()
        for tid, tok in found.items():
            self.tokens[tid] = tok
        self.tokens = {k: v for k, v in self.tokens.items() if v.end_ts > now2}
        self._last_discovery = now2

        if found:
            log.info(
                "Markets: %d active NO tokens (%d new)", len(self.tokens), len(found)
            )

    async def _fetch_slug_with_retry(
        self,
        session: aiohttp.ClientSession,
        slug: str,
        asset: str,
        end_ts: float,
        wts: int,
        dur_label: str,
        max_retries: int = 3,
    ) -> dict[str, Token]:
        for attempt in range(max_retries):
            result = await self._fetch_slug(
                session, slug, asset, end_ts, wts, dur_label
            )
            if result is not None:
                return result
            if attempt < max_retries - 1:
                await asyncio.sleep(1.0 * (2**attempt))
        return {}

    async def _fetch_slug(
        self,
        session: aiohttp.ClientSession,
        slug: str,
        asset: str,
        end_ts: float,
        wts: int,
        dur_label: str,
    ) -> Optional[dict[str, Token]]:
        """Fetch a single slug and extract the NO token only."""
        found: dict[str, Token] = {}
        try:
            async with session.get(
                f"{CFG.gamma_url}?slug={slug}",
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 429:
                    log.warning("Rate limited on slug %s", slug)
                    return None
                if resp.status != 200:
                    return found
                events = await resp.json(content_type=None)

            if not events:
                return found

            event = events[0] if isinstance(events, list) else events
            for m in event.get("markets") or []:
                if m.get("closed") or m.get("resolved"):
                    continue

                tids = m.get("clobTokenIds") or []
                if isinstance(tids, str):
                    tids = json.loads(tids)

                outcomes = m.get("outcomes") or []
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)

                prices = m.get("outcomePrices") or []
                if isinstance(prices, str):
                    prices = json.loads(prices)

                cid = m.get("conditionId") or m.get("condition_id") or ""
                dur_str = dur_label.replace("m", "min")

                # BTC/ETH/SOL updown markets are always NegRisk CTF markets.
                # Prefer the API field; fall back to True for known NegRisk assets.
                _nr_flag = (
                    m.get("enableNegRisk") or m.get("negRisk") or m.get("neg_risk")
                )
                if _nr_flag is None:
                    neg_risk = asset in ("BTC", "ETH", "SOL")
                else:
                    neg_risk = bool(_nr_flag)

                for i, tid in enumerate(tids):
                    tid = str(tid)
                    oc = str(outcomes[i]).lower() if i < len(outcomes) else ""

                    # Bear bot: only the NO token (outcome contains "no", or index 1)
                    is_no = "no" in oc or (
                        i == 1 and "yes" not in oc and "up" not in oc
                    )
                    if not is_no:
                        continue

                    price = float(prices[i]) if i < len(prices) else 0.5
                    found[tid] = Token(
                        token_id=tid,
                        asset=asset,
                        direction="DOWN",
                        duration=dur_str,
                        end_ts=end_ts,
                        window_ts=wts,
                        book_price=price,
                        book_updated=0.0,
                        conditionId=cid,
                        neg_risk=neg_risk,
                    )

                    # Trigger opening price capture for this window
                    if self._price_feeds:
                        self._price_feeds.capture_opening(asset, wts)

        except asyncio.TimeoutError:
            return None
        except aiohttp.ClientError as exc:
            log.debug("HTTP error for %s: %s", slug, exc)
            return None
        except Exception as exc:
            log.debug("Slug %s error: %s", slug, exc)

        return found

    async def refresh_book(self, token: Token) -> float:
        """Get fresh order book ask price and depth for a NO token.

        Returns best_ask. Updates token.book_price, token.book_depth_usd,
        and token.book_spread in-place.
        """
        now = time.time()
        cached = self._book_cache.get(token.token_id)
        if cached and now - cached[3] < CFG.book_cache_sec:
            token.book_price = cached[0]
            token.book_depth_usd = cached[1]
            token.book_spread = cached[2]
            return cached[0]

        if not HAS_CLOB or not self._clob:
            return token.book_price

        try:
            loop = asyncio.get_running_loop()

            def _fetch() -> tuple[float, float, float]:
                book = self._clob.get_order_book(token.token_id)
                asks = sorted(
                    [
                        (float(a.price), float(a.size))
                        for a in (book.asks or [])
                        if float(a.price) > 0
                    ],
                )
                bids = [float(b.price) for b in (book.bids or []) if float(b.price) > 0]

                if not asks:
                    # No asks = fully priced in or empty book
                    return 0.99, 0.0, 1.0

                best_ask, _ = asks[0]
                best_bid = max(bids) if bids else best_ask
                mid = (best_ask + best_bid) / 2
                spread = (best_ask - best_bid) / mid if mid > 0 else 1.0

                # Depth = total ask-side liquidity in USD up to 3 price levels
                depth_usd = sum(price * size for price, size in asks[:3])

                return best_ask, depth_usd, spread

            best_ask, depth_usd, spread = await loop.run_in_executor(
                self._executor, _fetch
            )
            self._book_cache[token.token_id] = (best_ask, depth_usd, spread, now)
            token.book_price = best_ask
            token.book_depth_usd = depth_usd
            token.book_spread = spread
            token.book_updated = now
            return best_ask

        except Exception as exc:
            log.debug("Book error %s: %s", token.token_id[:12], exc)
            return token.book_price
