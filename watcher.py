import os
import json
import time
import uuid
from datetime import datetime, timezone
from dotenv import load_dotenv

# Webull OpenAPI SDK Imports
from webull.core.client import ApiClient
from webull.trade.trade_client import TradeClient

# Load environment variables
load_dotenv()
APP_KEY = os.getenv("WEBULL_APP_KEY")
APP_SECRET = os.getenv("WEBULL_APP_SECRET")
# Fallback to DEA5JKN2 if not set in .env
TARGET_ACCOUNT = os.getenv("WEBULL_ACCOUNT_NUMBER") or "DEA5JKN2"

# Configuration
STATE_FILE = "webull_trades_state.json"
POLL_INTERVAL = 15  # Seconds between checks

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Could not read state file ({e}). Starting fresh.")
            return {}
    return {}

def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=4)
    except Exception as e:
        print(f"Warning: Failed to save state to file: {e}")

def execute_sell(trade_client, account_id, symbol, qty, reason):
    print(f"\n🚨 EXECUTING SELL: {symbol} | Qty: {qty} | Reason: {reason}")
    try:
        client_order_id = uuid.uuid4().hex
        new_orders = [
            {
                "combo_type": "NORMAL",
                "client_order_id": client_order_id,
                "symbol": symbol,
                "instrument_type": "OPTION", 
                "option_strategy": "SINGLE",
                "market": "US",
                "order_type": "STOP_LOSS",
                "stop_price": "0.01",  # Triggers immediate market fill
                "quantity": str(qty),
                "side": "SELL",
                "time_in_force": "DAY",
                "entrust_type": "QTY"
            }
        ]
        
        response = trade_client.order_v3.place_order(account_id, new_orders)
        if response.status_code == 200:
            print(f"✅ Sell order placed successfully for {symbol}: {response.json()}")
        else:
            print(f"❌ Failed to place sell order. Code {response.status_code}: {response.text}")
            
    except Exception as e:
        print(f"❌ Error placing sell order for {symbol}: {e}")

def get_open_positions(trade_client, account_id):
    try:
        response = trade_client.account_v2.get_account_position(account_id)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict):
                return data.get("positions", [])
            elif isinstance(data, list):
                return data
            return []
        else:
            print(f"⚠️ Error fetching positions: {response.status_code} - {response.text}")
            return []
    except Exception as e:
        print(f"⚠️ Exception fetching positions: {e}")
        return []

