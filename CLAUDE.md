# Claude Working Instructions — Oracle-Confirmed Bear Sniper

## Anti-Hallucination Protocol
- **Read Before Action**: NEVER suggest a change to a file you have not read
  in the current session.
- **Read the Bull Bot First**: The reference implementation lives at
  `/home/tengkorakhitam/polymarket-arbitrage-bot/oracle-confirmed-sniper/`.
  Before building any module, read the equivalent bull bot file. Copy
  patterns — do not reinvent them.
- **Strict Verification**: Check `requirements.txt` before assuming a library
  version supports a feature.
- **Reference Code**: When explaining logic, quote a snippet from the actual
  source file. No placeholder code.
- **Web Search**: Use `web_search` for any library released or updated after
  2024 (Polymarket APIs, py-clob-client-v2, web3.py, Binance futures API).

## Python Standards
- **Environment**: Always assume virtual environment at `venv/`. Run
  `pip list` to verify installed packages.
- **Linter**: Run `ruff check . && ruff format .` after every edit.
- **Type hints**: Required on all functions and class fields.
- **Pathlib**: Use `pathlib.Path` for file operations. Never hardcode OS paths.
- **Style**: PEP 8 strictly.

---

# Project: Oracle-Confirmed Bear Sniper

## What This Bot Does

Trades Polymarket 5-minute and 15-minute BTC/ETH/SOL prediction markets by
reading the Chainlink oracle price feed seconds before market resolution —
identical infrastructure to the bull bot — but targeting the DOWN side.

When the oracle has dropped significantly from a window's opening price AND
the broader market is in a confirmed bear regime, NO tokens are frequently
mispriced below their expected resolution value of $1.00. The bot buys NO
tokens as a taker (instant fill at best_ask).

**Only trades DOWN direction in BEAR regime** — UP oracle signals are ignored.
**Regime filter is mandatory** — without confirmed bear conditions, edge
disappears and the bot makes zero trades.

**Structural edge**: Chainlink settles Polymarket CTF markets. Its current
value IS the resolution answer. In a bear regime, sustained DOWN moves from
window open are highly predictive of NO token resolution.

## Why This Is Different From the Bull Bot

The bull bot (oracle-confirmed-sniper) observed 11.1% WR on DOWN signals.
That data was collected during bull-dominant market conditions — DOWN signals
in a bull regime are anti-predictive noise. In a bear regime, the same DOWN
oracle signal becomes the primary edge.

The core addition is `feeds/regime.py` — a 15-minute cycle that confirms
bear conditions before any trade is allowed. Everything else is adapted from
the bull bot.

## Architecture at a Glance

| File | Role |
|------|------|
| `bot.py` | Main async event loop |
| `core/config.py` | All parameters — `CFG` singleton |
| `core/models.py` | RegimeState enum + Token, OracleState, Signal, Trade |
| `core/database.py` | SQLite persistence (`bear_trades.db`) |
| `core/redeem.py` | On-chain CTF redemption via web3 |
| `feeds/prices.py` | Chainlink RTDS + Binance WebSocket + funding rate |
| `feeds/markets.py` | Gamma API polling — NO token discovery |
| `feeds/regime.py` | **NEW** RegimeMonitor: 15-min bear regime check |
| `engine/signal.py` | BearEngine: Gate 0 + 7-gate signal + anti-decoy filters |
| `engine/risk.py` | RiskManager: kill switch, daily cap, concurrent limit |
| `execution/executor.py` | Trade execution — paper and live |
| `ui/dashboard.py` | Rich terminal UI with REGIME panel |
| `setup.py` | Derive API creds from private key → write `.env` |
| `wrap_pusd.py` | One-time: convert USDC.e → pUSD via Collateral Onramp |
| `approve_usdc.py` | On-chain pUSD approve() for V2 CLOB contracts |
| `withdraw.py` | Interactive pUSD withdrawal to any Polygon address |
| `redeem_now.py` | Manual CTF redemption with position list + confirmation |
| `analysis/analyze.py` | Trade analysis: regime overlay, decoy breakdown, --watch |

## Regime Filter — Gate 0 (runs every 15 minutes, independent async task)

This is the most important gate. It runs on a 15-minute asyncio task
completely outside the per-trade signal loop so it never adds latency.

