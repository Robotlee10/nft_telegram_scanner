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
import shutil
import subprocess
from typing import Optional, Tuple

import httpx
import qrcode
from hexbytes import HexBytes
from web3 import Web3
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ==========================================
# CONFIGURATION & ENVIRONMENT VARIABLES
# ==========================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MY_AGENTIC_WALLET = os.getenv("MY_AGENTIC_WALLET")
BSC_HTTP_URL = os.getenv("BSC_HTTP_URL", "https://bsc-dataseed1.binance.org/")

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agentic_mints.db")
BSC_CHAIN_ID = 56
AUTO_MINT_THRESHOLD = 3  # Score >= 3 triggers automated minting
RPC_RECEIPT_TIMEOUT = 36 # Seconds to poll for transaction receipt
RPC_POLL_INTERVAL = 2    # Seconds between receipt checks
BAW_LOCAL_BIN = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "node_modules",
    ".bin",
    "baw.cmd" if os.name == "nt" else "baw"
)

MINT_CALLDATA = {
    "mint(1)": "0xa0712d680000000000000000000000000000000000000000000000000000000000000001",
    "freeMint()": "0x438d1d8f",
    "mint(0)": "0xa0712d680000000000000000000000000000000000000000000000000000000000000000",
    "claim()": "0x4e71d92d",
}

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
wallet_ready = asyncio.Event()

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
                order_id TEXT,
                error_log TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                executed_at TIMESTAMP
            )
        """)
        columns = {
            row[1] for row in cursor.execute("PRAGMA table_info(mints)").fetchall()
        }
        if "order_id" not in columns:
            cursor.execute("ALTER TABLE mints ADD COLUMN order_id TEXT")
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
class WalletCLIError(RuntimeError):
    pass

def _baw_command(arguments: list[str]) -> list[str]:
    executable = shutil.which("baw")
    if executable is None and os.path.isfile(BAW_LOCAL_BIN):
        executable = BAW_LOCAL_BIN
    if executable is None:
        raise WalletCLIError(
            "Binance Agentic Wallet CLI is missing. Install project dependencies with `npm install`."
        )

    command = [executable, *arguments]
    if os.name == "nt" and executable.lower().endswith((".cmd", ".bat")):
        command_line = subprocess.list2cmdline(command)
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", f'"{command_line}"']
    return command

async def run_baw(arguments: list[str], timeout: float = 30.0) -> dict:
    command = _baw_command(arguments)

    def run_command() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )

    try:
        result = await asyncio.to_thread(run_command)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise WalletCLIError(f"Binance Agentic Wallet CLI failed: {e}") from e

    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise WalletCLIError("Binance Agentic Wallet CLI returned invalid JSON.") from e

    if result.returncode != 0 or not response.get("success"):
        error = response.get("error", {})
        message = error.get("message") if isinstance(error, dict) else None
        raise WalletCLIError(message or "Binance Agentic Wallet CLI command failed.")

    data = response.get("data")
    if not isinstance(data, dict):
        raise WalletCLIError("Binance Agentic Wallet CLI returned an unexpected response.")
    return data

def print_signin_qr(url: str, pairing_code: str):
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    print("\nScan this Binance Agentic Wallet QR code in the Binance App:")
    qr.print_ascii(out=sys.stdout)
    print(f"\nPairing code: {pairing_code}")
    print(f"Sign-in URL: {url}\n")

async def ensure_wallet_connected():
    status = await run_baw(["wallet", "status", "--json"])
    if status.get("status") != "CONNECTED":
        signin = await run_baw(["auth", "signin", "--json"])
        if signin.get("status") != "ALREADY_CONNECTED":
            qr_url = signin.get("urlForWeb")
            qr_code_id = signin.get("qrCodeId")
            pairing_code = signin.get("pairingCode")
            if not isinstance(qr_url, str) or not qr_url:
                raise WalletCLIError("Sign-in response did not contain a QR URL.")
            if not isinstance(qr_code_id, str) or not qr_code_id:
                raise WalletCLIError("Sign-in response did not contain a QR code ID.")
            if not isinstance(pairing_code, str) or not pairing_code:
                raise WalletCLIError("Sign-in response did not contain the QR code details.")

            print_signin_qr(qr_url, pairing_code)
            await run_baw(
                ["auth", "verify", "--qrCodeId", qr_code_id, "--json"],
                timeout=330.0,
            )

        status = await run_baw(["wallet", "status", "--json"])
        if status.get("status") != "CONNECTED":
            raise WalletCLIError("Wallet sign-in did not reach CONNECTED status.")

    if not MY_AGENTIC_WALLET:
        raise WalletCLIError("MY_AGENTIC_WALLET is not configured in .env.")

    address_data = await run_baw(["wallet", "address", "--json"])
    bsc_address = next(
        (
            entry.get("address")
            for entry in address_data.get("addresses", [])
            if entry.get("binanceChainId") == str(BSC_CHAIN_ID)
        ),
        None,
    )
    if not isinstance(bsc_address, str) or bsc_address.lower() != MY_AGENTIC_WALLET.lower():
        raise WalletCLIError("MY_AGENTIC_WALLET does not match the connected BSC wallet.")

    logger.info("Binance Agentic Wallet is connected and matches MY_AGENTIC_WALLET.")

async def execute_agentic_mint(
    contract_address: str,
    function_name: str,
    trigger_source: str = "AUTO",
) -> Tuple[str, str]:
    logger.info(f"[{trigger_source}] Preparing reviewed mint: Contract={contract_address}, Function={function_name}")

    calldata = MINT_CALLDATA.get(function_name)
    if not calldata:
        return "failed", f"Unsupported mint function: {function_name}"
    if not wallet_ready.is_set():
        return "blocked", "Binance Agentic Wallet is not connected."
    if not sys.stdin.isatty():
        return "blocked", "Mint approval requires an interactive terminal."
    if not Web3.is_address(contract_address):
        return "failed", "Invalid contract address."
    wallet_address = MY_AGENTIC_WALLET
    if not wallet_address:
        return "blocked", "MY_AGENTIC_WALLET is not configured in .env."

    try:
        settings = await run_baw(["wallet", "settings", "--json"])
        dev_mode = settings.get("devMode", {})
        expires_at = dev_mode.get("expiresAt")
        if (
            not dev_mode.get("enabled")
            or not isinstance(expires_at, (int, float))
            or expires_at <= time.time()
        ):
            return "blocked", "Enable Developer Mode in the Binance App before minting."

        preview = await run_baw([
            "contract-call", "preview",
            "--binanceChainId", str(BSC_CHAIN_ID),
            "--from", wallet_address,
            "--to", contract_address,
            "--value", "0",
            "--inputData", calldata,
            "--json",
        ])
        request_id = preview.get("requestId")
        if not isinstance(request_id, str) or not request_id:
            return "failed", "Successful preview did not return a request ID."

        review = {
            "parsedTx": preview.get("parsedTx"),
            "simulationResult": preview.get("simulationResult"),
            "risks": preview.get("risks"),
            "requireConfirmation": preview.get("requireConfirmation"),
        }
        print("\n=== Binance Agentic Wallet transaction preview ===")
        print(json.dumps(review, indent=2, ensure_ascii=True))
        print(
            f"\nTarget: {contract_address}\nFunction: {function_name}\n"
            "Review the contract, parsed transaction, simulation, and risks above."
        )
        try:
            confirmation = await asyncio.to_thread(
                input,
                "Type 'yes' to execute this mint after reviewing it, or anything else to cancel: ",
            )
        except EOFError:
            return "blocked", "No terminal approval was available; transaction was not executed."

        if confirmation.strip().lower() != "yes":
            return "declined", "Transaction declined; nothing was executed."

        execution = await run_baw([
            "contract-call", "execute",
            "--requestId", request_id,
            "--json",
        ])
        execution_status = execution.get("status")

        if execution_status == "PENDING_CONFIRMATION":
            order_id = execution.get("orderId")
            await telegram_service.send_message(
                f"⏳ <b>Mint requires Binance App confirmation.</b>\n"
                f"<b>Contract:</b> <code>{contract_address}</code>\n"
                f"<b>Function:</b> <code>{function_name}</code>\n"
                f"<b>Order ID:</b> <code>{order_id or 'unavailable'}</code>"
            )
            return "awaiting_confirmation", str(order_id or "")

        if execution_status != "BROADCASTED":
            return "failed", f"Unexpected execution status: {execution_status!r}"

        tx_hash = execution.get("txHash")
        if not isinstance(tx_hash, str) or not tx_hash.startswith("0x"):
            return "failed", "Broadcast response did not contain a transaction hash."

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
            return "success", tx_hash

        await telegram_service.send_message(
            f"🔴 <b>MINT FAILED / REVERTED!</b>\n\n"
            f"<b>Contract:</b> <code>{contract_address}</code>\n"
            f"<b>TX Hash:</b> <code>{tx_hash}</code>\n"
            f"<b>Reason:</b> {err_detail}"
        )
        return "failed", err_detail

    except WalletCLIError as e:
        logger.error(f"Binance Agentic Wallet operation failed: {e}")
        return "failed", str(e)
    except Exception as e:
        logger.exception("Unexpected error during reviewed mint execution.")
        return "failed", str(e)

async def verify_tx_on_chain(tx_hash: str) -> Tuple[bool, Optional[int], str]:
    start_time = time.time()
    while time.time() - start_time < RPC_RECEIPT_TIMEOUT:
        try:
            if not w3.is_connected():
                await asyncio.sleep(RPC_POLL_INTERVAL)
                continue
            receipt = w3.eth.get_transaction_receipt(HexBytes(tx_hash))
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
    logger.info(f"Reviewed Mint Queue active. Monitoring DB for quality score >= {AUTO_MINT_THRESHOLD}/7...")
    while True:
        if not wallet_ready.is_set():
            await asyncio.sleep(3)
            continue

        conn = None
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
                logger.info(f"Mint review required for Target ID #{mint_id} (Score: {score}/7)")
                cursor.execute("UPDATE mints SET status = 'processing' WHERE id = ?", (mint_id,))
                conn.commit()

                status, result = await execute_agentic_mint(
                    contract,
                    func_name,
                    trigger_source=f"AUTO (Score: {score}/7)",
                )

                now = time.strftime("%Y-%m-%d %H:%M:%S")
                tx_hash = result if status == "success" else None
                order_id = result if status == "awaiting_confirmation" else None
                error_log = result if status in {"failed", "blocked", "declined"} else None
                cursor.execute(
                    """
                    UPDATE mints
                    SET status = ?, tx_hash = ?, order_id = ?, error_log = ?, executed_at = ?
                    WHERE id = ?
                    """,
                    (status, tx_hash, order_id, error_log, now, mint_id),
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Error in DB Poller: {e}")
        finally:
            if conn is not None:
                conn.close()
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
    logger.info("Starting Binance Agentic Wallet mint review worker.")
    init_db()

    async def connect_wallet():
        while not wallet_ready.is_set():
            try:
                await ensure_wallet_connected()
                wallet_ready.set()
                return
            except WalletCLIError as e:
                logger.error(f"Wallet setup is not ready: {e}")
                await asyncio.sleep(60)

    await asyncio.gather(
        connect_wallet(),
        poll_telegram_updates(),
        poll_db_auto_mints(),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Worker stopped gracefully.")