def run_watcher():
    if not APP_KEY or not APP_SECRET:
        print("❌ Error: WEBULL_APP_KEY or WEBULL_APP_SECRET missing in .env file.")
        return

    api_client = ApiClient(APP_KEY, APP_SECRET, "us")
    api_client.add_endpoint("us", "api.sandbox.webull.com")
    trade_client = TradeClient(api_client)
    
    print(f"Authenticating and searching for target account: {TARGET_ACCOUNT}...")
    
    active_account_id = None
    try:
        account_res = trade_client.account_v2.get_account_list()
        
        if account_res.status_code == 200:
            accounts_data = account_res.json()
            accounts = accounts_data if isinstance(accounts_data, list) else accounts_data.get("data", accounts_data.get("accounts", []))
            
            if not accounts:
                print("❌ Error: No accounts found attached to these API keys.")
                return

            # Print available accounts and match target
            print(f"Discovered {len(accounts)} linked account(s):")
            for acc in accounts:
                acc_num = acc.get("account_number") or acc.get("account_id")
                print(f"  - Account Number/ID: {acc_num} (Payload: {acc})")
                if TARGET_ACCOUNT in str(acc):
                    active_account_id = str(acc.get("account_id"))

            if active_account_id:
                print(f"✅ Successfully linked to Account {TARGET_ACCOUNT} (Internal ID: {active_account_id})")
            else:
                print(f"\n⚠️ Could not find account matching '{TARGET_ACCOUNT}'.")
                print(f"Defaulting to first available account ID: {accounts[0].get('account_id')}")
                active_account_id = str(accounts[0].get("account_id"))
        else:
            print(f"❌ Failed to fetch account list. Code {account_res.status_code}: {account_res.text}")
            return
            
    except Exception as e:
        print(f"❌ Exception during account linking: {e}")
        return

    print("\nWebull Watcher initialized. Polling positions every 15s...")
    
    while True:
        try:
            state = load_state()
            positions = get_open_positions(trade_client, active_account_id)
            current_time = datetime.now(timezone.utc).timestamp()
            active_symbols = []

            for pos in positions:
                sym = pos.get("symbol")
                qty = pos.get("quantity", 0)
                
                if not sym or float(qty) <= 0:
                    continue
                    
                pnl_str = pos.get("unrealizedProfitLossRate", 0)
                pnl_pct = float(pnl_str) * 100 if pnl_str else 0.0
                
                active_symbols.append(sym)

                # 1. Register new trades
                if sym not in state:
                    state[sym] = {
                        "entry_time": current_time,
                        "high_water_mark": pnl_pct,
                        "breakeven_locked": False,
                        "trail_active": False
                    }
                    print(f"📈 New position detected: {sym} ({qty} contracts) at {pnl_pct:.2f}% PnL. Timer started.")
                    continue

                trade = state[sym]
                minutes_elapsed = (current_time - trade["entry_time"]) / 60
                
                # Track highest unrealized profit percentage
                if pnl_pct > trade["high_water_mark"]:
                    trade["high_water_mark"] = pnl_pct

                # 2. Hard Floor Stop (-40%)
                if pnl_pct <= -40.0:
                    execute_sell(trade_client, active_account_id, sym, qty, f"Hard Stop -40% hit (Current: {pnl_pct:.2f}%)")
                    del state[sym]
                    continue
                    
                # 3. 30-Minute Check
                if 30 <= minutes_elapsed < 31: 
                    if pnl_pct <= -40.0:
                        execute_sell(trade_client, active_account_id, sym, qty, f"30-Min Rule: Cut at {pnl_pct:.2f}%")
                        del state[sym]
                        continue
                    else:
                        print(f"⏱️ 30-Min Check Passed: {sym} holding steady at {pnl_pct:.2f}%")

                # 4. Breakeven Lock at +30%
                if pnl_pct >= 30.0 and not trade["breakeven_locked"]:
                    trade["breakeven_locked"] = True
                    print(f"🔒 {sym} reached +30.00% (PnL: {pnl_pct:.2f}%). Breakeven stop locked.")

                if trade["breakeven_locked"] and pnl_pct <= 0.0:
                    execute_sell(trade_client, active_account_id, sym, qty, f"Breakeven Stop Hit (PnL: {pnl_pct:.2f}%)")
                    del state[sym]
                    continue

                # 5. Dynamic 20% Trailing Stop at +50%
                if pnl_pct >= 50.0 and not trade["trail_active"]:
                    trade["trail_active"] = True
                    print(f"🎯 {sym} reached +50.00% (PnL: {pnl_pct:.2f}%). Dynamic 20% trail activated.")

                if trade["trail_active"]:
                    trailing_stop_level = trade["high_water_mark"] - 20.0
                    if pnl_pct <= trailing_stop_level:
                        execute_sell(
                            trade_client, 
                            active_account_id, 
                            sym, 
                            qty, 
                            f"Trailing Stop Hit at {pnl_pct:.2f}% (Peak was {trade['high_water_mark']:.2f}%)"
                        )
                        del state[sym]
                        continue

            # Cleanup closed positions from local state
            for sym in list(state.keys()):
                if sym not in active_symbols:
                    del state[sym]

            save_state(state)

        except Exception as loop_err:
            print(f"⚠️ Transient error during polling cycle: {loop_err}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run_watcher()