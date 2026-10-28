import time
import re
import requests
from bs4 import BeautifulSoup

# CONFIGURATION
TELEGRAM_TOKEN = 9139372750:AAHlXAulHvu79mc9_mvO2CZVY5iZhSuGe3k
CHAT_ID = 5036813076
SCAN_INTERVAL_SECONDS = 300  # Scan every 5 minutes

# Tracking set to avoid duplicate notifications for the same contract
ALREADY_NOTIFIED_CONTRACTS = set()

def send_telegram_alert(contract_address, raw_text):
    """Sends a formatted notification to your Telegram chat."""
    url = f"https://telegram.org{TELEGRAM_TOKEN}/sendMessage"
    
    message = (
        "🚨 **New Free Mint Detected!** 🚨\n\n"
        f"**Contract Address:**\n`{contract_address}`\n\n"
        f"**Context Found:**\n{raw_text[:200]}...\n\n"
        "👉 _Use your Binance Agentic Wallet MCP server to execute this mint._"
    )
    
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    
    try:
        requests.post(url, json=payload, timeout=10)
        print(f"[+] Alert sent to Telegram for contract: {contract_address}")
    except Exception as e:
        print(f"[-] Failed to send Telegram message: {e}")

def scan_binance_square():
    """Scans the trending AgentID hashtag for contract patterns."""
    target_url = "https://binance.com"
    print("[*] Scanning Binance Square community feeds...")
    
    try:
        response = requests.get(target_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if response.status_code != 200:
            print(f"[-] Error fetching feed: Status {response.status_code}")
            return
            
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Look for typical Ethereum/BSC address formats (0x followed by 40 hex characters)
        contract_pattern = re.compile(r"0x[a-fA-F0-0]{40}")
        
        # Scan through post text blocks or snippets
        # (Adjusting text parsing to look broadly for key triggers)
        for element in soup.find_all(['p', 'span', 'div']):
            text_content = element.get_text().strip()
            
            # Look for mint indicators combined with an address
            if "mint" in text_content.lower() or "free" in text_content.lower():
                match = contract_pattern.search(text_content)
                if match:
                    contract_address = match.group(0)
                    
                    # Ensure we don't spam notifications for the same item
                    if contract_address not in ALREADY_NOTIFIED_CONTRACTS:
                        ALREADY_NOTIFIED_CONTRACTS.add(contract_address)
                        send_telegram_alert(contract_address, text_content)
                        
    except Exception as e:
        print(f"[-] Scan error occurred: {e}")

if __name__ == "__main__":
    print("[➔] Starting Binance Agentic ID Free Mint Scanner.")
    print("[➔] Monitoring mode active. Press Ctrl+C to stop.")
    
    while True:
        scan_binance_square()
        time.sleep(SCAN_INTERVAL_SECONDS)
          
