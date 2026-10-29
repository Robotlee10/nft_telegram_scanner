import sys
import json
import asyncio
import httpx
from pathlib import Path

SESSION_FILE = Path(".agentic_session.json")

async def pair():
    """Requests a pairing URL from the Binance Agentic Wallet API."""
    async with httpx.AsyncClient() as client:
        try:
            # Connect to the Binance Agentic Wallet session endpoint
            res = await client.post("https://agentic.binance.com/api/v1/session/create", json={"client": "cloud_bot"}, timeout=10.0)
            if res.status_code == 200:
                print(json.dumps(res.json()))
            else:
                print(json.dumps({"pairing_url": "https://agentic.binance.com/pair"}))
        except Exception as e:
            print(json.dumps({"pairing_url": "https://agentic.binance.com/pair", "error": str(e)}))

async def write(contract: str, func_name: str, value: int):
    """Triggers the contract execution using the saved session token."""
    if not SESSION_FILE.exists():
        print("Error: No active session file found.")
        return

    with open(SESSION_FILE, "r") as f:
        session_data = json.load(f)

    token = session_data.get("token")
    async with httpx.AsyncClient() as client:
        payload = {
            "token": token,
            "chain_id": 56,
            "contract": contract,
            "function": func_name,
            "value": str(value)
        }
        res = await client.post("https://agentic.binance.com/api/v1/tx/execute", json=payload, timeout=15.0)
        print(res.text)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "pair":
        asyncio.run(pair())
    elif len(sys.argv) > 4 and sys.argv[1] == "write":
        contract_arg = sys.argv[sys.argv.index("--contract") + 1]
        func_arg = sys.argv[sys.argv.index("--func") + 1]
        value_arg = int(sys.argv[sys.argv.index("--value") + 1])
        asyncio.run(write(contract_arg, func_arg, value_arg))
