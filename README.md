# NFT Telegram Scanner

A Python service that monitors BNB Chain for NFT mint activity, assesses candidate
contracts, reports wallet NFT activity to Telegram, and optionally mints eligible
NFTs through the Binance Agentic Wallet CLI.

## Features

- Watches BNB Chain ERC-721 and ERC-1155 mint events over WebSocket.
- Checks whether a candidate supports ERC-721/ERC-1155, whether the configured
  wallet can currently simulate a supported mint function, and whether a
  control-wallet failure contains a recognizable access-denial reason.
- Scores four technical checks: validated metadata and an available asset,
  verified source/ABI, no detected proxy, and no recognized high-risk admin
  controls. Activity, minter distribution, royalties, supply caps, and deployer
  history are displayed as informational signals and do not increase the score.
- Automatically queues only candidates that pass all four technical checks
  plus ownership, explicit access-gate, live-call, mint-condition, and cost
  safety checks. The worker repeats those checks and validates the exact wallet
  preview immediately before execution.
- Requires a successful receipt and an NFT mint-transfer event from the zero
  address to the configured wallet before reporting mint success. Confirmed
  token IDs and transaction hashes are saved in SQLite.
- Sends Telegram alerts with a manual mint option for candidates that were not
  automatically queued. Manual minting requires a successful preview and a
  separate confirmation.
- Tracks the configured wallet's ERC-721/ERC-1155 holdings and transfers.
- Accepts `/scannft` in the configured Telegram chat to scan wallet history and
  return a holdings report. The initial scan may take a while; subsequent scans
  continue from the last saved block.

The technical score is not investment advice and cannot guarantee that an NFT is
valuable, safe, or desirable. Market activity, declared scarcity, and royalties
can be manipulated or misrepresented. Proxy and privileged-control detection is
heuristic and does not prove that a contract has no hidden risks. Automatic mint
execution submits an on-chain transaction only after the configured hard gates
and exact transaction preview pass; network state may still change before
inclusion.

## Requirements

- Python 3.12 or later.
- Node.js 20.19 or later and npm (the Agentic Wallet dependency includes a
  Node.js 20.19 minimum transitive dependency).
- A Telegram bot token and the numeric chat ID authorized to control it.
- A BNB Chain HTTP RPC endpoint that supports `eth_getLogs`; a WebSocket RPC
  endpoint is needed for live mint-event scanning.
- A Binance Agentic Wallet configured for BNB Chain to use wallet minting.

## Installation

Run these commands from the project directory.

### Linux

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
npm ci
```

### Windows PowerShell

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
npm ci
```

## Configuration

Create a `.env` file in the project directory. Keep it private; do not commit bot
tokens, wallet credentials, or RPC API keys.

```dotenv
TELEGRAM_BOT_TOKEN=replace-with-your-telegram-bot-token
TELEGRAM_CHAT_ID=replace-with-your-numeric-chat-id
MY_AGENTIC_WALLET=0xYourBnbChainWalletAddress

# BNB Chain endpoints. Use provider URLs that support historical eth_getLogs.
BSC_HTTP_URL=https://your-bnb-http-rpc
BSC_WSS_URL=wss://your-bnb-websocket-rpc

# Required for automatic minting's verified-source/ABI safety gate.
ETHERSCAN_API_KEY=your-etherscan-api-key

# Optional: used as a fallback for wallet NFT history if eth_getLogs is capped.
# If omitted, ETHERSCAN_API_KEY is used for this fallback.
BSC_HISTORY_API_KEY=your-etherscan-api-key

# Optional: used as the comparison wallet for the exclusive mint access test.
CONTROL_WALLET_NO_ID=0x0000000000000000000000000000000000000001

# Minimum score required to auto-mint (0-7; defaults to 6).
AUTO_MINT_THRESHOLD=6

# Mint cost policy. Defaults to free mints only.
FREE_MINT_ONLY=true
# Used only when FREE_MINT_ONLY=false; direct/manual mint commands are capped.
MAX_MINT_COST_BNB=0.01
```

