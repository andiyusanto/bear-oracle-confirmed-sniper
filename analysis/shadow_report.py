#!/usr/bin/env python3
"""Post-run shadow mode report.

Reads a shadow_run.db (or any DB containing shadow_log) and prints:
  - Gate rejection breakdown with percentages
  - Would-be PASS signals with full context
  - Per-asset breakdown
  - Regime filter efficiency (what fraction of evaluations were killed by Gate 2)
  - Time-bucketed PASS rate (shows whether edge is time-of-day dependent)

Usage:
    python analysis/shadow_report.py                    # reads shadow_run.db
    python analysis/shadow_report.py --db my.db
    python analysis/shadow_report.py --passes-only      # just the PASS signals
    python analysis/shadow_report.py --since 3600       # last 1 hour only
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root is on sys.path when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from rich.console import Console
    from rich.table import Table

    _RICH = True
except ImportError:
    _RICH = False

from core.config import CFG
from core.shadow import GATES, GATE_LABELS

_BREAKEVEN_WR = 44.7  # reference for the report header


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.exists():
        print(f"ERROR: {db_path} not found.  Run shadow.py first.", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def report(db_path: str, since_sec: float, passes_only: bool) -> None:
    conn = _connect(db_path)
    console = Console() if _RICH else None

    since_ts = 0.0
    if since_sec > 0:
        since_ts = datetime.now(tz=timezone.utc).timestamp() - since_sec

    # ── Overall totals ────────────────────────────────────────────────
    (total,) = conn.execute(
        "SELECT COUNT(*) FROM shadow_log WHERE timestamp >= ?", (since_ts,)
    ).fetchone()
    if total == 0:
        print("No shadow records found.  Run shadow.py first.")
        return

    gate_rows = _rows(
        conn,
        "SELECT gate_blocked, COUNT(*) AS cnt FROM shadow_log "
        "WHERE timestamp >= ? GROUP BY gate_blocked ORDER BY cnt DESC",
        (since_ts,),
    )
    counts = {r["gate_blocked"]: r["cnt"] for r in gate_rows}
    passes = counts.get("PASS", 0)

    if not passes_only:
        _section(console, "Gate Rejection Breakdown")
        t = Table(show_header=True, header_style="bold")
        t.add_column("Gate / Filter")
        t.add_column("Count", justify="right")
        t.add_column("Share", justify="right")
        t.add_column("Bar")

        max_count = max(counts.values(), default=1)
        for gate in GATES:
            count = counts.get(gate, 0)
            share = count / total * 100 if total else 0
            bar = "█" * int(count / max_count * 30)
            label = GATE_LABELS[gate]
            if gate == "PASS":
                _row(
                    t,
                    console,
                    f"[bold green]{label}[/]",
                    str(count),
                    f"{share:.1f}%",
                    f"[green]{bar}[/]",
                )
            else:
                _row(
                    t,
                    console,
                    label,
                    str(count) if count else "0",
                    f"{share:.1f}%" if count else "—",
                    f"[cyan]{bar}[/]",
                )

        if console:
            console.print(t)
        else:
            print(f"{'Gate':<45} {'Count':>7} {'Share':>7}")
            for gate in GATES:
                count = counts.get(gate, 0)
                share = count / total * 100 if total else 0
                print(f"{GATE_LABELS[gate]:<45} {count:>7} {share:>6.1f}%")

        print()
        print(f"  Total evaluations : {total}")
        print(f"  PASS (would fire)  : {passes} ({passes / total * 100:.2f}%)")
        print(f"  Breakeven WR ref   : {_BREAKEVEN_WR}%")
        print()

        # ── Per-asset breakdown ───────────────────────────────────────
        _section(console, "Per-Asset Breakdown")
        asset_rows = _rows(
            conn,
            "SELECT asset, gate_blocked, COUNT(*) AS cnt FROM shadow_log "
            "WHERE timestamp >= ? GROUP BY asset, gate_blocked",
            (since_ts,),
        )
        by_asset: dict[str, dict[str, int]] = {}
        for r in asset_rows:
            by_asset.setdefault(r["asset"], {})[r["gate_blocked"]] = r["cnt"]

        at = Table(show_header=True)
        at.add_column("Asset")
        at.add_column("Total", justify="right")
        at.add_column("Gate 2 (regime)", justify="right")
        at.add_column("Gate 3 (not DOWN)", justify="right")
        at.add_column("PASS", justify="right")
        at.add_column("PASS %", justify="right")

        for asset in sorted(by_asset):
            ac = by_asset[asset]
            a_total = sum(ac.values())
            a_g2 = ac.get("GATE_2_REGIME", 0)
            a_g3 = ac.get("GATE_3_NOT_DOWN", 0)
            a_pass = ac.get("PASS", 0)
            a_pct = a_pass / a_total * 100 if a_total else 0
            if console:
                at.add_row(
                    asset,
                    str(a_total),
                    str(a_g2),
                    str(a_g3),
                    f"[green]{a_pass}[/]" if a_pass else "0",
                    f"[green]{a_pct:.2f}%[/]" if a_pass else "[dim]—[/]",
                )
            else:
                print(
                    f"  {asset}: total={a_total} regime_block={a_g2} not_down={a_g3} pass={a_pass} ({a_pct:.2f}%)"
                )

        if console:
            console.print(at)

        # ── Regime filter efficiency ──────────────────────────────────
        print()
        _section(console, "Regime Filter Efficiency")
        regime_blocked = counts.get("GATE_2_REGIME", 0)
        regime_pct = regime_blocked / total * 100 if total else 0
        non_regime = total - regime_blocked
        pass_of_non_regime = passes / non_regime * 100 if non_regime else 0

        print(
            f"  Evaluations blocked by regime filter : {regime_blocked} ({regime_pct:.1f}%)"
        )
        print(f"  Evaluations past regime gate         : {non_regime}")
        print(f"  PASS rate among post-regime evals    : {pass_of_non_regime:.2f}%")
        print()

        # ── Regime sub-check breakdown from reasons ───────────────────
        reason_rows = _rows(
            conn,
            "SELECT reason, COUNT(*) AS cnt FROM shadow_log "
            "WHERE gate_blocked='GATE_2_REGIME' AND timestamp >= ? "
            "GROUP BY reason ORDER BY cnt DESC LIMIT 10",
            (since_ts,),
        )
        if reason_rows:
            rt = Table(title="Regime block reasons (top 10)", show_header=True)
            rt.add_column("Reason")
            rt.add_column("Count", justify="right")
            for r in reason_rows:
                rt.add_row(r["reason"], str(r["cnt"]))
            if console:
                console.print(rt)
            else:
                for r in reason_rows:
                    print(f"  {r['reason']}: {r['cnt']}")

        # ── Hourly PASS rate ──────────────────────────────────────────
        print()
        _section(console, "PASS Rate by UTC Hour")
        hour_rows = _rows(
            conn,
            "SELECT CAST(strftime('%H', datetime(timestamp, 'unixepoch')) AS INTEGER) AS hour, "
            "COUNT(*) AS total, "
            "SUM(CASE WHEN gate_blocked='PASS' THEN 1 ELSE 0 END) AS passes "
            "FROM shadow_log WHERE timestamp >= ? GROUP BY hour ORDER BY hour",
            (since_ts,),
        )
        if hour_rows:
            ht = Table(show_header=True)
            ht.add_column("UTC Hour")
            ht.add_column("Evals", justify="right")
            ht.add_column("PASS", justify="right")
            ht.add_column("PASS %", justify="right")
            ht.add_column("Note")
            for r in hour_rows:
                h = r["hour"]
                h_pct = r["passes"] / r["total"] * 100 if r["total"] else 0
                note = "[red]BLACKOUT[/]" if h in CFG.blackout_hours else ""
                if console:
                    ht.add_row(
                        f"{h:02d}:00",
                        str(r["total"]),
                        f"[green]{r['passes']}[/]" if r["passes"] else "0",
                        f"{h_pct:.2f}%" if r["passes"] else "[dim]—[/]",
                        note,
                    )
                else:
                    print(
                        f"  {h:02d}:00 — evals={r['total']} pass={r['passes']} ({h_pct:.2f}%)"
                    )
            if console:
                console.print(ht)

    # ── PASS signal detail ────────────────────────────────────────────
    print()
    _section(console, f"Would-Be PASS Signals  ({passes} total)")
    if passes == 0:
        print("  No PASS signals yet — strategy filters are blocking everything.")
        print("  This is EXPECTED early in a run or when regime is NEUTRAL.")
        print(
            "  Check Gate 2 breakdown above to see which regime sub-check is failing."
        )
    else:
        pass_rows = _rows(
            conn,
            "SELECT * FROM shadow_log WHERE gate_blocked='PASS' AND timestamp >= ? "
            "ORDER BY timestamp DESC LIMIT 30",
            (since_ts,),
        )
        pt = Table(show_header=True)
        pt.add_column("Time (UTC)")
        pt.add_column("Asset")
        pt.add_column("NO ask", justify="right")
        pt.add_column("Oracle δ", justify="right")
        pt.add_column("TTL", justify="right")
        pt.add_column("Details")
        for r in pass_rows:
            ts_str = datetime.fromtimestamp(r["timestamp"], tz=timezone.utc).strftime(
                "%H:%M:%S"
            )
            if console:
                pt.add_row(
                    ts_str,
                    r["asset"],
                    f"${r['no_price']:.3f}",
                    f"[red]{r['oracle_delta']:+.4f}%[/]",
                    f"{r['ttl']:.0f}s",
                    r["reason"],
                )
            else:
                print(
                    f"  {ts_str} {r['asset']} NO=${r['no_price']:.3f} "
                    f"δ={r['oracle_delta']:+.4f}% ttl={r['ttl']:.0f}s | {r['reason']}"
                )
        if console:
            console.print(pt)

    conn.close()


def _section(console, title: str) -> None:
    if console:
        console.rule(f"[bold]{title}[/]")
    else:
        print(f"\n{'─' * 60}")
        print(f"  {title}")
        print(f"{'─' * 60}")


def _row(table, console, *cells) -> None:
    if console:
        table.add_row(*cells)


def main() -> None:
    parser = argparse.ArgumentParser(description="Shadow mode post-run report")
    parser.add_argument("--db", default="shadow_run.db")
    parser.add_argument(
        "--since",
        type=float,
        default=0,
        metavar="SECONDS",
        help="Only show records from last N seconds (0 = all time)",
    )
    parser.add_argument(
        "--passes-only",
        action="store_true",
        help="Only print PASS signals, skip full breakdown",
    )
    args = parser.parse_args()
    report(args.db, args.since, args.passes_only)


if __name__ == "__main__":
    main()
