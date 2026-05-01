# Oracle-Confirmed Bear Sniper

Trades Polymarket 5-minute and 15-minute BTC/ETH/SOL prediction markets by
reading the Chainlink oracle seconds before resolution — but only when a
confirmed bear regime is active. Buys NO tokens as a taker when oracle
confirms a sustained DOWN move from the window opening price.

---

## Edge

Chainlink settles Polymarket CTF binary markets. Its current value **is** the
resolution answer. In a bear regime, sustained DOWN moves from window open are
highly predictive of NO token resolution at $1.00.

The bull bot observed 11.1% WR on DOWN signals during bull-dominant conditions.
That data shows DOWN signals are **regime-dependent** — in a bear regime the
same oracle signal becomes the primary edge.

**Gate 0 (regime filter) is the entire hypothesis.** Without it the bot has no
edge and makes zero trades.

---

## Breakeven Math

```
Avg NO entry: $0.44   Taker fee: 1.5%

Win  (NO resolves $1.00): +$0.5534 per unit
Loss (NO resolves $0.00): −$0.4466 per unit

Breakeven WR = 0.4466 / (0.5534 + 0.4466) = 44.7%

Target WR > 58%  (13-point buffer above breakeven)
Halt threshold:  52% rolling WR over last 20 trades
```

All WR displays in the dashboard and analyze.py use 44.7% as the reference
line — not the bull bot's 62.2%.

---

## Architecture

```
shadow.py                    Shadow mode entrypoint (zero trades — verify first)
bot.py                       Main async event loop (paper / live)
core/
  config.py                  CFG dataclass — single source of all thresholds
  models.py                  RegimeState enum, Token, OracleState, Signal, Trade
  database.py                SQLite (bear_trades.db) with regime_log + shadow_log
  shadow.py                  ShadowLogger: in-memory gate telemetry, batch SQLite flush
feeds/
  prices.py                  Chainlink RTDS + Binance WebSocket + price history
  markets.py                 Gamma API — NO token discovery only
  regime.py                  RegimeMonitor: 15-min bear regime check (Gate 0)
engine/
  signal.py                  BearEngine: 7 gates + 5 anti-decoy filters
  risk.py                    RiskManager: kill switch, daily cap, WR halt
execution/
  executor.py                Paper and live trade execution
ui/
  dashboard.py               Rich terminal UI with REGIME panel
analysis/
  shadow_report.py           Post-run shadow mode gate breakdown and PASS signals
  analyze.py                 Live trade analysis with regime overlay
setup.py                     Derive API creds from private key → .env
wrap_pusd.py                 Convert USDC.e → pUSD via Collateral Onramp
approve_usdc.py              On-chain pUSD approve() for V2 CLOB contracts
withdraw.py                  Interactive pUSD withdrawal
redeem_now.py                Manual CTF redemption
```

### Build Status

| Module | Status |
|--------|--------|
| `core/config.py` | Done |
| `core/models.py` | Done |
| `core/database.py` | Done |
| `core/shadow.py` | Done |
| `feeds/prices.py` | Done |
| `feeds/markets.py` | Done |
| `feeds/regime.py` | Done |
| `engine/signal.py` | Done |
| `engine/risk.py` | Done |
| `ui/dashboard.py` | Done |
| `shadow.py` | Done |
| `analysis/shadow_report.py` | Done |
| `bot.py` | Pending |
| `execution/executor.py` | Pending |
| `analysis/analyze.py` | Pending |
| `setup.py`, on-chain scripts | Pending |

---

## Gate Architecture

### Gate 0 — Bear Regime Filter (`feeds/regime.py`)

Runs every 15 minutes as an independent asyncio task. Never adds latency to
the per-trade signal loop. Each asset evaluated independently.

Bear regime requires **all three**:

| Check | Condition |
|-------|-----------|
| A — 4h EMA | Current price < EMA(20) of 4h Binance Futures candles |
| B — Funding rate | Binance perp funding negative for ≥ 2 of last 3 intervals |
| C — CL 1h net | Chainlink price DOWN ≥ 0.3% over the last 60 minutes |

Failure: asset enters NEUTRAL → zero trades until all 3 pass again.
All transitions logged to `regime_log` SQLite table with timestamp + reason.

### Gates 1–7 — Per-Trade Signal (`engine/signal.py`)

All must pass in order:

| Gate | Condition |
|------|-----------|
| 1 | UTC hour NOT in `{0, 2, 6, 7, 17}` |
| 2 | Asset regime == BEAR |
| 3 | Oracle direction == DOWN (Chainlink delta < 0 vs window open) |
| 4 | TTL within snipe window for delta tier (see table below) |
| 5 | `abs(delta)` >= `min_delta_pct` |
| 6 | Binance 1-min confirms DOWN (fresh data only) |
| 7 | NO token `best_ask` in `[$0.37, $0.53]` |

**Tiered snipe windows:**