`FREE_MINT_ONLY=true` is the default and blocks transactions with nonzero BNB
value. Set it to `false` only if you explicitly want to allow manually specified
mint values up to `MAX_MINT_COST_BNB`. Automatic queue mints always use zero BNB
value. Zero BNB value does not rule out a contract attempting to transfer
previously approved tokens; review the wallet preview and existing approvals.
The maximum is a transaction-value cap, not a gas-fee cap.

`TELEGRAM_CHAT_ID` is the only chat authorized to use `/scannft` and mint
confirmation buttons. The bot uses Telegram long polling; do not run another
poller for the same bot token or configure a webhook at the same time.

The Binance Agentic Wallet CLI is invoked through the installed npm package.
When the worker starts, it checks wallet connection and may send a Telegram QR
code for sign-in. The configured `MY_AGENTIC_WALLET` must match the connected
wallet's BNB Chain address. Developer Mode must be enabled for contract-call
previews and minting.

## Run

Start both the live event scanner and the execution/Telegram worker:

```bash
python runner.py
```

Alternatively, run each service in a separate terminal:

```bash
python nft_telegram_scanner.py
python execution_worker.py
```

The Telegram worker polls updates and runs the holdings monitor in the
background. In the authorized chat, send:

```text
/scannft
```

The scan reports the latest block, scanned block range, number of token IDs held,
and each holding's contract, token ID, standard, and quantity. It does not report
market prices or floor prices.

You can also send a bare BNB Chain contract address to Telegram to scan that
contract's NFT standard, current mint simulation, ownership, access gate,
metadata, source, proxy/admin controls, sale-state getters, and technical score.
Unknown ownership, an unverified mint call, an over-policy price, a paused or
closed sale, or a sold-out supply blocks mint actions. Generic reverts from a
control wallet are not treated as proof of exclusivity.

### Mint safety and score

- Ownership checks are tri-state: owned, not owned, or unknown. RPC failures are
  unknown and block automated and manual mint flows.
- The wallet-ownership preflight currently requires ERC-721 `balanceOf(address)`.
  ERC-1155 contracts are identified and reported, but mint actions are blocked
  because this scanner does not yet aggregate all ERC-1155 token-ID balances.
- Mint previews must match the configured wallet, target contract, calldata,
  chain (when included by the wallet CLI), and exact BNB value.
- Automatic minting is blocked when the wallet preview reports risk details or
  authority changes; direct/manual calls show those details and still require
  confirmation.
- Common `paused`, sale-active, supply-cap, current-supply, and mint-price
  getters are checked when the contract exposes them. The exact mint call is
  also simulated immediately before execution. Getter availability varies;
  absence is reported as unknown, not as evidence that the contract is safe.
- Proxy inspection checks EIP-1967 implementation/admin/beacon slots and
  EIP-1167 minimal proxies, including implementation/admin/beacon addresses when
  exposed. A detected proxy and recognized high-risk controls fail their
  respective scored checks. Verified ABIs are scanned for owner status and
  upgrade, pause, blacklist, metadata, sale, price, supply, role, and royalty
  setters. Custom proxy patterns and unverified source may not be detected, and
  source verification does not establish trustworthiness.
- Metadata JSON is fetched with a 6-second timeout and a 512 KiB limit.
  Redirects are rejected; external HTTPS hosts are rejected if DNS resolves to
  non-public IP addresses. The first referenced image/asset is checked using a
  1 KiB range request, with redirects and non-media content types rejected.
  Metadata is not executed, and the scanner does not evaluate artistic quality.
- The quality score is out of seven: metadata, verified source, proxy safety,
  admin controls, recent mint activity, recipient diversity, and supply/royalty
  signals. Recent activity passes with at least one mint event in the last
  1,000 blocks; diversity passes with at least two distinct recipient wallets;
  supply/royalties passes with a declared cap of 5,555 or fewer or ERC-2981
  support. Activity, diversity, and supply/royalty signals can be manipulated
  and are not security guarantees.
