import os
import json
import time
import uuid
import math
from datetime import datetime, timezone
from dotenv import load_dotenv

# Webull OpenAPI SDK Imports
from webull.core.client import ApiClient
from webull.trade.trade_client import TradeClient

# Load environment variables
load_dotenv()
APP_KEY = os.getenv("WEBULL_APP_KEY")
APP_SECRET = os.getenv("WEBULL_APP_SECRET")
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

def execute_sell(trade_client, account_id, symbol, qty, reason, legs=None, force_price=None, last_price="0"):
    print(f"\n🚨 EXECUTING SELL: {symbol} | Qty: {qty} | Reason: {reason}")
    try:
        client_order_id = uuid.uuid4().hex
        
        # Determine Limit Price (Either forced by the 5-min step down, or initial calculation)
        if force_price is not None:
            adjusted_price = force_price
        else:
            price_val = float(last_price) if last_price else 0.0
            target = price_val * 0.85
            tick = 0.05 if target < 3.00 else 0.10
            adjusted_price = math.floor(target / tick) * tick
            
        # Absolute minimum tick to pass Webull validation
        if adjusted_price < 0.05:
            adjusted_price = 0.05
            
        lmt_price_str = f"{adjusted_price:.2f}"
        print(f"   ↳ Generating Marketable Limit Order at ${lmt_price_str} (Last Price: ${last_price})")
        
        # Build the base order
        order = {
            "combo_type": "NORMAL",
            "client_order_id": client_order_id,
            "symbol": symbol,
            "instrument_type": "OPTION", 
            "option_strategy": "SINGLE",
            "market": "US",
            "order_type": "LIMIT",
            "limit_price": lmt_price_str,
            "quantity": str(qty),
            "side": "SELL",
            "time_in_force": "DAY",
            "entrust_type": "QTY"
        }
        
        # Attach the required options legs data
        if legs:
            formatted_legs = []
            for pos_leg in legs:
                formatted_legs.append({
                    "side": "SELL",
                    "quantity": str(qty),
                    "symbol": pos_leg.get("symbol", symbol),
                    "strike_price": str(pos_leg.get("option_exercise_price") or pos_leg.get("strike_price")),
                    "option_expire_date": pos_leg.get("option_expire_date"),
                    "instrument_type": "OPTION",
                    "option_type": pos_leg.get("option_type"),
                    "market": "US"
                })
            order["legs"] = formatted_legs
            
        new_orders = [order]
        
        response = trade_client.order_v3.place_order(account_id, new_orders)
        if response.status_code == 200:
            print(f"   ↳ ✅ Sell order placed successfully for {symbol}")
            return True, client_order_id, adjusted_price
        else:
            print(f"   ↳ ❌ Failed to place sell order. Code {response.status_code}: {response.text}")
            return False, None, None
            
    except Exception as e:
        print(f"   ↳ ❌ Error placing sell order for {symbol}: {e}")
        return False, None, None

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

            print(f"Discovered {len(accounts)} linked account(s):")
            for acc in accounts:
                acc_num = acc.get("account_number") or acc.get("account_id")
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
                raw_legs = pos.get("legs", [])
                last_price = pos.get("last_price", "0")
                
                if not sym or float(qty) <= 0:
                    continue
                    
                pnl_str = pos.get("unrealized_profit_loss_rate", 0)
                pnl_pct = float(pnl_str) * 100 if pnl_str else 0.0
                
                active_symbols.append(sym)

                # 1. Register new trades (and attach the new pending_sell flags)
                if sym not in state:
                    state[sym] = {
                        "entry_time": current_time,
                        "high_water_mark": pnl_pct,
                        "breakeven_locked": False,
                        "trail_active": False,
                        "pending_sell": False,
                        "sell_time": 0,
                        "client_order_id": "",
                        "current_limit": 0.0,
                        "legs": raw_legs
                    }
                    print(f"📈 New position detected: {sym} ({qty} contracts) at {pnl_pct:.2f}% PnL. Timer started.")
                    continue

                trade = state[sym]
                
                # Update Legs in case they were missing on init
                trade["legs"] = raw_legs
                
                # --- DYNAMIC CANCEL/REPLACE ENGINE ---
                if trade.get("pending_sell"):
                    minutes_pending = (current_time - trade["sell_time"]) / 60
                    
                    if minutes_pending >= 5.0:
                        print(f"\n⏳ 5-Minute Timeout: {sym} hasn't filled. Cancelling and dropping price...")
                        
                        # 1. Cancel the old order
                        try:
                            trade_client.order_v3.cancel_order(active_account_id, trade["client_order_id"])
                            print(f"   ↳ ✅ Successfully requested cancel for {sym}")
                        except Exception as ce:
                            print(f"   ↳ ⚠️ Cancel attempt logged (Sandbox may auto-clear): {ce}")
                        
                        time.sleep(2)  # Give Webull 2 seconds to unlock the shares
                        
                        # 2. Step down the limit price by $0.05
                        new_limit = round(trade["current_limit"] - 0.05, 2)
                        if new_limit < 0.05:
                            new_limit = 0.05
                            
                        # 3. Re-issue the sell order
                        success, cid, limit_set = execute_sell(
                            trade_client, active_account_id, sym, qty, "Dynamic 5-Min Price Drop", trade["legs"], force_price=new_limit
                        )
                        
                        if success:
                            trade["sell_time"] = current_time
                            trade["client_order_id"] = cid
                            trade["current_limit"] = limit_set
                            
                    # Skip the rest of the rule checks for this symbol since we are already selling it
                    continue

                # Update tracking variables for healthy positions
                minutes_elapsed = (current_time - trade["entry_time"]) / 60
                if pnl_pct > trade["high_water_mark"]:
                    trade["high_water_mark"] = pnl_pct

                if pnl_pct >= 30.0 and not trade["breakeven_locked"]:
                    trade["breakeven_locked"] = True
                    print(f"🔒 {sym} reached +30.00% (PnL: {pnl_pct:.2f}%). Breakeven stop locked.")
                if pnl_pct >= 50.0 and not trade["trail_active"]:
                    trade["trail_active"] = True
                    print(f"🎯 {sym} reached +50.00% (PnL: {pnl_pct:.2f}%). Dynamic 20% trail activated.")

                # --- CENTRALIZED RULE CHECKER ---
                sell_reason = ""
                if pnl_pct <= -40.0:
                    sell_reason = f"Hard Stop -40% hit (Current: {pnl_pct:.2f}%)"
                elif 30 <= minutes_elapsed < 31 and pnl_pct <= -40.0:
                    sell_reason = f"30-Min Rule: Cut at {pnl_pct:.2f}%"
                elif trade["breakeven_locked"] and pnl_pct <= 0.0:
                    sell_reason = f"Breakeven Stop Hit (PnL: {pnl_pct:.2f}%)"
                elif trade["trail_active"] and pnl_pct <= (trade["high_water_mark"] - 20.0):
                    sell_reason = f"Trailing Stop Hit at {pnl_pct:.2f}% (Peak was {trade['high_water_mark']:.2f}%)"

                # If a rule was violated, place the order and activate pending mode
                if sell_reason:
                    success, cid, limit_set = execute_sell(trade_client, active_account_id, sym, qty, sell_reason, raw_legs, last_price=last_price)
                    if success:
                        trade["pending_sell"] = True
                        trade["sell_time"] = current_time
                        trade["client_order_id"] = cid
                        trade["current_limit"] = limit_set

            # Clean up positions that successfully sold and disappeared from the account
            for sym in list(state.keys()):
                if sym not in active_symbols:
                    del state[sym]
                    print(f"✅ {sym} successfully sold and cleared from account.")

            save_state(state)

        except Exception as loop_err:
            print(f"⚠️ Transient error during polling cycle: {loop_err}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run_watcher()