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

def send_telegram_alert(contract_address, platform_name):
    # Includes your verified working Telegram endpoint format
    url = f"https://telegram.org{TELEGRAM_TOKEN}/sendMessage"
    
    message = (
        f"🚨 **New Free Mint Detected on {platform_name}!** 🚨\n\n"
        f"**Contract Address:**\n`{contract_address}`\n\n"
        "👉 _Use your Binance Agentic Wallet MCP server to execute this mint._"
    )
    
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
        print(f"[+] Alert sent to Telegram: {contract_address} ({platform_name})")
    except Exception as e:
        print(f"[-] Failed to send Telegram message: {e}")

def query_google_index(site_domain, platform_label):
    """Queries Google's index for a specific domain to scan text safely."""
    print(f"[*] Pulling query updates for {platform_label} via Google Index...")
    
    base_url = "https://google.com"
    # Target specific site, agentid community tag, and hex contract layouts
    query_string = f'site:{site_domain} "agentid" "0x"'
    
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
            print(f"[-] {platform_label} Feed Pending: Status {response.status_code}.")
            return

        # Scan text block smoothly via regex
        contract_pattern = re.compile(r"0x[a-fA-F0-9]{40}")
        found_addresses = contract_pattern.findall(response.text)
        
        if not found_addresses:
            print(f"[*] {platform_label} Connection Stable (Status 200). No new addresses found.")
            return

        unique_addresses = set(found_addresses)
        new_discoveries = 0
        
        for contract_address in unique_addresses:
            if contract_address not in ALREADY_NOTIFIED_CONTRACTS:
                ALREADY_NOTIFIED_CONTRACTS.add(contract_address)
                send_telegram_alert(contract_address, platform_label)
                new_discoveries += 1
                
        if new_discoveries > 0:
            print(f"[+] Found {new_discoveries} new address layouts on {platform_label}.")
                    
    except Exception as e:
        print(f"[-] {platform_label} scan tracking failure: {e}")

if __name__ == "__main__":
    print("[➔] Starting Shielded Dual-Feed Free Mint Engine.")
    if verify_env_variables():
        print("[➔] Monitoring mode active.")
        while True:
            # Step 1: Scan Binance Square
            query_google_index("binance.com", "Binance Square")
            
            # Small delay to keep network calls cleanly separated
            time.sleep(5) 
            
            # Step 2: Scan X (Twitter)
            query_google_index("x.com", "X (Twitter)")
            
            # Wait for next check cycle
            time.sleep(SCAN_INTERVAL_SECONDS)
