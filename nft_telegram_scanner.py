import os
import time
import re
import requests
from bs4 import BeautifulSoup

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

def scan_binance_square():
    target_url = "https://binance.com"
    print("[*] Fetching Binance Square feed...")
    
    # Create a persistent session to hold headers and simulate a real browser session
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://google.com",
        "Cache-Control": "max-age=0"
    })
    
    try:
        # Using a timeout to prevent the bot from hanging indefinitely
        response = session.get(target_url, timeout=15)
        
        # Diagnostics to pinpoint exactly why it fails in the cloud dashboard logs
        if response.status_code != 200:
            print(f"[-] Error fetching feed: Status {response.status_code}")
            if response.status_code == 403:
                print("[-] Fix Notice: Cloud IP is blocked by Cloudflare/Binance firewall.")
            return
            
        soup = BeautifulSoup(response.text, 'html.parser')
        contract_pattern = re.compile(r"0x[a-fA-F0-9]{40}")
        
        # Scan broad semantic text nodes
        for element in soup.find_all(['p', 'span', 'div', 'a']):
            text_content = element.get_text().strip()
            
            if "mint" in text_content.lower() or "free" in text_content.lower():
                match = contract_pattern.search(text_content)
                if match:
                    contract_address = match.group(0)
                    
                    if contract_address not in ALREADY_NOTIFIED_CONTRACTS:
                        ALREADY_NOTIFIED_CONTRACTS.add(contract_address)
                        send_telegram_alert(contract_address, text_content)
                        
    except requests.exceptions.Timeout:
        print("[-] Error fetching feed: Connection timed out.")
    except Exception as e:
        print(f"[-] Scan error occurred: {e}")

if __name__ == "__main__":
    print("[➔] Starting Binance Agentic ID Free Mint Scanner.")
    if verify_env_variables():
        print("[➔] Monitoring mode active.")
        while True:
            scan_binance_square()
            time.sleep(SCAN_INTERVAL_SECONDS)
