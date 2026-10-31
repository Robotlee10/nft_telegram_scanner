import asyncio
import logging
import sqlite3
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from eth_abi.abi import decode
from eth_typing import ChecksumAddress
from hexbytes import HexBytes
from web3 import Web3


ERC721_TRANSFER = HexBytes(
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)
ERC1155_TRANSFER_SINGLE = HexBytes(
    "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
)
ERC1155_TRANSFER_BATCH = HexBytes(
    "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"
)
ERC20_TRANSFER = HexBytes(
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
SUMMARY_INTERVAL = 24 * 60 * 60
INITIAL_BLOCK_RANGE = 100_000
logger = logging.getLogger(__name__)


class NftChainMonitor:
    def __init__(
        self,
        web3: Web3,
        wallet_address: str,
        database_path: str | Path,
        telegram_service: Any,
        chain_name: str = "BNB Chain",
        explorer_base_url: str = "https://bscscan.com",
    ):
        self.web3 = web3
        self.wallet_address = web3.to_checksum_address(wallet_address)
        self.wallet_lower = self.wallet_address.lower()
        self.wallet_topic = HexBytes("0x" + self.wallet_lower[2:].zfill(64))
        self.database_path = str(database_path)
        self.telegram_service = telegram_service
        self.chain_name = chain_name
        self.explorer_base_url = explorer_base_url.rstrip("/")
        self.token_metadata: dict[str, tuple[str, int]] = {}
        self._scan_lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS nft_chain_monitor_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS nft_chain_holdings (
                    contract_address TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    nft_standard TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    PRIMARY KEY (contract_address, token_id, nft_standard)
                );
                """
            )
            connection.commit()
        finally:
            connection.close()

    def _get_state(self, key: str) -> str | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT value FROM nft_chain_monitor_state WHERE key = ?", (key,)
            ).fetchone()
            return row[0] if row else None
        finally:
            connection.close()

    def _set_state(self, key: str, value: str) -> None:
        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT INTO nft_chain_monitor_state (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            connection.commit()
        finally:
            connection.close()

    def _query_logs(self, from_block: int, to_block: int) -> list:
        event_topics = [ERC721_TRANSFER, ERC1155_TRANSFER_SINGLE, ERC1155_TRANSFER_BATCH]
        queries = (
            {"topics": [event_topics, self.wallet_topic]},
            {"topics": [event_topics, None, self.wallet_topic]},
            {
                "topics": [
                    [ERC1155_TRANSFER_SINGLE, ERC1155_TRANSFER_BATCH],
                    None,
                    None,
                    self.wallet_topic,
                ]
            },
        )
        logs = []
        seen: set[tuple[str, int]] = set()
        for topics in queries:
            result = self.web3.eth.get_logs(
                {
                    "fromBlock": from_block,
                    "toBlock": to_block,
                    "topics": topics["topics"],
                }
            )
            for log in result:
                key = (bytes(log["transactionHash"]).hex(), int(log["logIndex"]))
                if key not in seen:
                    logs.append(log)
                    seen.add(key)
        logs.sort(key=lambda log: (int(log["blockNumber"]), int(log["logIndex"])))
        return logs

    def _parse_log(self, log: Any) -> list[tuple[str, str, str, int]]:
        topics = log["topics"]
        event_signature = HexBytes(topics[0])
        contract = self.web3.to_checksum_address(log["address"])

        if event_signature == ERC721_TRANSFER:
            if len(topics) < 4:
                raise ValueError("Malformed ERC-721 Transfer log.")
            sender = self._topic_address(topics[1])
            recipient = self._topic_address(topics[2])
            token_id = str(int.from_bytes(bytes(topics[3]), "big"))
            return [(contract, token_id, "ERC-721", self._wallet_delta(sender, recipient))]

        if event_signature == ERC1155_TRANSFER_SINGLE:
            if len(topics) < 4:
                raise ValueError("Malformed ERC-1155 TransferSingle log.")
            sender = self._topic_address(topics[2])
            recipient = self._topic_address(topics[3])
            token_id, quantity = decode(
                ["uint256", "uint256"], bytes(log["data"])
            )
            delta = self._wallet_delta(sender, recipient)
            return [(contract, str(token_id), "ERC-1155", delta * int(quantity))]

        if event_signature == ERC1155_TRANSFER_BATCH:
            if len(topics) < 4:
                raise ValueError("Malformed ERC-1155 TransferBatch log.")
            sender = self._topic_address(topics[2])
            recipient = self._topic_address(topics[3])
            token_ids, quantities = decode(
                ["uint256[]", "uint256[]"], bytes(log["data"])
            )
            delta = self._wallet_delta(sender, recipient)
            return [
                (contract, str(token_id), "ERC-1155", delta * int(quantity))
                for token_id, quantity in zip(token_ids, quantities, strict=True)
            ]

        return []

    def _wallet_delta(self, sender: str, recipient: str) -> int:
        delta = 0
        if recipient.lower() == self.wallet_lower:
            delta += 1
        if sender.lower() == self.wallet_lower:
            delta -= 1
        return delta

    @staticmethod
    def _topic_address(topic: Any) -> str:
        return "0x" + bytes(topic).hex()[-40:]

    def _apply_logs(self, logs: list, scanned_to: int) -> None:
        connection = self._connect()
        try:
            with connection:
                for log in logs:
                    for contract, token_id, standard, delta in self._parse_log(log):
                        if delta == 0:
                            continue
                        row = connection.execute(
                            """
                            SELECT quantity FROM nft_chain_holdings
                            WHERE contract_address = ? AND token_id = ? AND nft_standard = ?
                            """,
                            (contract.lower(), token_id, standard),
                        ).fetchone()
                        current = int(row[0]) if row else 0
                        updated = max(0, current + delta)
                        if updated == 0:
                            connection.execute(
                                """
                                DELETE FROM nft_chain_holdings
                                WHERE contract_address = ? AND token_id = ? AND nft_standard = ?
                                """,
                                (contract.lower(), token_id, standard),
                            )
                        else:
                            connection.execute(
                                """
                                INSERT INTO nft_chain_holdings
                                    (contract_address, token_id, nft_standard, quantity)
                                VALUES (?, ?, ?, ?)
                                ON CONFLICT(contract_address, token_id, nft_standard)
                                DO UPDATE SET quantity = excluded.quantity
                                """,
                                (contract.lower(), token_id, standard, str(updated)),
                            )
                connection.execute(
                    """
                    INSERT INTO nft_chain_monitor_state (key, value)
                    VALUES ('last_scanned_block', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (str(scanned_to),),
                )
        finally:
            connection.close()

    def _get_holdings(self) -> list[tuple[str, str, str, str]]:
        connection = self._connect()
        try:
            return connection.execute(
                """
                SELECT contract_address, token_id, nft_standard, quantity
                FROM nft_chain_holdings
                ORDER BY contract_address, token_id
                """
            ).fetchall()
        finally:
            connection.close()

    def _get_erc20_payment_logs(self, receipt: Any) -> list[tuple[str, int]]:
        wallet_topic = self.wallet_topic.hex().lower().removeprefix("0x")
        payment_logs = []
        for log in receipt.get("logs", []):
            topics = log.get("topics", [])
            if len(topics) != 3 or HexBytes(topics[0]) != ERC20_TRANSFER:
                continue
            recipient = bytes(topics[2]).hex().lower()
            sender = bytes(topics[1]).hex().lower()
            amount = int.from_bytes(bytes(log["data"]), "big")
            if recipient.endswith(wallet_topic) and not sender.endswith(wallet_topic) and amount:
                payment_logs.append(
                    (
                        self.web3.to_checksum_address(log["address"]),
                        amount,
                    )
                )
        return payment_logs

    def _token_details(self, token_address: str) -> tuple[str, int]:
        token_key = token_address.lower()
        if token_key in self.token_metadata:
            return self.token_metadata[token_key]

        checksum_token_address: ChecksumAddress = self.web3.to_checksum_address(
            token_address
        )
        contract = self.web3.eth.contract(
            address=checksum_token_address,
            abi=[
                {
                    "inputs": [],
                    "name": "symbol",
                    "outputs": [{"type": "string"}],
                    "stateMutability": "view",
                    "type": "function",
                },
                {
                    "inputs": [],
                    "name": "decimals",
                    "outputs": [{"type": "uint8"}],
                    "stateMutability": "view",
                    "type": "function",
                },
            ],
        )
        try:
            symbol = contract.functions.symbol().call()
            decimals = int(contract.functions.decimals().call())
            if not isinstance(symbol, str):
                raise ValueError("ERC-20 symbol did not return a string.")
        except Exception:
            logger.warning("Could not read payment-token metadata for %s.", token_address)
            symbol, decimals = token_address, 0
        self.token_metadata[token_key] = symbol, decimals
        return symbol, decimals

    def _payment_description(self, payment_logs: list[tuple[str, int]]) -> str:
        descriptions = []
        for token_address, amount in payment_logs:
            symbol, decimals = self._token_details(token_address)
            if decimals:
                token_amount = Decimal(amount) / Decimal(10**decimals)
                amount_text = f"{format(token_amount, f',.{min(decimals, 8)}f')} {symbol}"
            else:
                amount_text = f"{amount} units of {symbol}"
            descriptions.append(
                f"{amount_text} (<code>{token_address}</code>)"
            )
        if not descriptions:
            return ""
        return (
            "\nPossible sale proceeds transferred to this wallet in the same transaction: "
            + ", ".join(descriptions)
            + ". This is payment evidence, not definitive proof of a marketplace sale."
        )

    def _find_payment_logs(self, transaction_hash: Any) -> list[tuple[str, int]]:
        receipt = self.web3.eth.get_transaction_receipt(transaction_hash)
        if receipt is None or receipt.get("status") != 1:
            return []
        return self._get_erc20_payment_logs(receipt)

    async def _notify_movements(self, logs: list) -> None:
        for log in logs:
            parsed = self._parse_log(log)
            transaction_hash = log["transactionHash"]
            transaction_link = (
                f"{self.explorer_base_url}/tx/0x{bytes(transaction_hash).hex()}"
            )
            for contract, token_id, standard, delta in parsed:
                if not delta:
                    continue
                direction = "received" if delta > 0 else "sent"
                payment_note = ""
                if delta < 0:
                    try:
                        payment_logs = await asyncio.to_thread(
                            self._find_payment_logs, transaction_hash
                        )
                        payment_note = self._payment_description(payment_logs)
                    except Exception:
                        logger.exception(
                            "Could not inspect payment logs for NFT transaction %s.",
                            transaction_link,
                        )
                await self.telegram_service.send_message(
                    f"🎨 <b>NFT {direction} on {self.chain_name}</b>\n"
                    f"<b>Standard:</b> {standard}\n"
                    f"<b>Collection:</b> <code>{contract}</code>\n"
                    f"<b>Token ID:</b> <code>{token_id}</code>\n"
                    f"<b>Quantity:</b> {abs(delta)}"
                    f"{payment_note}\n"
                    f"<b>Transaction:</b> {transaction_link}"
                )

    async def _send_holdings_summary(self, initial: bool) -> None:
        holdings = self._get_holdings()
        if not holdings:
            text = (
                f"📊 <b>{'Initial' if initial else 'Daily'} on-chain NFT check complete</b>\n"
                f"No ERC-721/ERC-1155 NFTs were found for <code>{self.wallet_address}</code> "
                f"on {self.chain_name}."
            )
            await self.telegram_service.send_message(text)
            return

        header = [
            f"📊 <b>{'Initial' if initial else 'Daily'} on-chain NFT holdings "
            f"({len(holdings)} token IDs)</b>",
            f"<b>Wallet:</b> <code>{self.wallet_address}</code>",
            f"<i>On-chain holdings only; this report does not include marketplace floor prices.</i>",
        ]
        messages = []
        current = "\n".join(header)
        for contract, token_id, standard, quantity in holdings:
            line = (
                f"• {standard} <code>{contract}</code> token "
                f"<code>{token_id}</code> × {quantity}"
            )
            if len(current) + len(line) + 1 > 3500:
                messages.append(current)
                current = f"📊 <b>Holdings (continued)</b>\n{line}"
            else:
                current += f"\n{line}"
        if current:
            messages.append(current)
        for message in messages:
            await self.telegram_service.send_message(message)

    async def scan_now(self) -> dict[str, int]:
        self.initialize()
        async with self._scan_lock:
            latest_block = await asyncio.to_thread(lambda: self.web3.eth.block_number)
            last_scanned = self._get_state("last_scanned_block")
            last_scanned_block = int(last_scanned) if last_scanned is not None else -1
            start_block = last_scanned_block + 1
            completed_initial_scan = self._get_state("initial_scan_complete") == "1"

            while last_scanned_block < latest_block:
                logs, scanned_to = await self._scan_range(
                    last_scanned_block + 1, latest_block
                )
                self._apply_logs(logs, scanned_to)
                last_scanned_block = scanned_to
                if completed_initial_scan:
                    await self._notify_movements(logs)
                await self._send_progress(last_scanned_block, latest_block)

            initial_scan_completed = not completed_initial_scan
            if initial_scan_completed:
                self._set_state("initial_scan_complete", "1")

            return {
                "start_block": start_block,
                "end_block": latest_block,
                "scanned_blocks": max(0, latest_block - start_block + 1),
                "holding_count": len(self._get_holdings()),
                "initial_scan_completed": int(initial_scan_completed),
                "initial_scan_complete": int(
                    self._get_state("initial_scan_complete") == "1"
                ),
            }

    async def send_scan_report(self, scan: dict[str, int]) -> None:
        holdings = self._get_holdings()
        block_summary = (
            f"<b>Blocks scanned:</b> {scan['scanned_blocks']} "
            f"({scan['start_block']}–{scan['end_block']})\n"
            if scan["scanned_blocks"]
            else "<b>Blocks scanned:</b> No new blocks\n"
        )
        header = (
            "📊 <b>NFT scan complete</b>\n"
            f"<b>Wallet:</b> <code>{self.wallet_address}</code>\n"
            f"<b>Network:</b> {self.chain_name}\n"
            f"<b>Latest block:</b> {scan['end_block']}\n"
            f"{block_summary}"
            f"<b>NFT token IDs held:</b> {scan['holding_count']}\n"
            "<i>On-chain holdings only; floor prices are not included.</i>"
        )
        if not holdings:
            await self.telegram_service.send_message(
                f"{header}\n\nNo ERC-721/ERC-1155 holdings were found."
            )
            return

        messages = []
        current = header
        for contract, token_id, standard, quantity in holdings:
            line = (
                f"• {standard} <code>{contract}</code> token "
                f"<code>{token_id}</code> × {quantity}"
            )
            if len(current) + len(line) + 1 > 3500:
                messages.append(current)
                current = f"📊 <b>Holdings (continued)</b>\n{line}"
            else:
                current += f"\n{line}"
        if current:
            messages.append(current)
        for message in messages:
            await self.telegram_service.send_message(message)

    async def _send_progress(self, current: int, end: int) -> None:
        if current == 0 or current >= end or current % 1_000_000 < INITIAL_BLOCK_RANGE:
            logger.info(
                "NFT history scan for %s: block %s / %s.",
                self.wallet_address,
                current,
                end,
            )

    async def _scan_range(self, start: int, end: int) -> tuple[list, int]:
        range_size = min(INITIAL_BLOCK_RANGE, end - start + 1)
        while True:
            range_end = min(start + range_size - 1, end)
            try:
                logs = await asyncio.to_thread(self._query_logs, start, range_end)
                return logs, range_end
            except Exception:
                if range_size <= 1:
                    raise
                range_size = max(1, range_size // 2)
                logger.warning(
                    "NFT log query failed for blocks %s-%s; retrying with a %s-block range.",
                    start,
                    range_end,
                    range_size,
                    exc_info=True,
                )

    async def run(self) -> None:
        self.initialize()
        next_summary_at = 0.0
        logger.info(
            "Starting on-chain NFT monitor for %s (%s).",
            self.wallet_address,
            self.chain_name,
        )
        while True:
            try:
                scan = await self.scan_now()
                if scan["initial_scan_completed"]:
                    await self._send_holdings_summary(initial=True)
                    next_summary_at = time.monotonic() + SUMMARY_INTERVAL
                elif not scan["initial_scan_complete"]:
                    next_summary_at = 0.0
                elif next_summary_at == 0.0:
                    next_summary_at = time.monotonic() + SUMMARY_INTERVAL
                elif time.monotonic() >= next_summary_at:
                    await self._send_holdings_summary(initial=False)
                    next_summary_at = time.monotonic() + SUMMARY_INTERVAL
                await asyncio.sleep(15)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "On-chain NFT scan failed; retrying after 60 seconds."
                )
                await asyncio.sleep(60)
