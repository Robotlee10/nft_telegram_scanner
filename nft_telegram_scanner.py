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

CHECK_INTERVAL_SECONDS = 3600  # Poll search index every 5 minutes
DB_FILE = "mint_tracker.db"

# Search queries targeting multi-source platforms
SEARCH_QUERIES = [
    # Explicit X (Twitter) Search Queries
    'site:x.com "Binance Agentic Wallet" "mint" "0x"',
    'site:x.com "Agentic ID" "free mint" "BNB Chain" "0x"',
    'site:twitter.com "Binance Agentic Wallet" "mint" "0x"',
    
    # Binance Square, Substack & AI Agent Forums
    'site:binance.com/square "agentic" "mint" "0x"',
    'site:substack.com "Binance Agentic Wallet" "free mint" "0x"',
    '"Boltbook" "mint log" "0x"',
    
    # Broad catch-all query
    '"Binance Agentic Wallet" "mint" "BNB Chain" "0x"'
]


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ==========================================
# WEB3 & ERC-165 SETUP
# ==========================================
w3 = AsyncWeb3(AsyncHTTPProvider(BSC_RPC_URL))

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
SELECTOR_SET_APPROVAL_FOR_ALL = "a22cb465"
SELECTOR_MINT = "a0712d68"

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
# WEB3.PY CONTRACT SECURITY & NFT INSPECTOR
# ==========================================
async def inspect_contract(address: str) -> dict:
    """Performs on-chain ABI and bytecode analysis using Web3.py."""
    checksum_addr = w3.to_checksum_address(address)
    result = {
        "is_contract": False,
        "is_nft": False,
        "standard": "Unknown/Non-NFT",
        "has_approval_risk": False,
        "has_mint_selector": False,
        "passed_security": False
    }

    try:
        # 1. Bytecode Check (EOA vs Contract)
        code = await w3.eth.get_code(checksum_addr)
        if not code or code == b"" or len(code) <= 2:
            return result  # Wallet address / EOA
        
        result["is_contract"] = True
        code_hex = code.hex()

        # 2. Bytecode Selector Scanning
        if SELECTOR_MINT in code_hex:
            result["has_mint_selector"] = True
        if SELECTOR_SET_APPROVAL_FOR_ALL in code_hex:
            result["has_approval_risk"] = True

        # 3. ERC-165 On-Chain Interface Query
        contract = w3.eth.contract(address=checksum_addr, abi=ERC165_ABI)
        
        try:
            if await contract.functions.supportsInterface(bytes.fromhex(ERC721_INTERFACE_ID[2:])).call():
                result["is_nft"] = True
                result["standard"] = "ERC-721"
        except Exception:
            pass

        if not result["is_nft"]:
            try:
                if await contract.functions.supportsInterface(bytes.fromhex(ERC1155_INTERFACE_ID[2:])).call():
                    result["is_nft"] = True
                    result["standard"] = "ERC-1155"
            except Exception:
                pass

        # 4. Filter Decision
        if result["is_contract"] and (result["is_nft"] or result["has_mint_selector"]):
            result["passed_security"] = True

    except Exception as e:
        logging.warning(f"Error inspecting contract {address}: {e}")

    return result

# ==========================================
# TELEGRAM ALERT SENDER
# ==========================================
async def send_telegram_alert(client: httpx.AsyncClient, contract: str, source: str, snippet: str, link: str, report: dict):
    """Sends alert formatted with Web3 verification status."""
    clean_snippet = html.escape(snippet[:300])
    agent_command = f"Access my Binance Agentic Wallet. Call the write function mint(1) on BNB Chain at contract address {contract} with value 0 BNB."

    nft_status = f"✅ Verified {report['standard']}" if report['is_nft'] else "⚠️ Unconfirmed Standard"
    drain_risk = "⚠️ Approval Functions Found" if report['has_approval_risk'] else "🟢 Low Risk"

    message = (
        f"🚨 <b>FREE NFT MINT DETECTED</b> 🚨\n\n"
        f"📍 <b>Source:</b> {html.escape(source)}\n"
        f"📄 <b>Contract Address:</b> <code>{contract}</code>\n"
        f"🛡️ <b>NFT Standard:</b> {nft_status}\n"
        f"🔒 <b>Drainer Risk Check:</b> {drain_risk}\n"
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
# TAVILY SEARCH ENGINE
# ==========================================
async def run_search_aggr(tavily_client: AsyncTavilyClient, http_client: httpx.AsyncClient):
    """Scans Web3 sources via Tavily API and passes contract candidates to Web3 inspector."""
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
                    
                    # Run Web3 On-Chain Inspection before alerting
                    report = await inspect_contract(addr_lower)
                    if report["passed_security"]:
                        mark_as_notified(addr_lower)
                        await send_telegram_alert(
                            http_client,
                            addr,
                            f"Multi-Source Search Index ({url})",
                            snippet,
                            url,
                            report
                        )
                    else:
                        logging.info(f"Filtered out {addr}: Not a valid NFT contract.")
        except Exception as e:
            logging.error(f"Search query failed for '{query}': {e}")

# ==========================================
# MAIN EXECUTION LOOP
# ==========================================
async def main():
    init_db()
    logging.info("Starting Tavily Search + Web3 Security Mint Scanner...")

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
