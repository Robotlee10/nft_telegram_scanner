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

def scan_binance_square_api():
    """Queries the backend Binance API directly to bypass JavaScript/202 issues."""
    print("[*] Contacting Binance Square API...")
    
    # Direct internal API endpoint for hashtag feeds
    api_url = "https://binance.com"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Content-Type": "application/json",
        "Lang": "en"
    }
    
    # JSON body structure mimicking what the frontend queries
    payload = {
        "hashtag": "agentid",
        "page": 1,
        "pageSize": 20,
        "sortType": 2  # 2 usually sorts by 'Latest/Newest'
    }
    
    try:
        response = requests.post(api_url, json=payload, headers=headers, timeout=15)
        
        if response.status_code != 200:
            print(f"[-] API Error: Status {response.status_code}")
            return
            
        data = response.json()
        posts = data.get("data", {}).get("list", [])
        
        if not posts:
            print("[*] Feed connected successfully, but no posts found in this block.")
            return

        contract_pattern = re.compile(r"0x[a-fA-F0-9]{40}")
        
        for post in posts:
            # Safely extract text block from the json response
            post_text = post.get("content", "") or ""
            
            if "mint" in post_text.lower() or "free" in post_text.lower():
                match = contract_pattern.search(post_text)
                if match:
                    contract_address = match.group(0)
                    
                    if contract_address not in ALREADY_NOTIFIED_CONTRACTS:
                        ALREADY_NOTIFIED_CONTRACTS.add(contract_address)
                        send_telegram_alert(contract_address, post_text)
                        
    except Exception as e:
        print(f"[-] Scan tracking failure: {e}")

if __name__ == "__main__":
    print("[➔] Starting Binance Agentic ID Free Mint API Scanner.")
    if verify_env_variables():
        print("[➔] Monitoring mode active.")
        while True:
            scan_binance_square_api()
            time.sleep(SCAN_INTERVAL_SECONDS)
