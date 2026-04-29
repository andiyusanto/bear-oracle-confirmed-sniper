"""Configuration for Oracle-Confirmed Bear Sniper.

Trades NO tokens on BTC/ETH/SOL price markets when Chainlink confirms a DOWN
move from the window opening price AND a bear regime is active (Gate 0).
All thresholds flow from the breakeven math: WR ≥ 44.7% at avg entry $0.44.
"""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # ── Credentials ─────────────────────────────────────────────────
    private_key: str = os.getenv("POLY_PRIVATE_KEY", "")
    api_key: str = os.getenv("POLY_API_KEY", "")
    api_secret: str = os.getenv("POLY_API_SECRET", "")
    api_passphrase: str = os.getenv("POLY_API_PASSPHRASE", "")
    funder_address: str = os.getenv("POLY_FUNDER_ADDRESS", "")
    sig_type: int = int(os.getenv("POLY_SIG_TYPE", "0"))
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # ── Endpoints ───────────────────────────────────────────────────
    clob_host: str = "https://clob.polymarket.com"
    gamma_url: str = "https://gamma-api.polymarket.com/events"
    rtds_url: str = "wss://ws-live-data.polymarket.com"
    binance_ws: str = "wss://data-stream.binance.com/stream"
    binance_rest: str = "https://fapi.binance.com"  # futures REST for regime checks

    # ── Assets and markets ──────────────────────────────────────────
    assets: list = field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    durations: list = field(default_factory=lambda: [("5m", 300), ("15m", 900)])

    # ── Gate 0 — Regime filter (15-minute async cycle) ──────────────
    # Bear regime requires ALL THREE per asset:
    #   A) 4h candle: current price < EMA(20)
    #   B) Funding rate: negative for ≥ 2 of last 3 intervals
    #   C) Chainlink 1h net: DOWN ≥ 0.3% over last 60 minutes
    #
    # Fail-safe: any error in regime check defaults asset to NEUTRAL → no trades.
    regime_check_interval_sec: int = 900  # 15 minutes between checks
    ema_period_4h: int = 20  # EMA(20) of 4h candles
    funding_rate_bear_threshold: float = 0.0  # rate must be strictly < this
    funding_intervals_required: int = 2  # of last 3 intervals negative
    chainlink_1h_net_pct_bear: float = -0.003  # -0.3% net over 60 min

    # ── Signal gates 1–7 ────────────────────────────────────────────
    # Gate 1: UTC hour NOT in blackout set
    # Gate 2: asset regime == BEAR
    # Gate 3: oracle direction == DOWN
    # Gate 4: time remaining in snipe window (tiered by delta strength)
    # Gate 5: delta ≥ min_delta_pct from window open (DOWN direction)
    # Gate 6: Binance 1-min confirms DOWN
    # Gate 7: NO token best_ask in [no_price_min, no_price_max]
    blackout_hours: set = field(default_factory=lambda: {0, 2, 6, 7, 17})

    # Delta tiers — define the snipe window entry point
    strong_delta_pct: float = 0.025  # STRONG: delta ≥ 0.025% → enter from T-75s
    normal_delta_pct: float = 0.015  # NORMAL: delta ≥ 0.015% → enter from T-55s
    weak_delta_pct: float = 0.010  # WEAK:   delta ≥ 0.010% → enter from T-40s
    min_delta_pct: float = 0.010  # anything below WEAK is ignored entirely

    # Snipe entry window per tier (seconds before resolution)
    snipe_entry_strong_sec: float = 75.0  # STRONG signals can enter early
    snipe_entry_normal_sec: float = 55.0  # NORMAL signals
    snipe_entry_weak_sec: float = 40.0  # WEAK signals — latest entry allowed

    # Ghost-zone hard block — never enter with less than this TTL
    snipe_exit_sec: float = 20.0

    # NO token price range — outside this means negative EV or already priced in
    no_price_min: float = 0.37
    no_price_max: float = 0.53

    # ── Anti-decoy filters (applied after Gate 7) ────────────────────
    # Applied in order; first failure rejects the signal.
    # Never remove a filter without ≥ 30-trade data showing zero catches.
    consecutive_down_ticks_required: int = 3  # ticks all below window open
    volatility_damper_pct: float = 0.0008  # 5-min CL range > 0.08% → skip
    rebound_veto_ratio: float = 0.30  # UP recovery > 30% of drop → skip
    no_book_depth_min_usd: float = 200.0  # minimum NO side liquidity

    # Staleness gate — both feeds must not be stale at the same time
    cl_staleness_hard_sec: float = 30.0

    # ── Risk management ─────────────────────────────────────────────
    max_concurrent: int = 2  # never stack into a turning market
    daily_loss_cap_usd: float = 15.0  # kill switch threshold ($)
    halt_wr_threshold: float = 0.52  # pause live trading below this WR
    halt_wr_min_trades: int = 20  # rolling window size for WR check

    # ── Execution ───────────────────────────────────────────────────
    shadow_mode: bool = (
        False  # gate-level logging, zero trades (for strategy verification)
    )
    paper_mode: bool = True
    stake_per_trade_usd: float = 3.0  # flat stake — not Kelly
    min_shares: float = 5.0  # Polymarket minimum order size
    live_max_usd: float = 15.0  # hard cap per live order

    # ── Fee structure ────────────────────────────────────────────────
    # Taker fee confirmed ~1.31% on-chain; 1.5% is a safe ceiling.
    # Breakeven WR at avg entry $0.44, fee 1.5%:
    #   Win:  +$0.5534  Loss: -$0.4466  → breakeven at 44.7%
    taker_fee_pct: float = 1.5

    # ── Infrastructure ──────────────────────────────────────────────
    db_path: str = "bear_trades.db"
    log_dir: str = "logs"
    poll_interval: float = 0.1  # main loop sleep
    discovery_interval: float = 30.0  # market re-discovery interval
    book_cache_sec: float = 2.0  # order book cache TTL
    cooldown_sec: float = 0.5  # minimum seconds between orders


CFG = Config()
