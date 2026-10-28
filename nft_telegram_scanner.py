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

def send_telegram_alert(contract_address, title, source_link):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    message = (
        "🚨 **New Free Mint Detected!** 🚨\n\n"
        f"**Contract Address:**\n`{contract_address}`\n\n"
        f"**Context:** {title}\n\n"
        f"🔗 [View Source Link]({source_link})\n\n"
        "👉 _Use your Binance Agentic Wallet MCP server to execute this mint._"
    )
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown", "disable_web_page_preview": True}
    try:
        requests.post(url, json=payload, timeout=10)
        print(f"[+] Alert sent to Telegram: {contract_address}")
    except Exception as e:
        print(f"[-] Failed to send Telegram message: {e}")

def scan_via_google_feed():
    """Queries Google's indexing cluster to grab web data completely bypassing firewalls."""
    print("[*] Pulling query updates from Google Index Feeds...")
    
    # 1. Properly target keywords
    search_query = 'site:binance.com "agentid" "0x"'
    encoded_query = urllib.parse.quote(search_query)
    
    # 2. Build the Google RSS link
    google_feed_url = f"https://google.com{encoded_query}&hl=en-US&gl=US&ceid=US:en"
    
    # 3. Cleanly encode the link to pass into the converter api
    encoded_google_url = urllib.parse.quote(google_feed_url, safe='')
    bridge_url = f"https://rss2json.com{encoded_google_url}"
    
    try:
        # Pass a standard browser header to keep the endpoint connection fresh
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        response = requests.get(bridge_url, headers=headers, timeout=15)
        
        if response.status_code != 200:
            print(f"[-] Feed Stream Pending: Status {response.status_code}. Retrying next cycle.")
            return
            
        data = response.json()
        items = data.get("items", [])
        
        if not items:
            print("[*] Connection Stable (Status 200). Waiting for new community posts.")
            return

        # Pattern to pull out the 40-character BSC/ERC-20 hex wallet/contract format
        contract_pattern = re.compile(r"0x[a-fA-F0-9]{40}")
        
        for item in items:
            title_text = item.get("title", "")
            description_text = item.get("content", "") or item.get("description", "")
            combined_body = f"{title_text} {description_text}"
            
            match = contract_pattern.search(combined_body)
            if match:
                contract_address = match.group(0)
                link = item.get("link", "https://binance.com")
                
                if contract_address not in ALREADY_NOTIFIED_CONTRACTS:
                    ALREADY_NOTIFIED_CONTRACTS.add(contract_address)
                    send_telegram_alert(contract_address, title_text, link)
                    
    except Exception as e:
        print(f"[-] Scan tracking failure: {e}")

if __name__ == "__main__":
    print("[➔] Starting Shielded Free Mint Feed Engine.")
    if verify_env_variables():
        print("[➔] Monitoring mode active.")
        while True:
            scan_via_google_feed()
            time.sleep(SCAN_INTERVAL_SECONDS)
