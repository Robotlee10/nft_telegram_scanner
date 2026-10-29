import os
import html
import asyncio
import logging
import sqlite3
import httpx
from web3 import AsyncWeb3
from web3.providers import WebSocketProvider

# ==========================================
# CONFIGURATION
# ==========================================
BSC_WSS_URL = os.getenv("BSC_WSS_URL", "wss://bsc-rpc.publicnode.com")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")

# 1. Wallet WITH Binance Agentic ID (.ai)
MY_AGENTIC_WALLET = os.getenv("MY_AGENTIC_WALLET", "0xYourBinanceAgenticWalletAddress")

# 2. Control Wallet WITHOUT Binance Agentic ID (Standard empty wallet address)
CONTROL_WALLET_NO_ID = os.getenv("CONTROL_WALLET_NO_ID", "0x0000000000000000000000000000000000000001")

# Standard Topics
ERC721_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDRESS_TOPIC = "0x0000000000000000000000000000000000000000000000000000000000000000"

# Common Agentic Mint Call Signatures
MINT_FUNCTIONS = [
    {"name": "mint(1)", "data": "a0712d680000000000000000000000000000000000000000000000000000000000000001"},
    {"name": "freeMint()", "data": "0x438d1d8f"},
    {"name": "mint(0)", "data": "a0712d680000000000000000000000000000000000000000000000000000000000000000"},
    {"name": "claim()", "data": "0x4e71d92d"}
]

# ERC-165 ABI
ERC165_ABI = [
    {
        "inputs": [{"internalType": "bytes4", "name": "interfaceId", "type": "bytes4"}],
        "name": "supportsInterface",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    }
]

ERC721_INTERFACE_ID = "0x80ac58cd"
ERC1155_INTERFACE_ID = "0xd9b67a26"

