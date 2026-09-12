import os
import time
import uuid
import re
from datetime import datetime
import mss
import pytesseract
import cv2
import numpy as np
import pandas as pd
import yfinance as yf
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


def log_option_execution_to_db(trade_data: dict):
    if not supabase: return
    try:
        payload = {
            "signal_time": str(trade_data.get("signal_time")),
            "ticker": trade_data.get("ticker"),
            "contract_symbol": trade_data.get("contract_symbol", "UNKNOWN"),
            "option_type": trade_data.get("option_type"),
            "strike": trade_data.get("strike", 0),
            "expiration": str(trade_data.get("expiration", "2026-01-01")),
            "entry_price": trade_data.get("entry_price"),
            "underlying_price": trade_data.get("underlying_price", 0.0),
            "delta": trade_data.get("delta", 0.0),
            "gamma": trade_data.get("gamma", 0.0),
            "theta": trade_data.get("theta", 0.0),
            "vega": trade_data.get("vega", 0.0),
            "implied_volatility": trade_data.get("iv", 0.0),
            "status": trade_data.get("status", "OPEN")
        }
        supabase.table("option_executions").insert(payload).execute()
    except Exception as e:
        print(f"⚠️ Supabase Option Logging Error: {e}")


# --- CONFIGURATION ---
ALLOWED_TICKERS = ["SPY", "QQQ", "IWM"]
TARGET_PREMIUM_MIN = 0.80
TARGET_PREMIUM_MAX = 1.10
EXECUTION_DELAY_SECONDS = 60  

# Dynamic Trailing Stop Logic (Percentages based on Webull's PnL reporting)
HARD_STOP_PCT = -45.0   # -45% Hard stop loss
ACTIVATION_PCT = 25.0   # +25% Profit required to activate the trail
TRAIL_PCT = 15.0        # 15% Trailing distance from peak

# Tracks processed signal fingerprints to prevent duplicate executions
PROCESSED_SIGNAL_IDS = set()


# --- DISCORD OCR ENGINE ---
def watch_discord_screen(last_processed_text, is_startup):
    with mss.MSS() as sct:
        monitor = {"top": 470, "left": 100, "width": 750, "height": 500}
        img = np.array(sct.grab(monitor))
        gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)

        cv2.imwrite("debug_capture.png", gray)
        text = pytesseract.image_to_string(gray, config='--psm 6').upper()
        
        if text == last_processed_text or text.strip() == "":
            return None, None, text

        pattern = r'DIAMOND[\s\S]{1,60}?(LONG|SHORT)\s+(SPY|QQQ|IWM)\s+AT\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})'
        matches = list(re.finditer(pattern, text))

        newest_ticker = None
        newest_opt_type = None

        for match in matches:
            direction = match.group(1)
            ticker = match.group(2)
            raw_dt_str = match.group(3)

            opt_type = "CALL" if direction == "LONG" else "PUT"
            signal_id = f"{ticker}_{opt_type}_{raw_dt_str}"

            if signal_id not in PROCESSED_SIGNAL_IDS:
                PROCESSED_SIGNAL_IDS.add(signal_id)
                if is_startup:
                    print(f"🧹 [STARTUP WARMUP]: Memorized pre-existing signal -> {ticker} {opt_type} at {raw_dt_str}")
                else:
                    print(f"\n🔔 [FRESH LIVE SIGNAL DETECTED]: {ticker} {opt_type} at {raw_dt_str}")
                    newest_ticker = ticker
                    newest_opt_type = opt_type

        return newest_ticker, newest_opt_type, text


# --- BUY EXECUTION ENGINE ---
def get_target_option_contract(api_client, ticker, opt_type):
    try:
        today_str = datetime.now().strftime("%Y-%m-%d")
        stock = yf.Ticker(ticker)
        
        if today_str not in stock.options:
            print(f"⚠️ [MARKET DATA]: No 0DTE options chain found for {ticker} on {today_str}.")
            return None
            
        chain = stock.option_chain(today_str)
        options_df = chain.calls if opt_type.upper() == "CALL" else chain.puts
        
        valid_options = options_df[(options_df['ask'] >= TARGET_PREMIUM_MIN) & (options_df['ask'] <= TARGET_PREMIUM_MAX)].copy()
        
        if valid_options.empty:
            print(f"❌ No {ticker} {opt_type} contracts found between ${TARGET_PREMIUM_MIN} and ${TARGET_PREMIUM_MAX}.")
            return None
            
        valid_options['diff'] = abs(valid_options['ask'] - 1.00)
        best_contract = valid_options.loc[valid_options['diff'].idxmin()]
        
        target_strike = best_contract['strike']
        target_ask = best_contract['ask']
        
        print(f"📊 [MARKET DATA]: Found {ticker} {opt_type} at ${target_ask:.2f} (Strike: {target_strike}).")

        return {
            "strike_price": f"{target_strike:.1f}",
            "option_expire_date": today_str,
            "ask_price": float(target_ask),
            "iv": float(best_contract.get('impliedVolatility', 0.0)),
            "contractSymbol": str(best_contract.get('contractSymbol', 'UNKNOWN'))
        }

    except Exception as e:
        print(f"❌ Option Chain Fetch Error: {e}")
        return None


