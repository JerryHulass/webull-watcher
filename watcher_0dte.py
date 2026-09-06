import os
import time
import uuid
import mss
import pytesseract
import cv2
import numpy as np
from datetime import datetime, timezone
from dotenv import load_dotenv

# --- IMPORT YOUR EXISTING WEBULL FUNCTIONS ---
from watcher import execute_sell, get_open_positions

# --- SUPABASE INITIALIZATION ---
from supabase import create_client, Client

load_dotenv()
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_KEY")

# Initialize DB connection only if credentials exist
if supabase_url and supabase_key:
    supabase: Client = create_client(supabase_url, supabase_key)
    print("✅ Supabase Real-Time DB Connected.")
else:
    supabase = None
    print("⚠️ Supabase credentials missing. DB logging disabled.")


def log_signal_to_db(ticker, opt_type, raw_text):
    """Fires the raw signal data to the cloud"""
    if not supabase: return
    try:
        supabase.table("signals").insert({
            "ticker": ticker,
            "option_type": opt_type,
            "raw_text": raw_text
        }).execute()
    except Exception as e:
        print(f"DB Error (Signal): {e}")


def log_trade_to_db(ticker, action, qty, price, reason, pnl_pct=0.0):
    """Fires the actual Webull execution to the cloud"""
    if not supabase: return
    try:
        supabase.table("trades").insert({
            "ticker": ticker,
            "action": action,
            "qty": qty,
            "price": price,
            "reason": reason,
            "pnl_pct": pnl_pct
        }).execute()
    except Exception as e:
        print(f"DB Error (Trade): {e}")


# --- CONFIGURATION ---
ALLOWED_TICKERS = ["SPY", "QQQ", "IWM"]
TARGET_PREMIUM_MIN = 0.90
TARGET_PREMIUM_MAX = 1.10


# --- DISCORD OCR ENGINE ---
def watch_discord_screen(last_processed_text):
    """Captures the screen and scans for Diamond signals."""
    with mss.MSS() as sct:
        monitor = {"top": 120, "left": 50, "width": 1200, "height": 750}
        img = np.array(sct.grab(monitor))
        gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
        
        # Convert all text to uppercase immediately for easier matching
        text = pytesseract.image_to_string(gray).upper()
        
        # 1. Anti-Spam: Don't process the exact same screen text twice
        if text == last_processed_text or text.strip() == "":
            return None, None, text

        # 2. Filter for Quality: Must be Diamond, must NOT be Rough
        if "DIAMOND" in text and "ROUGH" not in text:
            # 3. Check if it is one of our allowed tickers (SPY, QQQ, IWM)
            for ticker in ALLOWED_TICKERS:
                if f" {ticker} " in text or f"{ticker} AT" in text:
                    # 4. Translate Long/Short to Call/Put based on your signal format
                    if "LONG" in text:
                        return ticker, "CALL", text
                    elif "SHORT" in text:
                        return ticker, "PUT", text
                        
        return None, None, text


# --- BUY EXECUTION ENGINE ---
def get_target_option_contract(ticker, opt_type):
    """
    TODO: Integrate your market data here.
    This function should query Webull's option chain for 0DTEs, 
    scan the Ask prices, and return the strike price closest to ~$1.00.
    """
    # Example format of what this function should return once you connect market data:
    # return {
    #     "strike_price": "550.0", 
    #     "option_expire_date": "2026-09-08", # Today's date
    #     "ask_price": "0.95"
    # }
    
    print(f"⚠️ [MARKET DATA MISSING]: You need to connect an options chain fetcher to find the $1.00 strike for {ticker} {opt_type}.")
    return None


