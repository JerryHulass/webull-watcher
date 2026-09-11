import os
import time
import uuid
import re
from datetime import datetime
import mss
import pytesseract
import cv2
import numpy as np
from dotenv import load_dotenv

# --- IMPORT YOUR EXISTING WEBULL FUNCTIONS ---
from watcher import execute_sell, get_open_positions

# --- SUPABASE INITIALIZATION ---
from supabase import create_client, Client

load_dotenv()
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_KEY")

if supabase_url and supabase_key:
    supabase: Client = create_client(supabase_url, supabase_key)
    print("✅ Supabase Real-Time DB Connected.")
else:
    supabase = None
    print("⚠️ Supabase credentials missing. DB logging disabled.")


def log_signal_to_db(ticker, opt_type, raw_text):
    if not supabase: return
    try:
        supabase.table("signals").insert({
            "ticker": ticker, "option_type": opt_type, "raw_text": raw_text
        }).execute()
    except Exception as e:
        print(f"⚠️ DB Error (Signal logged locally): {e}")


def log_trade_to_db(ticker, action, qty, price, reason, pnl_pct=0.0):
    if not supabase: return
    try:
        supabase.table("trades").insert({
            "ticker": ticker, "action": action, "qty": qty, 
            "price": price, "reason": reason, "pnl_pct": pnl_pct
        }).execute()
    except Exception as e:
        print(f"⚠️ DB Error (Trade logged locally): {e}")


# --- CONFIGURATION ---
ALLOWED_TICKERS = ["SPY", "QQQ", "IWM"]
TARGET_PREMIUM_MIN = 0.80
TARGET_PREMIUM_MAX = 1.10

# 20-minute window for Discord's UI timestamp
MAX_SIGNAL_AGE_SECONDS = 1200
PROCESSED_SIGNAL_IDS = set()


# --- DISCORD OCR ENGINE ---
def watch_discord_screen(last_processed_text):
    with mss.MSS() as sct:
        monitor = {"top": 470, "left": 100, "width": 750, "height": 500}
        img = np.array(sct.grab(monitor))
        gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)

        cv2.imwrite("debug_capture.png", gray)
        text = pytesseract.image_to_string(gray, config='--psm 6').upper()
        
        if text == last_processed_text or text.strip() == "":
            return None, None, text

        if "DIAMOND" in text:
            # Split screen text by the bot's name to isolate Discord's UI timestamps
            blocks = text.split("GGAIZ")
            if len(blocks) <= 1:
                blocks = [text]

            # REVERSED: Scan from the bottom (newest) to top
            for block in reversed(blocks):
                # Safely ignore old Rough alerts
                if "DIAMOND" not in block or "ROUGH" in block:
                    continue
                
                # Explicitly block alerts from previous days shown in the UI
                if "YESTERDAY" in block or "/" in block:
                    continue

                # Isolate Discord's native UI timestamp (e.g., 11:30 AM or 11:30AM)
                ui_time_match = re.search(r'(\d{1,2}:\d{2}\s*[APM]+)', block)
                if not ui_time_match:
                    continue

                # Clean the string to HH:MMAM for flawless datetime parsing
                raw_ui_time = ui_time_match.group(1).replace(" ", "").strip()
                
                try:
                    now = datetime.now()
                    # Parse the UI time and fuse it with today's date
                    parsed_time = datetime.strptime(raw_ui_time, "%I:%M%p").time()
                    parsed_dt = datetime.combine(now.date(), parsed_time)
                    
                    age_seconds = (now - parsed_dt).total_seconds()

                    # Clock skew catch (if the OCR misread AM as PM, age will be massively negative)
                    if age_seconds < -60:
                        continue

                    # Reject if the Discord UI timestamp is older than 20 minutes
                    if age_seconds > MAX_SIGNAL_AGE_SECONDS:
                        print(f"⏳ [STALE UI TIME IGNORED]: Discord says {raw_ui_time} ({age_seconds:.0f}s old).")
                        continue

                except Exception as e:
                    continue

                # Extract Ticker & Direction
                detected_ticker = None
                detected_opt_type = None

                for ticker in ALLOWED_TICKERS:
                    if f" {ticker} " in block or f"{ticker} AT" in block:
                        detected_ticker = ticker
                        detected_opt_type = "CALL" if "LONG" in block else "PUT"
                        break

                if detected_ticker and detected_opt_type:
                    # Fingerprint using the Discord UI time instead of the candle close time
                    signal_id = f"{detected_ticker}_{detected_opt_type}_{raw_ui_time}"

                    if signal_id in PROCESSED_SIGNAL_IDS:
                        continue

                    PROCESSED_SIGNAL_IDS.add(signal_id)
                    print(f"\n💎 [FRESH UI SIGNAL ACCEPTED]: {detected_ticker} {detected_opt_type} posted at {raw_ui_time}")
                    return detected_ticker, detected_opt_type, text

        return None, None, text


