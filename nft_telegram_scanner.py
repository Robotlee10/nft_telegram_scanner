import os
import asyncio
import base64
import html
import ipaddress
import json
import logging
import socket
import sqlite3
import httpx
import websockets
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from eth_typing import ChecksumAddress
from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider
from web3.types import BlockIdentifier, FilterParams, TxParams, Wei
from hexbytes import HexBytes
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().with_name(".env"))

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
BSC_WSS_URL = os.getenv("BSC_WSS_URL")
BSC_HTTP_URL = os.getenv("BSC_HTTP_URL")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")

MY_AGENTIC_WALLET = os.getenv("MY_AGENTIC_WALLET", "0xYourBinanceAgenticWalletAddress")
CONTROL_WALLET_NO_ID = os.getenv("CONTROL_WALLET_NO_ID", "0x0000000000000000000000000000000000000001")
_FREE_MINT_ONLY_VALUE = os.getenv("FREE_MINT_ONLY", "true").strip().lower()
if _FREE_MINT_ONLY_VALUE not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
    raise ValueError("FREE_MINT_ONLY must be a boolean value.")
FREE_MINT_ONLY = _FREE_MINT_ONLY_VALUE in {"1", "true", "yes", "on"}
try:
    MAX_MINT_COST_BNB = Decimal(os.getenv("MAX_MINT_COST_BNB", "0.01"))
except InvalidOperation as error:
    raise ValueError("MAX_MINT_COST_BNB must be a non-negative number.") from error
if not MAX_MINT_COST_BNB.is_finite() or MAX_MINT_COST_BNB < 0:
    raise ValueError("MAX_MINT_COST_BNB must be a non-negative number.")
MAX_MINT_COST_WEI = int(MAX_MINT_COST_BNB * Decimal(10**18))

# Standard On-Chain Topics
ERC721_TRANSFER_TOPIC = HexBytes("0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")
ZERO_ADDRESS_TOPIC = HexBytes("0x0000000000000000000000000000000000000000000000000000000000000000")
ERC1155_TRANSFER_SINGLE_TOPIC = HexBytes("0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62")
ERC1155_TRANSFER_BATCH_TOPIC = HexBytes("0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb")

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

