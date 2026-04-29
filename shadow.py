#!/usr/bin/env python3
"""
Oracle-Confirmed Bear Sniper — Shadow Mode
==========================================

Runs the complete pipeline (feeds → regime → market discovery → signal
evaluation) but executes ZERO trades.  Every gate evaluation is logged to
SQLite so you can verify:

  ✓ Chainlink and Binance feeds are streaming
  ✓ Regime monitor is evaluating correctly (EMA / funding / 1h net)
  ✓ NO tokens are being discovered for BTC/ETH/SOL markets
  ✓ Signal gates are firing and rejecting at the right places
  ✓ Would-be PASS signals are appearing (strategy has live edge)

Usage:
    python shadow.py                  # run shadow mode
    python shadow.py --duration 3600  # stop after 1 hour
    python shadow.py --db my.db       # custom DB path (default: shadow_run.db)
"""

import argparse
import asyncio
import logging
import logging.handlers
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from core.config import CFG
from core.database import Database
from core.shadow import GATES, GATE_LABELS, ShadowLogger
from feeds.markets import MarketDiscovery
from feeds.prices import PriceFeeds
from feeds.regime import RegimeMonitor
from engine.signal import BearEngine

# ── Logging ─────────────────────────────────────────────────────────

_LOG_DIR = Path(CFG.log_dir)
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_today = datetime.now().strftime("%Y-%m-%d")
_log_path = _LOG_DIR / f"{_today}_shadow.log"

_fh = logging.handlers.TimedRotatingFileHandler(
    str(_log_path), when="midnight", backupCount=30, encoding="utf-8"
)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)

_root = logging.getLogger()
_root.setLevel(logging.INFO)
_root.addHandler(_fh)
_root.addHandler(_sh)

