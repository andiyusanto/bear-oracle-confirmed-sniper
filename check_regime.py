#!/usr/bin/env python3
"""Quick regime diagnostic — runs one check cycle and exits.

Shows exactly which sub-conditions pass/fail for each asset without
needing to wait for shadow mode's 15-minute cycle.

Usage:
    python check_regime.py
"""

import asyncio
import logging
import sys
import time
import aiohttp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
for noisy in ("httpx", "httpcore", "websockets", "asyncio", "hpack"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

log = logging.getLogger("bear.check_regime")


async def run() -> None:
    from core.config import CFG
    from feeds.prices import PriceFeeds

    _KLINES_URL = f"{CFG.binance_rest}/fapi/v1/klines"
    _FUNDING_URL = f"{CFG.binance_rest}/fapi/v1/fundingRate"
    _SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}

    # Start feeds and wait for first tick
    feeds = PriceFeeds()
    feeds._running = True
    feed_task = asyncio.create_task(feeds.run_rtds())
    bn_task = asyncio.create_task(feeds.run_binance())

    log.info("Waiting for price feeds (up to 15s)...")
    for _ in range(15):
        if feeds.is_ready:
            break
        await asyncio.sleep(1)

    if not feeds.is_ready:
        log.error("No price data after 15s — check network")
        feeds.stop()
        feed_task.cancel()
        bn_task.cancel()
        return

    for asset in CFG.assets:
        log.info(
            "  %s: CL=$%.2f BN=$%.2f",
            asset,
            feeds.chainlink[asset],
            feeds.binance[asset],
        )

    print()
    print("=" * 60)
    print("  REGIME SUB-CHECK RESULTS")
    print("=" * 60)

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=15)
    ) as session:
        for asset in CFG.assets:
            symbol = _SYMBOLS[asset]

            # --- EMA check ---
            try:
                async with session.get(
                    _KLINES_URL,
                    params={"symbol": symbol, "interval": "4h", "limit": 21},
                ) as resp:
                    klines = (
                        await resp.json(content_type=None) if resp.status == 200 else []
                    )
            except Exception as exc:
                klines = []
                log.warning("%s klines error: %s", asset, exc)

            if klines and len(klines) >= 21:
                closes = [float(k[4]) for k in klines]
                k = 2.0 / (CFG.ema_period_4h + 1)
                ema = closes[0]
                for p in closes[1 : CFG.ema_period_4h]:
                    ema = p * k + ema * (1.0 - k)
                current = feeds.best_price(asset) or closes[-1]
                ema_pass = current < ema
                ema_detail = f"price=${current:,.2f} ema=${ema:,.2f} → {'PASS ✓' if ema_pass else 'FAIL ✗'}"
            else:
                ema_pass = False
                ema_detail = f"insufficient klines ({len(klines) if klines else 0})"

            # --- Funding rate check ---
            try:
                async with session.get(
                    _FUNDING_URL, params={"symbol": symbol, "limit": 3}
                ) as resp:
                    funding_data = (
                        await resp.json(content_type=None) if resp.status == 200 else []
                    )
            except Exception as exc:
                funding_data = []
                log.warning("%s funding error: %s", asset, exc)

            if funding_data:
                rates = [float(r.get("fundingRate", 0)) for r in funding_data]
                neg = sum(1 for r in rates if r < CFG.funding_rate_bear_threshold)
                funding_pass = neg >= CFG.funding_intervals_required
                funding_detail = f"rates={[f'{r:.6f}' for r in rates]} neg={neg}/{CFG.funding_intervals_required} → {'PASS ✓' if funding_pass else 'FAIL ✗'}"
            else:
                funding_pass = False
                funding_detail = "no data"

            # --- CL 1h net check ---
            current_price = feeds.best_price(asset)
            target_ts = time.time() - 3600.0
            past_price = feeds.price_at(asset, target_ts)
            if current_price > 0 and past_price > 0:
                net_pct = (current_price - past_price) / past_price * 100
                threshold = CFG.chainlink_1h_net_pct_bear * 100
                cl_pass = net_pct <= threshold
                cl_detail = f"now=${current_price:,.2f} 1h_ago=${past_price:,.2f} net={net_pct:+.4f}% (need <={threshold:.2f}%) → {'PASS ✓' if cl_pass else 'FAIL ✗'}"
            else:
                cl_pass = False
                if current_price <= 0:
                    cl_detail = "no current price"
                else:
                    cl_detail = f"no 1h history (bot running <60min) — now=${current_price:,.2f}"

            all_pass = ema_pass and funding_pass and cl_pass

            print(f"\n  {asset}: {'[BEAR ✓]' if all_pass else '[NEUTRAL]'}")
            print(f"    A) EMA(20) 4h : {ema_detail}")
            print(f"    B) Funding    : {funding_detail}")
            print(f"    C) CL 1h net  : {cl_detail}")

    print()
    print("=" * 60)
    print()
    print("Note: CL 1h check needs 60min of history — run shadow mode")
    print("for >1h before the CL check can ever pass.")
    print()

    feeds.stop()
    feed_task.cancel()
    bn_task.cancel()


if __name__ == "__main__":
    asyncio.run(run())
