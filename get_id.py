import os
import json
from dotenv import load_dotenv

import webull
webull.__version__ = "1.0.0"

from webull.core.client import ApiClient
from webull.trade.trade_client import TradeClient

load_dotenv()
app_key = os.getenv("WEBULL_APP_KEY")
app_secret = os.getenv("WEBULL_APP_SECRET")

api_client = ApiClient(app_key, app_secret, "us")
api_client.add_endpoint("us", "api.sandbox.webull.com")
trade_client = TradeClient(api_client)

print("Fetching your real Paper Trading accounts...")
response = trade_client.account_v2.get_account_list()

print("\n✅ SUCCESS! Here is the data inside:")
# This opens the response and prints it neatly!
data = response.json() 
print(json.dumps(data, indent=2))