for _noisy in ("httpx", "httpcore", "websockets", "asyncio", "hpack", "h2"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

log = logging.getLogger("bear.shadow")


# ── Dashboard renderer ───────────────────────────────────────────────


def _render(
    feeds: PriceFeeds,
    regime: RegimeMonitor,
    markets: MarketDiscovery,
    shadow: ShadowLogger,
    db: Database,
    start_ts: float,
) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(name="left", ratio=2),
        Layout(name="right", ratio=3),
    )
    layout["left"].split_column(
        Layout(name="feeds", size=11),
        Layout(name="regime"),
    )

    # ── Header ───────────────────────────────────────────────────────
    elapsed = time.time() - start_ts
    h, rem = divmod(int(elapsed), 3600)
    m, s = divmod(rem, 60)
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    layout["header"].update(
        Panel(
            Text.from_markup(
                f"  [bold yellow]SHADOW MODE[/] — Bear Oracle Sniper  |  {ts}  |  "
                f"[dim]elapsed {h:02d}:{m:02d}:{s:02d}  |  log: {_log_path}[/]"
            ),
            style="bold",
        )
    )

    # ── Feeds panel ───────────────────────────────────────────────────
    ft = Table(title="Live Feeds", expand=True)
    ft.add_column("Asset")
    ft.add_column("Chainlink", justify="right")
    ft.add_column("Binance", justify="right")
    ft.add_column("CL age", justify="right")
    ft.add_column("1h net", justify="right")
    ft.add_column("Last δ", justify="right")
    for a in CFG.assets:
        cl_price = feeds.chainlink[a]
        bn_price = feeds.binance[a]
        cl = f"${cl_price:,.2f}" if cl_price > 0 else "[red]---[/]"
        bn = f"${bn_price:,.2f}" if bn_price > 0 else "[red]---[/]"
        age = feeds.chainlink_staleness(a)
        age_str = f"[red]{age:.0f}s[/]" if age > 30 else f"{age:.0f}s"
        net = feeds.chainlink_hourly_net(a)
        net_str = f"[red]{net:+.3f}%[/]" if net < 0 else f"[dim]{net:+.3f}%[/]"
        last_d = shadow.last_delta(a)
        d_str = (
            f"[red]{last_d:+.4f}%[/]"
            if last_d and last_d < 0
            else (f"{last_d:+.4f}%" if last_d else "[dim]—[/]")
        )
        ft.add_row(a, cl, bn, age_str, net_str, d_str)
    layout["feeds"].update(Panel(ft))

    # ── Regime panel ──────────────────────────────────────────────────
    from core.models import RegimeState

    _COLOR = {
        RegimeState.BEAR: "green",
        RegimeState.NEUTRAL: "yellow",
        RegimeState.BULL: "red",
    }
    rt = Table(title="Regime Checks", expand=True)
    rt.add_column("Asset")
    rt.add_column("State")
    rt.add_column("EMA(20)")
    rt.add_column("Funding")
    rt.add_column("CL 1h")
    rt.add_column("Last seen")
    for a in CFG.assets:
        state = regime.current_regime(a)
        color = _COLOR[state]
        diag = regime.all_diagnostics(a)
        ema_s = "[green]PASS[/]" if diag["ema"] else "[red]FAIL[/]"
        fund_s = "[green]PASS[/]" if diag["funding"] else "[red]FAIL[/]"
        cl_s = "[green]PASS[/]" if diag["chainlink"] else "[red]FAIL[/]"
        checked = diag["checked_at"]
        age_s = f"{time.time() - checked:.0f}s ago" if checked else "[dim]pending[/]"
        rt.add_row(a, f"[{color}]{state.value}[/]", ema_s, fund_s, cl_s, age_s)
    layout["regime"].update(Panel(rt))

    # ── Gate breakdown panel ──────────────────────────────────────────
    total = shadow.total()
    counts = shadow.counts()
    passes = shadow.passes()

    gt = Table(title=f"Gate Rejection Breakdown  (total: {total})", expand=True)
    gt.add_column("Gate / Filter", style="bold")
    gt.add_column("Count", justify="right")
    gt.add_column("Share", justify="right")
    gt.add_column("Bar")

    max_count = max((counts.get(g, 0) for g in GATES), default=1) or 1
    for gate in GATES:
        count = counts.get(gate, 0)
        share = count / total * 100 if total else 0
        bar_len = int(count / max_count * 20)
        bar = "█" * bar_len

        if gate == "PASS":
            label_str = f"[bold green]{GATE_LABELS[gate]}[/]"
            count_str = f"[bold green]{count}[/]"
            share_str = f"[bold green]{share:.1f}%[/]"
            bar_str = f"[bold green]{bar}[/]"
        elif gate.startswith("GATE_2"):
            label_str = f"[yellow]{GATE_LABELS[gate]}[/]"
            count_str = f"[yellow]{count}[/]"
            share_str = f"[yellow]{share:.1f}%[/]" if count else "[dim]0[/]"
            bar_str = f"[yellow]{bar}[/]"
        else:
            label_str = GATE_LABELS[gate]
            count_str = str(count) if count else "[dim]0[/]"
            share_str = f"{share:.1f}%" if count else "[dim]—[/]"
            bar_str = f"[cyan]{bar}[/]"

        gt.add_row(label_str, count_str, share_str, bar_str)

    layout["right"].update(Panel(gt))

    # ── Footer ───────────────────────────────────────────────────────
    rate = shadow.rate_per_min()
    no_tokens = len(markets.tokens)
    pass_rate = passes / total * 100 if total else 0
    layout["footer"].update(
        Panel(
            Text.from_markup(
                f"  [dim]NO markets: {no_tokens}  |  "
                f"Eval rate: {rate:.1f}/min  |  "
                f"PASS rate: {pass_rate:.2f}%  |  "
                f"Regime check: every {CFG.regime_check_interval_sec}s  |  "
                f"Ctrl+C to stop and show report[/]"
            )
        )
    )

    return layout


# ── Main shadow loop ─────────────────────────────────────────────────