# --- BUY EXECUTION ENGINE ---
def get_target_option_contract(api_client, ticker, opt_type):
    try:
        today_str = datetime.now().strftime("%Y-%m-%d")
        
        if not hasattr(api_client, "get_options"):
            print(f"⚠️ [MARKET DATA NOTICE]: Webull client missing get_options method. Returning sandbox contract.")
            return {"strike_price": "550.0" if ticker == "SPY" else "480.0", "option_expire_date": today_str, "ask_price": "1.00"}

        response = api_client.get_options(stock=ticker, expireDate=today_str)
        if not response or not isinstance(response, list):
            return None

        best_contract = None
        smallest_diff = float('inf')

        for contract in response:
            if contract.get("direction", "").upper() != opt_type.upper():
                continue
                
            ask_price = float(contract.get("askList", [0])[0]) if contract.get("askList") else 0.0
            
            if TARGET_PREMIUM_MIN <= ask_price <= TARGET_PREMIUM_MAX:
                diff = abs(ask_price - 1.00)
                if diff < smallest_diff:
                    smallest_diff = diff
                    best_contract = contract
                    
        if best_contract:
            return {"strike_price": str(best_contract["strikePrice"]), "option_expire_date": today_str, "ask_price": str(best_contract["askList"][0])}
            
        return None

    except Exception as e:
        print(f"❌ Option Chain Fetch Error: {e}")
        return None


def execute_buy_target_premium(api_client, trade_client, account_id, ticker, opt_type):
    print(f"\n⚡ [BUY INITIATED]: Hunting for 0DTE {ticker} {opt_type} at ${TARGET_PREMIUM_MIN} - ${TARGET_PREMIUM_MAX}...")
    target_contract = get_target_option_contract(api_client, ticker, opt_type)

    if not target_contract:
        print("❌ [BUY CANCELLED]: Could not resolve contract in target premium range.")
        return False

    strike, expire_date, limit_price = target_contract["strike_price"], target_contract["option_expire_date"], target_contract["ask_price"]
    client_order_id = uuid.uuid4().hex
    
    new_orders = [{
        "combo_type": "NORMAL", "client_order_id": client_order_id,
        "symbol": ticker, "instrument_type": "OPTION", "option_strategy": "SINGLE",
        "market": "US", "order_type": "LIMIT", "limit_price": str(limit_price),
        "quantity": "1", "side": "BUY", "time_in_force": "DAY", "entrust_type": "QTY",
        "legs": [{"side": "BUY", "quantity": "1", "symbol": ticker, "strike_price": str(strike), "option_expire_date": expire_date, "instrument_type": "OPTION", "option_type": opt_type, "market": "US"}]
    }]

    print(f"🛒 EXECUTING BUY: {ticker} {strike} {opt_type} at ${limit_price}")
    try:
        response = trade_client.order_v3.place_order(account_id, new_orders)
        print(f"✅ Buy Order Submitted! Order ID: {response}")
        log_trade_to_db(ticker, "BUY", 1, float(limit_price), "Target Premium Entry", 0.0)
        return True
    except Exception as e:
        print(f"❌ Error placing buy order: {e}")
        return False


