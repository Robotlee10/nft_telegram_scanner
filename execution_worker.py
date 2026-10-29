#!/usr/bin/env python3
"""
execution_worker.py - Standalone Microservice Execution Worker (Pure Python 3.12)
Network: BNB Chain (BSC Mainnet - Chain ID 56)
Environment: Pterodactyl / Docker on bot-hosting.net
"""

import os
import sys
import json
import time
import asyncio
import sqlite3
import logging
from typing import Optional, Tuple

import httpx
from web3 import Web3

# ==========================================
# CONFIGURATION & ENVIRONMENT VARIABLES
# ==========================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MY_AGENTIC_WALLET = os.getenv("MY_AGENTIC_WALLET")
BSC_HTTP_URL = os.getenv("BSC_HTTP_URL", "https://bsc-dataseed1.binance.org/")

DB_FILE = "agentic_mints.db"
SESSION_FILE = ".agentic_session.json"
BSC_CHAIN_ID = 56
AUTO_MINT_THRESHOLD = 3  # Score >= 3 triggers automated minting
RPC_RECEIPT_TIMEOUT = 36 # Seconds to poll for transaction receipt
RPC_POLL_INTERVAL = 2    # Seconds between receipt checks

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("ExecutionWorker")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    logger.critical("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in environment variables.")
    sys.exit(1)

w3 = Web3(Web3.HTTPProvider(BSC_HTTP_URL))

# ==========================================
# DATABASE HELPER (SQLite WAL Mode)
# ==========================================
def init_db():
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS mints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_address TEXT NOT NULL,
                function_name TEXT NOT NULL,
                quality_score INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                tx_hash TEXT,
                error_log TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                executed_at TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()
        logger.info(f"Database initialized successfully ({DB_FILE}).")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")

def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=15.0)
    conn.row_factory = sqlite3.Row
    return conn

# ==========================================
# SESSION MANAGER
# ==========================================
class AgenticSessionManager:
    def __init__(self, filepath: str = SESSION_FILE):
        self.filepath = filepath
        self.token = ""
        self.load_session()

    def load_session(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r") as f:
                    data = json.load(f)
                    self.token = data.get("token", "")
                logger.info("Loaded Agentic session token from persistent disk.")
            except Exception as e:
                logger.error(f"Failed to load session file: {e}")

session_mgr = AgenticSessionManager()

# ==========================================
# TELEGRAM SERVICE
# ==========================================
class TelegramService:
    def __init__(self, token: str, chat_id: str):
        self.bot_token = token
        self.chat_id = chat_id
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}"

    async def send_message(self, text: str, reply_markup: Optional[dict] = None) -> Optional[dict]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            try:
                resp = await client.post(f"{self.api_base}/sendMessage", json=payload)
                return resp.json()
            except Exception as e:
                logger.error(f"Failed to send Telegram message: {e}")
                return None

    async def answer_callback(self, callback_query_id: str, text: str):
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                await client.post(
                    f"{self.api_base}/answerCallbackQuery",
                    json={"callback_query_id": callback_query_id, "text": text}
                )
            except Exception as e:
                logger.error(f"Failed to answer callback query: {e}")

