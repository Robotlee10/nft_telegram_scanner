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
# Free BSC WebSocket Nodes (No API key needed)
BSC_WSS_URL = os.getenv("BSC_WSS_URL", "wss://bsc-rpc.publicnode.com")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")

# Your Binance Agentic Wallet Address
MY_AGENTIC_WALLET = os.getenv("MY_AGENTIC_WALLET", "0xD0833B61A69e69FA31B02F877336d33d5B7fB169")

# Standard ERC-721 Transfer Event Hash: keccak256("Transfer(address,address,uint256)")
ERC721_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDRESS_TOPIC = "0x0000000000000000000000000000000000000000000000000000000000000000"

# Common Agentic Mint Function Selectors
MINT_FUNCTIONS = [
    {"name": "mint(1)", "data": "a0712d680000000000000000000000000000000000000000000000000000000000000001"},
    {"name": "freeMint()", "data": "0x438d1d8f"},
    {"name": "claim()", "data": "0x4e71d92d"}
]

DB_FILE = "agentic_mints.db"
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ==========================================
# PERSISTENT STORAGE
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
# ON-CHAIN SIMULATION (0 BNB FREE CHECK)
# ==========================================
async def verify_agentic_free_mint(w3: AsyncWeb3, contract_address: str) -> tuple[bool, str]:
    """
    Simulates calling mint on the target contract with value=0 BNB 
    from YOUR Agentic Wallet to confirm instant eligibility.
    """
    checksum_target = w3.to_checksum_address(contract_address)
    checksum_wallet = w3.to_checksum_address(MY_AGENTIC_WALLET)

    for fn in MINT_FUNCTIONS:
        try:
            tx_payload = {
                "from": checksum_wallet,
                "to": checksum_target,
                "value": 0,  # Enforce 0 BNB
                "data": fn["data"]
            }
            # Execute read-only simulation
            await w3.eth.call(tx_payload)
            
            # Call succeeded without revert -> Guaranteed Free Mint!
            return True, fn["name"]
        except Exception:
            continue

    return False, "None"

# ==========================================
# TELEGRAM ALERT SENDER
# ==========================================
async def send_telegram_alert(http_client: httpx.AsyncClient, contract: str, func_name: str, tx_hash: str):
    """Sends structured alert with exact agent prompt."""
    agent_command = f"Access my Binance Agentic Wallet. Call the write function {func_name} on BNB Chain at contract address {contract} with value 0 BNB."

    message = (
        f"🚨 <b>REAL-TIME AGENTIC FREE MINT DETECTED</b> 🚨\n\n"
        f"📍 <b>Source:</b> Direct BSC WebSocket On-Chain Event\n"
        f"📄 <b>Contract Address:</b> <code>{contract}</code>\n"
        f"🧪 <b>Sim Status:</b> 🟢 0 BNB Call Verified\n"
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
            logging.info(f"Alert sent for contract: {contract}")
    except Exception as e:
        logging.error(f"Failed sending Telegram alert: {e}")

# ==========================================
# WEBSOCKET ON-CHAIN LISTENER
# ==========================================
async def listen_onchain():
    init_db()
    logging.info("Connecting to BSC WebSocket Node...")

    async with httpx.AsyncClient() as http_client:
        async with AsyncWeb3(WebSocketProvider(BSC_WSS_URL)) as w3:
            logging.info("Connected to BSC. Listening for real-time NFT mint events...")

            # Subscribe to Transfer logs where from = 0x0000...0000 (Mints)
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

                    # Simulate 0 BNB mint for your Agentic Wallet
                    is_eligible, func_name = await verify_agentic_free_mint(w3, contract_address)
                    
                    if is_eligible:
                        mark_as_notified(contract_address)
                        await send_telegram_alert(http_client, contract_address, func_name, tx_hash)

                except Exception as e:
                    logging.error(f"Error processing event log: {e}")

# ==========================================
# MAIN EXECUTION WITH AUTO-RECONNECT
# ==========================================
async def main():
    while True:
        try:
            await listen_onchain()
        except Exception as e:
            logging.error(f"WebSocket connection dropped ({e}). Reconnecting in 5 seconds...")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
