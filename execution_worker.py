#!/usr/bin/env python3
"""
execution_worker.py - Standalone Microservice Execution Worker (Pure Python 3.12)
Network: BNB Chain (BSC Mainnet - Chain ID 56)
Environment: Pterodactyl / Docker on bot-hosting.net
"""

import os
import sys
import io
import html
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
import nft_telegram_scanner as nft_scanner
from nft_chain_monitor import NftChainMonitor
from telegram_nft_features import TelegramNftFeatures, developer_mode_is_active

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
AUTO_MINT_THRESHOLD = 4
WALLET_NOT_CONNECTED_ERROR = "Binance Agentic Wallet is not connected."
RPC_RECEIPT_TIMEOUT = 36  # Seconds to poll for transaction receipt
RPC_POLL_INTERVAL = 2  # Seconds between receipt checks
BAW_PACKAGE_ENTRY = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "node_modules",
    "@binance",
    "agentic-wallet",
    "dist",
    "index.js",
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
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ExecutionWorker")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    logger.critical("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in environment variables.")
    sys.exit(1)

w3 = Web3(Web3.HTTPProvider(BSC_HTTP_URL))
wallet_ready = asyncio.Event()
wallet_login_required = asyncio.Event()

# Keep strong references to fire-and-forget tasks so they aren't garbage collected
_background_tasks: set = set()


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def report_task_result(completed_task: asyncio.Task) -> None:
        _background_tasks.discard(completed_task)
        if completed_task.cancelled():
            return
        error = completed_task.exception()
        if error is not None:
            logger.error(
                "Background task failed.",
                exc_info=(type(error), error, error.__traceback__),
            )

    task.add_done_callback(report_task_result)
    return task


