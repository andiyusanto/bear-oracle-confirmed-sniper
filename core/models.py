"""Data models for the Oracle-Confirmed Bear Sniper."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class RegimeState(Enum):
    BEAR = "BEAR"  # all 3 conditions pass → trades allowed
    NEUTRAL = "NEUTRAL"  # any condition fails → no trades
    BULL = "BULL"  # reserved for future upside-regime detection


@dataclass
class Token:
    token_id: str
    asset: str  # BTC, ETH, SOL
    direction: str  # always "DOWN" — NO tokens win when price falls
    duration: str  # "5min", "15min"
    end_ts: float  # UNIX timestamp of window resolution
    window_ts: int  # UNIX timestamp of window opening
    book_price: float = 0.5  # best_ask on the NO token (taker entry price)
    book_updated: float = 0.0
    book_spread: float = 0.0  # bid-ask spread fraction (e.g. 0.15 = 15%)
    book_depth_usd: float = 0.0  # ask-side liquidity depth in USD
    conditionId: str = ""  # CTF conditionId for redemption


@dataclass
class OracleState:
    asset: str
    window_ts: int
    opening_price: float
    current_price: float = 0.0
    delta_pct: float = 0.0  # signed %; negative = DOWN from window open
    oracle_says: str = ""  # "DOWN" only — "UP" signals are ignored
    binance_agrees: bool = False
    last_update: float = 0.0


@dataclass
class Signal:
    token: Token
    oracle: OracleState
    entry_price: float  # best_ask on NO token at signal time
    size_usdc: float  # flat stake from CFG.stake_per_trade_usd
    time_remaining: float  # seconds until window resolution
    consecutive_ticks: int = 0  # how many consecutive ticks were below window open
    delta_tier: str = "WEAK"  # STRONG | NORMAL | WEAK


@dataclass
class Trade:
    id: str
    asset: str
    direction: str  # always "DOWN" in bear bot
    side: str  # always "NO"
    entry_price: float
    size_usdc: float
    oracle_delta: float  # signed % delta at entry
    regime_state: str  # "BEAR" — captured at entry time for analysis
    pnl: float = 0.0
    status: str = "OPEN"  # OPEN, EXPIRED, CANCELLED
    mode: str = "PAPER"
    opened_at: float = 0.0
    closed_at: Optional[float] = None
    window_ts: int = 0
    time_remaining: float = 0.0
    binance_price: float = 0.0
    chainlink_price: float = 0.0
    opening_price: float = 0.0
    duration_sec: int = 300  # 300 for 5m markets, 900 for 15m
    condition_id: str = ""  # CTF conditionId — used for redemption
    delta_tier: str = "WEAK"  # STRONG | NORMAL | WEAK at entry
