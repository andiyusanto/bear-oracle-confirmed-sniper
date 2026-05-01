"""Telegram notifications for the Bear Oracle Sniper.

Sends async alerts for:
  - Shadow session start / stop summary
  - PASS signals (all 7 gates + anti-decoy filters cleared)
  - Regime transitions (asset enters/exits BEAR)

Falls back silently if credentials are missing or sends fail.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

import aiohttp

from core.config import CFG

log = logging.getLogger("bear.telegram")

_MIN_INTERVAL = 1.0
_last_send_ts = 0.0


def is_configured() -> bool:
    return bool(CFG.telegram_token and CFG.telegram_chat_id)


async def send(text: str, parse_mode: str = "HTML", _retries: int = 3) -> bool:
    """Send a Telegram message. Returns True on success. Never raises."""
    global _last_send_ts

    if not is_configured():
        return False

    url = f"https://api.telegram.org/bot{CFG.telegram_token}/sendMessage"
    payload = {
        "chat_id": CFG.telegram_chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    for attempt in range(_retries):
        now = time.time()
        elapsed = now - _last_send_ts
        if elapsed < _MIN_INTERVAL:
            await asyncio.sleep(_MIN_INTERVAL - elapsed)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    _last_send_ts = time.time()
                    if resp.status == 200:
                        return True
                    if resp.status == 429:
                        retry_after = int(resp.headers.get("Retry-After", 5))
                        log.warning(
                            "Telegram rate limited — waiting %ds (attempt %d/%d)",
                            retry_after,
                            attempt + 1,
                            _retries,
                        )
                        await asyncio.sleep(retry_after)
                        continue
                    body = await resp.text()
                    log.warning(
                        "Telegram send failed: %d %s (attempt %d/%d)",
                        resp.status,
                        body[:100],
                        attempt + 1,
                        _retries,
                    )
        except Exception as exc:
            log.warning(
                "Telegram send error (attempt %d/%d): %s", attempt + 1, _retries, exc
            )

        if attempt < _retries - 1:
            await asyncio.sleep(2.0 * (attempt + 1))

    return False


# ── Message formatters ────────────────────────────────────────────────────────


async def notify_shadow_started(assets: list[str]) -> bool:
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    msg = (
        f"👁 <b>SHADOW MODE STARTED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Time: {ts}\n"
        f"Assets: <b>{', '.join(assets)}</b>\n"
        f"Zero trades — gate telemetry only\n"
        f"Watching for PASS signals and regime changes..."
    )
    return await send(msg)


async def notify_shadow_pass(signal) -> bool:
    """Fire when all 7 gates + anti-decoy filters pass in shadow mode."""
    token = signal.token
    ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S UTC")
    tier = signal.delta_tier
    tier_emoji = {"STRONG": "🔴", "NORMAL": "🟠", "WEAK": "🟡"}.get(tier, "⚪")
    msg = (
        f"✅ <b>SHADOW PASS</b> {tier_emoji}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Asset: <b>{token.asset} {token.duration}</b>\n"
        f"NO ask: <b>${signal.entry_price:.4f}</b>\n"
        f"Oracle Δ: <b>{signal.oracle.delta_pct:+.4f}%</b> ({tier})\n"
        f"TTL: <b>{signal.time_remaining:.0f}s</b>\n"
        f"Consecutive ticks: {signal.consecutive_ticks}\n"
        f"Time: {ts}"
    )
    return await send(msg)


async def notify_regime_change(
    asset: str,
    prev_state: str,
    new_state: str,
    reason: str,
    ema_pass: bool,
    funding_pass: bool,
    chainlink_pass: bool,
) -> bool:
    ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S UTC")
    state_emoji = {"BEAR": "🟢", "NEUTRAL": "🟡", "BULL": "🔴"}.get(new_state, "⚪")
    checks = (
        f"EMA: {'✓' if ema_pass else '✗'}  "
        f"Funding: {'✓' if funding_pass else '✗'}  "
        f"CL 1h: {'✓' if chainlink_pass else '✗'}"
    )
    msg = (
        f"{state_emoji} <b>REGIME CHANGE — {asset}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{prev_state} → <b>{new_state}</b>\n"
        f"{checks}\n"
        f"Reason: {reason}\n"
        f"Time: {ts}"
    )
    return await send(msg)


async def notify_shadow_stopped(
    total: int,
    passes: int,
    elapsed_sec: float,
    rate_per_min: float,
    gate_counts: dict[str, int],
) -> bool:
    h, rem = divmod(int(elapsed_sec), 3600)
    m, s = divmod(rem, 60)
    pass_rate = passes / total * 100 if total else 0.0

    top_gates = sorted(
        [(g, c) for g, c in gate_counts.items() if g != "PASS"],
        key=lambda x: x[1],
        reverse=True,
    )[:3]
    gate_lines = "\n".join(f"  {g}: {c}" for g, c in top_gates) or "  (none)"

    msg = (
        f"🛑 <b>SHADOW MODE STOPPED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Duration: <b>{h:02d}:{m:02d}:{s:02d}</b>\n"
        f"Evaluations: <b>{total}</b> ({rate_per_min:.1f}/min)\n"
        f"PASS signals: <b>{passes}</b> ({pass_rate:.2f}%)\n"
        f"\nTop rejections:\n{gate_lines}"
    )
    return await send(msg)
