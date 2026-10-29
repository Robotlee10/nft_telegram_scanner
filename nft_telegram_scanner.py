import os
import html
import asyncio
import logging
import sqlite3
import httpx
from web3 import AsyncWeb3
from web3.providers import WebSocketProvider, AsyncHTTPProvider

# ==========================================
# CONFIGURATION
# ==========================================
# WebSocket Node for LIVE EVENT STREAMING ONLY
BSC_WSS_URL = os.getenv("BSC_WSS_URL", "wss://bsc-rpc.publicnode.com")

# HTTP Node for ON-CHAIN CONTRACT CHECKS & SIMULATIONS
BSC_HTTP_URL = os.getenv("BSC_HTTP_URL", "https://bsc-dataseed1.binance.org/")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")

MY_AGENTIC_WALLET = os.getenv("MY_AGENTIC_WALLET", "0xYourBinanceAgenticWalletAddress")
CONTROL_WALLET_NO_ID = os.getenv("CONTROL_WALLET_NO_ID", "0x0000000000000000000000000000000000000001")

ERC721_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDRESS_TOPIC = "0x0000000000000000000000000000000000000000000000000000000000000000"

MINT_FUNCTIONS = [
    {"name": "mint(1)", "data": "a0712d680000000000000000000000000000000000000000000000000000000000000001"},
    {"name": "freeMint()", "data": "0x438d1d8f"},
    {"name": "mint(0)", "data": "a0712d680000000000000000000000000000000000000000000000000000000000000000"},
    {"name": "claim()", "data": "0x4e71d92d"}
]

ERC165_ABI = [
    {
        "inputs": [{"internalType": "bytes4", "name": "interfaceId", "type": "bytes4"}],
        "name": "supportsInterface",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view", "type": "function"
    }
]

ERC721_INTERFACE_ID = "0x80ac58cd"
ERC1155_INTERFACE_ID = "0xd9b67a26"

DB_FILE = "agentic_mints.db"
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Independent Async HTTP Web3 instance for queries
w3_http = AsyncWeb3(AsyncHTTPProvider(BSC_HTTP_URL))

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
# ON-CHAIN CHECKS OVER HTTP RPC
# ==========================================
async def verify_is_nft_contract(contract_address: str) -> tuple[bool, str]:
    """Uses HTTP RPC to verify ERC-721 / ERC-1155 compliance without touching WebSocket socket."""
    checksum_target = w3_http.to_checksum_address(contract_address)
    
    try:
        code = await w3_http.eth.get_code(checksum_target)
        if not code or len(code) <= 2:
            return False, "Not a contract"

        code_hex = code.hex()
        contract_obj = w3_http.eth.contract(address=checksum_target, abi=ERC165_ABI)

        try:
            if await contract_obj.functions.supportsInterface(bytes.fromhex(ERC721_INTERFACE_ID[2:])).call():
                return True, "ERC-721"
        except Exception:
            pass

        try:
            if await contract_obj.functions.supportsInterface(bytes.fromhex(ERC1155_INTERFACE_ID[2:])).call():
                return True, "ERC-1155"
        except Exception:
            pass

        if "63706577" in code_hex or "70a08231" in code_hex:
            return True, "ERC-721 (Bytecode Verified)"

    except Exception as e:
        logging.debug(f"NFT check failed for {contract_address}: {e}")

    return False, "Non-NFT"

async def simulate_exclusive_agentic_mint(contract_address: str) -> tuple[bool, str, str]:
    """Uses HTTP RPC for dual-wallet eth_call simulations."""
    checksum_target = w3_http.to_checksum_address(contract_address)
    checksum_agentic = w3_http.to_checksum_address(MY_AGENTIC_WALLET)
    checksum_control = w3_http.to_checksum_address(CONTROL_WALLET_NO_ID)

    for fn in MINT_FUNCTIONS:
        # Step A: Agentic Wallet must succeed
        agentic_success = False
        try:
            tx_agentic = {"from": checksum_agentic, "to": checksum_target, "value": 0, "data": fn["data"]}
            await w3_http.eth.call(tx_agentic)
            agentic_success = True
        except Exception:
            agentic_success = False

        if not agentic_success:
            continue

        # Step B: Control Wallet must revert
        control_success = False
        try:
            tx_control = {"from": checksum_control, "to": checksum_target, "value": 0, "data": fn["data"]}
            await w3_http.eth.call(tx_control)
            control_success = True
        except Exception:
            control_success = False

        if agentic_success and not control_success:
            return True, fn["name"], "EXCLUSIVE Agentic Free Mint Confirmed!"

    return False, "None", "Failed simulation or public mint."

