import os
from dotenv import load_dotenv
from webull.core.client import ApiClient
from webull.trade.trade_client import TradeClient

# Load your App Key and Secret
load_dotenv()
APP_KEY = os.getenv("WEBULL_APP_KEY")
APP_SECRET = os.getenv("WEBULL_APP_SECRET")

# Connect to the Webull Sandbox (Paper Trading)
api_client = ApiClient(APP_KEY, APP_SECRET, "us")
api_client.add_endpoint("us", "api.sandbox.webull.com")
trade_client = TradeClient(api_client)

# Request your account list and print the results
res = trade_client.account_v2.get_account_list()
print("Account Info:", res.json())