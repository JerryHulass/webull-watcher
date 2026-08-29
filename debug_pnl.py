import os
import json
from dotenv import load_dotenv
from webull.core.client import ApiClient
from webull.trade.trade_client import TradeClient

load_dotenv()
APP_KEY = os.getenv("WEBULL_APP_KEY")
APP_SECRET = os.getenv("WEBULL_APP_SECRET")
TARGET_ACCOUNT = os.getenv("WEBULL_ACCOUNT_NUMBER") or "DEA5JKN2"

api_client = ApiClient(APP_KEY, APP_SECRET, "us")
api_client.add_endpoint("us", "api.sandbox.webull.com")
trade_client = TradeClient(api_client)

print("Fetching raw API positions...")
accounts_data = trade_client.account_v2.get_account_list().json()
accounts = accounts_data if isinstance(accounts_data, list) else accounts_data.get("data", accounts_data.get("accounts", []))

active_id = None
for acc in accounts:
    if TARGET_ACCOUNT in str(acc):
        active_id = str(acc.get("account_id"))
        break

if active_id:
    res = trade_client.account_v2.get_account_position(active_id)
    data = res.json()
    
    # Safely handle the list format Webull just threw at us
    positions = data.get("positions", []) if isinstance(data, dict) else data
    
    print(json.dumps(positions[:2], indent=2))
else:
    print("Could not link account.")