# ==========================================
# DATABASE HELPER (SQLite WAL Mode)
# ==========================================
def init_db():
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("BEGIN IMMEDIATE")
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
                scarcity_source_override INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                executed_at TIMESTAMP
            )
        """)
        columns = {
            row[1] for row in cursor.execute("PRAGMA table_info(mints)").fetchall()
        }
        if "order_id" not in columns:
            cursor.execute("ALTER TABLE mints ADD COLUMN order_id TEXT")
        if "scarcity_source_override" not in columns:
            cursor.execute(
                "ALTER TABLE mints ADD COLUMN scarcity_source_override INTEGER NOT NULL DEFAULT 0"
            )
        conn.commit()
        conn.close()
        logger.info(f"Database initialized successfully ({DB_FILE}).")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")


def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=15.0)
    conn.row_factory = sqlite3.Row
    return conn


def clear_wallet_login_markers():
    conn = None
    try:
        conn = get_db_connection()
        conn.execute(
            """
            UPDATE mints
            SET error_log = NULL
            WHERE status = 'pending' AND error_log = ?
            """,
            (WALLET_NOT_CONNECTED_ERROR,),
        )
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Failed to clear stale wallet sign-in markers: {e}")
    finally:
        if conn is not None:
            conn.close()


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
                "disable_web_page_preview": True,
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            try:
                resp = await client.post(f"{self.api_base}/sendMessage", json=payload)
                return resp.json()
            except Exception as e:
                logger.error(f"Failed to send Telegram message: {e}")
                return None

    async def send_photo(self, photo_bytes: bytes, caption: str, reply_markup: Optional[dict] = None) -> Optional[dict]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            files = {"photo": ("qrcode.png", photo_bytes, "image/png")}
            data = {
                "chat_id": self.chat_id,
                "caption": caption,
                "parse_mode": "HTML",
            }
            if reply_markup:
                data["reply_markup"] = json.dumps(reply_markup)
            try:
                resp = await client.post(f"{self.api_base}/sendPhoto", data=data, files=files)
                return resp.json()
            except Exception as e:
                logger.error(f"Failed to send Telegram photo: {e}")
                return None

    async def answer_callback(self, callback_query_id: str, text: str):
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                await client.post(
                    f"{self.api_base}/answerCallbackQuery",
                    json={"callback_query_id": callback_query_id, "text": text},
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
    if os.path.isfile(BAW_PACKAGE_ENTRY):
        node = shutil.which("node")
        if not node:
            raise WalletCLIError("Node.js is required to run the Binance Agentic Wallet CLI.")
        return [node, BAW_PACKAGE_ENTRY, *arguments]

    executable = shutil.which("baw")
    if executable and not executable.lower().endswith((".cmd", ".bat")):
        return [executable, *arguments]

    raise WalletCLIError(
        "Binance Agentic Wallet CLI is missing. Install project dependencies with `npm install`."
    )


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

    response = None
    decoder = json.JSONDecoder()
    for output in (result.stdout, result.stderr):
        for index, character in enumerate(output):
            if character != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(output[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                response = candidate
                break
        if response is not None:
            break

    if response is None:
        # Capture the actual stderr text if available to debug exit code 1 instantly
        err_output = result.stderr.strip() or result.stdout.strip() or "Unknown CLI crash"
        raise WalletCLIError(
            f"CLI Error (Exit Code {result.returncode}): {err_output[:200]}"
        )

    if result.returncode != 0 or not response.get("success"):
        error = response.get("error", {})
        message = error.get("message") if isinstance(error, dict) else None
        raise WalletCLIError(message or "Binance Agentic Wallet CLI command failed.")

    data = response.get("data")
    if not isinstance(data, dict):
        raise WalletCLIError("Binance Agentic Wallet CLI returned an unexpected response.")
    return data


def generate_qr_bytes(url: str) -> bytes:
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def ensure_wallet_connected():
    status = await run_baw(["wallet", "status", "--json"])
    if status.get("status") != "CONNECTED":
        if not MY_AGENTIC_WALLET:
            raise WalletCLIError("MY_AGENTIC_WALLET is not configured in .env.")
        wallet_login_required.set()
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

            # Send QR code image, sign-in link, & pairing details to Telegram with a regenerate button
            qr_bytes = generate_qr_bytes(qr_url)
            markup = {
                "inline_keyboard": [[
                    {"text": "🔄 Regenerate QR Code", "callback_data": f"auth:regen:{qr_code_id}"}
                ]]
            }
            caption = (
                f"🔐 <b>Binance Agentic Wallet Sign-In Required</b>\n\n"
                f"Scan the QR code above or open the direct sign-in link below:\n"
                f"🔗 <b>Sign-In Link:</b> <a href='{qr_url}'>{qr_url}</a>\n\n"
                f"🔢 <b>Pairing Code:</b> <code>{pairing_code}</code>\n"
                f"⏱️ <i>Expires in 5 minutes. Tap below to regenerate if needed.</i>"
            )
            await telegram_service.send_photo(qr_bytes, caption, reply_markup=markup)

            await run_baw(
                ["auth", "verify", "--qrCodeId", qr_code_id, "--json"],
                timeout=330.0,
            )

        status = await run_baw(["wallet", "status", "--json"])
        if status.get("status") != "CONNECTED":
            raise WalletCLIError("Wallet sign-in did not reach CONNECTED status.")
        wallet_login_required.clear()
    else:
        wallet_login_required.clear()

    clear_wallet_login_markers()

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
    logger.info(f"[{trigger_source}] Processing autonomous mint: Contract={contract_address}, Function={function_name}")

    calldata = MINT_CALLDATA.get(function_name)
    if not calldata:
        return "failed", f"Unsupported mint function: {function_name}"
    if not wallet_ready.is_set():
        return "blocked", WALLET_NOT_CONNECTED_ERROR
    if not Web3.is_address(contract_address):
        return "failed", "Invalid contract address."

    wallet_address = MY_AGENTIC_WALLET
    if not wallet_address:
        return "blocked", "MY_AGENTIC_WALLET is not configured in .env."

    try:
        settings = await run_baw(["wallet", "settings", "--json"])
        if not developer_mode_is_active(settings):
            error = (
                "Developer Mode is off or expired. Enable it in the Binance App "
                "(Agentic Wallet → Settings → Developer Mode); no mint was executed."
            )
            await telegram_service.send_message(
                f"❌ <b>Mint paused ({trigger_source}):</b> {error}",
                reply_markup={
                    "inline_keyboard": [[
                        {
                            "text": "🪙 Retry mint",
                            "callback_data": f"mint:{contract_address}:{function_name}",
                        }
                    ]]
                },
            )
            return "blocked", error

        # 1. Preview the contract call to guarantee it is valid and safe
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
        simulation = preview.get("simulationResult")
        if (
            not isinstance(request_id, str)
            or not request_id
            or not isinstance(preview.get("parsedTx"), dict)
            or not isinstance(simulation, dict)
            or simulation.get("simulationErrorDetail")
            or simulation.get("simulationCode") not in {"000000000", "0", 0}
        ):
            error = "Preview did not clearly confirm a successful simulation; nothing was executed."
            await telegram_service.send_message(
                f"❌ <b>Mint preview rejected ({trigger_source}).</b>\n"
                f"<b>Contract:</b> <code>{contract_address}</code>\n{error}"
            )
            return "failed", error

        logger.info(f"Preview passed for {contract_address}. Executing autonomously...")

        # 2. Execute immediately without waiting for manual app confirmation
        execution = await run_baw([
            "contract-call", "execute",
            "--requestId", request_id,
            "--json",
        ])

        tx_hash = execution.get("txHash")
        if not isinstance(tx_hash, str) or not tx_hash.startswith("0x"):
            execution_status = execution.get("status")
            return "failed", f"Execution did not broadcast. Status: {execution_status}"

        # 3. Verify transaction outcome on-chain and report back to Telegram
        receipt_success, block_num, err_detail = await verify_tx_on_chain(tx_hash)
        bscscan_link = f"https://bscscan.com/tx/{tx_hash}"

        if receipt_success:
            await telegram_service.send_message(
                f"🟢 <b>AUTONOMOUS MINT SUCCESSFUL!</b>\n\n"
                f"<b>Contract:</b> <code>{contract_address}</code>\n"
                f"<b>Function:</b> <code>{function_name}</code>\n"
                f"<b>Source:</b> {trigger_source}\n"
                f"<b>Block:</b> {block_num}\n"
                f"🔗 <a href='{bscscan_link}'>View on BscScan</a>"
            )
            return "success", tx_hash
        else:
            await telegram_service.send_message(
                f"🔴 <b>AUTONOMOUS MINT REVERTED!</b>\n\n"
                f"<b>Contract:</b> <code>{contract_address}</code>\n"
                f"<b>TX Hash:</b> <code>{tx_hash}</code>\n"
                f"<b>Reason:</b> {err_detail}"
            )
            return "failed", err_detail

    except WalletCLIError as e:
        err_msg = str(e)
        logger.error(f"Agentic Wallet CLI error: {err_msg}")
        await telegram_service.send_message(f"❌ <b>Mint Error ({trigger_source}):</b>\n<code>{err_msg}</code>")
        return "failed", err_msg
    except Exception as e:
        logger.exception("Unexpected error during autonomous execution.")
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


async def scan_telegram_contract(contract_address: str) -> dict:
    is_nft, nft_standard = await nft_scanner.verify_is_nft_contract(contract_address)
    if not is_nft:
        return {"is_nft": False}

    already_owned = await nft_scanner.already_owns_nft(contract_address)
    function_name, is_exclusive, access_reason = await nft_scanner.inspect_mint_access(
        contract_address
    )
    async with httpx.AsyncClient(follow_redirects=True) as client:
        quality_report = await nft_scanner.run_quality_checks(
            client,
            contract_address,
            tx_hash=None,
        )

    auto_mint_eligible = bool(
        not already_owned
        and is_exclusive
        and function_name
        and nft_scanner.is_auto_mint_eligible(quality_report)
    )
    queue_status = (
        nft_scanner.queue_auto_mint(contract_address, function_name, quality_report)
        if auto_mint_eligible and function_name
        else None
    )

    if is_exclusive:
        access_gate = f"Configured agent wallet only ({access_reason})"
    elif function_name:
        access_gate = f"Public mint ({access_reason})"
    else:
        access_gate = f"Not confirmed ({access_reason})"

    return {
        "is_nft": True,
        "nft_standard": nft_standard,
        "already_owned": already_owned,
        "access_gate": access_gate,
        "function_name": function_name,
        "quality_report": quality_report,
        "auto_mint_threshold": nft_scanner.AUTO_MINT_THRESHOLD,
        "auto_mint_eligible": auto_mint_eligible,
        "queue_status": queue_status,
    }


telegram_nft_features = TelegramNftFeatures(
    telegram_service=telegram_service,
    run_baw=run_baw,
    wallet_ready=wallet_ready,
    wallet_address=MY_AGENTIC_WALLET,
    web3=w3,
    mint_calldata=MINT_CALLDATA,
    verify_transaction=verify_tx_on_chain,
    chat_id=TELEGRAM_CHAT_ID,
    scan_contract=scan_telegram_contract,
    chain_id=BSC_CHAIN_ID,
)

nft_chain_monitor = (
    NftChainMonitor(
        web3=w3,
        wallet_address=MY_AGENTIC_WALLET,
        database_path=DB_FILE,
        telegram_service=telegram_service,
    )
    if MY_AGENTIC_WALLET and Web3.is_address(MY_AGENTIC_WALLET)
    else None
)


# ==========================================
# AUTO-MINT ENGINE & TELEGRAM POLLER
# ==========================================
async def poll_db_auto_mints():
    logger.info(
        "Reviewed Mint Queue active. Monitoring threshold-qualified and scarce verified-source NFTs..."
    )
    while True:
        if not wallet_ready.is_set():
            conn = None
            try:
                if wallet_login_required.is_set():
                    conn = get_db_connection()
                    conn.execute(
                        """
                        UPDATE mints
                        SET error_log = ?
                        WHERE status = 'pending'
                          AND error_log IS NULL
                          AND (
                              quality_score >= ?
                              OR (quality_score = ? AND scarcity_source_override = 1)
                          )
                        """,
                        (
                            WALLET_NOT_CONNECTED_ERROR,
                            AUTO_MINT_THRESHOLD,
                            AUTO_MINT_THRESHOLD - 1,
                        ),
                    )
                    conn.commit()
            except Exception as e:
                logger.error(f"Failed to record wallet sign-in requirement for queued mints: {e}")
            finally:
                if conn is not None:
                    conn.close()
            await asyncio.sleep(3)
            continue

        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, contract_address, function_name, quality_score
                FROM mints
                WHERE status = 'pending'
                  AND (
                      quality_score >= ?
                      OR (quality_score = ? AND scarcity_source_override = 1)
                  )
                ORDER BY id ASC LIMIT 5
            """, (AUTO_MINT_THRESHOLD, AUTO_MINT_THRESHOLD - 1))
            rows = cursor.fetchall()

            for row in rows:
                mint_id = row["id"]
                contract = row["contract_address"]
                func_name = row["function_name"]
                score = row["quality_score"]
                logger.info(f"Auto-minting Target ID #{mint_id} (Score: {score}/7)")
                cursor.execute(
                    "UPDATE mints SET status = 'processing', error_log = NULL WHERE id = ?",
                    (mint_id,),
                )
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
    global manual_nft_scan_requested
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

                        if cb_data.startswith("mint:") or cb_data.startswith("direct:"):
                            await telegram_nft_features.handle_callback(cq)

                        elif cb_data.startswith("auth:regen"):
                            await telegram_service.answer_callback(cq_id, "🔄 Generating fresh QR code...")
                            try:
                                signin = await run_baw(["auth", "signin", "--json"])
                                qr_url = signin.get("urlForWeb")
                                qr_code_id = signin.get("qrCodeId")
                                pairing_code = signin.get("pairingCode")
                                if qr_url:
                                    qr_bytes = generate_qr_bytes(qr_url)
                                    markup = {
                                        "inline_keyboard": [[
                                            {"text": "🔄 Regenerate QR Code", "callback_data": f"auth:regen:{qr_code_id}"}
                                        ]]
                                    }
                                    caption = (
                                        f"🔐 <b>Regenerated Binance Sign-In QR Code</b>\n\n"
                                        f"🔗 <b>Sign-In Link:</b> <a href='{qr_url}'>{qr_url}</a>\n\n"
                                        f"🔢 <b>Pairing Code:</b> <code>{pairing_code}</code>\n"
                                        f"⏱️ <i>Expires in 5 minutes. Tap below to regenerate if needed.</i>"
                                    )
                                    await telegram_service.send_photo(qr_bytes, caption, reply_markup=markup)
                            except Exception as ex:
                                logger.error(f"Failed to regenerate QR code: {ex}")
                    elif "message" in update:
                        message = update["message"]
                        text = message.get("text", "")
                        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""
                        if command == "/scannft":
                            if str((message.get("chat") or {}).get("id")) != str(TELEGRAM_CHAT_ID):
                                continue
                            if nft_chain_monitor is None:
                                await telegram_service.send_message(
                                    "❌ NFT monitor is disabled. Configure a valid MY_AGENTIC_WALLET address first."
                                )
                                continue
                            if manual_nft_scan_lock.locked() or manual_nft_scan_requested:
                                await telegram_service.send_message(
                                    "⏳ An NFT scan is already running; please wait for its report."
                                )
                                continue
                            manual_nft_scan_requested = True
                            spawn(run_manual_nft_scan())
                        else:
                            await telegram_nft_features.handle_message(message)
        except Exception as e:
            logger.error(f"Error in Telegram Polling: {e}")
            await asyncio.sleep(5)


