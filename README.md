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

All WR displays use 44.7% as the reference line — not the bull bot's 62.2%.

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
get_creds.py                 Cloudflare-safe credential generator (curl_cffi)
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
| `setup.py` | Done |
| `get_creds.py` | Done |
| `wrap_pusd.py` | Done |
| `approve_usdc.py` | Done |
| `.env.example` | Done |
| `bot.py` | Pending |
| `execution/executor.py` | Pending |
| `analysis/analyze.py` | Pending |
| `withdraw.py` | Pending |
| `redeem_now.py` | Pending |

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

## Polymarket V2 Infrastructure

Active since April 28, 2026. Collateral: **pUSD** (not USDC.e).

| Contract | Address |
|----------|---------|
| pUSD | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` |
| Collateral Onramp | `0x93070a847efEf7F70739046A929D47a521F5B8ee` |
| CTF Exchange V2 | `0xE111180000d2663C0091e4f400237545B87B996B` |
| NegRisk CTF Exchange V2 | `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` |
| USDC Transfer Helper V2 | `0xe2222d279d744050d28e00520010520000310F59` |

CLOB library: `py-clob-client-v2` (import as `py_clob_client_v2`).

---

## Prerequisites

- Python 3.11+
- tmux (`apt install tmux` / `brew install tmux`)
- MATIC in your wallet for gas (≥ 1 MATIC recommended)
- USDC.e on Polygon (to convert to pUSD)
- Polymarket account with a funded funder wallet

Verify Python version:

```bash
python3 --version   # must be 3.11+
tmux -V             # must be 3.x+
```

---

## Setup

### 1. Navigate to the project directory

```bash
cd /path/to/bear-oracle-confirmed-sniper
```

### 2. Create and activate virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

Add to your shell profile to auto-activate when entering the directory (optional):

```bash
echo 'source venv/bin/activate' >> .envrc   # direnv users
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Create credentials file

Copy the example and fill in your wallet credentials:

```bash
cp .env.example .env
```

Edit `.env` — set these two fields only:

```
POLY_PRIVATE_KEY=0x<your_private_key>
POLY_FUNDER_ADDRESS=0x<your_wallet_address>
```

> **Security**: `.env` is gitignored. Never commit private keys.

**Option A — standard (run on your server):**

```bash
python setup.py
```

**Option B — if `setup.py` is blocked by Cloudflare** (common on cloud/VPS IPs):

Run this on your **local machine** instead. It uses `curl_cffi` to impersonate
Chrome's TLS fingerprint and bypasses Cloudflare bot detection:

```bash
# On your local machine
pip install curl_cffi py-clob-client-v2 python-dotenv
python get_creds.py
```

Then copy the populated `.env` to the server:

```bash
scp .env <user>@<server>:~/polymarket-arbitrage-bot/bear-oracle-confirmed-sniper/.env
```

Before running either script, make sure your wallet is registered on Polymarket —
connect it at **app.polymarket.com** and complete the sign-in at least once.

Expected output:
```
--- Generating API Credentials ---
  API Key:        <key>
  API Secret:     xxxxxxxx...
  API Passphrase: xxxxxxxx...
Done. Credentials written to '.env'
```

Verify `.env` was written:

```bash
grep POLY_API_KEY .env   # should print a non-empty value
```

### 5. (Optional) Configure Telegram alerts

Add to `.env`:

```bash
TELEGRAM_BOT_TOKEN=<bot_token_from_botfather>
TELEGRAM_CHAT_ID=<your_chat_id>
```

Get your chat ID by messaging `@userinfobot` on Telegram.

### 6. Fund wallet with pUSD (one-time)

pUSD is the only accepted collateral on Polymarket V2. Convert your USDC.e:

```bash
python wrap_pusd.py
```

Expected output:
```
  USDC.e balance: $500.000000
  pUSD   balance: $0.000000

--- Step 1: Approve Collateral Onramp for USDC.e ---
  ✅ Already approved. Skipping.

--- Step 2: Wrap $500.000000 USDC.e → pUSD ---
  Tx sent (wrap): 0x...
  ✅ Confirmed! Block: 68432100

--- Final Balance Check ---
  USDC.e: $0.000000
  pUSD:   $500.000000
✅ Wrap complete. Now run: python3 approve_usdc.py
```

### 7. Approve pUSD for V2 exchange contracts (one-time)

```bash
python approve_usdc.py
```

This approves pUSD for all three V2 contracts (CTF Exchange, NegRisk CTF Exchange,
USDC Transfer Helper). Skips any already-approved spender automatically.

Expected output:
```
--- CTF Exchange (V2) ---
  Approved! Block: 68432105

--- NegRisk CTF Exchange (V2) ---
  Approved! Block: 68432108

--- USDC Transfer Helper (V2) ---
  Approved! Block: 68432111

--- Final Allowance Check ---
  [OK] CTF Exchange (V2): $115792...
  [OK] NegRisk CTF Exchange (V2): $115792...
  [OK] USDC Transfer Helper (V2): $115792...
```