def execute_raw_buy(trade_client, account_id, ticker, strike, opt_type, expire_date, limit_price):
    client_order_id = uuid.uuid4().hex
    new_orders = [{
        "combo_type": "NORMAL", "client_order_id": client_order_id,
        "symbol": ticker, "instrument_type": "OPTION", "option_strategy": "SINGLE",
        "market": "US", "order_type": "LIMIT", "limit_price": f"{limit_price:.2f}",
        "quantity": "1", "side": "BUY", "time_in_force": "DAY", "entrust_type": "QTY",
        "legs": [{
            "side": "BUY", "quantity": "1", "symbol": ticker,
            "strike_price": str(strike), "option_expire_date": expire_date,
            "instrument_type": "OPTION", "option_type": opt_type, "market": "US"
        }]
    }]
    trade_client.order_v3.place_order(account_id, new_orders)
    return client_order_id


def execute_buy_target_premium(api_client, trade_client, account_id, ticker, opt_type):
    print(f"\n⚡ [BUY INITIATED]: Hunting for 0DTE {ticker} {opt_type} at ${TARGET_PREMIUM_MIN} - ${TARGET_PREMIUM_MAX}...")
    target_contract = get_target_option_contract(api_client, ticker, opt_type)

    if not target_contract:
        print("❌ [BUY CANCELLED]: Could not resolve contract in target premium range.")
        return None

    strike = target_contract["strike_price"]
    expire_date = target_contract["option_expire_date"]
    
    entry_limit_price = min(target_contract["ask_price"], TARGET_PREMIUM_MAX)

    print(f"🛒 EXECUTING BUY: {ticker} {strike} {opt_type} at Limit ${entry_limit_price:.2f}")
    try:
        cid = execute_raw_buy(trade_client, account_id, ticker, strike, opt_type, expire_date, entry_limit_price)
        print(f"✅ Buy Order Submitted! Order ID: {cid}")
        log_trade_to_db(ticker, "BUY", 1, float(entry_limit_price), "Initial Premium Entry", 0.0)
        
        return {
            "client_order_id": cid,
            "ticker": ticker,
            "opt_type": opt_type,
            "strike": strike,
            "expire_date": expire_date,
            "limit_price": float(entry_limit_price),
            "iv": target_contract["iv"],
            "contractSymbol": target_contract["contractSymbol"],
            "time": time.time(),
            "retries": 0
        }
    except Exception as e:
        print(f"❌ Error placing buy order: {e}")
        return None


