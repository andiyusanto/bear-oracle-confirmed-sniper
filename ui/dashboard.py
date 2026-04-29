"""Rich terminal dashboard for the Oracle-Confirmed Bear Sniper.

Additions vs bull bot:
  - REGIME panel at top: per-asset state, last change, time in state
  - Trade table shows NO token price and delta tier
  - Breakeven reference: 44.7% (not bull bot's 62.2%)
"""

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from core.config import CFG
from core.database import Database
from core.models import RegimeState
from feeds.prices import PriceFeeds
from feeds.markets import MarketDiscovery
from feeds.regime import RegimeMonitor
from engine.risk import RiskManager

if TYPE_CHECKING:
    from execution.executor import Executor

_REGIME_COLOR = {
    RegimeState.BEAR: "green",
    RegimeState.NEUTRAL: "yellow",
    RegimeState.BULL: "red",
}


class Dashboard:
    def __init__(
        self,
        db: Database,
        feeds: PriceFeeds,
        markets: MarketDiscovery,
        regime: RegimeMonitor,
        risk: RiskManager,
        executor: "Executor",
        is_live: bool,
    ) -> None:
        self.db = db
        self.feeds = feeds
        self.markets = markets
        self.regime = regime
        self.risk = risk
        self.executor = executor
        self.is_live = is_live
        self.signals_seen = 0
        self.signals_fired = 0
        self.console = Console()

    def render(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="regime", size=5),
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=3),
        )
        layout["body"].split_row(
            Layout(name="left", ratio=1),
            Layout(name="right", ratio=1),
        )
        layout["left"].split_column(
            Layout(name="prices", size=9),
            Layout(name="positions"),
        )
        layout["right"].split_column(
            Layout(name="stats", size=14),
            Layout(name="trades"),
        )

        # ── Regime panel ─────────────────────────────────────────────
        states = self.regime.states
        regime_parts: list[str] = []
        for a in CFG.assets:
            state = states.get(a, RegimeState.NEUTRAL)
            color = _REGIME_COLOR[state]
            dur = self.db.regime_duration(a)
            dur_str = f"{dur / 3600:.1f}h" if dur else "?"
            diag = self.regime.all_diagnostics(a)
            ema_icon = "✓" if diag["ema"] else "✗"
            fund_icon = "✓" if diag["funding"] else "✗"
            cl_icon = "✓" if diag["chainlink"] else "✗"
            regime_parts.append(
                f"  [{color}]{a}: {state.value}[/] "
                f"[dim](EMA{ema_icon} Fund{fund_icon} CL{cl_icon} {dur_str})[/]"
            )

        recent_regime = self.db.recent_regime_log(1)
        last_change = ""
        if recent_regime:
            r = recent_regime[0]
            ts_str = datetime.fromtimestamp(r["timestamp"], tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            )
            last_change = f"\n  [dim]Last change: {ts_str} — {r['asset']} {r['prev_state']}→{r['new_state']}: {r['reason']}[/]"

        layout["regime"].update(
            Panel(
                Text.from_markup("".join(regime_parts) + last_change),
                title="[bold]REGIME STATUS[/]",
                border_style="green"
                if all(s == RegimeState.BEAR for s in states.values())
                else "yellow",
            )
        )

        # ── Header ───────────────────────────────────────────────────
        mode = "[bold red]LIVE MODE[/]" if self.is_live else "[bold green]PAPER MODE[/]"
        kill = "  [bold red]KILL SWITCH[/]" if self.risk.kill_switch else ""
        ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        layout["header"].update(
            Panel(
                Text.from_markup(f"  Bear Oracle Sniper  |  {mode}{kill}  |  {ts}"),
                style="bold",
            )
        )

        # ── Prices ───────────────────────────────────────────────────
        pt = Table(title="Oracle Prices", expand=True)
        pt.add_column("Asset")
        pt.add_column("Chainlink", justify="right")
        pt.add_column("Binance", justify="right")
        pt.add_column("CL age", justify="right")
        pt.add_column("1h net", justify="right")
        for a in CFG.assets:
            cl = (
                f"${self.feeds.chainlink[a]:,.2f}"
                if self.feeds.chainlink[a] > 0
                else "---"
            )
            bn = (
                f"${self.feeds.binance[a]:,.2f}" if self.feeds.binance[a] > 0 else "---"
            )
            age = f"{self.feeds.chainlink_staleness(a):.0f}s"
            net = self.feeds.chainlink_hourly_net(a)
            net_str = f"[red]{net:+.3f}%[/]" if net < 0 else f"{net:+.3f}%"
            pt.add_row(a, cl, bn, age, net_str)
        layout["prices"].update(Panel(pt))

        # ── Stats ────────────────────────────────────────────────────
        st = self.db.lifetime_stats()
        daily = self.db.daily_pnl()
        dc = "green" if daily >= 0 else "red"
        tc = "green" if st["pnl"] >= 0 else "red"
        wr_color = "green" if st["wr"] > 44.7 else "red"  # breakeven at 44.7%
        rolling = self.db.rolling_wr(20)
        rwr_str = (
            f"[{'green' if rolling and rolling > 0.447 else 'red'}]"
            f"{rolling * 100:.1f}%[/]"
            if rolling is not None
            else "[dim]< 20 trades[/]"
        )

        stats = Table(title="Performance (breakeven: 44.7%)", expand=True)
        stats.add_column("Metric", style="bold")
        stats.add_column("Value", justify="right")
        stats.add_row("Portfolio", f"${self.risk.portfolio:,.2f}")
        stats.add_row("Daily P&L", f"[{dc}]${daily:+,.4f}[/]")
        stats.add_row("Total P&L", f"[{tc}]${st['pnl']:+,.4f}[/]")
        stats.add_row(
            "Win Rate",
            f"[{wr_color}]{st['wr']:.1f}%[/] ({st['wins']}/{st['total']})",
        )
        stats.add_row("Rolling WR (20)", rwr_str)
        stats.add_row("Expectancy", f"${st['expectancy']:+,.4f}/trade")
        stats.add_row("Avg Win", f"${st['avg_win']:+,.4f}")
        stats.add_row("Avg Loss", f"${st['avg_loss']:+,.4f}")
        stats.add_row("Signals", f"{self.signals_fired}/{self.signals_seen}")
        stats.add_row("NO markets", str(len(self.markets.tokens)))
        layout["stats"].update(Panel(stats))

        # ── Open positions ───────────────────────────────────────────
        ot = Table(title=f"Open ({self.executor.open_count})", expand=True)
        ot.add_column("Asset")
        ot.add_column("Entry", justify="right")
        ot.add_column("Delta", justify="right")
        ot.add_column("Tier")
        ot.add_column("TTL", justify="right")
        for _, pos in list(self.executor.open_positions.items()):
            dur = getattr(pos, "duration_sec", 300)
            ttl = max(0, pos.window_ts + dur - time.time())
            ot.add_row(
                pos.asset,
                f"${pos.entry_price:.3f}",
                f"[red]{pos.oracle_delta:.4f}%[/]",
                getattr(pos, "delta_tier", "?"),
                f"{ttl:.0f}s",
            )
        layout["positions"].update(Panel(ot))

        # ── Recent trades ────────────────────────────────────────────
        recent = self.db.recent(10)
        rt = Table(title="Recent Trades (NO tokens)", expand=True)
        rt.add_column("ID", max_width=12)
        rt.add_column("Asset")
        rt.add_column("Entry", justify="right")
        rt.add_column("Delta", justify="right")
        rt.add_column("Tier")
        rt.add_column("P&L", justify="right")
        rt.add_column("St")
        for t in recent:
            pc = "green" if t["pnl"] > 0 else ("red" if t["pnl"] < 0 else "dim")
            rt.add_row(
                t["id"][:12],
                t["asset"],
                f"${t['entry_price']:.3f}",
                f"{t['oracle_delta']:.3f}%",
                t.get("delta_tier", "?"),
                f"[{pc}]${t['pnl']:+.4f}[/]",
                t["status"][:4],
            )
        layout["trades"].update(Panel(rt))

        # ── Footer ───────────────────────────────────────────────────
        layout["footer"].update(
            Panel(
                Text.from_markup(
                    f"  [dim]Ctrl+C to stop  |  "
                    f"NO price: ${CFG.no_price_min}–${CFG.no_price_max}  |  "
                    f"Ghost zone: T-{CFG.snipe_exit_sec:.0f}s  |  "
                    f"Stake: ${CFG.stake_per_trade_usd}  |  "
                    f"Daily cap: ${CFG.daily_loss_cap_usd}  |  "
                    f"Breakeven: 44.7%[/]"
                )
            )
        )

        return layout