def execute_buy_target_premium(trade_client, account_id, ticker, opt_type):
    """Finds a contract between $0.90-$1.10 and executes a Buy to Open order."""
    print(f"\n⚡ [BUY INITIATED]: Hunting for 0DTE {ticker} {opt_type} at $0.90 - $1.10...")
    
    target_contract = get_target_option_contract(ticker, opt_type)
    
    if not target_contract:
        print("❌ [BUY CANCELLED]: Could not find a suitable contract in the target premium range.")
        return False
        
    strike = target_contract["strike_price"]
    expire_date = target_contract["option_expire_date"]
    limit_price = target_contract["ask_price"]
    
    # Generate the exact Webull Buy payload format
    client_order_id = uuid.uuid4().hex
    new_orders = [{
        "combo_type": "NORMAL",
        "client_order_id": client_order_id,
        "symbol": ticker,
        "instrument_type": "OPTION",
        "option_strategy": "SINGLE",
        "market": "US",
        "order_type": "LIMIT",
        "limit_price": str(limit_price),
        "quantity": "1",
        "side": "BUY",
        "time_in_force": "DAY",
        "entrust_type": "QTY",
        "legs": [{
            "side": "BUY",
            "quantity": "1",
            "symbol": ticker,
            "strike_price": str(strike),
            "option_expire_date": expire_date,
            "instrument_type": "OPTION",
            "option_type": opt_type,
            "market": "US"
        }]
    }]

    print(f"🛒 EXECUTING BUY: {ticker} {strike} {opt_type} at ${limit_price}")
    
    try:
        response = trade_client.order_v3.place_order(account_id, new_orders)
        print(f"✅ Buy Order Submitted Successfully! Order ID: {response}")
        
        # Log the successful buy to Supabase
        log_trade_to_db(ticker, "BUY", 1, float(limit_price), "Target Premium Entry", 0.0)
        return True
        
    except Exception as e:
        print(f"❌ Error placing buy order: {e}")
        return False