# ==========================================
# TELEGRAM NOTIFICATIONS
# ==========================================
async def send_telegram_alert(http_client: httpx.AsyncClient, contract: str, nft_standard: str, func_name: str, tx_hash: str):
    agent_command = f"Access my Binance Agentic Wallet. Call the write function {func_name} on BNB Chain at contract address {contract} with value 0 BNB. execute it, if it preview clearly and would return a ERC-721 token"

    message = (
        f"🚨 <b>EXCLUSIVE AGENTIC FREE NFT DETECTED</b> 🚨\n\n"
        f"📍 <b>Source:</b> Direct BSC On-Chain Stream\n"
        f"📄 <b>Contract Address:</b> <code>{contract}</code>\n"
        f"🎨 <b>NFT Standard:</b> ✅ {nft_standard}\n"
        f"🛡️ <b>Access Gate:</b> 🔒 Agentic ID Holders Only\n"
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
        logging.error(f"Failed sending alert: {e}")

# ==========================================
# ASYNCHRONOUS WORKER PROCESSOR
# ==========================================
async def process_contract_candidate(http_client: httpx.AsyncClient, contract_address: str, tx_hash: str):
    """Processes candidate in background task without blocking the WebSocket listener."""
    if is_already_notified(contract_address):
        return

    # Check NFT standard over HTTP
    is_nft, nft_standard = await verify_is_nft_contract(contract_address)
    if not is_nft:
        return

    # Run dual simulation over HTTP
    is_exclusive, func_name, reason = await simulate_exclusive_agentic_mint(contract_address)
    if is_exclusive:
        mark_as_notified(contract_address)
        await send_telegram_alert(http_client, contract_address, nft_standard, func_name, tx_hash)

# ==========================================
# WEBSOCKET EVENT LISTENER
# ==========================================
async def listen_onchain():
    init_db()
    logging.info("Connecting to BSC WebSocket Node...")

    # Configure WebSocket provider with custom keepalive options
    ws_provider = WebSocketProvider(
        BSC_WSS_URL,
        websocket_kwargs={
            "ping_interval": 20,
            "ping_timeout": 20,
            "close_timeout": 10
        }
    )

    async with httpx.AsyncClient() as http_client:
        async with AsyncWeb3(ws_provider) as w3_ws:
            logging.info("Connected to BSC WebSocket. Streaming mint logs...")

            await w3_ws.eth.subscribe(
                "logs",
                {
                    "topics": [
                        ERC721_TRANSFER_TOPIC,
                        ZERO_ADDRESS_TOPIC
                    ]
                }
            )

            async for log in w3_ws.socket.process_subscriptions():
                try:
                    result = log.get("result", {})
                    contract_address = result.get("address", "").lower()
                    tx_hash = result.get("transactionHash", "").hex()

                    if contract_address and not is_already_notified(contract_address):
                        # Spawn background task to process event asynchronously
                        asyncio.create_task(process_contract_candidate(http_client, contract_address, tx_hash))

                except Exception as e:
                    logging.error(f"Subscription event error: {e}")

async def main():
    while True:
        try:
            await listen_onchain()
        except Exception as e:
            logging.error(f"WebSocket connection dropped ({e}). Reconnecting in 5 seconds...")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
          await listen_onchain()
        except Exception as e:
            logging.error(f"WebSocket dropped ({e}). Reconnecting in 5 seconds...")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