# --- 0DTE SELL & POSITION MANAGEMENT ENGINE ---
def run_0dte_watcher(api_client, trade_client, active_account_id):
    state = {}
    last_processed_text = ""
    is_startup = True
    pending_buy_signal = None  
    pending_buy_order = None   
    
    print(f"🚀 0DTE Watcher running. Tracking: {ALLOWED_TICKERS}")

    while True:
        try:
            ticker_signal, opt_type_signal, current_text = watch_discord_screen(last_processed_text, is_startup)

            if is_startup:
                is_startup = False
                last_processed_text = current_text
                time.sleep(5)
                continue

            # 1. Queue new signals with delay
            if ticker_signal and not pending_buy_order:
                print(f"\n⏳ [SIGNAL QUEUED]: {ticker_signal} {opt_type_signal}. Waiting {EXECUTION_DELAY_SECONDS} seconds before executing...")
                log_signal_to_db(ticker_signal, opt_type_signal, current_text)
                
                pending_buy_signal = {
                    "ticker": ticker_signal,
                    "opt_type": opt_type_signal,
                    "execute_at": time.time() + EXECUTION_DELAY_SECONDS
                }
                last_processed_text = current_text
            elif current_text != last_processed_text:
                last_processed_text = current_text

            # 2. Execute queued signals
            if pending_buy_signal and time.time() >= pending_buy_signal["execute_at"]:
                print(f"\n⏰ [DELAY COMPLETE]: Executing queued {pending_buy_signal['ticker']} signal...")
                pending_buy_order = execute_buy_target_premium(api_client, trade_client, active_account_id, pending_buy_signal["ticker"], pending_buy_signal["opt_type"])
                pending_buy_signal = None

            positions = get_open_positions(trade_client, active_account_id) or []
            current_time = time.time()

            # 3. Step-up bidding and fill logging
            if pending_buy_order:
                elapsed = current_time - pending_buy_order["time"]
                has_filled = any(pending_buy_order["ticker"] in pos.get("symbol", "") for pos in positions)
                
                if has_filled:
                    print(f"🎉 [ENTRY FILLED]: {pending_buy_order['ticker']} order executed successfully.")
                    
                    # Log Option Execution to DB
                    log_option_execution_to_db({
                        "signal_time": datetime.now().isoformat(),
                        "ticker": pending_buy_order["ticker"],
                        "contract_symbol": pending_buy_order["contractSymbol"],
                        "option_type": pending_buy_order["opt_type"],
                        "strike": pending_buy_order["strike"],
                        "expiration": pending_buy_order["expire_date"],
                        "entry_price": pending_buy_order["limit_price"],
                        "iv": pending_buy_order["iv"]
                    })
                    
                    pending_buy_order = None
                elif elapsed >= 10:
                    print(f"⛔ [TIMEOUT]: Order {pending_buy_order['ticker']} unfilled after 10s. Cancelling...")
                    try: trade_client.order_v3.cancel_order(active_account_id, pending_buy_order["client_order_id"])
                    except: pass
                    time.sleep(1) 
                    
                    current_limit = pending_buy_order["limit_price"]
                    if current_limit < TARGET_PREMIUM_MAX:
                        new_limit = min(current_limit + 0.05, TARGET_PREMIUM_MAX)
                        print(f"🔄 [STEP-UP BUY]: Adjusting limit up to catch premium -> ${new_limit:.2f}")
                        try:
                            cid = execute_raw_buy(
                                trade_client, active_account_id, 
                                pending_buy_order["ticker"], pending_buy_order["strike"], 
                                pending_buy_order["opt_type"], pending_buy_order["expire_date"], 
                                new_limit
                            )
                            pending_buy_order["client_order_id"] = cid
                            pending_buy_order["limit_price"] = new_limit
                            pending_buy_order["time"] = time.time()
                            pending_buy_order["retries"] += 1
                            log_trade_to_db(pending_buy_order["ticker"], "BUY", 1, new_limit, f"Step-Up Retry #{pending_buy_order['retries']}", 0.0)
                        except Exception as e:
                            print(f"❌ Error placing step-up order: {e}")
                            pending_buy_order = None
                    else:
                        print(f"🛑 [ABORT]: Max premium limit of ${TARGET_PREMIUM_MAX:.2f} reached. Trade cancelled.")
                        pending_buy_order = None

            # 4. Dynamic Trailing Engine
            for pos in positions:
                sym = pos.get("symbol", "").upper()
                if not any(allowed in sym for allowed in ALLOWED_TICKERS): continue
                qty = int(pos.get("quantity", 0))
                pnl_pct = float(pos.get("unrealized_profit_loss_rate", 0)) * 100

                if sym not in state:
                    state[sym] = {
                        "peak_pnl": pnl_pct, 
                        "trail_active": False,
                        "pending_sell": False, 
                        "sell_time": 0, 
                        "client_order_id": None, 
                        "current_limit": 0.0
                    }
                    print(f"📈 [TRACKING {sym[:3]}]: {sym} | Entry PnL: {pnl_pct:.2f}%")
                    continue

                trade = state[sym]
                
                # Update peak profit
                if pnl_pct > trade["peak_pnl"]:
                    trade["peak_pnl"] = pnl_pct
                
                # Activate trailing logic if profit hits the activation threshold (+25%)
                if trade["peak_pnl"] >= ACTIVATION_PCT:
                    trade["trail_active"] = True
                    current_stop = max(trade["peak_pnl"] - TRAIL_PCT, HARD_STOP_PCT)
                else:
                    trade["trail_active"] = False
                    current_stop = HARD_STOP_PCT

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

                sell_reason = ""
                
                # Check for Stop Loss Trigger
                if pnl_pct <= current_stop:
                    if trade["trail_active"]:
                        sell_reason = f"Trailing Stop Triggered at {pnl_pct:.2f}% (Peak was {trade['peak_pnl']:.2f}%)"
                    else:
                        sell_reason = f"Hard Stop Triggered at {pnl_pct:.2f}%"

                if sell_reason:
                    print(f"\n🚨 [0DTE EXIT SIGNAL]: {sym} | Qty: {qty} | Reason: {sell_reason}")
                    success, cid, limit_set = execute_sell(trade_client, active_account_id, sym, qty, sell_reason, pos.get("legs", []))
                    if success:
                        trade["pending_sell"], trade["sell_time"], trade["client_order_id"], trade["current_limit"] = True, current_time, cid, limit_set
                        log_trade_to_db(sym, "SELL", qty, limit_set, sell_reason, pnl_pct)

        except Exception as e:
            print(f"⚠️ 0DTE Polling Exception: {e}")
        finally:
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