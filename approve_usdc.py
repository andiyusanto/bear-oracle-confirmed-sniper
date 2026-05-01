"""
Polymarket pUSD On-Chain Approval

Directly submits ERC20 approve() transactions to Polygon for all three
V2 exchange contracts that need to spend pUSD on your behalf.

After Polymarket Exchange V2 (April 28, 2026), collateral is pUSD —
not USDC.e. Run AFTER wrap_pusd.py has converted your USDC.e to pUSD.

Usage:
    python3 approve_usdc.py
"""

import sys
from pathlib import Path

from dotenv import dotenv_values
from web3 import Web3

# ── Polygon contracts ─────────────────────────────────────────────────────────
POLYGON_RPCS = [
    "https://rpc.ankr.com/polygon",
    "https://polygon-mainnet.public.blastapi.io",
    "https://polygon-bor-rpc.publicnode.com",
    "https://rpc-mainnet.matic.quiknode.pro",
    "https://polygon-rpc.com",
]

# pUSD: Polymarket USD (V2 collateral, backed 1:1 by native USDC)
PUSD = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")

SPENDERS: list[tuple[str, str]] = [
    ("CTF Exchange (V2)", "0xE111180000d2663C0091e4f400237545B87B996B"),
    ("NegRisk CTF Exchange (V2)", "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"),
    ("USDC Transfer Helper (V2)", "0xe2222d279d744050d28e00520010520000310F59"),
]

MAX_UINT256 = 2**256 - 1

ERC20_ABI = [
    {
        "name": "approve",
        "type": "function",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
    },
    {
        "name": "allowance",
        "type": "function",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
]


def main() -> None:
    if not Path(".env").exists():
        print("  .env not found — run setup.py first.")
        sys.exit(1)

    env = dotenv_values(".env")
    pk = env.get("POLY_PRIVATE_KEY", "").strip()
    funder = env.get("POLY_FUNDER_ADDRESS", "").strip()

    if not pk:
        print("  POLY_PRIVATE_KEY not found in .env")
        sys.exit(1)

    w3: Web3 | None = None
    for rpc in POLYGON_RPCS:
        try:
            candidate = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
            if candidate.is_connected():
                print(f"  RPC:     {rpc}")
                w3 = candidate
                break
        except Exception:
            continue

    if w3 is None:
        print("  Could not connect to any Polygon RPC.")
        sys.exit(1)

    account = w3.eth.account.from_key(pk)
    wallet = Web3.to_checksum_address(funder if funder else account.address)
    print(f"  Wallet:  {wallet}")

    pusd = w3.eth.contract(address=PUSD, abi=ERC20_ABI)

    raw_bal = pusd.functions.balanceOf(wallet).call()
    print(f"  Balance: ${raw_bal / 1e6:.2f} pUSD\n")

    if raw_bal == 0:
        print("  No pUSD balance detected. Run wrap_pusd.py first.")
        sys.exit(1)

    # 30% gas buffer avoids "replacement transaction underpriced" errors
    gas_price = int(w3.eth.gas_price * 1.3)

    for name, spender_raw in SPENDERS:
        spender = Web3.to_checksum_address(spender_raw)

        current = pusd.functions.allowance(wallet, spender).call()
        print(f"--- {name} ---")
        print(f"  Spender:   {spender}")
        print(f"  Allowance: ${current / 1e6:.2f} pUSD")

        if current >= MAX_UINT256 // 2:
            print("  Already approved (max). Skipping.\n")
            continue

        print("  Submitting approve() transaction...")
        try:
            nonce = w3.eth.get_transaction_count(wallet, "pending")
            tx = pusd.functions.approve(spender, MAX_UINT256).build_transaction(
                {
                    "from": wallet,
                    "nonce": nonce,
                    "gas": 100_000,
                    "gasPrice": gas_price,
                    "chainId": 137,
                }
            )
            signed = w3.eth.account.sign_transaction(tx, pk)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            print(f"  Tx sent: {tx_hash.hex()}")
            print("  Waiting for confirmation...")
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt.status == 1:
                print(f"  Approved! Block: {receipt.blockNumber}")
            else:
                print(f"  Transaction reverted. Hash: {tx_hash.hex()}")
        except Exception as exc:
            print(f"  Error: {exc}")
        print()

    # ── Final verification ────────────────────────────────────────────────────
    print("--- Final Allowance Check ---")
    for name, spender_raw in SPENDERS:
        spender = Web3.to_checksum_address(spender_raw)
        current = pusd.functions.allowance(wallet, spender).call()
        status = "OK" if current > 0 else "MISSING"
        print(f"  [{status}] {name}: ${current / 1e6:.2f} pUSD")

    print("\nDone. Bot is ready — run: python shadow.py")


if __name__ == "__main__":
    main()
