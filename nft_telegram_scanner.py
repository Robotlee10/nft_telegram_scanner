import os
import re
import html
import asyncio
import logging
import sqlite3
import httpx
from tavily import AsyncTavilyClient
from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider

# ==========================================
# CONFIGURATION
# ==========================================
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "YOUR_TAVILY_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")
BSC_RPC_URL = os.getenv("BSC_RPC_URL", "https://bsc-dataseed1.binance.org/")

# Your Binance Agentic Wallet Address (used to simulate eligibility)
AGENTIC_WALLET_ADDRESS = os.getenv("AGENTIC_WALLET_ADDRESS", "0xD0833B61A69e69FA31B02F877336d33d5B7fB169")

CHECK_INTERVAL_SECONDS = 600  # Check every 3 minutes
DB_FILE = "agentic_mints.db"

# Highly targeted queries matching Agentic Wallet post patterns
SEARCH_QUERIES = [
    '"Use my Binance Agentic Wallet" "contract"',
    '".ai Agent ID" "free" "0x"',
    'site:binance.com/square "Agentic Wallet" "mint" "0x"',
    'site:x.com "Binance Agentic Wallet" "free mint" "0x"'
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Web3 Initialization
w3 = AsyncWeb3(AsyncHTTPProvider(BSC_RPC_URL))

# Common Agentic Mint Function Selectors (first 4 bytes of keccak256)
MINT_FUNCTIONS = [
    {"name": "mint(uint256)", "data": "a0712d680000000000000000000000000000000000000000000000000000000000000001"},  # mint(1)
    {"name": "freeMint()", "data": "0x438d1d8f"},                                                               # freeMint()
    {"name": "claim()", "data": "0x4e71d92d"},                                                                  # claim()
    {"name": "mint(uint256)", "data": "a0712d680000000000000000000000000000000000000000000000000000000000000000"}   # mint(0)
]

# ==========================================
# PERSISTENT DATABASE (SQLite)
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
# ON-CHAIN 0 BNB SIMULATION ENGINE
# ==========================================
async def simulate_zero_value_mint(contract_address: str) -> tuple[bool, str, str]:
    """
    Simulates a call to the contract with value = 0 BNB using your Agentic Wallet.
    If the call reverts, it means paid mint required, sold out, or not eligible.
    """
    checksum_target = w3.to_checksum_address(contract_address)
    checksum_wallet = w3.to_checksum_address(AGENTIC_WALLET_ADDRESS)

    for fn in MINT_FUNCTIONS:
        try:
            tx_payload = {
                "from": checksum_wallet,
                "to": checksum_target,
                "value": 0,  # Enforce 0 BNB
                "data": fn["data"]
            }
            
            # Simulate transaction execution on-chain
            await w3.eth.call(tx_payload)
            
            # If call doesn't revert, 0 BNB mint IS ACTIVE AND VALID!
            return True, fn["name"], "0 BNB Free Mint Verified On-Chain"

        except Exception as e:
            err_msg = str(e).lower()
            if "insufficient funds" in err_msg or "execution reverted" in err_msg:
                continue

    return False, "None", "Simulation failed: Requires BNB, Sold Out, or Ineligible."

# ==========================================
# TELEGRAM ALERT SENDER
# ==========================================
async def send_telegram_alert(client: httpx.AsyncClient, contract: str, source: str, snippet: str, link: str, func_used: str):
    """Sends alert with pre-tested command payload."""
    clean_snippet = html.escape(snippet[:300])
    agent_command = f"Access my Binance Agentic Wallet. Call the write function {func_used} on BNB Chain at contract address {contract} with value 0 BNB."

    message = (
        f"🚨 <b>VERIFIED AGENTIC FREE MINT DETECTED</b> 🚨\n\n"
        f"📍 <b>Source:</b> {html.escape(source)}\n"
        f"📄 <b>Contract Address:</b> <code>{contract}</code>\n"
        f"🧪 <b>On-Chain Sim Status:</b> 🟢 0 BNB Call Succeeded\n"
        f"⚙️ <b>Verified Function:</b> <code>{func_used}</code>\n"
        f"🔗 <b>BscScan:</b> https://bscscan.com/address/{contract}\n"
        f"🌐 <b>Post Link:</b> {link}\n\n"
        f"📝 <b>Context Snippet:</b>\n<i>{clean_snippet}...</i>\n\n"
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
        res = await client.post(url, json=payload, timeout=10.0)
        if res.status_code != 200:
            logging.error(f"Telegram API Error ({res.status_code}): {res.text}")
    except Exception as e:
        logging.error(f"Failed sending alert: {e}")

# ==========================================
# MAIN SCANNING LOOP
# ==========================================
async def scan_and_verify(tavily_client: AsyncTavilyClient, http_client: httpx.AsyncClient):
    for query in SEARCH_QUERIES:
        try:
            response = await tavily_client.search(
                query=query, 
                search_depth="advanced", 
                time_range="day"
            )
            
            for item in response.get("results", []):
                snippet = item.get("content", "")
                full_text = f"{item.get('title', '')} {snippet}"
                url = item.get("url", "")
                
                # Extract EVM contract addresses
                found_contracts = re.findall(r"0x[a-fA-F0-9]{40}", full_text)
                for addr in set(found_contracts):
                    addr_lower = addr.lower()
                    
                    if is_already_notified(addr_lower):
                        continue
                    
                    # SIMULATE ON-CHAIN BEFORE ALERTING
                    is_free, func_used, sim_status = await simulate_zero_value_mint(addr_lower)
                    
                    if is_free:
                        mark_as_notified(addr_lower)
                        await send_telegram_alert(
                            http_client,
                            addr,
                            f"Agentic Feed Index ({url})",
                            snippet,
                            url,
                            func_used
                        )
                    else:
                        logging.info(f"Skipped {addr}: {sim_status}")

        except Exception as e:
            logging.error(f"Search query error: {e}")

async def main():
    init_db()
    logging.info("Starting Verified Agentic Free Mint Scanner...")

    tavily_client = AsyncTavilyClient(api_key=TAVILY_API_KEY)

    async with httpx.AsyncClient() as http_client:
        while True:
            try:
                await scan_and_verify(tavily_client, http_client)
            except Exception as e:
                logging.error(f"Main loop exception: {e}")
            
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    asyncio.run(main())
