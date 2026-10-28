import os
import re
import html
import asyncio
import logging
import sqlite3
import httpx
from pyrogram import Client, filters
from pyrogram.types import Message

# ==========================================
# CONFIGURATION
# ==========================================
# Telegram API credentials from my.telegram.org
API_ID = int(os.getenv("TELEGRAM_API_ID", "123456"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "YOUR_API_HASH")

# Bot credentials for sending alerts to your personal chat
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")

# Public BSC RPC Node
BSC_RPC_URL = os.getenv("BSC_RPC_URL", "https://bsc-dataseed1.binance.org/")

# Target channels to monitor (without @)
TARGET_CHANNELS = [
    "robotlee_10group",
    "binance_alpha_calls",
    "bsc_gems",
    "Airdrop_farming"
]

KEYWORDS = ["mint", "free", "agentic", "binance", ".ai", "claim", "nft", "bnb"]
DB_FILE = "mint_tracker.db"

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
async def is_valid_bsc_contract(address: str) -> bool:
    """Queries BSC RPC to verify address is a deployed smart contract."""
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getCode",
        "params": [address, "latest"],
        "id": 1
    }
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(BSC_RPC_URL, json=payload, timeout=5.0)
            if resp.status_code == 200:
                result = resp.json().get("result", "0x")
                return result != "0x" and len(result) > 2
        except Exception as e:
            logging.warning(f"BSC RPC verification error for {address}: {e}")
            return True # Fallback to True to avoid missing mints
    return False

# ==========================================
# TELEGRAM ALERT SENDER (SAFE HTML PARSER)
# ==========================================
async def send_telegram_alert(contract: str, source: str, snippet: str):
    """Sends formatted alert using Telegram HTML parsing to avoid offset errors."""
    clean_snippet = html.escape(snippet[:300])
    agent_command = f"Access my Binance Agentic Wallet. Call the write function mint(1) on BNB Chain at contract address {contract} with value 0 BNB."

    message = (
        f"🚨 <b>FREE NFT MINT DETECTED</b> 🚨\n\n"
        f"📍 <b>Source:</b> {html.escape(source)}\n"
        f"📄 <b>Contract Address:</b> <code>{contract}</code>\n"
        f"🔗 <b>BscScan:</b> https://bscscan.com/address/{contract}\n\n"
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
    
    async with httpx.AsyncClient() as client:
        try:
            res = await client.post(url, json=payload, timeout=10.0)
            if res.status_code == 200:
                logging.info(f"Successfully alerted contract: {contract}")
            else:
                logging.error(f"Failed to send Telegram alert: {res.text}")
        except Exception as e:
            logging.error(f"Telegram notification exception: {e}")

# ==========================================
# PYROGRAM REAL-TIME LISTENER
# ==========================================
app = Client("mint_userbot", api_id=API_ID, api_hash=API_HASH)

@app.on_message(filters.chat(TARGET_CHANNELS))
async def handle_new_message(client: Client, message: Message):
    text = message.text or message.caption or ""
    text_lower = text.lower()

    if any(kw in text_lower for kw in KEYWORDS):
        # Extract 0x EVM contract addresses
        found_addresses = re.findall(r"0x[a-fA-F0-9]{40}", text)
        for addr in set(found_addresses):
            addr_lower = addr.lower()
            
            if is_already_notified(addr_lower):
                continue
            
            # Verify on-chain contract status
            if await is_valid_bsc_contract(addr_lower):
                mark_as_notified(addr_lower)
                source_title = message.chat.title or message.chat.username
                await send_telegram_alert(addr, f"Telegram ({source_title})", text)

# ==========================================
# MAIN EXECUTION
# ==========================================
if __name__ == "__main__":
    init_db()
    logging.info("Starting Native Telegram MTProto Real-Time Mint Listener...")
    app.run()