- `AUTO_MINT_THRESHOLD` sets the minimum score from 0 to 7 and defaults to 6.
  Any combination of the seven scored checks can meet the threshold, including
  when one or more core checks fail. Required ownership, access-gate,
  live-condition, and cost checks remain mandatory regardless of the threshold.
  Wallet-preview risk details or authority changes still block automatic
  execution.
- A confirmed transaction must emit an ERC-721 `Transfer` or ERC-1155 transfer
  event from the zero address to the configured wallet at the scanned contract.
  The verified transaction and token IDs are stored in the `verified_mints`
  database table.
- Existing queued records from older versions are not trusted. Database
  migration marks them unapproved; rescan the contract to qualify it under the
  current safety checks.

## RPC log limits

Some BNB Chain RPC providers cap or disable historical `eth_getLogs` queries.
The monitor responds to recognized range/result-limit errors by shrinking block
ranges and splitting queries by NFT event and wallet direction. If even one
block is rejected, it falls back to the [Etherscan API V2 ERC-721](https://docs.etherscan.io/api-reference/endpoint/tokennfttx)
and [ERC-1155](https://docs.etherscan.io/api-reference/endpoint/token1155tx)
transfer-history endpoints, paginating the wallet's transfers for the remaining
block range. Set `BSC_HISTORY_API_KEY`, or reuse `ETHERSCAN_API_KEY`, to enable
this fallback. The wallet address and requested block range are sent to
Etherscan when this fallback is used.

If neither the RPC nor the history API can complete the query, `/scannft`
reports the error and the scan checkpoint is not advanced for the failed range.
You can also configure `BSC_HTTP_URL` to a BNB Chain RPC provider that supports
historical `eth_getLogs`. [BNB Chain's public endpoint documentation](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/json-rpc-endpoint/)
notes that `eth_getLogs` is disabled on its listed mainnet public dataseed
endpoints; it lists third-party providers for log access.

## Data and logs

- `agentic_mints.db` stores candidate mint status, scan checkpoints, current
  wallet holdings, and verified mint transaction/token IDs. It is created
  automatically and is excluded from Git.
- Service diagnostics are written to standard output. When using `runner.py`,
  monitor the runner output for scanner and worker errors.
- Do not delete the database to troubleshoot RPC errors; doing so loses the
  holdings scan checkpoint and causes the historical scan to start again.

## Troubleshooting

| Symptom | What to check |
|---|---|
| `/scannft` gets no response | Confirm `execution_worker.py` is running, the bot token and chat ID are correct, and no webhook or other long-polling process is using the token. |
| NFT monitor reports `limit exceeded` | Configure `BSC_HISTORY_API_KEY` (or `ETHERSCAN_API_KEY`) for indexed NFT-history fallback, or set `BSC_HTTP_URL` to a provider that supports historical `eth_getLogs`; retry `/scannft`. |
| Live mint alerts do not arrive | Check `BSC_WSS_URL`, WebSocket availability, and the scanner service logs. |
| Wallet minting is unavailable | Check that `npm ci` succeeded, the wallet is connected, `MY_AGENTIC_WALLET` matches the connected BNB Chain address, and Developer Mode is active. |
| Automatic minting is not eligible | Check all four technical checks, wallet ownership, explicit access-gate evidence, live mint conditions, configured cost policy, and a valid `ETHERSCAN_API_KEY`. |

## Project files

- `nft_telegram_scanner.py` — live mint-event scanner, contract assessment,
  quality scoring, and automatic mint queue.
- `nft_chain_monitor.py` — wallet transfer and holdings-history monitor.
- `execution_worker.py` — Telegram polling, Agentic Wallet integration, mint
  queue execution, and `/scannft`.
- `telegram_nft_features.py` — Telegram contract scan, preview, and
  confirmation workflows.
- `runner.py` — restarts the scanner and worker if either process exits.
- `requirements.txt` and `package.json` — Python and Node.js dependencies.
