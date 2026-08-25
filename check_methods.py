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

# Print all available methods in account_v2
print("Available methods in AccountV2:")
methods = [method for method in dir(trade_client.account_v2) if not method.startswith('_')]
for m in methods:
    print(f"- {m}")