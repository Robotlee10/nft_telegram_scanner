import os
import time
import re
import requests
import urllib.parse

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

def send_telegram_alert(contract_address):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    message = (
        "🚨 **New Free Mint Detected!** 🚨\n\n"
        f"**Contract Address:**\n`{contract_address}`\n\n"
        "👉 _Use your Binance Agentic Wallet MCP server to execute this mint._"
    )
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
        print(f"[+] Alert sent to Telegram: {contract_address}")
    except Exception as e:
        print(f"[-] Failed to send Telegram message: {e}")

def scan_via_google_feed():
    """Queries Google using direct regex extraction to completely eliminate XML parsing crashes."""
    print("[*] Pulling query updates from Google Index Feeds...")
    
    base_url = "https://google.com"
    query_string = 'site:binance.com "agentid" "0x"'
    
    params = {
        "q": query_string,
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en"
    }
    
    google_url = f"{base_url}?{urllib.parse.urlencode(params)}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    
    try:
        response = requests.get(google_url, headers=headers, timeout=15)
        
        if response.status_code != 200:
            print(f"[-] Google Feed Pending: Status {response.status_code}.")
            return

        # Use an unanchored regex match to scan the raw content stream directly
        contract_pattern = re.compile(r"0x[a-fA-F0-9]{40}")
        found_addresses = contract_pattern.findall(response.text)
        
        if not found_addresses:
            print("[*] Connection Stable (Status 200). Waiting for new community posts.")
            return

        # Filter duplicates and notify
        unique_addresses = set(found_addresses)
        new_discoveries = 0
        
        for contract_address in unique_addresses:
            if contract_address not in ALREADY_NOTIFIED_CONTRACTS:
                ALREADY_NOTIFIED_CONTRACTS.add(contract_address)
                send_telegram_alert(contract_address)
                new_discoveries += 1
                
        print(f"[+] Scan iteration complete. Processed {new_discoveries} new unique address layouts.")
                    
    except Exception as e:
        print(f"[-] Scan tracking failure: {e}")

if __name__ == "__main__":
    print("[➔] Starting Shielded Free Mint Feed Engine.")
    if verify_env_variables():
        print("[➔] Monitoring mode active.")
        while True:
            scan_via_google_feed()
            time.sleep(SCAN_INTERVAL_SECONDS)
