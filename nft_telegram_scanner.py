import os
import time
import re
import requests
import urllib.parse
import xml.etree.ElementTree as ET

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
    url = f"https://telegram.org{TELEGRAM_TOKEN}/sendMessage"
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
    """Queries Google Index using dictionary-based parameter builds to prevent layout errors."""
    print("[*] Pulling query updates from Google Index Feeds...")
    
    # Define our raw base parameters explicitly to isolate text blocks
    base_url = "https://google.com"
    query_string = 'site:binance.com "agentid" "0x"'
    
    params = {
        "q": query_string,
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en"
    }
    
    # urlencode automatically escapes spaces, symbols, and adds correct parameter indicators (?, &)
    google_url = f"{base_url}?{urllib.parse.urlencode(params)}"
    
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    
    try:
        response = requests.get(google_url, headers=headers, timeout=15)
        
        if response.status_code != 200:
            print(f"[-] Google Feed Pending: Status {response.status_code}.")
            return

        root = ET.fromstring(response.text)
        items = root.findall('.//item')
        
        if not items:
            print("[*] Connection Stable (Status 200). Waiting for new community posts.")
            return

        contract_pattern = re.compile(r"0x[a-fA-F0-9]{40}")
        
        for item in items:
            title_text = item.find('title').text if item.find('title') is not None else ""
            description_text = item.find('description').text if item.find('description') is not None else ""
            link_text = item.find('link').text if item.find('link') is not None else "https://binance.com"
            
            combined_body = f"{title_text} {description_text}"
            
            match = contract_pattern.search(combined_body)
            if match:
                contract_address = match.group(0)
                
                if contract_address not in ALREADY_NOTIFIED_CONTRACTS:
                    ALREADY_NOTIFIED_CONTRACTS.add(contract_address)
                    send_telegram_alert(contract_address, title_text, link_text)
                    
        print("[*] Scan iteration complete. No new unnotified contracts found.")
                    
    except Exception as e:
        print(f"[-] Scan tracking failure: {e}")

if __name__ == "__main__":
    print("[➔] Starting Shielded Free Mint Feed Engine.")
    if verify_env_variables():
        print("[➔] Monitoring mode active.")
        while True:
            scan_via_google_feed()
            time.sleep(SCAN_INTERVAL_SECONDS)
