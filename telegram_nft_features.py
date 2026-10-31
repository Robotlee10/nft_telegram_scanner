import asyncio
import html
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from hexbytes import HexBytes
from web3 import Web3
from web3.types import TxParams, Wei


DIRECT_COMMAND_PATTERN = re.compile(
    r"Access my Binance Agentic Wallet\.\s*"
    r"Call the write function\s+(?P<function>[A-Za-z_][A-Za-z0-9_]*\([^)]*\))\s+"
    r"on BNB Chain at contract address\s+(?P<contract>0x[a-fA-F0-9]{40})\s+"
    r"with value\s+(?P<value>[0-9]+(?:\.[0-9]{1,18})?)\s+BNB\.\s*"
    r"execute it,\s*if it preview clearly and would return a[n]?\s+ERC-721 token\.?",
    re.IGNORECASE,
)
CONTRACT_ADDRESS_PATTERN = re.compile(r"0x[a-fA-F0-9]{40}")

ERC721_INTERFACE_ID = bytes.fromhex("80ac58cd")
ERC1155_INTERFACE_ID = bytes.fromhex("d9b67a26")
logger = logging.getLogger(__name__)


def developer_mode_is_active(settings: dict, now: float | None = None) -> bool:
    dev_mode = settings.get("devMode")
    if not isinstance(dev_mode, dict) or dev_mode.get("enabled") is not True:
        return False

    expires_at = dev_mode.get("expiresAt")
    if expires_at is None:
        return True

    expiration_timestamp = None
    if isinstance(expires_at, (int, float)):
        expiration_timestamp = float(expires_at)
    elif isinstance(expires_at, str):
        try:
            expiration_timestamp = float(expires_at)
        except ValueError:
            try:
                expiration = datetime.fromisoformat(
                    expires_at.replace("Z", "+00:00")
                )
                if expiration.tzinfo is None:
                    expiration = expiration.replace(tzinfo=timezone.utc)
                expiration_timestamp = expiration.timestamp()
            except ValueError:
                return True
    else:
        return True

    if expiration_timestamp > 10**12:
        expiration_timestamp /= 1000
    return expiration_timestamp > (time.time() if now is None else now)


@dataclass(frozen=True)
class PendingContractCall:
    request_id: str
    contract: str
    function_name: str
    value_wei: int
    preview_expires_at: float


def _parse_wei_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _preview_matches_mint(
    parsed_tx: dict,
    contract: str,
    wallet: str,
    calldata: str,
    expected_value_wei: int,
    chain_id: int,
) -> bool:
    target = parsed_tx.get("to")
    sender = parsed_tx.get("from")
    data = parsed_tx.get("inputData", parsed_tx.get("data"))
    value = _parse_wei_value(parsed_tx.get("value"))
    if (
        not isinstance(target, str)
        or target.lower() != contract.lower()
        or not isinstance(sender, str)
        or sender.lower() != wallet.lower()
        or not isinstance(data, str)
        or data.lower().removeprefix("0x") != calldata.lower().removeprefix("0x")
        or value != expected_value_wei
    ):
        return False
    parsed_chain_id = parsed_tx.get("binanceChainId", parsed_tx.get("chainId"))
    if parsed_chain_id is not None:
        parsed_chain_id_value = _parse_wei_value(parsed_chain_id)
        if parsed_chain_id_value != chain_id:
            return False
    return True


