"""Pre-flight for live trading from a plain wallet (POLY_SIGNATURE_TYPE=0).

Checks, in order:
  1. POL (gas) balance
  2. USDC.e balance — the token Polymarket actually trades in — and native
     USDC balance, with swap instructions if the money is in the wrong one
  3. The six one-time approvals Polymarket's exchange contracts need
     (3x USDC.e allowance + 3x conditional-token approval)

DRY_RUN=true  -> report everything, send NOTHING.
DRY_RUN=false -> send only the missing approval transactions (each costs a
                 fraction of a cent in POL), wait for confirmation, re-check.
KILL_SWITCH=true -> refuses to send any transaction regardless of DRY_RUN.

Runs once and exits. Safe to re-run any time; already-granted approvals are
skipped.
"""

import sys

from eth_account import Account
from web3 import Web3

from config import ConfigError, load_settings
from trade_logger import get_logger, setup_logging

log = get_logger("preflight")

# Canonical Polygon mainnet contract addresses (checksummed)
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"   # bridged USDC — Polymarket collateral
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"      # conditional tokens (positions)

SPENDERS = {
    "CTF Exchange": "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
    "Neg-Risk CTF Exchange": "0xC5d563A36AE78145C45a50134d48A1215220f80a",
    "Neg-Risk Adapter": "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
}

MAX_UINT256 = 2**256 - 1
MIN_POL_FOR_GAS = 0.05

ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"},
                {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"},
                {"name": "value", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]

ERC1155_ABI = [
    {"name": "isApprovedForAll", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"},
                {"name": "operator", "type": "address"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "setApprovalForAll", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "operator", "type": "address"},
                {"name": "approved", "type": "bool"}],
     "outputs": []},
]


def find_missing_approvals(usdc_e, ctf, address: str) -> list:
    """Return [(description, contract_fn)] for approvals not yet granted."""
    missing = []
    for name, spender in SPENDERS.items():
        if usdc_e.functions.allowance(address, spender).call() == 0:
            missing.append(
                (f"allow {name} to spend USDC.e",
                 usdc_e.functions.approve(spender, MAX_UINT256))
            )
        if not ctf.functions.isApprovedForAll(address, spender).call():
            missing.append(
                (f"allow {name} to move outcome shares",
                 ctf.functions.setApprovalForAll(spender, True))
            )
    return missing


def send_tx(w3: Web3, account, fn, description: str) -> bool:
    nonce = w3.eth.get_transaction_count(account.address)
    tx = fn.build_transaction({
        "from": account.address,
        "nonce": nonce,
        "chainId": 137,
        "maxFeePerGas": w3.to_wei(200, "gwei"),
        "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    log.info("Sent approval (%s): tx %s — waiting for confirmation ...",
             description, tx_hash.hex())
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    ok = receipt.status == 1
    log.info("Confirmed in block %s: %s", receipt.blockNumber,
             "success" if ok else "FAILED")
    return ok


def main() -> int:
    setup_logging()
    try:
        settings = load_settings(require_market=False)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 1

    if settings.signature_type != 0:
        log.info(
            "POLY_SIGNATURE_TYPE=%s uses a Polymarket proxy wallet, which "
            "already has exchange approvals. Nothing to do.",
            settings.signature_type,
        )
        return 0

    w3 = Web3(Web3.HTTPProvider(settings.polygon_rpc_url,
                                request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        log.error("Could not reach Polygon RPC at %s", settings.polygon_rpc_url)
        return 1

    account = Account.from_key(settings.private_key)
    address = account.address
    log.info("Wallet: %s (check this matches your MetaMask account!)", address)

    # --- balances ---
    pol = w3.eth.get_balance(address) / 1e18
    usdc_e = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)
    usdc_native = w3.eth.contract(address=USDC_NATIVE, abi=ERC20_ABI)
    ctf = w3.eth.contract(address=CTF, abi=ERC1155_ABI)
    bal_e = usdc_e.functions.balanceOf(address).call() / 1e6
    bal_native = usdc_native.functions.balanceOf(address).call() / 1e6

    log.info("Balances: %.4f POL (gas) | %.2f USDC.e (tradeable on Polymarket) "
             "| %.2f native USDC", pol, bal_e, bal_native)

    ready = True
    if pol < MIN_POL_FOR_GAS:
        ready = False
        log.warning(
            "Not enough POL for gas (%.4f). Send a little POL (~$1-2, network: "
            "Polygon) to %s", pol, address,
        )

    if bal_e == 0:
        ready = False
        if bal_native > 0:
            log.warning(
                "Your %.2f USDC is the NATIVE flavor, but Polymarket trades "
                "USDC.e. Swap it in MetaMask: Swap -> from USDC -> to USDCe "
                "(bridged USDC, contract 0x2791...4174) -> confirm. "
                "Then re-run this script.", bal_native,
            )
        else:
            log.warning("No USDC in this wallet yet. Send USDC (network: "
                        "Polygon) to %s", address)

    # --- approvals ---
    missing = find_missing_approvals(usdc_e, ctf, address)
    if not missing:
        log.info("All 6 exchange approvals already granted.")
        if ready:
            log.info("PRE-FLIGHT PASSED: wallet is ready for a live order.")
            return 0
        log.warning("Approvals fine, but fix the balance issues above first.")
        return 1

    log.info("Missing approvals (%d):", len(missing))
    for description, _ in missing:
        log.info("  - %s", description)

    if settings.kill_switch:
        log.error("KILL_SWITCH is on — refusing to send any transaction.")
        return 1

    if settings.dry_run:
        log.info(
            "DRY_RUN: no transactions sent. When you're ready: set "
            "DRY_RUN=false in .env and re-run this script to grant the "
            "approvals above (one-time, costs well under $0.01 total)."
        )
        return 0

    if pol < MIN_POL_FOR_GAS:
        log.error("Cannot send approvals without POL for gas. Failing closed.")
        return 1

    log.info("LIVE MODE: sending %d approval transaction(s) ...", len(missing))
    for description, fn in missing:
        try:
            if not send_tx(w3, account, fn, description):
                log.error("Approval failed (%s). Stopping.", description)
                return 1
        except Exception as exc:
            log.error("Approval transaction error (%s): %s", description, exc)
            return 1

    if find_missing_approvals(usdc_e, ctf, address):
        log.error("Some approvals still missing after sending. Re-run to retry.")
        return 1

    log.info("All approvals granted.")
    if ready:
        log.info("PRE-FLIGHT PASSED: wallet is ready for a live order.")
        return 0
    log.warning("Approvals done, but fix the balance issues above first.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
