import os
import re
import asyncio
import logging
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime
import httpx
from bs4 import BeautifulSoup

# ==========================================
# CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")

# Public BSC RPC for on-chain verification
BSC_RPC_URL = os.getenv("BSC_RPC_URL", "https://bsc-dataseed1.binance.org/")

# Scanning Targets
TELEGRAM_CHANNELS = [
    "bsc_gems",
    "binance_alpha_calls",
    # Add target telegram channel handles here (without @ or t.me/)
]

X_TARGET_USERS = [
    "Airdrop_farming",
    "binance",
    "BNBCHAIN",
    # Add target X handles here
]

# Nitter Fallback Pool
NITTER_INSTANCES = [
    "https://nitter.net",
    "https://privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.cz",
]

KEYWORDS = ["mint", "free", "agentic", "binance", ".ai", "claim", "nft"]
CHECK_INTERVAL_SECONDS = 45

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ==========================================
# PERSISTENT DATABASE (SQLite)
# ==========================================
DB_FILE = "notified_contracts.db"

def init_db():
    """Initializes SQLite database to persist notified contracts across restarts."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS contracts (
            address TEXT PRIMARY KEY,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def is_already_notified(address: str) -> bool:
    """Checks if address has already been processed."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM contracts WHERE address = ?", (address.lower(),))
    row = cursor.fetchone()
    conn.close()
    return row is not None

def mark_as_notified(address: str):
    """Saves processed contract address into database."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO contracts (address) VALUES (?)", (address.lower(),))
    conn.commit()
    conn.close()

# ==========================================
# ON-CHAIN BSC CONTRACT VERIFIER
# ==========================================
async def is_valid_bsc_contract(client: httpx.AsyncClient, address: str) -> bool:
    """
    Queries the BSC node to ensure the address is an actual deployed smart contract 
    and not a user's wallet address or dead address.
    """
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getCode",
        "params": [address, "latest"],
        "id": 1
    }
    try:
        response = await client.post(BSC_RPC_URL, json=payload, timeout=5.0)
        if response.status_code == 200:
            result = response.json().get("result", "0x")
            # If code length is > 2 ('0x'), it is a smart contract
            return result != "0x" and len(result) > 2
    except Exception as e:
        logging.warning(f"BSC RPC verification error for {address}: {e}")
        # Default to True on RPC failure to avoid missing potential mints
        return True
    return False

# ==========================================
# TELEGRAM ALERT SENDER
# ==========================================
async def send_telegram_alert(client: httpx.AsyncClient, contract: str, source: str, snippet: str):
    """Sends structured alert with actionable Claude Code / MCP execution payload."""
    
    # Formatted command string ready for direct agent execution
    agent_command = f"Access my Binance Agentic Wallet. Call the write function mint(1) on BNB Chain at contract address {contract} with value 0 BNB."

    message = (
        f"🚨 **FREE NFT MINT DETECTED** 🚨\n\n"
        f"📍 **Source:** {source}\n"
        f"📄 **Contract Address:** `{contract}`\n"
        f"🔗 **Explorer:** https://bscscan.com/address/{contract}\n\n"
        f"📝 **Context Snippet:**\n_{snippet[:250]}..._\n\n"
        f"🤖 **Direct Agent Command (Copy & Paste):**\n"
        f"```bash\n{agent_command}\n```"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    
    try:
        res = await client.post(url, json=payload, timeout=10.0)
        if res.status_code == 200:
            logging.info(f"Successfully alerted contract: {contract}")
        else:
            logging.error(f"Failed to send Telegram alert: {res.text}")
    except Exception as e:
        logging.error(f"Telegram notification exception: {e}")

# ==========================================
# ENGINE A: NITTER / TWITTER SCANNER
# ==========================================
async def scan_x_user(client: httpx.AsyncClient, username: str):
    """Scans user RSS feed across multiple Nitter instances with failover."""
    for instance in NITTER_INSTANCES:
        rss_url = f"{instance}/{username}/rss"
        try:
            resp = await client.get(rss_url, timeout=8.0, follow_redirects=True)
            if resp.status_code == 200 and "xml" in resp.headers.get("content-type", ""):
                root = ET.fromstring(resp.content)
                for item in root.findall(".//item"):
                    title = item.find("title").text if item.find("title") is not None else ""
                    desc = item.find("description").text if item.find("description") is not None else ""
                    content = f"{title} {desc}".lower()
                    
                    if any(kw in content for kw in KEYWORDS):
                        await process_text_content(client, content, f"X (@{username})")
                return # Exit loop if successful
        except Exception:
            continue # Try next instance on failure

# ==========================================
# ENGINE B: TELEGRAM CHANNEL SCANNER
# ==========================================
async def scan_telegram_channel(client: httpx.AsyncClient, channel_name: str):
    """Scans public Telegram channel web previews."""
    url = f"https://t.me/s/{channel_name}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        resp = await client.get(url, headers=headers, timeout=10.0)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            messages = soup.find_all("div", class_="tgme_widget_message_text")
            
            for msg in messages:
                text = msg.get_text(strip=True).lower()
                if any(kw in text for kw in KEYWORDS):
                    await process_text_content(client, text, f"Telegram (t.me/{channel_name})")
    except Exception as e:
        logging.warning(f"Failed scraping Telegram channel {channel_name}: {e}")

# ==========================================
# CORE PARSING & FILTERING
# ==========================================
async def process_text_content(client: httpx.AsyncClient, text: str, source: str):
    """Extracts 0x contract addresses, verifies on-chain status, and notifies."""
    # Matches EVM contract addresses
    found_addresses = re.findall(r"0x[a-fA-F0-9]{40}", text)
    
    for addr in set(found_addresses):
        addr_lower = addr.lower()
        if is_already_notified(addr_lower):
            continue

        # Check BSC On-Chain status
        if await is_valid_bsc_contract(client, addr_lower):
            mark_as_notified(addr_lower)
            await send_telegram_alert(client, addr, source, text)

# ==========================================
# MAIN CONTROLLER LOOP
# ==========================================
async def main():
    init_db()
    logging.info("Starting Dual-Engine Free NFT Mint Scanner...")

    async with httpx.AsyncClient() as client:
        while True:
            try:
                tasks = []
                for user in X_TARGET_USERS:
                    tasks.append(scan_x_user(client, user))
                for channel in TELEGRAM_CHANNELS:
                    tasks.append(scan_telegram_channel(client, channel))
                
                await asyncio.gather(*tasks)
            except Exception as e:
                logging.error(f"Error in execution loop: {e}")

            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    asyncio.run(main())