Bear regime requires ALL THREE for each asset independently:
A) Binance 4h candle: price < EMA(20) for the asset
B) Binance perpetual funding rate: negative for ≥ 2 of last 3 intervals
C) Chainlink 1h net move: DOWN ≥ 0.3% over last 60 minutes


If any condition fails → asset enters HALT_NEUTRAL → zero trades for that
asset until all 3 pass again. BTC/ETH/SOL are evaluated independently.

Store every regime transition in SQLite with timestamp and reason string.
Never bypass or soft-fail the regime check. No trade is worth taking without it.

## Signal Gate Order (Gates 1–7, all must pass)

1. UTC hour NOT in blackout set (currently empty — no hours blocked; set from shadow data)
2. Asset regime == BEAR (from Gate 0)
3. Oracle direction == DOWN
4. Time remaining within snipe window by delta tier:
   - STRONG (delta ≥ 0.025%): T-75s to T-40s
   - NORMAL (delta ≥ 0.015%): T-55s to T-40s
   - WEAK   (delta ≥ 0.010%): T-40s to T-25s
5. Chainlink delta ≥ `min_delta_pct` from window opening (DOWN direction)
6. Binance 1-min confirms DOWN (last completed candle close < open)
7. NO token best_ask in `$0.30–$0.63` (widened from $0.37–$0.53 — bear regime NO tokens price higher)

If all 7 pass → proceed to anti-decoy filters → execute.

## Anti-Decoy Filters (applied after Gate 7, before order submission)

These are the primary WR defenders. Apply in this order:
Ghost-zone hard block: TTL < 20s → reject unconditionally
Volatility damper: 5-min Chainlink range > 0.2% → skip (was 0.08% — too tight for bear moves)
Consecutive tick check: require 3 consecutive ticks below window open
Micro-rebound veto: if last 2 ticks show UP recovery > 30% of the initial drop → skip
Liquidity check: NO token depth < $100 → skip (was $200 — NO tokens are thinner than YES)


Never remove a filter without a data-backed justification from analyze.py
showing it is catching zero true positives over ≥ 30 trades.

## Breakeven Math (hardcoded context)

At avg NO entry $0.44, taker fee 1.5%:
- Win (NO resolves $1): `+$0.5534` per unit
- Loss (NO resolves $0): `−$0.4466` per unit
- Breakeven WR: **44.7%**

Target WR > 58% (13-point buffer). If 20-trade rolling WR drops below 52%,
halt and alert. Always compare observed WR against 44.7% first when reviewing
trade data — that is the only number that determines whether the bot has edge.

---

# Polymarket V2 Infrastructure (live since April 28, 2026)

## Collateral Token: pUSD (NOT USDC.e)

| Contract | Address |
|----------|---------|
| pUSD (active collateral) | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` |
| Collateral Onramp | `0x93070a847efEf7F70739046A929D47a521F5B8ee` |
| CTF Exchange V2 | `0xE111180000d2663C0091e4f400237545B87B996B` |
| NegRisk CTF Exchange V2 | `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` |
| USDC Transfer Helper V2 | `0xe2222d279d744050d28e00520010520000310F59` |

## CLOB Client Library

- **Package**: `py-clob-client-v2`
- **Import module**: `py_clob_client` (unchanged — no import changes needed)
- **Install**: `pip install py-clob-client-v2`

## Known RPC Quirk — Stale State

Public Polygon RPCs return stale on-chain state immediately after a confirmed
transaction. `receipt.status == 1` is the source of truth — not the balance
read immediately after.

Always use this pattern in on-chain scripts:
```python
gas_price = int(w3.eth.gas_price * 1.3)
nonce = w3.eth.get_transaction_count(wallet, "pending")
receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
assert receipt.status == 1, "Transaction failed"

CLOB API get_balance_allowance() Warning
setup.py may show Allowance: $0.00 ❌. This is a cosmetic false alarm —
the CLOB backend's allowance view is unreliable. What matters is the on-chain
approve() executed by approve_usdc.py. Do not attempt to fix this warning.

feeds/regime.py — Implementation Contract
This module must be built before any signal or executor logic.


class RegimeMonitor:
    async def check_regime(self, asset: str) -> RegimeState: ...
    async def _binance_4h_ema(self, asset: str) -> bool: ...     # price < EMA(20)?
    async def _funding_rate_negative(self, asset: str) -> bool:  # ≥2 of 3 negative?
    async def _chainlink_1h_net(self, asset: str) -> bool: ...   # DOWN ≥ 0.3%?
    async def run_loop(self) -> None: ...                        # 15-min async cycle
