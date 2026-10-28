import os
import time
import re
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup

# SECURE CONFIGURATION
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SCAN_INTERVAL_SECONDS = 60  # Check every 60 seconds for near real-time alerts

# TARGET CONFIGURATION - Replace these example handles with your favorite callers
TARGET_X_ACCOUNTS = ["Airdrop_Farming", "Crypto_Alerts_AI"]  # Public X usernames
TARGET_TELEGRAM_CHANNELS = ["robotlee_10group"]  # Public Telegram channel slugs

ALREADY_NOTIFIED_CONTRACTS = set()

def verify_env_variables():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("[-] CRITICAL ERROR: Missing environment variables!")
        return False
    return True

def send_telegram_alert(contract_address, source, platform):
    url = f"https://telegram.org{TELEGRAM_TOKEN}/sendMessage"
    message = (
        f"🚨 **INSTANT MINT DETECTED!** 🚨\n\n"
        f"**Platform:** {platform}\n"
        f"**Source:** @{source}\n\n"
        f"**Contract Address:**\n`{contract_address}`\n\n"
        "⚡ _Hurry! Use your Binance Agentic Wallet MCP server to execute this mint._"
    )
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
        print(f"[+] Alert sent to Telegram: {contract_address} from {source} ({platform})")
    except Exception as e:
        print(f"[-] Failed to send Telegram message: {e}")

def scan_x_via_nitter(username):
    """Scans public X accounts instantly using a stable open Nitter proxy."""
    print(f"[*] Checking X timeline for @{username}...")
    
    # We use a reliable public Nitter instance that serves instant RSS feeds
    nitter_url = f"https://privacydev.net{username}/rss"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    try:
        response = requests.get(nitter_url, headers=headers, timeout=15)
        if response.status_code != 200:
            print(f"[-] Nitter instance returned status {response.status_code} for @{username}")
            return

        # Nitter feeds use standard XML RSS formatting
        root = ET.fromstring(response.text)
        items = root.findall('.//item')
        
        contract_pattern = re.compile(r"0x[a-fA-F0-9]{40}")
        
        for item in items:
            title = item.find('title').text if item.find('title') is not None else ""
            desc = item.find('description').text if item.find('description') is not None else ""
            combined_text = f"{title} {desc}".lower()
            
            # Match parameters for free agentic mints
            if "mint" in combined_text or "free" in combined_text or "agentic" in combined_text:
                match = contract_pattern.search(combined_text)
                if match:
                    contract_address = match.group(0)
                    if contract_address not in ALREADY_NOTIFIED_CONTRACTS:
                        ALREADY_NOTIFIED_CONTRACTS.add(contract_address)
                        send_telegram_alert(contract_address, username, "X (Twitter)")
                        
    except Exception as e:
        print(f"[-] Failed to scan X user @{username}: {e}")

def scan_telegram_preview(channel_name):
    """Scans public Telegram channel message histories instantly via Web Previews."""
    print(f"[*] Checking Telegram Channel @{channel_name}...")
    
    # Simple open-source endpoint layout for public channels
    tme_url = f"https://t.me{channel_name}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    try:
        response = requests.get(tme_url, headers=headers, timeout=15)
        if response.status_code != 200:
            print(f"[-] Telegram preview returned status {response.status_code} for @{channel_name}")
            return

        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Grab text directly from Telegram message blocks
        message_elements = soup.find_all(class_="tgme_widget_message_text")
        contract_pattern = re.compile(r"0x[a-fA-F0-9]{40}")
        
        for element in message_elements:
            message_text = element.get_text()
            lowered_text = message_text.lower()
            
            if "mint" in lowered_text or "free" in lowered_text or "agent" in lowered_text:
                match = contract_pattern.search(message_text)
                if match:
                    contract_address = match.group(0)
                    if contract_address not in ALREADY_NOTIFIED_CONTRACTS:
                        ALREADY_NOTIFIED_CONTRACTS.add(contract_address)
                        send_telegram_alert(contract_address, channel_name, "Telegram Channel")
                        
    except Exception as e:
        print(f"[-] Failed to scan Telegram channel @{channel_name}: {e}")

if __name__ == "__main__":
    print("[➔] Starting Instant Multi-Engine Free Mint Monitor.")
    if verify_env_variables():
        print(f"[➔] Real-time engine active. Cycling every {SCAN_INTERVAL_SECONDS} seconds.")
        while True:
            # 1. Process X Targets
            for x_user in TARGET_X_ACCOUNTS:
                scan_x_via_nitter(x_user)
                time.sleep(2)  # Avoid flooding cloud nodes
                
            # 2. Process Telegram Targets
            for tg_channel in TARGET_TELEGRAM_CHANNELS:
                scan_telegram_preview(tg_channel)
                time.sleep(2)
                
            print("[*] Scan loop complete. Standing by for updates...\n")
            time.sleep(SCAN_INTERVAL_SECONDS)