async def run(db_path: str, duration: float) -> None:
    log.info("=" * 60)
    log.info("  BEAR ORACLE SNIPER — Shadow Mode")
    log.info("  Zero trades.  Gate-level telemetry only.")
    log.info("  DB: %s", db_path)
    log.info("=" * 60)

    db = Database(db_path)
    shadow = ShadowLogger()
    feeds = PriceFeeds()
    regime = RegimeMonitor(db, feeds)
    markets = MarketDiscovery(price_feeds=feeds)
    engine = BearEngine(feeds, regime, shadow=shadow)

    feeds._running = True
    tasks = [
        asyncio.create_task(feeds.run_rtds()),
        asyncio.create_task(feeds.run_binance()),
        asyncio.create_task(regime.run_loop()),
    ]

    log.info("Waiting for price feeds (up to 30s)...")
    for _ in range(30):
        if feeds.is_ready:
            break
        await asyncio.sleep(1)

    if not feeds.is_ready:
        log.error("No price data after 30s — check network / RTDS connection.")
        return

    for a in CFG.assets:
        log.info("%s: CL=$%.2f BN=$%.2f", a, feeds.chainlink[a], feeds.binance[a])

    await markets.discover()
    log.info("Initial discovery: %d NO tokens", len(markets.tokens))

    start_ts = time.time()
    console = Console()
    _last_flush_ts = start_ts
    _last_status_ts = start_ts
    _STATUS_INTERVAL = 60.0

    try:
        with Live(
            _render(feeds, regime, markets, shadow, db, start_ts),
            refresh_per_second=2,
            console=console,
        ) as live:
            while True:
                now = time.time()

                if duration > 0 and now - start_ts >= duration:
                    log.info("Duration limit reached (%.0fs) — stopping.", duration)
                    break

                if markets.needs_refresh():
                    await markets.discover()

                # Evaluate all discovered NO tokens
                for _, token in list(markets.tokens.items()):
                    ttl = token.end_ts - now
                    if ttl < 0 or ttl > CFG.snipe_entry_strong_sec + 5:
                        continue  # outside any snipe window — skip book refresh
                    await markets.refresh_book(token)
                    engine.evaluate(token)  # result discarded — shadow logs gate

                # Batch-flush shadow log to DB every 5 seconds
                if now - _last_flush_ts >= 5.0:
                    flushed = shadow.flush(db)
                    if flushed:
                        log.debug("Shadow flush: %d records written", flushed)
                    _last_flush_ts = now

                # Periodic console status
                if now - _last_status_ts >= _STATUS_INTERVAL:
                    _last_status_ts = now
                    total = shadow.total()
                    passes = shadow.passes()
                    log.info(
                        "SHADOW STATUS: %d evals, %d PASS (%.2f%%), rate=%.1f/min",
                        total,
                        passes,
                        passes / total * 100 if total else 0,
                        shadow.rate_per_min(),
                    )

                live.update(_render(feeds, regime, markets, shadow, db, start_ts))
                await asyncio.sleep(CFG.poll_interval)

    except KeyboardInterrupt:
        log.info("Interrupted — flushing remaining records...")
    finally:
        feeds.stop()
        regime.stop()
        for t in tasks:
            t.cancel()
        flushed = shadow.flush_all(db)
        if flushed:
            log.info("Final flush: %d records written", flushed)

    # ── End-of-session summary ────────────────────────────────────────
    _print_summary(shadow, db, start_ts, console)


def _print_summary(
    shadow: ShadowLogger,
    db: Database,
    start_ts: float,
    console: Console,
) -> None:
    total = shadow.total()
    passes = shadow.passes()
    elapsed = shadow.session_elapsed()
    h, rem = divmod(int(elapsed), 3600)
    m, s = divmod(rem, 60)

    counts = shadow.counts()

    console.print()
    console.rule("[bold yellow]Shadow Session Summary[/]")
    console.print(f"  Duration   : {h:02d}:{m:02d}:{s:02d}")
    console.print(f"  Evaluations: {total}")
    console.print(f"  Rate       : {shadow.rate_per_min():.1f} / min")
    console.print(
        f"  PASS       : [bold green]{passes}[/] ({passes / total * 100:.2f}% of evals)"
        if total
        else "  PASS       : 0"
    )
    console.print()

    t = Table(title="Gate Breakdown", show_header=True)
    t.add_column("Gate / Filter")
    t.add_column("Count", justify="right")
    t.add_column("Share", justify="right")

    for gate in GATES:
        count = counts.get(gate, 0)
        share = count / total * 100 if total else 0
        label = GATE_LABELS[gate]
        if gate == "PASS":
            t.add_row(
                f"[bold green]{label}[/]",
                f"[bold green]{count}[/]",
                f"[bold green]{share:.1f}%[/]",
            )
        else:
            t.add_row(
                label,
                str(count) if count else "[dim]0[/]",
                f"{share:.1f}%" if count else "[dim]—[/]",
            )

    console.print(t)

    # Would-be trades
    samples = db.shadow_pass_samples(5)
    if samples:
        console.print()
        console.print("[bold green]Would-be PASS signals:[/]")
        pt = Table(show_header=True)
        pt.add_column("Asset")
        pt.add_column("NO ask", justify="right")
        pt.add_column("Oracle δ", justify="right")
        pt.add_column("TTL", justify="right")
        pt.add_column("Reason")
        for row in samples:
            pt.add_row(
                row["asset"],
                f"${row['no_price']:.3f}",
                f"[red]{row['oracle_delta']:+.4f}%[/]",
                f"{row['ttl']:.0f}s",
                row["reason"],
            )
        console.print(pt)

    console.print()
    console.print(
        f"  [dim]Full log written to: {db.path}  (query: python analysis/shadow_report.py)[/]"
    )
    console.print()


# ── Entry point ──────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bear Oracle Sniper — Shadow Mode (no trades)"
    )
    parser.add_argument(
        "--db",
        default="shadow_run.db",
        help="SQLite DB path (default: shadow_run.db)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0,
        help="Stop after N seconds (0 = run until Ctrl+C)",
    )
    args = parser.parse_args()

    try:
        asyncio.run(run(args.db, args.duration))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
