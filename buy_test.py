import os
import uuid
import json
from dotenv import load_dotenv
from webull.core.client import ApiClient
from webull.trade.trade_client import TradeClient

# Load credentials
load_dotenv()
APP_KEY = os.getenv("WEBULL_APP_KEY")
APP_SECRET = os.getenv("WEBULL_APP_SECRET")
TARGET_ACCOUNT = os.getenv("WEBULL_ACCOUNT_NUMBER") or "DEA5JKN2"

def place_mock_order():
    api_client = ApiClient(APP_KEY, APP_SECRET, "us")
    api_client.add_endpoint("us", "api.sandbox.webull.com")
    trade_client = TradeClient(api_client)
    
    print("Linking account...")
    account_res = trade_client.account_v2.get_account_list()
    active_account_id = None
    if account_res.status_code == 200:
        accounts = account_res.json()
        accounts_list = accounts if isinstance(accounts, list) else accounts.get("data", accounts.get("accounts", []))
        for acc in accounts_list:
            if TARGET_ACCOUNT in str(acc):
                active_account_id = str(acc.get("account_id"))
                break
                
    if not active_account_id:
        print(f"❌ Could not find {TARGET_ACCOUNT}")
        return

    print("✅ Account linked! Placing mock BUY order for 1 share of F (Ford)...")
    
    # 2. Build the Buy Order Payload
    client_order_id = uuid.uuid4().hex
    new_orders = [
        {
            "combo_type": "NORMAL",
            "client_order_id": client_order_id,
            "symbol": "F", 
            "instrument_type": "EQUITY", # Buying Stock for the test
            "market": "US",
            "order_type": "MKT",
            "quantity": "1",
            "support_trading_session": "CORE",
            "side": "BUY",
            "time_in_force": "DAY",
            "entrust_type": "QTY"
        }
    ]
    
    # 3. Submit to the Sandbox
    response = trade_client.order_v3.place_order(active_account_id, new_orders)
    
    if response.status_code == 200:
        print("✅ SUCCESS! Order submitted to Webull Sandbox.")
        print(json.dumps(response.json(), indent=2))
    else:
        print(f"❌ Failed to place order. Code {response.status_code}: {response.text}")

if __name__ == "__main__":
    place_mock_order()