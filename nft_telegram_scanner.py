import os
import time
import re
import requests

# SECURE CONFIGURATION
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SCAN_INTERVAL_SECONDS = 300  # 5 minutes

ALREADY_NOTIFIED_CONTRACTS = set()

def verify_env_variables():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("[-] CRITICAL ERROR: Missing environment variables!")
        return False
    return True

def send_telegram_alert(contract_address, raw_text):
    url = f"https://telegram.org{TELEGRAM_TOKEN}/sendMessage"
    message = (
        "🚨 **New Free Mint Detected!** 🚨\n\n"
        f"**Contract Address:**\n`{contract_address}`\n\n"
        f"**Context Found:**\n{raw_text[:200]}...\n\n"
        "👉 _Use your Binance Agentic Wallet MCP server to execute this mint._"
    )
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
        print(f"[+] Alert sent to Telegram: {contract_address}")
    except Exception as e:
        print(f"[-] Failed to send Telegram message: {e}")

def scan_via_mirror():
    """Pulls feed data using a public RSS-to-JSON bridge to avoid 202 firewall blocks."""
    print("[*] Reading decentralized feed mirror...")
    
    # We use an open RSS bridge format that extracts clean, public Binance Square feed strings
    rss_url = "https://rss2json.com"
    
    try:
        response = requests.get(rss_url, timeout=15)
        
        if response.status_code != 200:
            print(f"[-] Mirror Feed Failed: Status {response.status_code}")
            return
            
        data = response.json()
        items = data.get("items", [])
        
        if not items:
            print("[*] Connected to feed! No new items in this window.")
            return

        contract_pattern = re.compile(r"0x[a-fA-F0-9]{40}")
        
        for item in items:
            # Check body text and titles for triggers
            content = f"{item.get('title', '')} {item.get('description', '')}"
            
            # Target AgentID community keyword flags
            if "agentid" in content.lower() or "mint" in content.lower():
                match = contract_pattern.search(content)
                if match:
                    contract_address = match.group(0)
                    
                    if contract_address not in ALREADY_NOTIFIED_CONTRACTS:
                        ALREADY_NOTIFIED_CONTRACTS.add(contract_address)
                        send_telegram_alert(contract_address, content)
                        
    except Exception as e:
        print(f"[-] Mirror tracking error: {e}")

if __name__ == "__main__":
    print("[➔] Starting Alternative Free Mint Scanner.")
    if verify_env_variables():
        print("[➔] Monitoring mode active.")
        while True:
            scan_via_mirror()
            time.sleep(SCAN_INTERVAL_SECONDS)