> **Note**: The final allowance check may show `[MISSING]` for USDC Transfer Helper
> even after a confirmed approval. This is the known Polygon RPC stale-read issue —
> the on-chain approval is real (`receipt.status == 1`). The bot is ready to trade.

---

## Running with tmux

tmux keeps the bot alive after you disconnect from SSH and lets you split the
terminal into panes for simultaneous monitoring.

### tmux primer (essential commands)

| Action | Keys |
|--------|------|
| Detach from session (bot keeps running) | `Ctrl+b d` |
| Reattach to session | `tmux attach -t bear` |
| Split pane horizontally | `Ctrl+b "` |
| Split pane vertically | `Ctrl+b %` |
| Switch pane | `Ctrl+b ←/→/↑/↓` |
| Scroll up in pane | `Ctrl+b [` then arrow keys (q to exit) |
| Kill current pane | `Ctrl+b x` |
| List sessions | `tmux ls` |

---

### Phase 1 — Shadow mode (verify before trading)

Shadow mode runs the complete pipeline with **zero trades**. Verify feeds,
regime checks, and signal gates are working before risking capital.

**Create a dedicated tmux session:**

```bash
tmux new-session -s bear-shadow
source venv/bin/activate
python shadow.py --duration 7200   # 2-hour minimum run
```

**Detach and let it run in background:**

```bash
Ctrl+b d
```

**Check on it later:**

```bash
tmux attach -t bear-shadow
```

**After 2+ hours, generate the shadow report:**

```bash
# In a new terminal (or split pane)
source venv/bin/activate
python analysis/shadow_report.py
```

**Minimum criteria before proceeding to paper mode:**

- [ ] Feeds streaming: Chainlink and Binance prices updating, CL age < 30s
- [ ] Regime evaluating: EMA / Funding / CL 1h sub-checks firing per asset
- [ ] NO tokens discovered: at least 1 active market per asset visible
- [ ] Gate 2 (regime) accounts for majority of rejections — this is expected
- [ ] At least one PASS signal per asset observed
- [ ] Zero PASS signals during blackout hours `{0, 2, 6, 7, 17} UTC`

If regime is blocking 100% and no PASSes appear, the market is not in a bear
regime. This is correct — do not loosen the filters. Wait for a genuine bear regime.

**Kill shadow session when done:**

```bash
tmux kill-session -t bear-shadow
```

---

### Phase 2 — Paper trading

Paper mode executes the full signal pipeline and logs trades to SQLite with
simulated fills (slippage modeled). No real orders are placed.

**Create the paper trading session with a 3-pane layout:**

```bash
tmux new-session -s bear
```

**Split into 3 panes (bot + logs + analysis):**

```
Ctrl+b "       # split horizontally → top and bottom panes
Ctrl+b ↑       # go back to top pane
Ctrl+b %       # split top pane vertically → left and right
```

Layout:
```
┌─────────────────────┬─────────────────────┐
│  Pane 1: Bot UI     │  Pane 2: Log tail   │
│  (Rich dashboard)   │  (live log file)    │
├─────────────────────┴─────────────────────┤
│  Pane 3: Analysis / ad-hoc commands       │
└───────────────────────────────────────────┘
```

**Pane 1 — start the bot:**

```bash
source venv/bin/activate
python bot.py
```

**Pane 2 (Ctrl+b →) — tail the log:**

```bash
source venv/bin/activate
tail -f logs/$(date +%Y-%m-%d)_bot.log
```

**Pane 3 (Ctrl+b ↓) — run analysis while bot runs:**

```bash
source venv/bin/activate
python analysis/analyze.py --watch
```

**Detach and let it run:**

```bash
Ctrl+b d
```

**Reattach later:**

```bash
tmux attach -t bear
```

**Paper mode exit criteria (before going live):**

- [ ] ≥ 20 closed trades
- [ ] Rolling 20-trade WR > 52%
- [ ] Avg entry price in `$0.37–$0.47`
- [ ] No PASS signals during blackout hours
- [ ] Daily loss cap never triggered
- [ ] Max concurrent (2) never exceeded simultaneously

---

### Phase 3 — Live trading

Only proceed after paper mode meets all exit criteria above.

**In the existing `bear` session, stop the paper bot:**

```bash
Ctrl+c   # in Pane 1
```

**Verify pUSD balance before going live:**

```bash
# Pane 3
python -c "
from dotenv import dotenv_values
from web3 import Web3
env = dotenv_values('.env')
w3 = Web3(Web3.HTTPProvider('https://rpc.ankr.com/polygon'))
pusd = w3.eth.contract(
    address=Web3.to_checksum_address('0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB'),
    abi=[{'name':'balanceOf','type':'function','inputs':[{'name':'account','type':'address'}],'outputs':[{'name':'','type':'uint256'}],'stateMutability':'view'}]
)
bal = pusd.functions.balanceOf(Web3.to_checksum_address(env['POLY_FUNDER_ADDRESS'])).call() / 1e6
print(f'pUSD balance: \${bal:.2f}')
"
```

