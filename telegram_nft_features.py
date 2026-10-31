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
from web3.types import TxParams


DIRECT_COMMAND_PATTERN = re.compile(
    r"Access my Binance Agentic Wallet\.\s*"
    r"Call the write function\s+(?P<function>[A-Za-z_][A-Za-z0-9_]*\([^)]*\))\s+"
    r"on BNB Chain at contract address\s+(?P<contract>0x[a-fA-F0-9]{40})\s+"
    r"with value\s+(?P<value>[0-9]+(?:\.[0-9]{1,18})?)\s+BNB\.\s*"
    r"execute it,\s*if it preview clearly and would return a[n]?\s+ERC-721 token\.?",
    re.IGNORECASE,
)

ERC721_INTERFACE_ID = bytes.fromhex("80ac58cd")
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
    preview_expires_at: float


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
        chain_id: int = 56,
    ):
        self.telegram_service = telegram_service
        self.run_baw = run_baw
        self.wallet_ready = wallet_ready
        self.wallet_address = wallet_address
        self.web3 = web3
        self.mint_calldata = mint_calldata
        self.verify_transaction = verify_transaction
        self.chat_id = str(chat_id)
        self.chain_id = chain_id
        self.pending_calls: dict[str, PendingContractCall] = {}

    def _is_authorized_chat(self, chat: dict | None) -> bool:
        return bool(chat and str(chat.get("id")) == self.chat_id)

    async def handle_message(self, message: dict) -> bool:
        text = message.get("text")
        if not isinstance(text, str) or "Access my Binance Agentic Wallet." not in text:
            return False

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
            preview_expires_at=float(expiration),
        )

        await self._send_preview(preview)
        await self.telegram_service.send_message(
            "✅ The contract reports ERC-721 support and the mint call simulated successfully. "
            "Review the preview above, then confirm to execute. The simulation cannot guarantee "
            "the eventual on-chain result.",
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
        if not isinstance(data, str) or not data.startswith("direct:"):
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

        try:
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

            success, block_number, detail = await self.verify_transaction(tx_hash)
            if success:
                await self.telegram_service.send_message(
                    "🟢 <b>Contract call executed successfully.</b>\n"
                    f"<b>Function:</b> <code>{html.escape(pending.function_name)}</code>\n"
                    f"<b>Contract:</b> <code>{html.escape(pending.contract)}</code>\n"
                    f"<b>Block:</b> <code>{html.escape(str(block_number))}</code>\n"
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