# --- 0DTE SELL EXECUTION ENGINE ---
def run_0dte_watcher(trade_client, active_account_id):
    """Watches Discord for visual signals and actively manages positions."""
    state = {}
    last_processed_text = ""
    print(f"🚀 0DTE Watcher running. Tracking: {ALLOWED_TICKERS}")

    while True:
        try:
            # 1. Scan for new Buy signals
            ticker_signal, opt_type_signal, current_text = watch_discord_screen(last_processed_text)
            
            if ticker_signal:
                print(f"\n💎 [DIAMOND SIGNAL DETECTED]: {ticker_signal} {opt_type_signal}")
                
                # Log the raw signal to Supabase instantly
                log_signal_to_db(ticker_signal, opt_type_signal, current_text)
                
                execute_buy_target_premium(trade_client, active_account_id, ticker_signal, opt_type_signal)
                
                # Update memory so we don't process this exact alert again
                last_processed_text = current_text

            # 2. Fetch live positions from Webull
            positions = get_open_positions(trade_client, active_account_id)
            current_time = time.time()

            for pos in positions:
                sym = pos.get("symbol", "").upper()
                
                # --- TICKER ISOLATION ---
                if not any(allowed in sym for allowed in ALLOWED_TICKERS):
                    continue

                qty = int(pos.get("quantity", 0))
                pnl_pct = float(pos.get("unrealized_profit_loss_rate", 0)) * 100

                # Initialize state
                if sym not in state:
                    state[sym] = {
                        "initial_qty": qty,
                        "runner_active": False,
                        "floor_locked": False,
                        "pending_sell": False,
                        "sell_time": 0,
                        "client_order_id": None,
                        "current_limit": 0.0
                    }
                    print(f"📈 [TRACKING {sym[:3]}]: {sym} | Qty: {qty} | Entry PnL: {pnl_pct:.2f}%")
                    continue

                trade = state[sym]

                # --- DYNAMIC CANCEL/REPLACE ENGINE ---
                if trade["pending_sell"]:
                    minutes_pending = (current_time - trade["sell_time"]) / 60
                    timeout_limit = 1.0 if pnl_pct > 0.0 else 5.0

                    if minutes_pending >= timeout_limit:
                        label = "1-Min (Profit)" if pnl_pct > 0.0 else "5-Min (Loss)"
                        print(f"⏳ {label} Timeout: {sym} unfilled. Stepping down limit price...")
                        
                        try:
                            trade_client.order_v3.cancel_order(active_account_id, trade["client_order_id"])
                        except Exception as ce:
                            print(f"   ↳ Cancel log: {ce}")

                        time.sleep(2)
                        new_limit = max(0.05, round(trade["current_limit"] - 0.05, 2))

                        success, cid, limit_set = execute_sell(
                            trade_client, active_account_id, sym, qty, f"0DTE Step-Down", pos.get("legs", []), force_price=new_limit
                        )
                        if success:
                            trade["sell_time"] = current_time
                            trade["client_order_id"] = cid
                            trade["current_limit"] = limit_set
                            
                            # Log the step-down replace order to Supabase
                            log_trade_to_db(sym, "SELL", qty, limit_set, f"Step-Down Replace ({label})", pnl_pct)
                    continue

                sell_reason = ""
                sell_qty = qty

                # --- RULE 1: UNIVERSAL HARD STOP LOSS (-35%) ---
                if pnl_pct <= -35.0:
                    sell_reason = f"Hard Stop Triggered at {pnl_pct:.2f}%"
                    sell_qty = qty

                # --- RULE 2: TWO-CONTRACT SCALING LOGIC ---
                elif trade["initial_qty"] == 2 and not trade["runner_active"]:
                    if pnl_pct >= 30.0:
                        sell_reason = f"Scale-Out Target (+30.0% Reached: {pnl_pct:.2f}%)"
                        sell_qty = 1
                        trade["runner_active"] = True

                # --- RULE 3: RUNNER MANAGEMENT (AFTER 1 SOLD) ---
                elif trade["runner_active"]:
                    if pnl_pct >= 50.0:
                        sell_reason = f"Runner Hit +50% Target ({pnl_pct:.2f}%)"
                        sell_qty = qty
                    elif pnl_pct <= 0.0:
                        sell_reason = f"Runner Reached Breakeven (0.0%). Securing Initial Gain."
                        sell_qty = qty

                # --- RULE 4: SINGLE CONTRACT MANAGEMENT ---
                elif trade["initial_qty"] == 1:
                    if pnl_pct >= 30.0 and not trade["floor_locked"]:
                        trade["floor_locked"] = True
                        print(f"🔒 [FLOOR LOCKED]: {sym} crossed +30.0%. Profit floor active.")

                    if pnl_pct >= 50.0:
                        sell_reason = f"Single Contract Hit +50% Target ({pnl_pct:.2f}%)"
                        sell_qty = 1
                    elif trade["floor_locked"] and pnl_pct <= 30.0:
                        sell_reason = f"Single Contract Fell Back to +30% Floor. Securing Profit."
                        sell_qty = 1

                # --- DISPATCH SELL ---
                if sell_reason:
                    print(f"\n🚨 [0DTE EXIT SIGNAL]: {sym} | Qty: {sell_qty} | Reason: {sell_reason}")
                    success, cid, limit_set = execute_sell(
                        trade_client, active_account_id, sym, sell_qty, sell_reason, pos.get("legs", [])
                    )
                    if success:
                        trade["pending_sell"] = True
                        trade["sell_time"] = current_time
                        trade["client_order_id"] = cid
                        trade["current_limit"] = limit_set
                        
                        # Log the successful sell signal to Supabase
                        log_trade_to_db(sym, "SELL", sell_qty, limit_set, sell_reason, pnl_pct)

        except Exception as e:
            print(f"⚠️ 0DTE Polling Exception: {e}")

        time.sleep(5)


# --- SCRIPT ENTRY POINT ---
if __name__ == "__main__":
    print("Loading credentials from .env file...")
    
    app_key = os.getenv("WEBULL_APP_KEY")
    app_secret = os.getenv("WEBULL_APP_SECRET")
    account_id = os.getenv("WEBULL_ACCOUNT_ID")

    try:
        # Version Hotfix
        import webull
        webull.__version__ = "1.0.0"

        from webull.core.client import ApiClient
        from webull.trade.trade_client import TradeClient
        
        api_client = ApiClient(app_key, app_secret, "us")
        api_client.add_endpoint("us", "api.sandbox.webull.com")
        trade_client = TradeClient(api_client)
        
        run_0dte_watcher(trade_client, account_id)
        
    except ImportError as e:
        print(f"❌ Error: {e}")