# --- 0DTE SELL & POSITION MANAGEMENT ENGINE ---
def run_0dte_watcher(api_client, trade_client, active_account_id):
    state = {}
    last_processed_text = ""
    print(f"🚀 0DTE Watcher running. Tracking: {ALLOWED_TICKERS}")

    while True:
        try:
            ticker_signal, opt_type_signal, current_text = watch_discord_screen(last_processed_text)

            if ticker_signal:
                print(f"\n💎 [EXECUTING LIVE SIGNAL]: {ticker_signal} {opt_type_signal}")
                log_signal_to_db(ticker_signal, opt_type_signal, current_text)
                execute_buy_target_premium(api_client, trade_client, active_account_id, ticker_signal, opt_type_signal)
                last_processed_text = current_text
            elif current_text != last_processed_text:
                last_processed_text = current_text

            positions = get_open_positions(trade_client, active_account_id)
            current_time = time.time()

            for pos in positions:
                sym = pos.get("symbol", "").upper()
                if not any(allowed in sym for allowed in ALLOWED_TICKERS): continue
                qty = int(pos.get("quantity", 0))
                pnl_pct = float(pos.get("unrealized_profit_loss_rate", 0)) * 100

                if sym not in state:
                    state[sym] = {"initial_qty": qty, "runner_active": False, "floor_locked": False, "pending_sell": False, "sell_time": 0, "client_order_id": None, "current_limit": 0.0}
                    print(f"📈 [TRACKING {sym[:3]}]: {sym} | Qty: {qty} | Entry PnL: {pnl_pct:.2f}%")
                    continue

                trade = state[sym]
                if trade["pending_sell"]:
                    minutes_pending = (current_time - trade["sell_time"]) / 60
                    timeout_limit = 1.0 if pnl_pct > 0.0 else 5.0

                    if minutes_pending >= timeout_limit:
                        label = "1-Min (Profit)" if pnl_pct > 0.0 else "5-Min (Loss)"
                        print(f"⏳ {label} Timeout: {sym} unfilled. Stepping down limit price...")
                        try: trade_client.order_v3.cancel_order(active_account_id, trade["client_order_id"])
                        except: pass
                        time.sleep(2)
                        new_limit = max(0.05, round(trade["current_limit"] - 0.05, 2))
                        success, cid, limit_set = execute_sell(trade_client, active_account_id, sym, qty, "0DTE Step-Down", pos.get("legs", []), force_price=new_limit)
                        if success:
                            trade["sell_time"], trade["client_order_id"], trade["current_limit"] = current_time, cid, limit_set
                            log_trade_to_db(sym, "SELL", qty, limit_set, f"Step-Down Replace ({label})", pnl_pct)
                    continue

                sell_reason, sell_qty = "", qty
                if pnl_pct <= -35.0: sell_reason = f"Hard Stop Triggered at {pnl_pct:.2f}%"
                elif trade["initial_qty"] == 2 and not trade["runner_active"] and pnl_pct >= 30.0: sell_reason, sell_qty, trade["runner_active"] = f"Scale-Out (+30.0%: {pnl_pct:.2f}%)", 1, True
                elif trade["runner_active"]:
                    if pnl_pct >= 50.0: sell_reason = f"Runner Hit +50% Target ({pnl_pct:.2f}%)"
                    elif pnl_pct <= 0.0: sell_reason = "Runner Reached Breakeven (0.0%)."
                elif trade["initial_qty"] == 1:
                    if pnl_pct >= 30.0 and not trade["floor_locked"]: trade["floor_locked"] = True; print(f"🔒 [FLOOR LOCKED]: {sym} crossed +30.0%.")
                    if pnl_pct >= 50.0: sell_reason, sell_qty = f"Hit +50% Target ({pnl_pct:.2f}%)", 1
                    elif trade["floor_locked"] and pnl_pct <= 30.0: sell_reason, sell_qty = "Fell Back to +30% Floor.", 1

                if sell_reason:
                    print(f"\n🚨 [0DTE EXIT SIGNAL]: {sym} | Qty: {sell_qty} | Reason: {sell_reason}")
                    success, cid, limit_set = execute_sell(trade_client, active_account_id, sym, sell_qty, sell_reason, pos.get("legs", []))
                    if success:
                        trade["pending_sell"], trade["sell_time"], trade["client_order_id"], trade["current_limit"] = True, current_time, cid, limit_set
                        log_trade_to_db(sym, "SELL", sell_qty, limit_set, sell_reason, pnl_pct)

        except Exception as e:
            print(f"⚠️ 0DTE Polling Exception: {e}")

        time.sleep(5)


def test_webull_connection(trade_client, account_id):
    print("🔌 Testing Webull Paper Trading Connection...")
    try:
        get_open_positions(trade_client, account_id)
        print(f"✅ Webull Connection Verified Successfully! Account ID: {account_id}")
        return True
    except Exception as e:
        print(f"❌ Webull Connection Failed: {e}")
        return False


if __name__ == "__main__":
    print("Loading credentials from .env file...")
    app_key, app_secret, account_id = os.getenv("WEBULL_APP_KEY"), os.getenv("WEBULL_APP_SECRET"), os.getenv("WEBULL_ACCOUNT_ID")

    try:
        import webull
        webull.__version__ = "1.0.0"
        from webull.core.client import ApiClient
        from webull.trade.trade_client import TradeClient

        api_client = ApiClient(app_key, app_secret, "us")
        api_client.add_endpoint("us", "api.sandbox.webull.com")
        trade_client = TradeClient(api_client)

        if test_webull_connection(trade_client, account_id):
            run_0dte_watcher(api_client, trade_client, account_id)
    except ImportError as e:
        print(f"❌ Error: {e}")