SUPPLY_ABI = [
    {"inputs": [], "name": "maxSupply", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "MAX_SUPPLY", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "totalSupplyCap", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
]

MINT_STATE_ABI = [
    {"inputs": [], "name": name, "outputs": [{"type": output_type}],
     "stateMutability": "view", "type": "function"}
    for name, output_type in (
        ("paused", "bool"),
        ("isPaused", "bool"),
        ("saleActive", "bool"),
        ("saleIsActive", "bool"),
        ("isSaleActive", "bool"),
        ("mintingActive", "bool"),
        ("publicSaleActive", "bool"),
        ("publicSaleIsActive", "bool"),
        ("presaleActive", "bool"),
        ("mintEnabled", "bool"),
        ("totalSupply", "uint256"),
        ("mintPrice", "uint256"),
        ("price", "uint256"),
        ("cost", "uint256"),
    )
]

ERC721_INTERFACE_ID = "0x80ac58cd"
ERC1155_INTERFACE_ID = "0xd9b67a26"
ERC2981_ROYALTY_INTERFACE_ID = "0x2a55205a"

DB_FILE = Path(__file__).resolve().with_name("agentic_mints.db")
MAX_QUALITY_CHECKS = 7
try:
    AUTO_MINT_THRESHOLD = int(os.getenv("AUTO_MINT_THRESHOLD", "6"))
except ValueError as error:
    raise ValueError("AUTO_MINT_THRESHOLD must be an integer from 0 to 7.") from error
if not 0 <= AUTO_MINT_THRESHOLD <= MAX_QUALITY_CHECKS:
    raise ValueError("AUTO_MINT_THRESHOLD must be an integer from 0 to 7.")
WALLET_NOT_CONNECTED_ERROR = "Binance Agentic Wallet is not connected."
MAX_METADATA_BYTES = 512 * 1024
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

w3_http = AsyncWeb3(AsyncHTTPProvider(BSC_HTTP_URL))

async def get_mint_logs(
    address: ChecksumAddress,
    from_block: BlockIdentifier | None = None,
    to_block: BlockIdentifier | None = None,
) -> list:
    event_topics = (
        [ERC721_TRANSFER_TOPIC, ZERO_ADDRESS_TOPIC],
        [ERC1155_TRANSFER_SINGLE_TOPIC, None, ZERO_ADDRESS_TOPIC],
        [ERC1155_TRANSFER_BATCH_TOPIC, None, ZERO_ADDRESS_TOPIC],
    )
    logs = []
    for topics in event_topics:
        filter_params: FilterParams = {"topics": topics}
        filter_params["address"] = address
        if from_block is not None:
            filter_params["fromBlock"] = from_block
        if to_block is not None:
            filter_params["toBlock"] = to_block
        logs.extend(await w3_http.eth.get_logs(filter_params))
    return logs


def get_mint_recipient(log) -> str | None:
    event_topic = log["topics"][0].hex().lower()
    recipient_index = 2 if event_topic == ERC721_TRANSFER_TOPIC.hex().lower() else 3
    if len(log["topics"]) <= recipient_index:
        return None
    return "0x" + log["topics"][recipient_index].hex().removeprefix("0x").lower()


def topic_to_rpc_hex(topic: HexBytes) -> str:
    return "0x" + topic.hex().removeprefix("0x")


# ==========================================
# LOCAL SQLITE DATABASE ENGINE (SOLE STORAGE)
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_FILE, timeout=15.0)
    conn.execute("BEGIN IMMEDIATE")
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
            safety_approved INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            executed_at TIMESTAMP
        )
    """)
    columns = {row[1] for row in cursor.execute("PRAGMA table_info(mints)").fetchall()}
    if "scarcity_source_override" not in columns:
        cursor.execute(
            "ALTER TABLE mints ADD COLUMN scarcity_source_override INTEGER NOT NULL DEFAULT 0"
        )
    if "safety_approved" not in columns:
        cursor.execute(
            "ALTER TABLE mints ADD COLUMN safety_approved INTEGER NOT NULL DEFAULT 0"
        )
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
                nft_standard = excluded.nft_standard,
                function_name = excluded.function_name,
                tx_hash = excluded.tx_hash,
                minted = excluded.minted,
                last_notified_at = excluded.last_notified_at
        """, (address.lower(), nft_standard, function_name, tx_hash, minted, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        conn.close()
        logging.info(f"Saved contract {address} to local SQLite cache.")
    except Exception as e:
        logging.error(f"SQLite write error for {address}: {e}")


def is_mint_cost_allowed(value_wei: int) -> bool:
    if not isinstance(value_wei, int) or isinstance(value_wei, bool) or value_wei < 0:
        return False
    if FREE_MINT_ONLY:
        return value_wei == 0
    return value_wei <= MAX_MINT_COST_WEI


def mint_cost_policy_description() -> str:
    if FREE_MINT_ONLY:
        return "free mints only"
    return f"up to {MAX_MINT_COST_BNB.normalize():g} BNB"


def is_auto_mint_eligible(quality_report: dict) -> bool:
    required_checks = quality_report.get("required_checks")
    return bool(
        quality_report.get("auto_mint_safe") is True
        and quality_report.get("passed_count", 0) >= AUTO_MINT_THRESHOLD
        and isinstance(required_checks, dict)
        and required_checks
        and all(value is True for value in required_checks.values())
    )


def add_required_mint_checks(
    quality_report: dict,
    *,
    ownership_verified: bool,
    access_gate_verified: bool,
    live_conditions_safe: bool,
    mint_cost_allowed: bool,
) -> dict:
    required_checks = {
        "wallet_ownership_verified": ownership_verified,
        "access_gate_verified": access_gate_verified,
        "live_mint_conditions": live_conditions_safe,
        "mint_cost_policy": mint_cost_allowed,
    }
    quality_report["required_checks"] = required_checks
    failed = [name for name, passed in required_checks.items() if not passed]
    quality_report["auto_mint_blockers"].extend(
        f"Required check failed or is unknown: {name}." for name in failed
    )
    quality_report["auto_mint_safe"] = (
        quality_report.get("passed_count", 0) >= AUTO_MINT_THRESHOLD
        and all(required_checks.values())
    )
    return quality_report


def auto_mint_waiting_for_wallet_login(address: str, function_name: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    try:
        row = conn.execute(
            """
            SELECT status, error_log
            FROM mints
            WHERE contract_address = ? AND function_name = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (address.lower(), function_name),
        ).fetchone()
    finally:
        conn.close()

    return bool(
        row
        and row[0] in {"pending", "blocked"}
        and row[1] == WALLET_NOT_CONNECTED_ERROR
    )


def queue_auto_mint(address: str, function_name: str, quality_report: dict) -> str:
    quality_score = quality_report["passed_count"]
    if not is_auto_mint_eligible(quality_report):
        return "not_eligible"

    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        existing = conn.execute(
            """
            SELECT id FROM mints
            WHERE contract_address = ? AND function_name = ?
              AND safety_approved = 1
              AND status IN ('pending', 'processing', 'success')
            LIMIT 1
            """,
            (address.lower(), function_name)
        ).fetchone()
        if existing:
            return "already_exists"

        conn.execute(
            """
            INSERT INTO mints (
                contract_address, function_name, quality_score,
                scarcity_source_override, safety_approved
            )
            VALUES (?, ?, ?, 0, 1)
            """,
            (address.lower(), function_name, quality_score)
        )
        conn.commit()
        logging.info(
            "Queued contract %s for safety-approved mint execution "
            "(quality checks %s/%s).",
            address,
            quality_score,
            AUTO_MINT_THRESHOLD,
        )
        return "queued"
    except sqlite3.Error as e:
        logging.error(f"Failed to queue mint for {address}: {e}")
        return "error"
    finally:
        if conn is not None:
            conn.close()
        

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

async def already_owns_nft(contract_address: str) -> bool | None:
    """Return unknown on an RPC failure instead of treating it as no ownership."""
    try:
        if not MY_AGENTIC_WALLET or not w3_http.is_address(MY_AGENTIC_WALLET):
            return None
        checksum_target = w3_http.to_checksum_address(contract_address)
        checksum_wallet = w3_http.to_checksum_address(MY_AGENTIC_WALLET)

        interface_contract = w3_http.eth.contract(
            address=checksum_target, abi=ERC165_ABI
        )
        supports_erc721 = await interface_contract.functions.supportsInterface(
            bytes.fromhex(ERC721_INTERFACE_ID[2:])
        ).call()
        if not supports_erc721:
            logging.info(
                "Ownership cannot be aggregated for non-ERC721 contract %s; "
                "automatic/manual mint actions remain blocked.",
                contract_address,
            )
            return None

        contract = w3_http.eth.contract(address=checksum_target, abi=BALANCE_ABI)
        balance = await contract.functions.balanceOf(checksum_wallet).call()
        
        if balance > 0:
            logging.info(f"Skipped {contract_address}: Wallet already holds {balance} NFT(s).")
            return True
    except Exception as e:
        logging.warning("Could not verify NFT ownership for %s: %s", contract_address, e)
        return None
        
    return False


def _is_explicit_access_denial(error: Exception) -> bool:
    message = str(error).lower()
    access_phrases = (
        "unauthorized",
        "not authorized",
        "not eligible",
        "not whitelisted",
        "not on whitelist",
        "whitelist required",
        "allowlist required",
        "agent id required",
        "agentid required",
        "only agent",
        "only the owner",
        "access denied",
        "accesscontrol",
    )
    return any(phrase in message for phrase in access_phrases)


async def inspect_mint_access(
    contract_address: str, mint_value_wei: int = 0
) -> tuple[str | None, bool, str]:
    checksum_target = w3_http.to_checksum_address(contract_address)
    checksum_agentic = w3_http.to_checksum_address(MY_AGENTIC_WALLET)
    checksum_control = w3_http.to_checksum_address(CONTROL_WALLET_NO_ID)
    public_function = None

    for fn in MINT_FUNCTIONS:
        try:
            tx_agentic: TxParams = {
                "from": checksum_agentic,
                "to": checksum_target,
                "value": Wei(mint_value_wei),
                "data": HexBytes(fn["data"]),
            }
            await w3_http.eth.call(tx_agentic)
        except Exception as error:
            logging.debug(
                "Mint simulation for %s by configured wallet failed for %s: %s",
                contract_address,
                fn["name"],
                error,
            )
            continue

        try:
            tx_control: TxParams = {
                "from": checksum_control,
                "to": checksum_target,
                "value": Wei(mint_value_wei),
                "data": HexBytes(fn["data"]),
            }
            await w3_http.eth.call(tx_control)
        except Exception as error:
            if _is_explicit_access_denial(error):
                return (
                    fn["name"],
                    True,
                    "Configured wallet passed; control wallet received an explicit access denial.",
                )
            return (
                fn["name"],
                False,
                "Control-wallet simulation failed for an unclassified reason; access gate is unknown.",
            )

        if public_function is None:
            public_function = fn["name"]

    if public_function:
        return public_function, False, "Supported mint function is callable by the control wallet."
    return None, False, "No supported mint function passed simulation."


async def inspect_mint_conditions(
    contract_address: str, expected_value_wei: int = 0
) -> dict:
    """Inspect common live sale, pause, supply, and price getters when exposed."""
    checksum_target = w3_http.to_checksum_address(contract_address)
    abi = MINT_STATE_ABI + SUPPLY_ABI
    contract = w3_http.eth.contract(address=checksum_target, abi=abi)
    values: dict[str, object] = {}
    blockers: list[str] = []
    aliases = {
        "saleActive": (
            "saleActive",
            "saleIsActive",
            "isSaleActive",
            "mintingActive",
            "publicSaleActive",
            "publicSaleIsActive",
            "presaleActive",
            "mintEnabled",
        ),
        "supplyCap": ("maxSupply", "MAX_SUPPLY", "totalSupplyCap"),
        "mintPriceWei": ("mintPrice", "price", "cost"),
    }

    queried = set()
    for label, names in (
        ("paused", ("paused", "isPaused")),
        *aliases.items(),
        ("totalSupply", ("totalSupply",)),
    ):
        for name in names:
            try:
                value = await contract.get_function_by_name(name)().call()
            except Exception:
                continue
            values[label] = value
            queried.add(label)
            if label == "paused" and value is True:
                blockers.append("Mint is paused.")
            elif label == "saleActive" and value is False:
                blockers.append("A sale-active getter reports that minting is closed.")
            elif label == "mintPriceWei" and isinstance(value, int):
                if not is_mint_cost_allowed(value):
                    blockers.append(
                        f"Reported mint price {value} wei exceeds the configured cost policy."
                    )
                if value != expected_value_wei:
                    blockers.append(
                        f"Reported mint price {value} wei does not match the transaction value."
                    )
            break
        if label not in queried:
            values[label] = None

    cap = values.get("supplyCap")
    current_supply = values.get("totalSupply")
    if isinstance(cap, int) and isinstance(current_supply, int) and cap > 0:
        if current_supply >= cap:
            blockers.append(f"Mint is sold out ({current_supply}/{cap}).")

    return {
        "values": values,
        "unknown_checks": [name for name, value in values.items() if value is None],
        "blockers": blockers,
        "safe": not blockers,
        "cost_policy": mint_cost_policy_description(),
    }


async def simulate_exclusive_agentic_mint(contract_address: str) -> tuple[bool, str, str]:
    function_name, is_exclusive, reason = await inspect_mint_access(contract_address)
    return is_exclusive, function_name if is_exclusive and function_name else "None", reason


async def _safe_metadata_url(uri: str) -> str:
    if not uri or (
        not uri.startswith("data:") and len(uri) > 4096
    ):
        raise ValueError("Metadata URI is empty or exceeds the 4096-character limit.")
    if uri.startswith("ipfs://"):
        path = uri[len("ipfs://"):].lstrip("/")
        if path.startswith("ipfs/"):
            path = path[len("ipfs/"):]
        url = f"https://ipfs.io/ipfs/{path}"
    elif uri.startswith("ar://"):
        url = f"https://arweave.net/{uri[len('ar://'):].lstrip('/')}"
    elif uri.startswith("https://"):
        url = uri
    elif uri.startswith("data:application/json;base64,"):
        return uri
    else:
        raise ValueError("Metadata URI must use HTTPS, IPFS, Arweave, or JSON data URI.")

    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Metadata URL is not a safe HTTPS URL.")

    try:
        literal_address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal_address = None
    if literal_address is not None and not literal_address.is_global:
        raise ValueError("Metadata URL points to a non-public IP address.")

    if literal_address is None:
        try:
            addresses = await asyncio.to_thread(
                socket.getaddrinfo,
                parsed.hostname,
                parsed.port or 443,
                0,
                socket.SOCK_STREAM,
            )
        except OSError as error:
            raise ValueError("Metadata hostname could not be resolved.") from error
        resolved = {item[4][0] for item in addresses}
        if not resolved or any(not ipaddress.ip_address(item).is_global for item in resolved):
            raise ValueError("Metadata hostname resolves to a non-public IP address.")
    return urlunsplit(parsed)


async def _fetch_metadata_json(
    http_client: httpx.AsyncClient, uri: str
) -> dict:
    url = await _safe_metadata_url(uri)
    if url.startswith("data:"):
        encoded = url.split(",", 1)[1]
        if len(encoded) > MAX_METADATA_BYTES * 4 // 3:
            raise ValueError("Metadata document exceeds the 512 KiB size limit.")
        content = base64.b64decode(encoded, validate=True)
    else:
        async with http_client.stream(
            "GET",
            url,
            follow_redirects=False,
            timeout=httpx.Timeout(6.0),
        ) as response:
            if response.is_redirect:
                raise ValueError("Metadata URL redirects are not followed.")
            response.raise_for_status()
            content_type = response.headers.get(
                "content-type", ""
            ).split(";", 1)[0].strip().lower()
            if not (
                content_type == "application/json"
                or content_type == "application/ld+json"
                or (
                    content_type.startswith("application/")
                    and content_type.endswith("+json")
                )
            ):
                raise ValueError("Metadata response has a non-JSON content type.")
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=1024):
                body.extend(chunk)
                if len(body) > MAX_METADATA_BYTES:
                    raise ValueError("Metadata document exceeds the 512 KiB size limit.")
            content = bytes(body)
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Metadata response is not valid JSON.") from error
    if not isinstance(document, dict):
        raise ValueError("Metadata JSON must be an object.")
    return document


def _metadata_asset_uris(document: dict) -> list[str]:
    candidates = [
        document.get("image"),
        document.get("image_url"),
        document.get("animation_url"),
    ]
    properties = document.get("properties")
    if isinstance(properties, dict):
        files = properties.get("files")
        if isinstance(files, list):
            candidates.extend(
                item.get("uri")
                for item in files
                if isinstance(item, dict)
            )
    return [
        value for value in candidates
        if isinstance(value, str)
        and urlsplit(value).scheme.lower() in {"https", "ipfs", "ar", "data"}
    ]


async def _check_asset_available(
    http_client: httpx.AsyncClient, uri: str
) -> bool:
    if uri.startswith("data:"):
        header, separator, encoded = uri.partition(",")
        if (
            not separator
            or not header.lower().startswith("data:image/")
            or ";base64" not in header.lower()
            or len(encoded) > MAX_METADATA_BYTES * 4 // 3
        ):
            return False
        try:
            return bool(base64.b64decode(encoded, validate=True))
        except ValueError:
            return False
    url = await _safe_metadata_url(uri)
    async with http_client.stream(
        "GET",
        url,
        headers={"Range": "bytes=0-1023"},
        follow_redirects=False,
        timeout=httpx.Timeout(6.0),
    ) as response:
        if response.is_redirect:
            return False
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if not (
            content_type.startswith("image/")
            or content_type.startswith("video/")
            or content_type == "application/octet-stream"
        ):
            return False
        received = 0
        async for chunk in response.aiter_bytes(chunk_size=1024):
            received += len(chunk)
            if received >= 1024:
                break
        return received > 0


async def _inspect_metadata(
    http_client: httpx.AsyncClient, contract_address: str
) -> tuple[bool, str]:
    contract = w3_http.eth.contract(
        address=w3_http.to_checksum_address(contract_address), abi=METADATA_ABI
    )
    uri_candidates = []
    try:
        uri_candidates.append(await contract.functions.tokenURI(1).call())
    except Exception as error:
        logging.debug("tokenURI(1) unavailable for %s: %s", contract_address, error)
    try:
        uri_candidates.append(await contract.functions.contractURI().call())
    except Exception as error:
        logging.debug("contractURI unavailable for %s: %s", contract_address, error)

    errors = []
    for uri in uri_candidates:
        if not isinstance(uri, str) or not uri:
            continue
        resolved_uri = uri.replace("{id}", f"{1:064x}")
        try:
            document = await _fetch_metadata_json(http_client, resolved_uri)
        except (ValueError, httpx.HTTPError) as error:
            errors.append(str(error))
            continue

        text = " ".join(
            str(document.get(key, ""))
            for key in ("name", "description", "image")
        ).lower()
        if any(word in text for word in ("placeholder", "coming soon", "unrevealed")):
            return False, "Metadata resolves but contains placeholder/reveal content."
        asset_uris = _metadata_asset_uris(document)
        if not asset_uris:
            return False, "Metadata resolves but has no valid image/asset URI."
        try:
            asset_available = await _check_asset_available(http_client, asset_uris[0])
        except (ValueError, httpx.HTTPError) as error:
            return False, f"Metadata asset could not be fetched safely: {error}"
        if not asset_available:
            return False, "Metadata asset is unreachable, redirects, or has an unsafe content type."
        return True, "Metadata JSON resolves and contains a usable asset URI."

    detail = errors[-1] if errors else "No tokenURI(1) or contractURI was available."
    return False, f"Metadata could not be validated: {detail}"


async def _fetch_source_data(
    http_client: httpx.AsyncClient, contract_address: str
) -> dict | None:
    api_key = os.getenv("ETHERSCAN_API_KEY")
    if not api_key:
        return None
    response = await http_client.get(
        "https://api.etherscan.io/v2/api",
        params={
            "chainid": "56",
            "module": "contract",
            "action": "getsourcecode",
            "address": contract_address,
            "apikey": api_key,
        },
        timeout=6.0,
        follow_redirects=False,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    if not isinstance(result, list) or not result or not isinstance(result[0], dict):
        return None
    data = result[0]
    return data if isinstance(data.get("SourceCode"), str) and data["SourceCode"] else None


async def _inspect_contract_risk(
    contract_address: str, source_data: dict | None
) -> dict:
    address = w3_http.to_checksum_address(contract_address)
    try:
        code = bytes(await w3_http.eth.get_code(address))
        if not code:
            return {
                "proxy": None,
                "controls": None,
                "proxy_detail": "Contract bytecode unavailable.",
                "owner_status": "unknown",
                "owner_address": None,
            }

        implementation_slot = int.from_bytes(
            w3_http.keccak(text="eip1967.proxy.implementation"), "big"
        ) - 1
        admin_slot = int.from_bytes(
            w3_http.keccak(text="eip1967.proxy.admin"), "big"
        ) - 1
        beacon_slot = int.from_bytes(
            w3_http.keccak(text="eip1967.proxy.beacon"), "big"
        ) - 1
        slot_values = [
            bytes(await w3_http.eth.get_storage_at(address, slot))
            for slot in (implementation_slot, admin_slot, beacon_slot)
        ]
    except Exception as error:
        logging.warning("Could not inspect proxy slots for %s: %s", contract_address, error)
        return {
            "proxy": None,
            "controls": None,
            "proxy_detail": "Proxy check failed.",
            "owner_status": "unknown",
            "owner_address": None,
        }

    minimal_proxy = (
        len(code) == 45
        and code.startswith(bytes.fromhex("363d3d373d3d3d363d73"))
        and code.endswith(bytes.fromhex("5af43d82803e903d91602b57fd5bf3"))
    )
    implementation = (
        "0x" + slot_values[0][-20:].hex()
        if any(slot_values[0][-20:])
        else ("0x" + code[10:30].hex() if minimal_proxy else None)
    )
    proxy_admin = "0x" + slot_values[1][-20:].hex() if any(slot_values[1][-20:]) else None
    beacon = "0x" + slot_values[2][-20:].hex() if any(slot_values[2][-20:]) else None
    proxy = minimal_proxy or any(any(value[-20:]) for value in slot_values)
    proxy_detail = "No EIP-1967 or minimal proxy detected."
    if proxy:
        proxy_detail = "Upgradeable/minimal proxy detected."
        if implementation:
            proxy_detail += f" Implementation: {implementation}."
        if proxy_admin:
            proxy_detail += f" EIP-1967 admin: {proxy_admin}."
        if beacon:
            proxy_detail += f" EIP-1967 beacon: {beacon}."
    if source_data is None:
        return {
            "proxy": proxy,
            "controls": None,
            "proxy_detail": proxy_detail,
            "implementation": implementation,
            "proxy_admin": proxy_admin,
            "beacon": beacon,
            "owner_status": "unknown",
            "owner_address": None,
        }

    try:
        abi = json.loads(source_data.get("ABI", ""))
    except (TypeError, json.JSONDecodeError):
        abi = None
    if not isinstance(abi, list):
        return {
            "proxy": proxy,
            "controls": None,
            "proxy_detail": proxy_detail,
            "implementation": implementation,
            "proxy_admin": proxy_admin,
            "beacon": beacon,
            "owner_status": "unknown",
            "owner_address": None,
        }

    risky_fragments = (
        "upgrade",
        "implementation",
        "changeadmin",
        "transferownership",
        "setowner",
        "blacklist",
        "denylist",
        "pause",
        "freeze",
        "setbase",
        "settokenuri",
        "setmint",
        "setsale",
        "setprice",
        "setmaxsupply",
        "setwhitelist",
        "setallowlist",
        "setroyalty",
        "grantrole",
        "revokerole",
        "setrole",
    )
    exposed_controls: set[str] = set()
    for item in abi:
        if not isinstance(item, dict) or item.get("type") != "function":
            continue
        name = item.get("name")
        compact_name = (
            name.lower().replace("_", "") if isinstance(name, str) else ""
        )
        if (
            isinstance(name, str)
            and compact_name not in {"paused", "ispaused"}
            and any(fragment in compact_name for fragment in risky_fragments)
        ):
            exposed_controls.add(name)
    owner_status = "not exposed"
    owner_address = None
    has_owner_getter = any(
        isinstance(item, dict)
        and item.get("type") == "function"
        and item.get("name") == "owner"
        and not item.get("inputs")
        for item in abi
    )
    if has_owner_getter:
        owner_contract = w3_http.eth.contract(
            address=address,
            abi=[{
                "inputs": [],
                "name": "owner",
                "outputs": [{"type": "address"}],
                "stateMutability": "view",
                "type": "function",
            }],
        )
        try:
            owner_address = await owner_contract.functions.owner().call()
            owner_status = (
                "renounced"
                if str(owner_address).lower() == ZERO_ADDRESS
                else f"active owner {owner_address}"
            )
        except Exception as error:
            logging.warning("Could not query owner for %s: %s", contract_address, error)
            owner_status = "unknown"
    return {
        "proxy": proxy,
        "controls": sorted(exposed_controls),
        "proxy_detail": proxy_detail,
        "implementation": implementation,
        "proxy_admin": proxy_admin,
        "beacon": beacon,
        "owner_status": owner_status,
        "owner_address": owner_address,
    }


async def run_quality_checks(
    http_client: httpx.AsyncClient, contract_address: str, tx_hash: str | None
) -> dict:
    checksum_target = w3_http.to_checksum_address(contract_address)
    passed_count = 0
    results: dict[str, str] = {}
    source_verified = False
    supply_scarce = False
    core_checks = {}
    blockers = []

    # Metadata is a core safety check; activity is scored but can be manipulated.
    try:
        metadata_ok, metadata_detail = await _inspect_metadata(http_client, checksum_target)
        results["1_metadata"] = f"{'✅' if metadata_ok else '❌'} {metadata_detail}"
        core_checks["metadata"] = metadata_ok
    except Exception as error:
        logging.exception("Metadata safety check failed for %s.", checksum_target)
        results["1_metadata"] = f"❌ Metadata check error: {error}"
        core_checks["metadata"] = False
    if not core_checks["metadata"]:
        blockers.append("Scored check failed: metadata did not pass safe JSON/asset validation.")

    # Verified source enables ABI-based inspection; it is not itself a trust guarantee.
    source_data = None
    try:
        source_data = await _fetch_source_data(http_client, checksum_target)
        source_verified = source_data is not None
    except (httpx.HTTPError, ValueError, json.JSONDecodeError) as error:
        results["2_source"] = f"⚠️ Source check unavailable: {error}"
    if source_verified:
        results["2_source"] = "✅ Verified source available (not a trust guarantee)."
    elif "2_source" not in results:
        results["2_source"] = "❌ Source verification unavailable or unverified."
    core_checks["source"] = source_verified
    if not source_verified:
        blockers.append("Scored check failed: verified source and ABI are unavailable.")

    risk = await _inspect_contract_risk(checksum_target, source_data)
    proxy_safe = risk["proxy"] is False
    controls_safe = (
        risk["controls"] == [] and risk.get("owner_status") != "unknown"
    )
    results["3_proxy"] = (
        f"{'✅' if proxy_safe else '❌'} {risk['proxy_detail']}"
    )
    if risk["proxy"] is None:
        results["3_proxy"] = "⚠️ Proxy status unknown."
    results["4_admin_controls"] = (
        (
            "✅ No recognized high-risk admin controls in verified ABI; "
            f"owner status: {risk.get('owner_status', 'unknown')}."
        )
        if controls_safe
        else (
            f"❌ High-risk controls exposed: {', '.join(risk['controls'])}."
            if isinstance(risk["controls"], list)
            else "⚠️ Admin-control status unknown."
        )
    )
    core_checks["proxy"] = proxy_safe
    core_checks["admin_controls"] = controls_safe
    if not proxy_safe:
        blockers.append("Scored check failed: contract is upgradeable or proxy status is unknown.")
    if not controls_safe:
        blockers.append("Scored check failed: high-risk admin controls are exposed or unknown.")

    activity_check = False
    distribution_check = False
    supply_or_royalties_check = False
    results["5_activity"] = "⚠️ Recent activity check unavailable."
    results["6_distribution"] = "⚠️ Minter diversity check unavailable."
    results["7_supply_and_royalties"] = "⚠️ Supply and royalty checks unavailable."

    # Activity and demand are scored signals, but wash trading can spoof them.
    try:
        latest_block = await w3_http.eth.block_number
        logs = await get_mint_logs(
            checksum_target, max(0, latest_block - 1000), latest_block
        )
        unique_minters = set()
        for log in logs:
            recipient = get_mint_recipient(log)
            if recipient is not None:
                unique_minters.add(recipient)
        activity_check = len(logs) > 0
        distribution_check = len(unique_minters) > 1
        results["5_activity"] = (
            f"{'✅' if activity_check else '❌'} {len(logs)} recent mint events "
            f"from {len(unique_minters)} recipient wallets."
        )
        results["6_distribution"] = (
            f"{'✅' if distribution_check else '❌'} {len(unique_minters)} "
            "distinct recent recipient wallets; this can be manipulated."
        )
    except Exception as error:
        results["5_activity"] = f"ℹ️ Activity data unavailable: {error}"
        results["6_distribution"] = f"ℹ️ Distribution data unavailable: {error}"

    has_royalty = False
    try:
        contract_165 = w3_http.eth.contract(address=checksum_target, abi=ERC165_ABI)
        has_royalty = await contract_165.functions.supportsInterface(bytes.fromhex(ERC2981_ROYALTY_INTERFACE_ID[2:])).call()
        royalty_note = "supports ERC-2981" if has_royalty else "does not report ERC-2981 support"
        results["7_supply_and_royalties"] = (
            f"ℹ️ Contract {royalty_note}; royalties are not a quality guarantee or score."
        )
    except Exception as error:
        results["7_supply_and_royalties"] = f"ℹ️ Royalty data unavailable: {error}"

    try:
        supply_contract = w3_http.eth.contract(address=checksum_target, abi=SUPPLY_ABI)
        max_supply = None
        for fn_name in ["maxSupply", "MAX_SUPPLY", "totalSupplyCap"]:
            try:
                max_supply = await supply_contract.get_function_by_name(fn_name)().call()
                break
            except Exception:
                continue
        if max_supply is not None and 0 < max_supply <= 5555:
            supply_scarce = True
            results["7_supply_and_royalties"] += f" Declared cap: {max_supply}."
        elif max_supply is not None:
            results["7_supply_and_royalties"] += f" Declared cap: {max_supply}."
        else:
            results["7_supply_and_royalties"] += " Supply cap not exposed."
    except Exception as error:
        results["7_supply_and_royalties"] += f" Supply cap unavailable: {error}"

    try:
        if not tx_hash:
            raise ValueError("No triggering transaction was supplied.")
        transaction = await w3_http.eth.get_transaction(HexBytes(tx_hash))
        minter_address = transaction.get("from")
        if not minter_address:
            raise ValueError("Mint transaction has no sender address.")
        minter_nonce = await w3_http.eth.get_transaction_count(minter_address)
        if minter_nonce > 3:
            results["7_supply_and_royalties"] += (
                f" Triggering wallet nonce: {minter_nonce} (informational only)."
            )
        else:
            results["7_supply_and_royalties"] += (
                f" Triggering wallet nonce: {minter_nonce} (informational only)."
            )
    except Exception as error:
        results["7_supply_and_royalties"] += f" Minter history unavailable: {error}"

    supply_or_royalties_check = supply_scarce or has_royalty
    results["7_supply_and_royalties"] = (
        f"{'✅' if supply_or_royalties_check else '❌'} "
        f"{results['7_supply_and_royalties']} "
        "(passes with a declared supply cap of 5,555 or fewer, or ERC-2981 support)."
    )
    score_checks = {
        **core_checks,
        "recent_activity": activity_check,
        "minter_diversity": distribution_check,
        "supply_or_royalties": supply_or_royalties_check,
    }
    passed_count = sum(score_checks.values())

    return {
        "score_str": f"{passed_count}/{MAX_QUALITY_CHECKS} checks",
        "passed_count": passed_count,
        "score_max": MAX_QUALITY_CHECKS,
        "auto_mint_threshold": AUTO_MINT_THRESHOLD,
        "source_verified": source_verified,
        "supply_scarce": supply_scarce,
        "core_checks": core_checks,
        "score_checks": score_checks,
        "auto_mint_safe": passed_count >= AUTO_MINT_THRESHOLD,
        "auto_mint_blockers": blockers,
        "details": results,
    }

# ==========================================
# ALERT SENDER WITH MINT BUTTON SUPPORT
# ==========================================
async def send_telegram_alert(
    http_client: httpx.AsyncClient,
    contract: str,
    nft_standard: str,
    func_name: str,
    tx_hash: str,
    quality_report: dict,
    is_reminder: bool = False,
    manual_mint_available: bool = True,
):

    details = quality_report["details"]
    quality_score = quality_report["score_str"]
    passed_count = quality_report["passed_count"]

    header_title = (
        "⏰ <b>1-HOUR REMINDER: UNMINTED NFT</b> ⏰"
        if is_reminder
        else "🚨 <b>EXCLUSIVE NFT MINT DETECTED</b> 🚨"
    )

    message = (
        f"{header_title}\n\n"
        f"📄 <b>Contract Address:</b> <code>{contract}</code>\n"
        f"🎨 <b>NFT Standard:</b> ✅ {nft_standard}\n"
        f"🛡️ <b>Access Gate:</b> 🔒 Explicitly verified agent-wallet gate\n"
        f"⚙️ <b>Function:</b> <code>{func_name}</code>\n\n"
        f"📊 <b>Quality Score:</b> <b>{quality_score}</b>\n"
        f"1️⃣ <b>Metadata:</b> {html.escape(str(details.get('1_metadata', 'Unknown')))}\n"
        f"2️⃣ <b>Verified source:</b> {html.escape(str(details.get('2_source', 'Unknown')))}\n"
        f"3️⃣ <b>Proxy check:</b> {html.escape(str(details.get('3_proxy', 'Unknown')))}\n"
        f"4️⃣ <b>Admin controls:</b> {html.escape(str(details.get('4_admin_controls', 'Unknown')))}\n"
        f"5️⃣ <b>Recent activity:</b> {html.escape(str(details.get('5_activity', 'Unknown')))}\n"
        f"6️⃣ <b>Minter diversity:</b> {html.escape(str(details.get('6_distribution', 'Unknown')))}\n"
        f"7️⃣ <b>Supply/royalties:</b> {html.escape(str(details.get('7_supply_and_royalties', 'Unknown')))}\n"
        f"🤖 <b>Auto-mint score threshold:</b> {AUTO_MINT_THRESHOLD}/{MAX_QUALITY_CHECKS} "
        "(all required ownership, access-gate, live-condition, and cost checks must also pass)\n"
        f"💰 <b>Mint cost policy:</b> {html.escape(mint_cost_policy_description())}\n\n"
        f"🧪 <b>Live mint getters:</b> "
        f"<code>{html.escape(json.dumps(quality_report.get('mint_conditions', {}).get('values', {}), default=str))}</code>\n"
        f"🔗 <b>BscScan:</b> https://bscscan.com/address/{contract}\n"
        f"⚡ <b>Detected TX:</b> https://bscscan.com/tx/{tx_hash}"
    )
    blockers = quality_report.get("auto_mint_blockers", [])
    if blockers:
        message += "\n\n⚠️ <b>Failed auto-mint checks:</b>\n" + "\n".join(
            f"• {html.escape(str(blocker))}" for blocker in blockers
        )

    reply_markup = None
    if manual_mint_available:
        reply_markup = {
            "inline_keyboard": [[
                {
                    "text": f"🪙 Mint Manually ({passed_count}/{MAX_QUALITY_CHECKS} checks)",
                    "callback_data": f"mint:{contract}:{func_name}",
                }
            ]]
        }

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": reply_markup
    }

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        res = await http_client.post(url, json=payload, timeout=10.0)
        try:
            response_data = res.json()
        except ValueError:
            logging.error(
                "Telegram sendMessage returned HTTP %s with a non-JSON response: %s",
                res.status_code,
                res.text[:300],
            )
            return
        if res.is_success and response_data.get("ok") is True:
            logging.info(f"Alert sent with mint button for contract: {contract} (Reminder: {is_reminder})")
        else:
            logging.error(
                "Telegram sendMessage failed (HTTP %s): %s",
                res.status_code,
                response_data.get("description", "Telegram returned ok=false"),
            )
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
        # Give HTTP provider 1.5 seconds to ensure block indexing/receipt sync is ready
        await asyncio.sleep(1.5)
        
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
                        logging.info("Skipped %s: reminder cooldown has not expired.", addr)
                        return
                except Exception as e:
                    logging.debug(f"Timestamp parse error: {e}")
                    return

            # Passed 1 hour without being minted -> Run quality checks again
            ownership = await already_owns_nft(addr)
            function_name, is_exclusive, access_reason = await inspect_mint_access(addr)
            mint_conditions = (
                await inspect_mint_conditions(addr)
                if function_name and is_exclusive
                else {"safe": False, "blockers": [access_reason]}
            )
            quality_report = await run_quality_checks(http_client, addr, tx_hash)
            quality_report["mint_conditions"] = mint_conditions
            add_required_mint_checks(
                quality_report,
                ownership_verified=ownership is False,
                access_gate_verified=is_exclusive and function_name == record["function_name"],
                live_conditions_safe=bool(mint_conditions["safe"]),
                mint_cost_allowed=is_mint_cost_allowed(0),
            )

            if (
                is_auto_mint_eligible(quality_report)
                and auto_mint_waiting_for_wallet_login(addr, record["function_name"])
            ):
                queue_status = queue_auto_mint(addr, record["function_name"], quality_report)
                save_local_record(addr, record["nft_standard"], record["function_name"], tx_hash, minted=False)
                await send_telegram_alert(
                    http_client,
                    contract=addr,
                    nft_standard=record["nft_standard"],
                    func_name=record["function_name"],
                    tx_hash=tx_hash,
                    quality_report=quality_report,
                    is_reminder=True,
                    manual_mint_available=queue_status not in {"queued", "already_exists"},
                )
            else:
                logging.info(
                    "Skipped 1-hour reminder for %s: automint is not waiting only on wallet sign-in "
                    "or quality requirements are no longer met (%s).",
                    addr,
                    quality_report["score_str"],
                )
            return

        # Run Full Inspection Pipeline
        is_nft, nft_standard = await verify_is_nft_contract(addr)
        if not is_nft:
            logging.info("Ignored mint event from non-NFT contract %s.", addr)
            return

        ownership = await already_owns_nft(addr)
        if ownership is True:
            return
        if ownership is None:
            logging.warning(
                "Skipped automatic mint candidate %s because wallet ownership is unknown.",
                addr,
            )
            return

        is_exclusive, func_name, reason = await simulate_exclusive_agentic_mint(addr)
        if not is_exclusive:
            logging.info("Ignored NFT %s: %s", addr, reason)
            return

        # Candidate Passed Entry Rules -> Run Quality Checks & Save
        quality_report = await run_quality_checks(http_client, addr, tx_hash)
        mint_conditions = await inspect_mint_conditions(addr)
        quality_report["mint_conditions"] = mint_conditions
        add_required_mint_checks(
            quality_report,
            ownership_verified=ownership is False,
            access_gate_verified=is_exclusive and func_name != "None",
            live_conditions_safe=bool(mint_conditions["safe"]),
            mint_cost_allowed=is_mint_cost_allowed(0),
        )

        save_local_record(addr, nft_standard, func_name, tx_hash, minted=False)
        queue_status = queue_auto_mint(addr, func_name, quality_report)
        
        # Notify for ALL valid initial NFT detections with score included
        await send_telegram_alert(
            http_client,
            addr,
            nft_standard,
            func_name,
            tx_hash,
            quality_report,
            is_reminder=False,
            manual_mint_available=queue_status not in {"queued", "already_exists"},
        )
        logging.info("Detected eligible %s NFT %s; Telegram alert attempted.", nft_standard, addr)

    except Exception:
        logging.exception("Failed processing mint candidate %s (tx %s).", addr, tx_hash)
    finally:
        ACTIVE_PROCESSING.remove(addr)


async def process_contract_candidate_logged(
    http_client: httpx.AsyncClient, contract_address: str, tx_hash: str
) -> None:
    try:
        await process_contract_candidate(http_client, contract_address, tx_hash)
    except Exception:
        logging.exception("Unhandled candidate processing error for %s (tx %s).", contract_address, tx_hash)


def queue_candidate_from_notification(http_client: httpx.AsyncClient, message: dict) -> None:
    if message.get("method") != "eth_subscription":
        return
    log = message.get("params", {}).get("result", {})
    contract_address = log.get("address")
    transaction_hash = log.get("transactionHash")
    if not contract_address or not transaction_hash:
        return
    logging.info(
        "Received NFT mint event for %s in block %s.",
        contract_address,
        log.get("blockNumber", "unknown"),
    )
    asyncio.create_task(
        process_contract_candidate_logged(
            http_client,
            contract_address,
            transaction_hash,
        )
    )


# ==========================================
# WEBSOCKET MINT EVENT SCANNER
# ==========================================
async def listen_onchain_websocket():
    init_db()
    logging.info("Starting BSC mint event scanner.")
    event_filters = (
        [topic_to_rpc_hex(ERC721_TRANSFER_TOPIC), topic_to_rpc_hex(ZERO_ADDRESS_TOPIC)],
        [
            topic_to_rpc_hex(ERC1155_TRANSFER_SINGLE_TOPIC),
            None,
            topic_to_rpc_hex(ZERO_ADDRESS_TOPIC),
        ],
        [
            topic_to_rpc_hex(ERC1155_TRANSFER_BATCH_TOPIC),
            None,
            topic_to_rpc_hex(ZERO_ADDRESS_TOPIC),
        ],
    )

    async with httpx.AsyncClient(follow_redirects=True) as http_client:
        while True:
            try:
                async with websockets.connect(
                    BSC_WSS_URL,
                    open_timeout=15,
                    ping_interval=20,
                    ping_timeout=20,
                ) as websocket:
                    subscriptions = set()
                    for request_id, topics in enumerate(event_filters, start=1):
                        await websocket.send(json.dumps({
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": "eth_subscribe",
                            "params": ["logs", {"topics": topics}],
                        }))
                        while True:
                            response = json.loads(
                                await asyncio.wait_for(websocket.recv(), timeout=15)
                            )
                            if response.get("method") == "eth_subscription":
                                queue_candidate_from_notification(http_client, response)
                                continue
                            break
                        if response.get("id") != request_id:
                            raise RuntimeError("Unexpected response while subscribing to BSC mint logs.")
                        if "error" in response:
                            error = response["error"]
                            raise RuntimeError(
                                f"BSC log subscription failed: {error.get('message', error)}"
                            )
                        subscriptions.add(response["result"])

                    logging.info("BSC mint event subscriptions are active.")
                    async for raw_message in websocket:
                        message = json.loads(raw_message)
                        if (
                            message.get("method") == "eth_subscription"
                            and message.get("params", {}).get("subscription") in subscriptions
                        ):
                            queue_candidate_from_notification(http_client, message)
            except Exception as e:
                logging.warning("BSC WebSocket scanner disconnected; reconnecting: %s", e)
                await asyncio.sleep(5)

# ==========================================
# MAIN LOOP
# ==========================================
async def main():
    init_db()
    await listen_onchain_websocket()

if __name__ == "__main__":
    asyncio.run(main())
