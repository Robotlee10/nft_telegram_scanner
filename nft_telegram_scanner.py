import os
import io
import re
import html
import json
import asyncio
import logging
import sqlite3
import httpx
from datetime import datetime, timezone
from pathlib import Path
from web3 import AsyncWeb3
from web3.providers import WebSocketProvider, AsyncHTTPProvider

# ==========================================
# 1. LOGGING CONFIGURATION & NOISE MUTE
# ==========================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Mute noisy third-party HTTP clients to stop log flooding
for logger_name in ["httpx", "httpcore", "urllib3", "requests"]:
    logging.getLogger(logger_name).setLevel(logging.WARNING)

# ==========================================
# CONFIGURATION & HIGH-SPEED RPC POOLS
# ==========================================
BSC_WSS_POOL = [
    os.getenv("BSC_WSS_URL", "wss://bsc-mainnet.nodereal.io/ws/v1/6a254628001a4daab023c41cea7e686c"),
    "wss://bnb-mainnet.g.alchemy.com/v2/alch_VKpuhQjbfjUDGyyc0Ntwc",
    "wss://tiniest-necessary-mountain.bsc.quiknode.pro/8d4b8e3bbbe17df68ca7a1bd32f48b67324a538a/",
    "wss://bsc-mainnet.core.chainstack.com/301d34f2b36b95a1a7d0632c072cbb0b",
    "wss://rpc-bsc.blockmachine.io",
    "wss://bnb-chain.wss.run"
]

BSC_HTTP_URL = os.getenv("BSC_HTTP_URL", "https://bsc-dataseed1.binance.org/")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")

MY_AGENTIC_WALLET = os.getenv("MY_AGENTIC_WALLET", "0xYourBinanceAgenticWalletAddress")
CONTROL_WALLET_NO_ID = os.getenv("CONTROL_WALLET_NO_ID", "0x0000000000000000000000000000000000000001")

# Standard On-Chain Topics
ERC721_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDRESS_TOPIC = "0x0000000000000000000000000000000000000000000000000000000000000000"

MINT_FUNCTIONS = [
    {"name": "mint(1)", "data": "a0712d680000000000000000000000000000000000000000000000000000000000000001"},
    {"name": "freeMint()", "data": "0x438d1d8f"},
    {"name": "mint(0)", "data": "a0712d680000000000000000000000000000000000000000000000000000000000000000"},
    {"name": "claim()", "data": "0x4e71d92d"}
]

# ABIs for Inspections
ERC165_ABI = [
    {
        "inputs": [{"internalType": "bytes4", "name": "interfaceId", "type": "bytes4"}],
        "name": "supportsInterface",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view", "type": "function"
    }
]

BALANCE_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view", "type": "function"
    }
]

