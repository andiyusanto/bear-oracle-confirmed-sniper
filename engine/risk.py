"""Risk manager: kill switch, daily loss cap, concurrent position limit."""

import logging
from datetime import datetime, timezone

from core.config import CFG
from core.database import Database

log = logging.getLogger("bear.risk")


class RiskManager:
    def __init__(self, db: Database, portfolio: float) -> None:
        self.db = db
        self.portfolio = portfolio
        self.kill_switch = False
        self._daily_count = 0
        self._last_day = ""
        self._check_day()

    def _check_day(self) -> None:
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        if today != self._last_day:
            self._last_day = today
            self._daily_count = self.db.daily_count()
            self.kill_switch = False

    def can_trade(self) -> tuple[bool, str]:
        self._check_day()

        if self.kill_switch:
            return False, "kill switch active"

        daily_pnl = self.db.daily_pnl()
        if daily_pnl < 0 and abs(daily_pnl) >= CFG.daily_loss_cap_usd:
            self.kill_switch = True
            log.critical(
                "KILL SWITCH: daily loss $%.2f >= cap $%.2f",
                abs(daily_pnl),
                CFG.daily_loss_cap_usd,
            )
            return False, f"kill switch: daily loss ${abs(daily_pnl):.2f}"

        # Rolling WR check (only after minimum trade count)
        wr = self.db.rolling_wr(CFG.halt_wr_min_trades)
        if wr is not None and wr < CFG.halt_wr_threshold:
            log.critical(
                "HALT: rolling WR %.1f%% < threshold %.1f%% over last %d trades",
                wr * 100,
                CFG.halt_wr_threshold * 100,
                CFG.halt_wr_min_trades,
            )
            return (
                False,
                f"WR halt: {wr * 100:.1f}% < {CFG.halt_wr_threshold * 100:.1f}%",
            )

        return True, "ok"

    def check_concurrent(self, open_count: int) -> bool:
        return open_count < CFG.max_concurrent

    def on_trade(self) -> None:
        self._daily_count += 1

    def on_trade_closed(self, pnl: float) -> None:
        self.portfolio = max(1.0, self.portfolio + pnl)

    def update_portfolio(self, pnl: float) -> None:
        self.portfolio = max(1.0, self.portfolio + pnl)