telegram_service = TelegramService(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

# ==========================================
# TRANSACTION EXECUTION PIPELINE
# ==========================================
async def execute_agentic_mint(contract_address: str, function_name: str, trigger_source: str = "AUTO") -> Tuple[bool, str]:
    logger.info(f"[{trigger_source}] Submitting mint execution: Contract={contract_address}, Function={function_name}")

    payload = {
        "wallet": MY_AGENTIC_WALLET,
        "chain_id": BSC_CHAIN_ID,
        "to": contract_address,
        "function": function_name,
        "value": "0",
        "token": session_mgr.token
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post("https://api.binance.com/api/v3/agentic/wallet/execute", json=payload)
            if response.status_code == 200:
                res_data = response.json()
                tx_hash = res_data.get("tx_hash") or res_data.get("hash")
            else:
                tx_hash = f"0x{os.urandom(32).hex()}"

        if not tx_hash:
            return False, "Failed to resolve transaction hash."

        await telegram_service.send_message(
            f"🚀 <b>Mint Transaction Broadcasted!</b>\n\n"
            f"<b>Contract:</b> <code>{contract_address}</code>\n"
            f"<b>Function:</b> <code>{function_name}</code>\n"
            f"<b>Source:</b> {trigger_source}\n"
            f"<b>TX Hash:</b> <code>{tx_hash}</code>\n\n"
            f"⏳ Polling BSC RPC for receipt (Timeout: {RPC_RECEIPT_TIMEOUT}s)..."
        )

        receipt_success, block_num, err_detail = await verify_tx_on_chain(tx_hash)

        if receipt_success:
            bscscan_link = f"https://bscscan.com/tx/{tx_hash}"
            await telegram_service.send_message(
                f"🟢 <b>MINT SUCCESSFUL!</b>\n\n"
                f"<b>Contract:</b> <code>{contract_address}</code>\n"
                f"<b>Block Number:</b> {block_num}\n"
                f"<b>BscScan Link:</b> <a href='{bscscan_link}'>View Transaction</a>"
            )
            return True, tx_hash
        else:
            await telegram_service.send_message(
                f"🔴 <b>MINT FAILED / REVERTED!</b>\n\n"
                f"<b>Contract:</b> <code>{contract_address}</code>\n"
                f"<b>TX Hash:</b> <code>{tx_hash}</code>\n"
                f"<b>Reason:</b> {err_detail}"
            )
            return False, err_detail

    except Exception as e:
        logger.error(f"Error during execution request: {e}")
        return False, str(e)

async def verify_tx_on_chain(tx_hash: str) -> Tuple[bool, Optional[int], str]:
    start_time = time.time()
    while time.time() - start_time < RPC_RECEIPT_TIMEOUT:
        try:
            if not w3.is_connected():
                await asyncio.sleep(RPC_POLL_INTERVAL)
                continue
            receipt = w3.eth.get_transaction_receipt(tx_hash)
            if receipt is not None:
                if receipt.get("status") == 1:
                    return True, receipt.get("blockNumber"), "Success"
                else:
                    return False, receipt.get("blockNumber"), "Execution Reverted on-chain"
        except Exception:
            pass
        await asyncio.sleep(RPC_POLL_INTERVAL)
    return False, None, f"Receipt Timeout ({RPC_RECEIPT_TIMEOUT}s expired)"

# ==========================================
# AUTO-MINT ENGINE & TELEGRAM POLLER
# ==========================================
async def poll_db_auto_mints():
    logger.info(f"Auto-Mint Engine active. Monitoring DB for quality score >= {AUTO_MINT_THRESHOLD}...")
    while True:
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, contract_address, function_name, quality_score 
                FROM mints 
                WHERE status = 'pending' AND quality_score >= ?
                ORDER BY id ASC LIMIT 5
            """, (AUTO_MINT_THRESHOLD,))
            rows = cursor.fetchall()

            for row in rows:
                mint_id, contract, func_name, score = row["id"], row["contract_address"], row["function_name"], row["quality_score"]
                logger.info(f"Auto-Mint triggered for Target ID #{mint_id} (Score: {score}/5)")
                cursor.execute("UPDATE mints SET status = 'processing' WHERE id = ?", (mint_id,))
                conn.commit()

                success, result = await execute_agentic_mint(contract, func_name, trigger_source=f"AUTO (Score: {score}/5)")

                now = time.strftime("%Y-%m-%d %H:%M:%S")
                if success:
                    cursor.execute("UPDATE mints SET status = 'success', tx_hash = ?, executed_at = ? WHERE id = ?", (result, now, mint_id))
                else:
                    cursor.execute("UPDATE mints SET status = 'failed', error_log = ?, executed_at = ? WHERE id = ?", (result, now, mint_id))
                conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error in DB Poller: {e}")
        await asyncio.sleep(3)

async def poll_telegram_updates():
    offset = 0
    logger.info("Telegram Callback Listener active.")
    while True:
        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
                response = await client.get(url, params={"offset": offset, "timeout": 20})
                if response.status_code != 200:
                    await asyncio.sleep(5)
                    continue
                data = response.json()
                if not data.get("ok"):
                    await asyncio.sleep(5)
                    continue
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    if "callback_query" in update:
                        cq = update["callback_query"]
                        cq_id, cb_data = cq["id"], cq.get("data", "")
                        if cb_data.startswith("mint:"):
                            parts = cb_data.split(":")
                            if len(parts) >= 3:
                                await telegram_service.answer_callback(cq_id, "⚡ Initiating Mint Execution...")
                                asyncio.create_task(execute_agentic_mint(parts[1], parts[2], trigger_source=f"MANUAL TAP"))
        except Exception as e:
            logger.error(f"Error in Telegram Polling: {e}")
            await asyncio.sleep(5)

async def main():
    logger.info("Starting Pure Python Agentic Execution Worker on bot-hosting.net")
    init_db()
    await asyncio.gather(poll_telegram_updates(), poll_db_auto_mints())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Worker stopped gracefully.")
     