METADATA_ABI = [
    {"inputs": [{"internalType": "uint256", "name": "tokenId", "type": "uint256"}], "name": "tokenURI", "outputs": [{"internalType": "string", "name": "", "type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "contractURI", "outputs": [{"internalType": "string", "name": "", "type": "string"}], "stateMutability": "view", "type": "function"}
]

ERC721_INTERFACE_ID = "0x80ac58cd"
ERC1155_INTERFACE_ID = "0xd9b67a26"
ERC2981_ROYALTY_INTERFACE_ID = "0x2a55205a"

DB_FILE = "agentic_mints.db"

w3_http = AsyncWeb3(AsyncHTTPProvider(BSC_HTTP_URL))

current_wss_index = 0

def get_active_wss_endpoint() -> str:
    return BSC_WSS_POOL[current_wss_index % len(BSC_WSS_POOL)]

def rotate_wss_endpoint() -> str:
    global current_wss_index
    current_wss_index += 1
    next_endpoint = get_active_wss_endpoint()
    logging.info(f"Switched active WebSocket RPC to: {next_endpoint}")
    return next_endpoint

# ==========================================
# LOCAL SQLITE DATABASE ENGINE (SOLE STORAGE)
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notified_contracts (
            address TEXT PRIMARY KEY,
            nft_standard TEXT,
            function_name TEXT,
            tx_hash TEXT,
            minted BOOLEAN DEFAULT 0,
            last_notified_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def get_local_record(address: str) -> dict | None:
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT address, nft_standard, function_name, tx_hash, minted, last_notified_at FROM notified_contracts WHERE address = ?",
            (address.lower(),)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return {
                "address": row[0],
                "nft_standard": row[1],
                "function_name": row[2],
                "tx_hash": row[3],
                "minted": bool(row[4]),
                "last_notified_at": row[5]
            }
    except Exception as e:
        logging.debug(f"SQLite read error for {address}: {e}")
    return None

def save_local_record(address: str, nft_standard: str, function_name: str, tx_hash: str, minted: bool = False):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO notified_contracts (address, nft_standard, function_name, tx_hash, minted, last_notified_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(address) DO UPDATE SET
                last_notified_at = excluded.last_notified_at
        """, (address.lower(), nft_standard, function_name, tx_hash, minted, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        conn.close()
        logging.info(f"Saved contract {address} to local SQLite cache.")
    except Exception as e:
        logging.error(f"SQLite write error for {address}: {e}")

# ==========================================
# ON-CHAIN CHECKS MODULE
# ==========================================
async def verify_is_nft_contract(contract_address: str) -> tuple[bool, str]:
    """Ensures contract is strictly an ERC-721 or ERC-1155 standard NFT."""
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

async def already_owns_nft(contract_address: str) -> bool:
    """Checks on-chain if MY_AGENTIC_WALLET already holds balance in verified NFT contract."""
    try:
        checksum_target = w3_http.to_checksum_address(contract_address)
        checksum_wallet = w3_http.to_checksum_address(MY_AGENTIC_WALLET)
        
        contract = w3_http.eth.contract(address=checksum_target, abi=BALANCE_ABI)
        balance = await contract.functions.balanceOf(checksum_wallet).call()
        
        if 0 < balance < 1000000:
            logging.info(f"Skipped {contract_address}: Wallet already holds {balance} NFT(s).")
            return True
    except Exception as e:
        logging.debug(f"balanceOf check skipped for {contract_address}: {e}")
        return False
        
    return False

async def simulate_exclusive_agentic_mint(contract_address: str) -> tuple[bool, str, str]:
    checksum_target = w3_http.to_checksum_address(contract_address)
    checksum_agentic = w3_http.to_checksum_address(MY_AGENTIC_WALLET)
    checksum_control = w3_http.to_checksum_address(CONTROL_WALLET_NO_ID)

    for fn in MINT_FUNCTIONS:
        agentic_success = False
        try:
            tx_agentic = {"from": checksum_agentic, "to": checksum_target, "value": 0, "data": fn["data"]}
            await w3_http.eth.call(tx_agentic)
            agentic_success = True
        except Exception:
            agentic_success = False

        if not agentic_success:
            continue

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

async def run_quality_checks(http_client: httpx.AsyncClient, contract_address: str) -> dict:
    checksum_target = w3_http.to_checksum_address(contract_address)
    passed_count = 0
    results = {}

    # CHECK 1: Secondary Market Liquidity (Element Market API)
    try:
        url = f"https://api.element.market/openapi/v1/contract/stat?contract_address={contract_address}"
        res = await http_client.get(url, timeout=5.0, follow_redirects=True)
        if res.status_code == 200:
            data = res.json().get("data", {})
            has_volume = float(data.get("volume", 0)) > 0 or float(data.get("floor_price", 0)) > 0
            if has_volume:
                results["1_market"] = "✅ Active Bids / Volume Found"
                passed_count += 1
            else:
                results["1_market"] = "⚠️ No Market Volume Yet"
        else:
            results["1_market"] = "⚠️ Market API Pending"
    except Exception:
        results["1_market"] = "⚠️ Market Unindexed"

    # CHECK 2: Metadata & Asset Integrity (tokenURI)
    try:
        contract = w3_http.eth.contract(address=checksum_target, abi=METADATA_ABI)
        uri = ""
        try:
            uri = await contract.functions.tokenURI(1).call()
        except Exception:
            try:
                uri = await contract.functions.contractURI().call()
            except Exception:
                pass

        if uri and ("ipfs://" in uri or "ar://" in uri or "http" in uri):
            results["2_metadata"] = "✅ Valid IPFS/Arweave Metadata"
            passed_count += 1
        else:
            results["2_metadata"] = "❌ Blank / Non-Standard Metadata"
    except Exception:
        results["2_metadata"] = "❌ Metadata Unreachable"

    # CHECK 3: Source Code Verification (Etherscan V2 API for BSC ChainID 56)
    try:
        etherscan_api_key = os.getenv("ETHERSCAN_API_KEY", "YOUR_API_KEY")
        url = f"https://api.etherscan.io/v2/api?chainid=56&module=contract&action=getsourcecode&address={contract_address}&apikey={etherscan_api_key}"
        res = await http_client.get(url, timeout=5.0, follow_redirects=True)
        if res.status_code == 200:
            source_data = res.json().get("result", [{}])[0]
            if source_data.get("SourceCode"):
                results["3_source"] = "✅ Verified Source Code"
                passed_count += 1
            else:
                results["3_source"] = "❌ Unverified Source Code"
        else:
            results["3_source"] = f"⚠️ Unverified ({res.status_code})"
    except Exception:
        results["3_source"] = "⚠️ Source Check Error"

    # CHECK 4: Unique Minter Velocity (Anti-Bot Farm Check)
    try:
        logs = await w3_http.eth.get_logs({
            "address": checksum_target,
            "topics": [ERC721_TRANSFER_TOPIC, ZERO_ADDRESS_TOPIC],
            "fromBlock": "latest"
        })
        unique_minters = set(log["topics"][2].hex() for log in logs if len(log["topics"]) > 2)
        if len(unique_minters) >= 2 or len(logs) < 3:
            results["4_diversity"] = f"✅ Organic Demand ({len(unique_minters)} minters)"
            passed_count += 1
        else:
            results["4_diversity"] = "❌ Bot Farm Concentration"
    except Exception:
        results["4_diversity"] = "✅ Single Mint Event"
        passed_count += 1

    # CHECK 5: ERC-2981 Royalty Standard Support
    try:
        contract_165 = w3_http.eth.contract(address=checksum_target, abi=ERC165_ABI)
        has_royalty = await contract_165.functions.supportsInterface(bytes.fromhex(ERC2981_ROYALTY_INTERFACE_ID[2:])).call()
        if has_royalty:
            results["5_royalty"] = "✅ ERC-2981 Royalty Enabled"
            passed_count += 1
        else:
            results["5_royalty"] = "❌ No ERC-2981 Standard"
    except Exception:
        results["5_royalty"] = "❌ Royalty Check Reverted"

    return {
        "score_str": f"{passed_count}/5 Passed",
        "passed_count": passed_count,
        "details": results
    }

# ==========================================
# ALERT SENDER WITH RE-NOTIFICATION BANNER
# ==========================================
async def send_telegram_alert(http_client: httpx.AsyncClient, contract: str, nft_standard: str, func_name: str, tx_hash: str, quality_report: dict, is_reminder: bool = False):
    agent_command = f"Access my Binance Agentic Wallet. Call the write function {func_name} on BNB Chain at contract address {contract} with value 0 BNB. execute it, if it preview clearly and would return a ERC-721 token"

    details = quality_report["details"]
    quality_score = quality_report["score_str"]

    header_title = "⏰ <b>1-HOUR REMINDER: UNMINTED EXCLUSIVE NFT</b> ⏰" if is_reminder else "🚨 <b>EXCLUSIVE AGENTIC FREE NFT DETECTED</b> 🚨"

    message = (
        f"{header_title}\n\n"
        f"📄 <b>Contract Address:</b> <code>{contract}</code>\n"
        f"🎨 <b>NFT Standard:</b> ✅ {nft_standard}\n"
        f"🛡️ <b>Access Gate:</b> 🔒 Agentic ID Holders Only\n"
        f"⚙️ <b>Function:</b> <code>{func_name}</code>\n\n"
        f"📊 <b>Quality Score:</b> <b>{quality_score}</b>\n"
        f"1️⃣ <b>Market Bids:</b> {details.get('1_market')}\n"
        f"2️⃣ <b>Metadata Integrity:</b> {details.get('2_metadata')}\n"
        f"3️⃣ <b>Source Code:</b> {details.get('3_source')}\n"
        f"4️⃣ <b>Minter Diversity:</b> {details.get('4_diversity')}\n"
        f"5️⃣ <b>ERC-2981 Royalty:</b> {details.get('5_royalty')}\n\n"
        f"🔗 <b>BscScan:</b> https://bscscan.com/address/{contract}\n"
        f"⚡ <b>Detected TX:</b> https://bscscan.com/tx/{tx_hash}\n\n"
        f"🤖 <b>Direct Agent Command:</b>\n"
        f"<pre><code>{agent_command}</code></pre>"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        res = await http_client.post(url, json=payload, timeout=10.0)
        if res.status_code == 200:
            logging.info(f"Alert sent for contract: {contract} (Reminder: {is_reminder})")
    except Exception as e:
        logging.error(f"Failed sending alert: {e}")

# ==========================================
# PIPELINE WITH LOCAL CACHE & QUALITY FILTER FOR REMINDERS
# ==========================================
ACTIVE_PROCESSING = set()

async def process_contract_candidate(http_client: httpx.AsyncClient, contract_address: str, tx_hash: str):
    addr = contract_address.lower()

    # Prevent concurrent duplicate processing for the same contract in flight
    if addr in ACTIVE_PROCESSING:
        return
    
    ACTIVE_PROCESSING.add(addr)
    try:
        # 1. Fetch record from local SQLite DB
        record = get_local_record(addr)

        if record:
            if record["minted"] is True:
                return

            last_notified_str = record["last_notified_at"]
            if last_notified_str:
                try:
                    last_dt = datetime.fromisoformat(last_notified_str.replace("Z", "+00:00"))
                    now_dt = datetime.now(timezone.utc)
                    elapsed_seconds = (now_dt - last_dt).total_seconds()

                    # Ignore candidate if notified less than 1 hour ago
                    if elapsed_seconds < 3600:
                        return
                except Exception as e:
                    logging.debug(f"Timestamp parse error: {e}")
                    return

            # Passed 1 hour without being minted -> Run quality checks again
            quality_report = await run_quality_checks(http_client, addr)
            
            # Update last notified timestamp locally to reset the 1-hour timer
            save_local_record(addr, record["nft_standard"], record["function_name"], tx_hash, minted=False)

            # ONLY send the reminder if it passes 3 or more quality checks (>= 3/5)
            if quality_report["passed_count"] >= 3:
                await send_telegram_alert(
                    http_client,
                    contract=addr,
                    nft_standard=record["nft_standard"],
                    func_name=record["function_name"],
                    tx_hash=tx_hash,
                    quality_report=quality_report,
                    is_reminder=True
                )
            else:
                logging.info(f"Skipped 1-hour reminder for {addr}: Quality score dropped ({quality_report['score_str']})")
            return

        # 2. Candidate is NEW -> Save placeholder immediately to prevent concurrent duplicates
        save_local_record(addr, "ERC-721", "mint(1)", tx_hash, minted=False)

        # Run Full Inspection Pipeline
        is_nft, nft_standard = await verify_is_nft_contract(addr)
        if not is_nft:
            return

        if await already_owns_nft(addr):
            return

        is_exclusive, func_name, reason = await simulate_exclusive_agentic_mint(addr)
        if not is_exclusive:
            return

        # Candidate Passed Entry Rules -> Run Quality Checks & Save
        quality_report = await run_quality_checks(http_client, addr)

        save_local_record(addr, nft_standard, func_name, tx_hash, minted=False)
        
        # Optional: You can also restrict initial alerts to >= 3/5 here if desired, 
        # otherwise it alerts on initial detection and filters reminders.
        await send_telegram_alert(http_client, addr, nft_standard, func_name, tx_hash, quality_report, is_reminder=False)

    finally:
        ACTIVE_PROCESSING.remove(addr)
        

# ==========================================
# WEBSOCKET EVENT LISTENER
# ==========================================
async def listen_onchain(wss_url: str):
    init_db()
    logging.info(f"Connecting to BSC WebSocket RPC: {wss_url}")

    ws_provider = WebSocketProvider(
        wss_url,
        websocket_kwargs={
            "ping_interval": 20,
            "ping_timeout": 20,
            "close_timeout": 10
        }
    )

    async with httpx.AsyncClient(follow_redirects=True) as http_client:
        async with AsyncWeb3(ws_provider) as w3_ws:
            logging.info(f"Connected to {wss_url}. Streaming mint logs...")

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

                    if contract_address:
                        asyncio.create_task(process_contract_candidate(http_client, contract_address, tx_hash))

                except Exception as e:
                    logging.error(f"Subscription event error: {e}")

# ==========================================
# MAIN LOOP WITH AUTOMATIC FAILOVER
# ==========================================
async def main():
    init_db()
    while True:
        current_endpoint = get_active_wss_endpoint()
        try:
            await listen_onchain(current_endpoint)
        except Exception as e:
            logging.warning(f"WebSocket endpoint '{current_endpoint}' failed ({e}).")
            next_endpoint = rotate_wss_endpoint()
            await asyncio.sleep(3)

if __name__ == "__main__":
    asyncio.run(main())