RegimeState enum: BEAR | NEUTRAL | BULL

The run_loop task must:

Check all 3 assets on every cycle
Write regime transitions to SQLite (regime_log table)
Expose current regime state via in-memory cache (dict keyed by asset)
Never raise — log errors and default to NEUTRAL on failure (fail safe)
feeds/markets.py — NO Token Discovery
Adapt from bull bot's YES token discovery. Key differences:

Identify NO token per market (outcome == "No" or token index 1)
Track best_ask on NO token (this is our entry price)
Only surface markets where NO token depth > $200
Confirm NO token is still active (not resolved) before adding to pool
ui/dashboard.py — REGIME Panel
Add a REGIME panel at the top of the Rich layout (above the trade table):


┌─ REGIME STATUS ──────────────────────────────────────────────┐
│  BTC: BEAR 🟢  ETH: BEAR 🟢  SOL: NEUTRAL 🟡                 │
│  Last change: 2026-05-01 09:15 UTC — SOL funding rate +0.01% │
│  BTC in BEAR: 4h 22m                                         │
└──────────────────────────────────────────────────────────────┘
Color codes: BEAR = green (tradeable), NEUTRAL = yellow, BULL = red (halt).

analysis/analyze.py — Required Additions vs Bull Bot
Beyond the bull bot's analyze.py, this must support:

Regime overlay: each trade row shows the regime state at entry time
Decoy breakdown: table showing how many signals each anti-decoy filter rejected — reveals which filters are doing real work vs. dead weight
Rolling 20-trade WR sparkline in terminal (Rich progress bar or chars)
Breakeven reference line at 44.7% on all WR displays (not 62.2%)
Regime efficiency: trades attempted vs. hours in BEAR state (shows whether regime filter is too tight or too loose)
Development Rules
On-Chain Scripts (wrap_pusd.py, approve_usdc.py, withdraw.py, redeem_now.py)
Always print wallet address and token balance before any transaction
Always print tx hash immediately after send_raw_transaction()
Always wait for receipt and check receipt.status == 1 before marking success
Never send a tx without confirmation prompt if the script is interactive
Always try multiple RPCs from POLYGON_RPCS list before failing
Bot Logic (feeds/, engine/, execution/)
Do not change signal gate thresholds without analyze.py output justification
Do not remove anti-decoy filters without ≥ 30-trade data showing zero catches
Do not add new gates without verifying trade count stays ≥ 3/day in paper mode
CFG is the single source of truth — never hardcode thresholds outside config
PriceFeeds.best_price() prefers Chainlink if fresh (<30s), Binance fallback
Regime check must NEVER be bypassed — treat it as a hard dependency, not a flag
Config Changes
Before changing any parameter in core/config.py, state:

What problem it solves
What the current value is and what it will become
Expected effect on trade count, WR, and regime sensitivity
Testing
pytest for unit tests
On-chain scripts: test with a small amount first
No mocking of web3 or CLOB client in integration paths — use real RPC/API calls
Regime logic: unit test each of the 3 sub-checks independently with fixture data
Performance Benchmarks
Metric  Target  Halt Threshold
Win Rate  > 58% < 52% (20-trade rolling)
Breakeven WR  44.7% —
Trades/day  ≥ 3 < 1 (loosen regime or delta)
Avg entry $0.37–$0.47 > $0.53 (negative EV)
Max concurrent  2 —
Daily loss cap  $15 kill switch triggers
If 20-trade rolling WR < 52%, stop live trading immediately and run
analyze.py --watch to identify whether the failure is:

Regime filter too loose (letting through neutral conditions)
Delta threshold too low (weak signals)
Ghost-zone slippage (TTL < 20s entries — check snipe_exit_sec)
Genuine regime change (bear trend ended)


---

Key structural decisions baked in:

- **Regime as Gate 0 is non-negotiable** — spelled out twice in rules to prevent any future session from treating it as optional
- **Anti-decoy filters are named and ordered** — Claude will never collapse them into a single vague "filter step"
- **Breakeven 44.7% is hardcoded everywhere** — no session will accidentally use the bull bot's 62.2% on trade reviews
- **feeds/regime.py implementation contract** — the async interface is defined so Claude builds it correctly the first time
- **Performance table** gives halt thresholds in one place so any session can immediately tell if the bot needs to stop