class TelegramNftFeatures:
    def __init__(
        self,
        telegram_service: Any,
        run_baw: Callable[..., Any],
        wallet_ready: asyncio.Event,
        wallet_address: str | None,
        web3: Web3,
        mint_calldata: dict[str, str],
        verify_transaction: Callable[..., Any],
        chat_id: str,
        scan_contract: Callable[..., Any] | None = None,
        chain_id: int = 56,
        mint_preflight: Callable[..., Any] | None = None,
        verify_mint_transaction: Callable[..., Any] | None = None,
        free_mint_only: bool = True,
        max_mint_cost_wei: int = 0,
    ):
        self.telegram_service = telegram_service
        self.run_baw = run_baw
        self.wallet_ready = wallet_ready
        self.wallet_address = wallet_address
        self.web3 = web3
        self.mint_calldata = mint_calldata
        self.verify_transaction = verify_transaction
        self.chat_id = str(chat_id)
        self.scan_contract = scan_contract
        self.chain_id = chain_id
        self.mint_preflight = mint_preflight
        self.verify_mint_transaction = verify_mint_transaction
        self.free_mint_only = free_mint_only
        self.max_mint_cost_wei = max_mint_cost_wei
        self.pending_calls: dict[str, PendingContractCall] = {}

    def _mint_value_allowed(self, value_wei: int) -> bool:
        if value_wei < 0:
            return False
        if self.free_mint_only:
            return value_wei == 0
        return value_wei <= self.max_mint_cost_wei

    async def _run_mint_preflight(
        self, contract: str, function_name: str, value_wei: int
    ) -> dict | None:
        if self.mint_preflight is None:
            return None
        result = await self.mint_preflight(contract, function_name, value_wei)
        return result if isinstance(result, dict) else None

    def _is_authorized_chat(self, chat: dict | None) -> bool:
        return bool(chat and str(chat.get("id")) == self.chat_id)

    async def handle_message(self, message: dict) -> bool:
        text = message.get("text")
        if not isinstance(text, str):
            return False

        if "Access my Binance Agentic Wallet." not in text:
            if CONTRACT_ADDRESS_PATTERN.fullmatch(text.strip()) is None:
                return False
            if not self._is_authorized_chat(message.get("chat")):
                return True
            await self._scan_submitted_contract(text.strip())
            return True

        if not self._is_authorized_chat(message.get("chat")):
            return True

        match = DIRECT_COMMAND_PATTERN.fullmatch(text.strip())
        if match is None:
            await self.telegram_service.send_message(
                "❌ I couldn't parse that direct command. Use the supported BNB Chain "
                "mint command format and a supported mint function."
            )
            return True

        function_name = match.group("function")
        calldata = self.mint_calldata.get(function_name)
        if not calldata:
            supported = ", ".join(sorted(self.mint_calldata))
            await self.telegram_service.send_message(
                f"❌ Unsupported write function <code>{html.escape(function_name)}</code>. "
                f"Supported functions: <code>{html.escape(supported)}</code>."
            )
            return True

        if not self.wallet_ready.is_set() or not self.wallet_address:
            await self.telegram_service.send_message(
                "❌ Binance Agentic Wallet is not connected or configured."
            )
            return True

        contract = self.web3.to_checksum_address(match.group("contract"))
        try:
            value_wei = self.web3.to_wei(Decimal(match.group("value")), "ether")
        except (InvalidOperation, ValueError):
            await self.telegram_service.send_message("❌ Invalid BNB value in the command.")
            return True
        if not self._mint_value_allowed(value_wei):
            await self.telegram_service.send_message(
                "❌ Mint value exceeds the configured policy: "
                f"{'free mints only' if self.free_mint_only else 'configured maximum cost'}."
            )
            return True

        try:
            preflight = await self._run_mint_preflight(
                contract, function_name, value_wei
            )
        except Exception as error:
            logger.exception("Could not verify mint safety before direct preview.")
            await self.telegram_service.send_message(
                f"❌ Mint safety preflight failed closed: <code>{html.escape(str(error))}</code>"
            )
            return True
        if not preflight or preflight.get("safe") is not True:
            blockers = (preflight or {}).get("blockers", ["Mint safety preflight unavailable."])
            await self.telegram_service.send_message(
                "❌ Mint blocked by safety preflight:\n"
                + "\n".join(f"• {html.escape(str(item))}" for item in blockers)
            )
            return True

        try:
            settings = await self.run_baw(["wallet", "settings", "--json"])
        except Exception as error:
            logger.exception("Could not check Developer Mode before direct contract preview.")
            await self.telegram_service.send_message(
                f"❌ Could not check wallet Developer Mode: <code>{html.escape(str(error))}</code>"
            )
            return True

        if not developer_mode_is_active(settings):
            await self.telegram_service.send_message(
                "❌ Developer Mode is off or expired. Enable it in the Binance App "
                "(Agentic Wallet → Settings → Developer Mode), then resend the command."
            )
            return True

        transaction: TxParams = {
            "from": self.web3.to_checksum_address(self.wallet_address),
            "to": contract,
            "value": value_wei,
            "data": HexBytes(calldata),
        }
        try:
            nft_contract = self.web3.eth.contract(
                address=contract,
                abi=[
                    {
                        "inputs": [{"name": "interfaceId", "type": "bytes4"}],
                        "name": "supportsInterface",
                        "outputs": [{"name": "", "type": "bool"}],
                        "stateMutability": "view",
                        "type": "function",
                    }
                ],
            )
            supports_erc721 = await asyncio.to_thread(
                nft_contract.functions.supportsInterface(ERC721_INTERFACE_ID).call
            )
            if not supports_erc721:
                await self.telegram_service.send_message(
                    f"❌ <code>{html.escape(contract)}</code> does not report ERC-721 support; "
                    "the write call was not previewed."
                )
                return True
            await asyncio.to_thread(self.web3.eth.call, transaction)
        except Exception as error:
            logger.info("Direct mint simulation failed for %s: %s", contract, error)
            await self.telegram_service.send_message(
                "❌ ERC-721 or mint-call simulation failed; the write call was not previewed.\n"
                f"<code>{html.escape(str(error))}</code>"
            )
            return True

        try:
            preview = await self.run_baw(
                [
                    "contract-call",
                    "preview",
                    "--binanceChainId",
                    str(self.chain_id),
                    "--from",
                    self.wallet_address,
                    "--to",
                    contract,
                    "--value",
                    str(value_wei),
                    "--inputData",
                    calldata,
                    "--json",
                ]
            )
        except Exception as error:
            logger.exception("Direct contract-call preview failed.")
            await self.telegram_service.send_message(
                f"❌ Contract-call preview failed: <code>{html.escape(str(error))}</code>"
            )
            return True

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
            await self._send_json_parts(
                "❌ Preview did not clearly confirm a successful simulation; nothing was executed.",
                preview,
            )
            return True
        if not _preview_matches_mint(
            preview["parsedTx"],
            contract,
            self.wallet_address,
            calldata,
            value_wei,
            self.chain_id,
        ):
            await self._send_json_parts(
                "❌ Preview transaction does not match the requested contract, wallet, "
                "function, value, and chain; nothing was executed.",
                preview,
            )
            return True

        expiration = preview.get("expiresAt")
        if not isinstance(expiration, (int, float)):
            expiration = time.time() + 300
        elif expiration > 10**12:
            expiration /= 1000

        confirmation_id = uuid.uuid4().hex[:16]
        self.pending_calls[confirmation_id] = PendingContractCall(
            request_id=request_id,
            contract=contract,
            function_name=function_name,
            value_wei=value_wei,
            preview_expires_at=float(expiration),
        )

        await self._send_preview(preview)
        warnings = preflight.get("warnings", [])
        warning_text = ""
        if warnings:
            warning_text = "\n\n⚠️ <b>Contract review warnings:</b>\n" + "\n".join(
                f"• {html.escape(str(item))}" for item in warnings
            )
        await self.telegram_service.send_message(
            "✅ The contract reports ERC-721 support and the mint call simulated successfully. "
            "Review the preview above, then confirm to execute. The simulation cannot guarantee "
            "the eventual on-chain result."
            + warning_text,
            reply_markup={
                "inline_keyboard": [[
                    {
                        "text": "✅ Confirm execution",
                        "callback_data": f"direct:confirm:{confirmation_id}",
                    },
                    {
                        "text": "Cancel",
                        "callback_data": f"direct:cancel:{confirmation_id}",
                    },
                ]]
            },
        )
        return True

    async def _scan_submitted_contract(self, address: str) -> None:
        if self.scan_contract is None:
            await self.telegram_service.send_message(
                "❌ Contract scanning is not configured; no mint was queued."
            )
            return

        try:
            contract = self.web3.to_checksum_address(address)
            assessment = await self.scan_contract(contract)
        except Exception as error:
            logger.exception("Could not scan submitted NFT contract.")
            await self.telegram_service.send_message(
                "❌ Contract scan failed; no mint was queued.\n"
                f"<code>{html.escape(str(error))}</code>"
            )
            return

        if not assessment.get("is_nft"):
            await self.telegram_service.send_message(
                f"❌ <code>{html.escape(contract)}</code> is not recognized as ERC-721 or "
                "ERC-1155; no mint was queued."
            )
            return

        standard = html.escape(str(assessment["nft_standard"]))
        access_gate = html.escape(str(assessment["access_gate"]))
        function_name = assessment.get("function_name")
        escaped_function = (
            f"<code>{html.escape(function_name)}</code>"
            if isinstance(function_name, str)
            else "No supported function simulated"
        )
        quality_report = assessment["quality_report"]
        details = quality_report["details"]
        mint_conditions = assessment.get("mint_conditions", {})
        condition_values = mint_conditions.get("values", {})
        message = (
            "🔎 <b>NFT contract scan</b>\n\n"
            f"<b>Contract:</b> <code>{html.escape(contract)}</code>\n"
            f"<b>NFT standard:</b> {standard}\n"
            f"<b>Access gate:</b> {access_gate}\n"
            f"<b>Mint function:</b> {escaped_function}\n"
            f"<b>Quality score:</b> {html.escape(quality_report['score_str'])}\n"
            f"<b>Auto-mint rule:</b> all 4 technical checks, all required safety "
            "checks, plus an explicitly verified access gate\n"
            f"1️⃣ Metadata: {html.escape(str(details.get('1_metadata', 'Not checked')))}\n"
            f"2️⃣ Source: {html.escape(str(details.get('2_source', 'Not checked')))}\n"
            f"3️⃣ Proxy: {html.escape(str(details.get('3_proxy', 'Not checked')))}\n"
            f"4️⃣ Admin controls: {html.escape(str(details.get('4_admin_controls', 'Not checked')))}\n"
            f"5️⃣ Activity: {html.escape(str(details.get('5_activity', 'Not checked')))}\n"
            f"6️⃣ Distribution: {html.escape(str(details.get('6_distribution', 'Not checked')))}\n"
            f"7️⃣ Supply/royalties: {html.escape(str(details.get('7_supply_and_royalties', 'Not checked')))}\n"
            f"💰 Mint cost policy: {html.escape(str(assessment.get('mint_cost_policy', 'Not checked')))}\n"
            f"🧪 Live getters (unknown if unexposed): "
            f"<code>{html.escape(json.dumps(condition_values, default=str))}</code>\n"
        )

        if assessment.get("already_owned") is True:
            message += "\nℹ️ This wallet already holds this NFT; no mint was queued."
            await self.telegram_service.send_message(message)
            return
        if assessment.get("already_owned") is None:
            message += "\n⛔ Wallet ownership could not be verified; mint actions are disabled."
            await self.telegram_service.send_message(message)
            return

        queue_status = assessment.get("queue_status")
        if queue_status == "queued":
            message += "\n🤖 Score qualifies; automatic minting has been queued."
        elif queue_status == "already_exists":
            message += "\nℹ️ A mint record already exists for this contract and function; no duplicate was queued."
        elif assessment.get("auto_mint_eligible"):
            message += "\n⚠️ Score qualifies, but automatic queueing failed. Use manual mint only if you want to proceed."
        elif not function_name:
            message += "\n❌ No supported mint call passed simulation; no mint action is available."
        else:
            message += "\nℹ️ This candidate did not meet automatic-mint criteria."

        blockers = assessment.get("blockers", [])
        if blockers:
            message += "\n\n⛔ <b>Mint blockers:</b>\n" + "\n".join(
                f"• {html.escape(str(blocker))}" for blocker in blockers
            )
        warnings = assessment.get("warnings", [])
        if warnings:
            message += "\n\n⚠️ <b>Quality warnings:</b>\n" + "\n".join(
                f"• {html.escape(str(warning))}" for warning in warnings
            )

        reply_markup = None
        if (
            function_name
            and assessment.get("safe") is True
            and queue_status not in {"queued", "already_exists"}
        ):
            reply_markup = {
                "inline_keyboard": [[
                    {
                        "text": "🪙 Mint manually",
                        "callback_data": f"mint:{contract}:{function_name}",
                    }
                ]]
            }
        await self.telegram_service.send_message(message, reply_markup=reply_markup)

    async def _handle_manual_mint_request(self, callback_query: dict) -> bool:
        if not self._is_authorized_chat(
            (callback_query.get("message") or {}).get("chat")
        ):
            await self.telegram_service.answer_callback(
                callback_query["id"], "This action is not authorized."
            )
            return True

        parts = callback_query.get("data", "").split(":", 2)
        if len(parts) != 3 or parts[0] != "mint":
            await self.telegram_service.answer_callback(
                callback_query["id"], "Invalid mint request."
            )
            return True

        contract_input, function_name = parts[1:]
        calldata = self.mint_calldata.get(function_name)
        if not self.web3.is_address(contract_input) or not calldata:
            await self.telegram_service.answer_callback(
                callback_query["id"], "Invalid contract or unsupported mint function."
            )
            return True

        if not self.wallet_ready.is_set() or not self.wallet_address:
            await self.telegram_service.answer_callback(
                callback_query["id"], "Wallet is not connected."
            )
            await self.telegram_service.send_message(
                "❌ Binance Agentic Wallet is not connected or configured; no mint was executed."
            )
            return True

        contract = self.web3.to_checksum_address(contract_input)
        try:
            settings = await self.run_baw(["wallet", "settings", "--json"])
        except Exception as error:
            logger.exception("Could not check Developer Mode before manual mint preview.")
            await self.telegram_service.answer_callback(
                callback_query["id"], "Could not check Developer Mode."
            )
            await self.telegram_service.send_message(
                f"❌ Could not check wallet Developer Mode: <code>{html.escape(str(error))}</code>"
            )
            return True

        if not developer_mode_is_active(settings):
            await self.telegram_service.answer_callback(
                callback_query["id"], "Developer Mode is off or expired."
            )
            await self.telegram_service.send_message(
                "❌ Developer Mode is off or expired. Enable it in the Binance App "
                "(Agentic Wallet → Settings → Developer Mode), then tap the mint button again."
            )
            return True

        await self.telegram_service.answer_callback(
            callback_query["id"], "Checking NFT standard and preparing preview..."
        )
        try:
            preflight = await self._run_mint_preflight(contract, function_name, 0)
        except Exception as error:
            logger.exception("Could not verify safety before manual NFT preview.")
            await self.telegram_service.send_message(
                f"❌ Mint safety preflight failed closed: <code>{html.escape(str(error))}</code>"
            )
            return True
        if not preflight or preflight.get("safe") is not True:
            blockers = (preflight or {}).get("blockers", ["Mint safety preflight unavailable."])
            await self.telegram_service.send_message(
                "❌ Mint blocked by safety preflight:\n"
                + "\n".join(f"• {html.escape(str(item))}" for item in blockers)
            )
            return True
        transaction: TxParams = {
            "from": self.web3.to_checksum_address(self.wallet_address),
            "to": contract,
            "value": Wei(0),
            "data": HexBytes(calldata),
        }
        try:
            nft_contract = self.web3.eth.contract(
                address=contract,
                abi=[
                    {
                        "inputs": [{"name": "interfaceId", "type": "bytes4"}],
                        "name": "supportsInterface",
                        "outputs": [{"name": "", "type": "bool"}],
                        "stateMutability": "view",
                        "type": "function",
                    }
                ],
            )
            supports_nft = False
            for interface_id in (ERC721_INTERFACE_ID, ERC1155_INTERFACE_ID):
                try:
                    supports_nft = await asyncio.to_thread(
                        nft_contract.functions.supportsInterface(interface_id).call
                    )
                except Exception:
                    continue
                if supports_nft:
                    break
            if not supports_nft:
                code = await asyncio.to_thread(self.web3.eth.get_code, contract)
                code_hex = code.hex()
                supports_nft = "63706577" in code_hex or "70a08231" in code_hex
            if not supports_nft:
                await self.telegram_service.send_message(
                    f"❌ <code>{html.escape(contract)}</code> no longer reports ERC-721 or "
                    "ERC-1155 support; the mint was not previewed."
                )
                return True
            await asyncio.to_thread(self.web3.eth.call, transaction)
        except Exception as error:
            logger.info("Manual mint simulation failed for %s: %s", contract, error)
            await self.telegram_service.send_message(
                "❌ Mint-call simulation failed; the call was not previewed.\n"
                f"<code>{html.escape(str(error))}</code>"
            )
            return True

        try:
            preview = await self.run_baw(
                [
                    "contract-call",
                    "preview",
                    "--binanceChainId",
                    str(self.chain_id),
                    "--from",
                    self.wallet_address,
                    "--to",
                    contract,
                    "--value",
                    "0",
                    "--inputData",
                    calldata,
                    "--json",
                ]
            )
        except Exception as error:
            logger.exception("Manual contract-call preview failed.")
            await self.telegram_service.send_message(
                f"❌ Contract-call preview failed: <code>{html.escape(str(error))}</code>"
            )
            return True

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
            await self._send_json_parts(
                "❌ Preview did not clearly confirm a successful simulation; nothing was executed.",
                preview,
            )
            return True
        if not _preview_matches_mint(
            preview["parsedTx"],
            contract,
            self.wallet_address,
            calldata,
            0,
            self.chain_id,
        ):
            await self._send_json_parts(
                "❌ Preview transaction does not match the NFT contract, wallet, "
                "function, value, and chain; nothing was executed.",
                preview,
            )
            return True

        expiration = preview.get("expiresAt")
        if not isinstance(expiration, (int, float)):
            expiration = time.time() + 300
        elif expiration > 10**12:
            expiration /= 1000

        confirmation_id = uuid.uuid4().hex[:16]
        self.pending_calls[confirmation_id] = PendingContractCall(
            request_id=request_id,
            contract=contract,
            function_name=function_name,
            value_wei=0,
            preview_expires_at=float(expiration),
        )
        await self._send_preview(preview)
        warnings = preflight.get("warnings", [])
        warning_text = ""
        if warnings:
            warning_text = "\n\n⚠️ <b>Automatic-mint checks:</b>\n" + "\n".join(
                f"• {html.escape(str(item))}" for item in warnings
            )
        await self.telegram_service.send_message(
            "✅ The NFT standard and mint-call simulation passed. Review the preview above, "
            "then confirm to execute. The simulation cannot guarantee the eventual on-chain result."
            + warning_text,
            reply_markup={
                "inline_keyboard": [[
                    {
                        "text": "✅ Confirm execution",
                        "callback_data": f"direct:confirm:{confirmation_id}",
                    },
                    {
                        "text": "Cancel",
                        "callback_data": f"direct:cancel:{confirmation_id}",
                    },
                ]]
            },
        )
        return True

    async def handle_callback(self, callback_query: dict) -> bool:
        data = callback_query.get("data", "")
        if not isinstance(data, str):
            return False
        if data.startswith("mint:"):
            return await self._handle_manual_mint_request(callback_query)
        if not data.startswith("direct:"):
            return False

        if not self._is_authorized_chat(
            (callback_query.get("message") or {}).get("chat")
        ):
            await self.telegram_service.answer_callback(
                callback_query["id"], "This action is not authorized."
            )
            return True

        parts = data.split(":")
        if len(parts) != 3 or parts[1] not in {"confirm", "cancel"}:
            await self.telegram_service.answer_callback(
                callback_query["id"], "Invalid or expired action."
            )
            return True

        pending = self.pending_calls.pop(parts[2], None)
        if pending is None:
            await self.telegram_service.answer_callback(
                callback_query["id"], "This preview is no longer available."
            )
            return True

        if parts[1] == "cancel":
            await self.telegram_service.answer_callback(callback_query["id"], "Cancelled.")
            await self.telegram_service.send_message("Direct contract call cancelled.")
            return True

        if pending.preview_expires_at <= time.time():
            await self.telegram_service.answer_callback(
                callback_query["id"], "Preview expired; send the command again."
            )
            await self.telegram_service.send_message(
                "❌ The preview expired. Resend the direct command to create a fresh preview."
            )
            return True

        if not self.wallet_ready.is_set():
            await self.telegram_service.answer_callback(
                callback_query["id"], "Wallet is not connected."
            )
            await self.telegram_service.send_message(
                "❌ Binance Agentic Wallet is no longer connected; nothing was executed."
            )
            return True
        if not self.wallet_address:
            await self.telegram_service.answer_callback(
                callback_query["id"], "Wallet address is unavailable."
            )
            await self.telegram_service.send_message(
                "❌ Configured wallet address is unavailable; nothing was executed."
            )
            return True

        try:
            preflight = await self._run_mint_preflight(
                pending.contract, pending.function_name, pending.value_wei
            )
            if not preflight or preflight.get("safe") is not True:
                blockers = (preflight or {}).get(
                    "blockers", ["Mint safety preflight unavailable."]
                )
                await self.telegram_service.answer_callback(
                    callback_query["id"], "Safety checks no longer pass."
                )
                await self.telegram_service.send_message(
                    "❌ Mint cancelled because fresh safety checks failed:\n"
                    + "\n".join(f"• {html.escape(str(item))}" for item in blockers)
                )
                return True
            settings = await self.run_baw(["wallet", "settings", "--json"])
            if not developer_mode_is_active(settings):
                await self.telegram_service.answer_callback(
                    callback_query["id"], "Developer Mode is off or expired."
                )
                await self.telegram_service.send_message(
                    "❌ Developer Mode is off or expired; the contract call was not executed."
                )
                return True

            await self.telegram_service.answer_callback(
                callback_query["id"], "Executing the previewed contract call..."
            )
            execution = await self.run_baw(
                [
                    "contract-call",
                    "execute",
                    "--requestId",
                    pending.request_id,
                    "--json",
                ]
            )
            tx_hash = execution.get("txHash")
            if not isinstance(tx_hash, str) or not tx_hash.startswith("0x"):
                await self._send_json_parts(
                    "❌ Contract call did not broadcast.",
                    execution,
                )
                return True

            if self.verify_mint_transaction is None:
                success, block_number, detail = (
                    False,
                    None,
                    "NFT transfer verification is unavailable.",
                )
            else:
                success, block_number, detail = await self.verify_mint_transaction(
                    tx_hash, pending.contract, self.wallet_address
                )
            if success:
                await self.telegram_service.send_message(
                    "🟢 <b>Contract call executed successfully.</b>\n"
                    f"<b>Function:</b> <code>{html.escape(pending.function_name)}</code>\n"
                    f"<b>Contract:</b> <code>{html.escape(pending.contract)}</code>\n"
                    f"<b>Block:</b> <code>{html.escape(str(block_number))}</code>\n"
                    f"<b>Mint verification:</b> {html.escape(detail)}\n"
                    f"<b>Transaction:</b> https://bscscan.com/tx/{html.escape(tx_hash)}"
                )
            else:
                await self.telegram_service.send_message(
                    "🔴 <b>Contract call failed or could not be confirmed.</b>\n"
                    f"<b>Contract:</b> <code>{html.escape(pending.contract)}</code>\n"
                    f"<b>Transaction:</b> <code>{html.escape(tx_hash)}</code>\n"
                    f"<b>Result:</b> {html.escape(detail)}"
                )
        except Exception as error:
            logger.exception("Confirmed direct contract call failed.")
            await self.telegram_service.send_message(
                f"❌ Contract call failed: <code>{html.escape(str(error))}</code>"
            )
        return True

    async def _send_preview(self, preview: dict) -> None:
        fields = (
            ("Parsed transaction", preview.get("parsedTx")),
            ("Simulation", preview.get("simulationResult")),
            ("Risks", preview.get("risks")),
            (
                "Authority changes",
                preview.get(
                    "authorityChanges",
                    (preview.get("simulationResult") or {}).get("authorityChanges"),
                ),
            ),
        )
        await self.telegram_service.send_message(
            "🔎 <b>Direct contract-call preview</b>"
        )
        for label, value in fields:
            if value is None:
                continue
            serialized = self._json_text(value)
            for index in range(0, len(serialized), 2800):
                part = serialized[index:index + 2800]
                await self.telegram_service.send_message(
                    f"<b>{html.escape(label)}:</b>\n<pre>{html.escape(part)}</pre>"
                )

    async def _send_json_parts(self, title: str, value: Any) -> None:
        serialized = self._json_text(value)
        for index in range(0, len(serialized), 2800):
            part = serialized[index:index + 2800]
            await self.telegram_service.send_message(
                f"<b>{html.escape(title)}</b>\n<pre>{html.escape(part)}</pre>"
            )

    @staticmethod
    def _json_text(value: Any) -> str:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=str)