DB_FILE = "agentic_mints.db"
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ==========================================
# PERSISTENT DATABASE
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notified_contracts (
            address TEXT PRIMARY KEY,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def is_already_notified(address: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM notified_contracts WHERE address = ?", (address.lower(),))
    row = cursor.fetchone()
    conn.close()
    return row is not None

def mark_as_notified(address: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO notified_contracts (address) VALUES (?)", (address.lower(),))
    conn.commit()
    conn.close()

# ==========================================
# TEST 1: ERC-721 / ERC-1155 NFT CONTRACT CHECK
# ==========================================
async def verify_is_nft_contract(w3: AsyncWeb3, contract_address: str) -> tuple[bool, str]:
    """Ensures contract is an actual ERC-721 or ERC-1155 NFT contract."""
    checksum_target = w3.to_checksum_address(contract_address)
    
    try:
        # Check bytecode length
        code = await w3.eth.get_code(checksum_target)
        if not code or len(code) <= 2:
            return False, "Not a contract"

        code_hex = code.hex()
        contract_obj = w3.eth.contract(address=checksum_target, abi=ERC165_ABI)

        # Check ERC-721 via ERC-165
        try:
            if await contract_obj.functions.supportsInterface(bytes.fromhex(ERC721_INTERFACE_ID[2:])).call():
                return True, "ERC-721"
        except Exception:
            pass

        # Check ERC-1155 via ERC-165
        try:
            if await contract_obj.functions.supportsInterface(bytes.fromhex(ERC1155_INTERFACE_ID[2:])).call():
                return True, "ERC-1155"
        except Exception:
            pass

        # Fallback Bytecode Signature Check for ERC-721 ownerOf / balanceOf selectors
        if "63706577" in code_hex or "70a08231" in code_hex:
            return True, "ERC-721 (Bytecode Verified)"

    except Exception as e:
        logging.warning(f"NFT check failed for {contract_address}: {e}")

    return False, "Non-NFT / Token / Router"

# ==========================================
# TEST 2: DUAL-WALLET DIFFERENTIAL SIMULATION
# ==========================================
async def simulate_exclusive_agentic_mint(w3: AsyncWeb3, contract_address: str) -> tuple[bool, str, str]:
    """
    Runs dual eth_call simulations:
    1. Agentic Wallet MUST succeed at value = 0 BNB.
    2. Control Wallet MUST revert at value = 0 BNB (proving it is exclusive to Agent ID holders).
    """
    checksum_target = w3.to_checksum_address(contract_address)
    checksum_agentic = w3.to_checksum_address(MY_AGENTIC_WALLET)
    checksum_control = w3.to_checksum_address(CONTROL_WALLET_NO_ID)

    for fn in MINT_FUNCTIONS:
        # Step A: Simulate with Agentic Wallet
        agentic_success = False
        try:
            tx_agentic = {
                "from": checksum_agentic,
                "to": checksum_target,
                "value": 0,
                "data": fn["data"]
            }
            await w3.eth.call(tx_agentic)
            agentic_success = True
        except Exception:
            agentic_success = False

        if not agentic_success:
            continue # Try next function selector

        # Step B: Simulate with Control Wallet (No Agent ID)
        control_success = False
        try:
            tx_control = {
                "from": checksum_control,
                "to": checksum_target,
                "value": 0,
                "data": fn["data"]
            }
            await w3.eth.call(tx_control)
            control_success = True
        except Exception:
            control_success = False

        # DECISION LOGIC:
        # - Agentic Wallet = SUCCEEDED
        # - Control Wallet = REVERTED
        if agentic_success and not control_success:
            return True, fn["name"], "EXCLUSIVE Agentic Free Mint Confirmed!"
        
        elif agentic_success and control_success:
            logging.info(f"Skipped {contract_address}: Free for ALL wallets (Not exclusive to Agentic ID).")
            return False, fn["name"], "Public Free Mint (Skipped)"

    return False, "None", "Simulation failed for both wallets or requires BNB payment."

# ==========================================
# TELEGRAM ALERT SENDER
# ==========================================
async def send_telegram_alert(http_client: httpx.AsyncClient, contract: str, nft_standard: str, func_name: str, tx_hash: str):
    agent_command = f"Access my Binance Agentic Wallet. Call the write function {func_name} on BNB Chain at contract address {contract} with value 0 BNB."

    message = (
        f"🚨 <b>EXCLUSIVE AGENTIC FREE NFT DETECTED</b> 🚨\n\n"
        f"📍 <b>Source:</b> Direct BSC On-Chain Stream\n"
        f"📄 <b>Contract Address:</b> <code>{contract}</code>\n"
        f"🎨 <b>NFT Standard:</b> ✅ {nft_standard}\n"
        f"🛡️ <b>Access Gate:</b> 🔒 Agentic ID Holders Only (Control Wallet Reverted)\n"
        f"⚙️ <b>Function:</b> <code>{func_name}</code>\n"
        f"🔗 <b>BscScan:</b> https://bscscan.com/address/{contract}\n"
        f"⚡ <b>Detected TX:</b> https://bscscan.com/tx/{tx_hash}\n\n"
        f"🤖 <b>Direct Agent Command:</b>\n"
        f"<pre><code>{agent_command}</code></pre>"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    
    try:
        res = await http_client.post(url, json=payload, timeout=10.0)
        if res.status_code == 200:
            logging.info(f"Alert sent for exclusive NFT contract: {contract}")
    except Exception as e:
        logging.error(f"Failed sending alert: {e}")

# ==========================================
# MAIN EVENT LISTENER
# ==========================================
async def listen_onchain():
    init_db()
    logging.info("Connecting to BSC WebSocket Node...")

    async with httpx.AsyncClient() as http_client:
        async with AsyncWeb3(WebSocketProvider(BSC_WSS_URL)) as w3:
            logging.info("Listening for real-time NFT mint events...")

            subscription_id = await w3.eth.subscribe(
                "logs",
                {
                    "topics": [
                        ERC721_TRANSFER_TOPIC,
                        ZERO_ADDRESS_TOPIC
                    ]
                }
            )

            async for log in w3.socket.process_subscriptions():
                try:
                    result = log.get("result", {})
                    contract_address = result.get("address", "").lower()
                    tx_hash = result.get("transactionHash", "").hex()

                    if not contract_address or is_already_notified(contract_address):
                        continue

                    # 1. TEST: Is it an NFT contract (ERC-721 / ERC-1155)?
                    is_nft, nft_standard = await verify_is_nft_contract(w3, contract_address)
                    if not is_nft:
                        continue

                    # 2. TEST: Is it EXCLUSIVELY free for Agentic ID holders?
                    is_exclusive, func_name, reason = await simulate_exclusive_agentic_mint(w3, contract_address)
                    
                    if is_exclusive:
                        mark_as_notified(contract_address)
                        await send_telegram_alert(http_client, contract_address, nft_standard, func_name, tx_hash)
                    else:
                        logging.info(f"Filtered {contract_address}: {reason}")

                except Exception as e:
                    logging.error(f"Log processing error: {e}")

async def main():
    while True:
        try:
            await listen_onchain()
        except Exception as e:
            logging.error(f"WebSocket dropped ({e}). Reconnecting in 5 seconds...")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
