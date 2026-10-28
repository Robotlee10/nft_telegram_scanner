import os
import re
import html
import asyncio
import logging
import sqlite3
import httpx

# ==========================================
# CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")
BSC_RPC_URL = os.getenv("BSC_RPC_URL", "https://bsc-dataseed1.binance.org/")

# Keywords to match in posts
KEYWORDS = ["mint", "free", "agentic", "binance", ".ai", "claim", "nft", "bnb"]

CHECK_INTERVAL_SECONDS = 30
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
async def is_valid_bsc_contract(client: httpx.AsyncClient, address: str) -> bool:
    """Verifies address is an active deployed contract on BSC (not a wallet)."""
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
        logging.warning(f"BSC RPC error for {address}: {e}")
        return True
    return False

# ==========================================
# SAFE TELEGRAM ALERT SENDER
# ==========================================
async def send_telegram_alert(client: httpx.AsyncClient, contract: str, source: str, snippet: str, link: str = ""):
    """Sends HTML-formatted alert to Telegram."""
    clean_snippet = html.escape(snippet[:300])
    agent_command = f"Access my Binance Agentic Wallet. Call the write function mint(1) on BNB Chain at contract address {contract} with value 0 BNB."

    message = (
        f"🚨 <b>FREE NFT MINT DETECTED</b> 🚨\n\n"
        f"📍 <b>Source:</b> {html.escape(source)}\n"
        f"📄 <b>Contract:</b> <code>{contract}</code>\n"
        f"🔗 <b>BscScan:</b> https://bscscan.com/address/{contract}\n"
        f"🌐 <b>Post Link:</b> {link if link else 'N/A'}\n\n"
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
        if res.status_code == 200:
            logging.info(f"Alert sent for contract: {contract}")
        else:
            logging.error(f"Telegram API Error ({res.status_code}): {res.text}")
    except Exception as e:
        logging.error(f"Failed sending alert: {e}")

# ==========================================
# ENGINE A: BINANCE SQUARE PUBLIC FEED
# ==========================================
async def scan_binance_square(client: httpx.AsyncClient):
    """
    Scans Binance Square public feeds using Binance's unauthenticated JSON API endpoints.
    Requires no login, zero cookies, zero ban risk.
    """
    url = "https://www.binance.com/bapi/composite/v1/public/cms/article/catalog/list"
    params = {"catalogId": 48, "pageNo": 1, "pageSize": 10}
    
    try:
        resp = await client.get(url, params=params, timeout=10.0)
        if resp.status_code == 200:
            data = resp.json()
            articles = data.get("data", {}).get("articles", [])
            
            for article in articles:
                title = article.get("title", "")
                body = article.get("code", "")
                text = f"{title} {body}".lower()
                
                if any(kw in text for kw in KEYWORDS):
                    contracts = re.findall(r"0x[a-fA-F0-9]{40}", text)
                    for addr in set(contracts):
                        addr_lower = addr.lower()
                        if not is_already_notified(addr_lower):
                            if await is_valid_bsc_contract(client, addr_lower):
                                mark_as_notified(addr_lower)
                                post_url = f"https://www.binance.com/en/square/post/{article.get('id', '')}"
                                await send_telegram_alert(client, addr, "Binance Square Official", title, post_url)
    except Exception as e:
        logging.warning(f"Binance Square API fetch error: {e}")

# ==========================================
# MAIN CONTROLLER
# ==========================================
async def main():
    init_db()
    logging.info("Starting Zero-Account Public Mint Scanner...")

    async with httpx.AsyncClient() as client:
        while True:
            try:
                await scan_binance_square(client)
            except Exception as e:
                logging.error(f"Main loop error: {e}")

            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    asyncio.run(main())