**Start live bot (Pane 1):**

```bash
python bot.py --live --confirm-live --accept-risk --portfolio 500
```

Replace `500` with your actual pUSD portfolio size. The bot uses this to
calculate position sizing relative to the configured stake per trade.

**Live monitoring layout — recommended 4-pane setup:**

```bash
# In the bear session, add a 4th pane for redemption/withdraw ops
Ctrl+b "        # split bottom pane
```

```
┌─────────────────────┬─────────────────────┐
│  Pane 1: Live bot   │  Pane 2: Log tail   │
│  (Rich dashboard)   │                     │
├─────────────────────┼─────────────────────┤
│  Pane 3: analyze    │  Pane 4: on-chain   │
│  --watch            │  ops (redeem/etc)   │
└─────────────────────┴─────────────────────┘
```

**Pane 4 — on-chain operations (run as needed):**

```bash
# Redeem resolved winning positions
python redeem_now.py

# Withdraw pUSD to another address
python withdraw.py
```

---

## Managing a Running Session

### Reattach after SSH disconnect

```bash
tmux attach -t bear
```

If you have multiple sessions:

```bash
tmux ls                 # list all sessions
tmux attach -t bear     # attach by name
```

### Check bot status without attaching

```bash
# Tail last 50 lines of today's log
tail -50 logs/$(date +%Y-%m-%d)_bot.log

# Quick trade count from DB
python -c "
import sqlite3
conn = sqlite3.connect('bear_trades.db')
rows = conn.execute('SELECT status, COUNT(*) FROM trades GROUP BY status').fetchall()
for r in rows: print(f'{r[0]}: {r[1]}')
conn.close()
"
```

### Graceful shutdown

```bash
# Attach to session
tmux attach -t bear

# In Pane 1 (bot)
Ctrl+c      # sends SIGINT — bot completes any open positions then exits
```

### Force kill (emergency only)

```bash
tmux kill-session -t bear
```

This immediately terminates all panes. Any open paper positions are left in
OPEN state in the DB — run `analyze.py` to reconcile manually.

---

## Analysis

```bash
# Shadow mode verification (before paper/live)
python analysis/shadow_report.py
python analysis/shadow_report.py --passes-only
python analysis/shadow_report.py --since 3600   # last 1 hour

# Live trade analysis (paper / live mode)
python analysis/analyze.py                      # summary stats
python analysis/analyze.py --watch              # live tail
python analysis/analyze.py --regime             # regime state at each entry
python analysis/analyze.py --decoys             # anti-decoy filter breakdown
```

All WR displays show a reference line at **44.7%** (breakeven).

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

If the 20-trade rolling WR drops below 52%, the bot halts automatically and
alerts via Telegram. Run `analyze.py --regime` to diagnose whether the regime
filter is too loose or the bear trend has ended.

---

## Troubleshooting

**Feeds not streaming after 30s**
- Check network connectivity and firewall rules for WebSocket connections
- Chainlink RTDS uses `wss://ws-live-data.polymarket.com` — must be reachable
- Binance uses `wss://data-stream.binance.com` — check if your server geolocates outside a blocked region

**No NO tokens discovered**
- Gamma API may be slow — wait one full discovery cycle (30s)
- Verify the asset slugs resolve: `curl "https://gamma-api.polymarket.com/events?slug=btc-updown-5m-<window_ts>"`

**Regime always NEUTRAL**
- This is correct behavior when the market is not in a bear regime
- Check sub-checks individually: EMA (price vs 4h EMA), funding rate (perp funding negative?), CL 1h net (>0.3% drop over 60 min?)
- Do not bypass or soften Gate 0 — no regime means no edge

**`setup.py` shows Allowance: $0.00`**
- Known false alarm from CLOB backend allowance view
- What matters is the on-chain `approve_usdc.py` ran successfully
- Verify on Polygonscan that the approve() tx confirmed with status 1

**`setup.py` blocked by Cloudflare (403 / "Could not derive api key")**
- Cloud/VPS IPs (GCP, AWS, DigitalOcean) and some residential IPs are blocked
  by Cloudflare on Polymarket's auth endpoints
- Use `get_creds.py` from your local machine instead — it uses `curl_cffi` to
  impersonate Chrome's TLS fingerprint and bypasses the block
- Then `scp .env` to the server

**`py_clob_client_v2` import error**
- Ensure virtual environment is activated: `source venv/bin/activate`
- Reinstall: `pip install -r requirements.txt --force-reinstall`

**RPC timeout / stale balance after redemption**
- Public Polygon RPCs return stale on-chain state immediately after a confirmed tx
- `receipt.status == 1` is the source of truth — not the balance read right after
- Wait 1–2 blocks (~3s) and recheck if needed
