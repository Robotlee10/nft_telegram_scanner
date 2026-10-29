import sys
import json
import re
import asyncio
import httpx
from pathlib import Path

SESSION_FILE = Path(".agentic_session.json")

async def pair():
    """Fetches or parses the official Binance app.binance.com/uni-qr pairing link."""
    async with httpx.AsyncClient() as client:
        try:
            # Example request to session endpoint
            res = await client.post("https://agentic.binance.com/api/v1/session/create", json={"client": "cloud_bot"}, timeout=10.0)
            if res.status_code == 200:
                data = res.json()
                pairing_url = data.get("pairing_url") or data.get("qr_url")
                
                # Ensure link follows Binance Universal QR format
                if pairing_url and "uni-qr" in pairing_url:
                    print(json.dumps({"pairing_url": pairing_url}))
                    return

            # If raw output contains a uni-qr code string
            raw_text = res.text
            match = re.search(r"https://app\.binance\.com/uni-qr/[a-zA-Z0-9]+", raw_text)
            if match:
                print(json.dumps({"pairing_url": match.group(0)}))
            else:
                print(json.dumps({"error": "No valid uni-qr link found"}))

        except Exception as e:
            print(json.dumps({"error": str(e)}))

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
        
