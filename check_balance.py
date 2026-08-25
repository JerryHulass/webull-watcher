import os
from dotenv import load_dotenv
from webull.core.client import ApiClient
from webull.trade.trade_client import TradeClient

# Load credentials
load_dotenv()
APP_KEY = os.getenv("WEBULL_APP_KEY")
APP_SECRET = os.getenv("WEBULL_APP_SECRET")

# Connect to Sandbox
api_client = ApiClient(APP_KEY, APP_SECRET, "us")
api_client.add_endpoint("us", "api.sandbox.webull.com")
trade_client = TradeClient(api_client)

print("Authenticating...")

try:
    # 1. Fetch Account ID
    account_res = trade_client.account_v2.get_account_list()
    if account_res.status_code == 200:
        accounts_data = account_res.json()
        accounts = accounts_data if isinstance(accounts_data, list) else accounts_data.get("data", accounts_data.get("accounts", []))
        
        if accounts:
            active_account_id = str(accounts[0].get("account_id"))
            print(f"✅ Successfully linked to Account ID: {active_account_id}")
            
            # 2. Fetch Paper Trading Balance
            balance_res = trade_client.account_v2.get_account_balance(active_account_id)
            if balance_res.status_code == 200:
                print(f"✅ Connection Verified! Paper Trading Balance Data:")
                print(balance_res.json())
            else:
                print("❌ Failed to fetch balance.")
        else:
            print("❌ No accounts found.")
except Exception as e:
    print(f"Exception: {e}")