# NFT Telegram Scanner

A Python service that monitors BNB Chain for NFT mint activity, assesses candidate
contracts, reports wallet NFT activity to Telegram, and optionally mints eligible
NFTs through the Binance Agentic Wallet CLI.

## Features

- Watches BNB Chain ERC-721 and ERC-1155 mint events over WebSocket.
- Checks whether a candidate supports an NFT standard, whether the configured
  wallet can call a supported mint function, and whether a control wallet is
  blocked by the contract's access gate.
- Scores candidates with seven quality checks: recent activity, metadata,
  verified source, minter diversity, ERC-2981 royalties, supply cap, and minter
  history.
- Automatically queues exclusive candidates at 4/7 checks, or at 3/7 when both
  source verification and scarce supply pass. The worker re-previews the call
  before execution.
- Sends Telegram alerts with a manual mint option for candidates that were not
  automatically queued. Manual minting requires a successful preview and a
  separate confirmation.
- Tracks the configured wallet's ERC-721/ERC-1155 holdings and transfers.
- Accepts `/scannft` in the configured Telegram chat to scan wallet history and
  return a holdings report. The initial scan may take a while; subsequent scans
  continue from the last saved block.

The quality score is informational, not investment advice or a guarantee that a
mint will succeed. A manual mint can still incur transaction fees. Automatic mint
execution may submit an on-chain transaction once its configured eligibility
checks and preview pass.

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

# Optional: used to check source-code verification in the quality score.
ETHERSCAN_API_KEY=your-etherscan-api-key

# Optional: used as the comparison wallet for the exclusive mint access test.
CONTROL_WALLET_NO_ID=0x0000000000000000000000000000000000000001
```

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
contract's NFT standard, access gate, mint function, and quality score.

## RPC log limits

Some BNB Chain RPC providers cap the number of blocks or logs returned by
`eth_getLogs`. The holdings monitor responds to recognized range/result-limit
errors by shrinking the block range and retrying. It also queries each NFT event
and wallet direction separately to keep each result set smaller.

If a provider still rejects a query covering a single block, the scan fails
without advancing its saved checkpoint. Configure `BSC_HTTP_URL` to a BNB Chain
RPC provider with a higher historical log-query limit, then run `/scannft` again.
The scanner logs the provider error and the block range it was attempting.

## Data and logs

- `agentic_mints.db` stores candidate mint status, scan checkpoints, and current
  wallet holdings. It is created automatically and is excluded from Git.
- Service diagnostics are written to standard output. When using `runner.py`,
  monitor the runner output for scanner and worker errors.
- Do not delete the database to troubleshoot RPC errors; doing so loses the
  holdings scan checkpoint and causes the historical scan to start again.

## Troubleshooting

| Symptom | What to check |
|---|---|
| `/scannft` gets no response | Confirm `execution_worker.py` is running, the bot token and chat ID are correct, and no webhook or other long-polling process is using the token. |
| NFT monitor reports `limit exceeded` | Set `BSC_HTTP_URL` to a provider with a higher `eth_getLogs` limit and retry `/scannft`. The checkpoint is only advanced after successful ranges. |
| Live mint alerts do not arrive | Check `BSC_WSS_URL`, WebSocket availability, and the scanner service logs. |
| Wallet minting is unavailable | Check that `npm ci` succeeded, the wallet is connected, `MY_AGENTIC_WALLET` matches the connected BNB Chain address, and Developer Mode is active. |
| Quality source check is unavailable | Set a valid `ETHERSCAN_API_KEY`; other on-chain quality checks can still run. |

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