manual_nft_scan_lock = asyncio.Lock()
manual_nft_scan_requested = False


async def run_manual_nft_scan() -> None:
    global manual_nft_scan_requested
    if nft_chain_monitor is None:
        manual_nft_scan_requested = False
        return
    try:
        async with manual_nft_scan_lock:
            await telegram_service.send_message(
                "🔎 Starting an on-chain NFT scan. The first run may take longer while it indexes the wallet's history."
            )
            scan = await nft_chain_monitor.scan_now()
            await nft_chain_monitor.send_scan_report(scan)
    except Exception as error:
        logger.exception("Manual NFT scan failed.")
        await telegram_service.send_message(
            "❌ NFT scan failed. Check the worker logs for details.\n"
            f"<code>{html.escape(str(error))}</code>"
        )
    finally:
        manual_nft_scan_requested = False


async def main():
    logger.info("Starting Binance Agentic Wallet mint review worker.")
    init_db()
    clear_wallet_login_markers()

    async def connect_wallet():
        while not wallet_ready.is_set():
            try:
                await ensure_wallet_connected()
                wallet_ready.set()
                return
            except WalletCLIError as e:
                logger.error(f"Wallet setup is not ready: {e}")
                await asyncio.sleep(60)

    spawn(connect_wallet())
    spawn(poll_db_auto_mints())
    if nft_chain_monitor is not None:
        spawn(nft_chain_monitor.run())
    else:
        logger.error("On-chain NFT monitor is disabled: MY_AGENTIC_WALLET is not a valid address.")
    await poll_telegram_updates()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Worker stopped gracefully.")