| Tier | Delta | Entry window |
|------|-------|-------------|
| STRONG | ≥ 0.025% | T-75s to T-20s |
| NORMAL | ≥ 0.015% | T-55s to T-20s |
| WEAK | ≥ 0.010% | T-40s to T-20s |

### Anti-Decoy Filters (after Gate 7)

Applied in order; first failure rejects the signal:

1. **Ghost-zone hard block** — TTL < 20s → reject unconditionally
2. **Volatility damper** — 5-min Chainlink range > 0.08% → skip (whipsaw)
3. **Consecutive tick check** — require 3 consecutive ticks below window open
4. **Micro-rebound veto** — last 2 ticks show UP recovery > 30% of drop → skip
5. **Liquidity check** — NO token depth < $200 → skip

---

## Shadow Mode

Shadow mode runs the complete pipeline — feeds, regime checks, market
discovery, and signal evaluation — with **zero trades**. Every gate evaluation
is logged to SQLite so you can verify the strategy before risking capital.

```bash
python shadow.py                  # run until Ctrl+C
python shadow.py --duration 3600  # stop after 1 hour
python shadow.py --db my.db       # custom DB path (default: shadow_run.db)
```

**What it verifies:**
- Chainlink RTDS and Binance feeds are streaming live prices
- Regime monitor is evaluating all 3 sub-checks correctly
- NO tokens are being discovered for BTC/ETH/SOL markets
- Signal gates are firing and rejecting at the right stages
- PASS signals are appearing (strategy has live edge before paper trading)

The live dashboard shows:
- Per-asset feed health (Chainlink price, Binance price, staleness, 1h net)
- Per-asset regime state with sub-check breakdown (EMA / Funding / CL 1h)
- Gate rejection breakdown with bar chart and percentages
- Eval rate, PASS rate, and market count in the footer

**After the run, generate a full report:**

```bash
python analysis/shadow_report.py                    # full breakdown
python analysis/shadow_report.py --passes-only      # just PASS signals
python analysis/shadow_report.py --since 3600       # last 1 hour only
```

The report shows gate rejection share, per-asset breakdown, regime filter
efficiency, hourly PASS rate (with blackout hours marked), and the last 30
would-be PASS signals with price, delta, TTL, and reason string.

**Minimum viable shadow run before paper trading:**
- ≥ 2 hours of data
- At least one PASS signal per asset observed
- Gate 2 (regime) accounting for the majority of rejections — this is expected
  and correct (markets spend most time outside bear regime)
- No PASS signals during blackout hours

---

## Polymarket V2 Infrastructure

Active since April 28, 2026. Collateral: **pUSD** (not USDC.e).

| Contract | Address |
|----------|---------|
| pUSD | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` |
| Collateral Onramp | `0x93070a847efEf7F70739046A929D47a521F5B8ee` |
| CTF Exchange V2 | `0xE111180000d2663C0091e4f400237545B87B996B` |
| NegRisk CTF Exchange V2 | `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` |
| USDC Transfer Helper V2 | `0xe2222d279d744050d28e00520010520000310F59` |

CLOB library: `pip install py-clob-client-v2` (import as `py_clob_client`).

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
python setup.py   # derives API keys from private key, writes .env
```

### 3. Fund wallet with pUSD

```bash
python wrap_pusd.py    # convert USDC.e → pUSD (one-time)
python approve_usdc.py # approve pUSD for CLOB spending (one-time)
```

### 4. Run shadow mode (verify strategy setup)

```bash
python shadow.py --duration 7200   # 2-hour minimum recommended
python analysis/shadow_report.py   # review gate breakdown and PASS signals
```

Do not proceed to paper trading until shadow mode confirms:
- Feeds streaming, regime evaluating, PASS signals appearing

### 5. Run in paper mode

```bash
python bot.py
```

### 6. Run live (after paper validates ≥ 20 trades at WR > 52%)

```bash
python bot.py --live --confirm-live --accept-risk --portfolio 500
```

---

## Performance Targets

| Metric | Target | Halt |
|--------|--------|------|
| Win Rate | > 58% | < 52% (20-trade rolling) |
| Breakeven WR | **44.7%** | — |
| Trades/day | ≥ 3 | < 1 (loosen regime or delta) |
| Avg entry | $0.37–$0.47 | > $0.53 (negative EV) |
| Max concurrent | 2 | — |
| Daily loss cap | — | $15 kill switch |

---

## Analysis

```bash
# Shadow mode verification (before paper/live)
python analysis/shadow_report.py
python analysis/shadow_report.py --passes-only
python analysis/shadow_report.py --since 3600

# Live trade analysis (paper / live mode)
python analysis/analyze.py                    # summary stats
python analysis/analyze.py --watch            # live tail
python analysis/analyze.py --regime           # regime overlay per trade
python analysis/analyze.py --decoys           # anti-decoy filter breakdown
```

All WR displays show a reference line at **44.7%** (breakeven).
