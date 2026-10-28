import os
import re
import html
import asyncio
import logging
import sqlite3
import httpx
from tavily import AsyncTavilyClient

# ==========================================
# CONFIGURATION
# ==========================================
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "YOUR_TAVILY_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")

BSC_RPC_URL = os.getenv("BSC_RPC_URL", "https://bsc-dataseed1.binance.org/")
CHECK_INTERVAL_SECONDS = 3600  # Poll search index every û0 minutes
DB_FILE = "mint_tracker.db"

# Search queries targeting multi-source platforms
SEARCH_QUERIES = [
    'site:binance.com/square "agentic" "mint" "0x"',
    'site:substack.com "Binance Agentic Wallet" "free mint" "0x"',
    '"Binance Agentic Wallet" "mint" "BNB Chain" "0x"',
    '"Boltbook" "mint log" "0x"'
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

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
# ON-CHAIN BSC SMART CONTRACT VERIFIER
# ==========================================
async def is_valid_bsc_contract(client: httpx.AsyncClient, address: str) -> bool:
    """Queries BSC node to verify address is an active smart contract."""
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getCode",
        "params": [address, "latest"],
        "id": 1
    }
    try:
        resp = await client.post(BSC_RPC_URL, json=payload, timeout=5.0)
        if resp.status_code == 200:
            result = resp.json().get("result", "0x")
            return result != "0x" and len(result) > 2
    except Exception as e:
        logging.warning(f"BSC RPC check failed for {address}: {e}")
        return True
    return False

# ==========================================
# SAFE TELEGRAM ALERT SENDER
# ==========================================
async def send_telegram_alert(client: httpx.AsyncClient, contract: str, source: str, snippet: str, link: str):
    """Sends HTML-formatted alert to Telegram."""
    clean_snippet = html.escape(snippet[:300])
    agent_command = f"Access my Binance Agentic Wallet. Call the write function mint(1) on BNB Chain at contract address {contract} with value 0 BNB."

    message = (
        f"🚨 <b>FREE NFT MINT DETECTED</b> 🚨\n\n"
        f"📍 <b>Source:</b> {html.escape(source)}\n"
        f"📄 <b>Contract Address:</b> <code>{contract}</code>\n"
        f"🔗 <b>BscScan:</b> https://bscscan.com/address/{contract}\n"
        f"🌐 <b>Source Link:</b> {link}\n\n"
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
# CORE AGGREGATOR ENGINE
# ==========================================
async def run_search_aggr(tavily_client: AsyncTavilyClient, http_client: httpx.AsyncClient):
    for query in SEARCH_QUERIES:
        try:
            # Time-bounded search (filters results published recently)
            response = await tavily_client.search(
                query=query, 
                search_depth="advanced", 
                time_range="day"
            )
            
            results = response.get("results", [])
            for item in results:
                title = item.get("title", "")
                snippet = item.get("content", "")
                url = item.get("url", "")
                full_text = f"{title} {snippet}"
                
                # Extract EVM 0x contract addresses
                found_contracts = re.findall(r"0x[a-fA-F0-9]{40}", full_text)
                for addr in set(found_contracts):
                    addr_lower = addr.lower()
                    
                    if is_already_notified(addr_lower):
                        continue
                    
                    # Validate contract code on BSC
                    if await is_valid_bsc_contract(http_client, addr_lower):
                        mark_as_notified(addr_lower)
                        await send_telegram_alert(
                            http_client,
                            addr,
                            f"Multi-Source Search Index ({url})",
                            snippet,
                            url
                        )
        except Exception as e:
            logging.error(f"Search aggregation failed for query '{query}': {e}")

# ==========================================
# MAIN LOOP
# ==========================================
async def main():
    init_db()
    logging.info("Starting Multi-Source Search Aggregator Mint Scanner...")

    tavily_client = AsyncTavilyClient(api_key=TAVILY_API_KEY)

    async with httpx.AsyncClient() as http_client:
        while True:
            try:
                await run_search_aggr(tavily_client, http_client)
            except Exception as e:
                logging.error(f"Main loop exception: {e}")
            
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    asyncio.run(main())
