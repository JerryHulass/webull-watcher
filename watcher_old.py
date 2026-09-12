import os
import time
import uuid
import re
import asyncio
from datetime import datetime
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

import discord
from discord.ext import tasks

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

# Dynamic Trailing Stop Logic
HARD_STOP_PCT = -45.0   
ACTIVATION_PCT = 25.0   
TRAIL_PCT = 15.0        

PROCESSED_SIGNAL_IDS = set()

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


# --- DISCORD SELF-BOT ARCHITECTURE ---
class WatcherClient(discord.Client):
    def __init__(self, api_client, trade_client, account_id, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.api_client = api_client
        self.trade_client = trade_client
        self.account_id = account_id
        self.target_channel_id = int(os.getenv("DISCORD_CHANNEL_ID", 0))
        
        # State variables previously held in the while loop
        self.pending_buy_signal = None
        self.pending_buy_order = None
        self.state = {}

    async def on_ready(self):
        print(f"🚀 Discord API Connected: Logged in as {self.user.name}")
        print(f"📡 Listening exclusively to Channel ID: {self.target_channel_id}")
        self.trading_loop.start()

    async def on_message(self, message):
        if message.channel.id != self.target_channel_id:
            return

        # Parse the live embed JSON
        embed_text = ""
        for embed in message.embeds:
            embed_text += f"{embed.title or ''} {embed.description or ''} "
        embed_text = embed_text.upper()

        pattern = r'DIAMOND[\s\S]{1,60}?(LONG|SHORT)\s+(SPY|QQQ|IWM)'
        match = re.search(pattern, embed_text)

        if match:
            direction = match.group(1)
            ticker = match.group(2)
            opt_type = "CALL" if direction == "LONG" else "PUT"
            
            # Use Discord's native message ID to absolutely prevent duplicates
            signal_id = str(message.id)

            if signal_id not in PROCESSED_SIGNAL_IDS:
                PROCESSED_SIGNAL_IDS.add(signal_id)
                print(f"\n🔔 [FRESH LIVE SIGNAL DETECTED]: {ticker} {opt_type}")
                
                if not self.pending_buy_order:
                    print(f"⏳ [SIGNAL QUEUED]: Waiting {EXECUTION_DELAY_SECONDS} seconds before executing...")
                    
                    # Run DB network calls in background to prevent API disconnects
                    asyncio.create_task(asyncio.to_thread(log_signal_to_db, ticker, opt_type, embed_text))
                    
                    self.pending_buy_signal = {
                        "ticker": ticker,
                        "opt_type": opt_type,
                        "execute_at": time.time() + EXECUTION_DELAY_SECONDS
                    }

    @tasks.loop(seconds=5)
    async def trading_loop(self):
        current_time = time.time()

        # 1. Execute queued signals
        if self.pending_buy_signal and current_time >= self.pending_buy_signal["execute_at"]:
            print(f"\n⏰ [DELAY COMPLETE]: Executing queued {self.pending_buy_signal['ticker']} signal...")
            self.pending_buy_order = await asyncio.to_thread(
                execute_buy_target_premium, self.api_client, self.trade_client, 
                self.account_id, self.pending_buy_signal["ticker"], self.pending_buy_signal["opt_type"]
            )
            self.pending_buy_signal = None

        # 2. Get positions asynchronously to avoid blocking Discord heartbeat
        try:
            positions = await asyncio.to_thread(get_open_positions, self.trade_client, self.account_id)
            positions = positions or []
        except Exception as e:
            print(f"⚠️ Position Fetch Error: {e}")
            return

        # 3. Step-up bidding and fill logging
        if self.pending_buy_order:
            elapsed = current_time - self.pending_buy_order["time"]
            has_filled = any(self.pending_buy_order["ticker"] in pos.get("symbol", "") for pos in positions)
            
            if has_filled:
                print(f"🎉 [ENTRY FILLED]: {self.pending_buy_order['ticker']} order executed successfully.")
                
                payload = {
                    "signal_time": datetime.now().isoformat(),
                    "ticker": self.pending_buy_order["ticker"],
                    "contract_symbol": self.pending_buy_order["contractSymbol"],
                    "option_type": self.pending_buy_order["opt_type"],
                    "strike": self.pending_buy_order["strike"],
                    "expiration": self.pending_buy_order["expire_date"],
                    "entry_price": self.pending_buy_order["limit_price"],
                    "iv": self.pending_buy_order["iv"]
                }
                asyncio.create_task(asyncio.to_thread(log_option_execution_to_db, payload))
                self.pending_buy_order = None
                
            elif elapsed >= 10:
                print(f"⛔ [TIMEOUT]: Order {self.pending_buy_order['ticker']} unfilled after 10s. Cancelling...")
                try: 
                    await asyncio.to_thread(self.trade_client.order_v3.cancel_order, self.account_id, self.pending_buy_order["client_order_id"])
                except: pass
                await asyncio.sleep(1) 
                
                current_limit = self.pending_buy_order["limit_price"]
                if current_limit < TARGET_PREMIUM_MAX:
                    new_limit = min(current_limit + 0.05, TARGET_PREMIUM_MAX)
                    print(f"🔄 [STEP-UP BUY]: Adjusting limit up to catch premium -> ${new_limit:.2f}")
                    try:
                        cid = await asyncio.to_thread(
                            execute_raw_buy, self.trade_client, self.account_id, 
                            self.pending_buy_order["ticker"], self.pending_buy_order["strike"], 
                            self.pending_buy_order["opt_type"], self.pending_buy_order["expire_date"], 
                            new_limit
                        )
                        self.pending_buy_order["client_order_id"] = cid
                        self.pending_buy_order["limit_price"] = new_limit
                        self.pending_buy_order["time"] = time.time()
                        self.pending_buy_order["retries"] += 1
                        asyncio.create_task(asyncio.to_thread(log_trade_to_db, self.pending_buy_order["ticker"], "BUY", 1, new_limit, f"Step-Up Retry #{self.pending_buy_order['retries']}", 0.0))
                    except Exception as e:
                        print(f"❌ Error placing step-up order: {e}")
                        self.pending_buy_order = None
                else:
                    print(f"🛑 [ABORT]: Max premium limit of ${TARGET_PREMIUM_MAX:.2f} reached. Trade cancelled.")
                    self.pending_buy_order = None

        # 4. Dynamic Trailing Engine
        for pos in positions:
            sym = pos.get("symbol", "").upper()
            if not any(allowed in sym for allowed in ALLOWED_TICKERS): continue
            qty = int(pos.get("quantity", 0))
            pnl_pct = float(pos.get("unrealized_profit_loss_rate", 0)) * 100

            if sym not in self.state:
                self.state[sym] = {
                    "peak_pnl": pnl_pct, 
                    "trail_active": False,
                    "pending_sell": False, 
                    "sell_time": 0, 
                    "client_order_id": None, 
                    "current_limit": 0.0
                }
                print(f"📈 [TRACKING {sym[:3]}]: {sym} | Entry PnL: {pnl_pct:.2f}%")
                continue

            trade = self.state[sym]
            
            if pnl_pct > trade["peak_pnl"]:
                trade["peak_pnl"] = pnl_pct
            
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
                    try: await asyncio.to_thread(self.trade_client.order_v3.cancel_order, self.account_id, trade["client_order_id"])
                    except: pass
                    await asyncio.sleep(2)
                    new_limit = max(0.05, round(trade["current_limit"] - 0.05, 2))
                    success, cid, limit_set = await asyncio.to_thread(execute_sell, self.trade_client, self.account_id, sym, qty, "0DTE Step-Down", pos.get("legs", []), force_price=new_limit)
                    if success:
                        trade["sell_time"], trade["client_order_id"], trade["current_limit"] = current_time, cid, limit_set
                        asyncio.create_task(asyncio.to_thread(log_trade_to_db, sym, "SELL", qty, limit_set, f"Step-Down Replace ({label})", pnl_pct))
                continue

            sell_reason = ""
            
            if pnl_pct <= current_stop:
                if trade["trail_active"]:
                    sell_reason = f"Trailing Stop Triggered at {pnl_pct:.2f}% (Peak was {trade['peak_pnl']:.2f}%)"
                else:
                    sell_reason = f"Hard Stop Triggered at {pnl_pct:.2f}%"

            if sell_reason:
                print(f"\n🚨 [0DTE EXIT SIGNAL]: {sym} | Qty: {qty} | Reason: {sell_reason}")
                success, cid, limit_set = await asyncio.to_thread(execute_sell, self.trade_client, self.account_id, sym, qty, sell_reason, pos.get("legs", []))
                if success:
                    trade["pending_sell"], trade["sell_time"], trade["client_order_id"], trade["current_limit"] = True, current_time, cid, limit_set
                    asyncio.create_task(asyncio.to_thread(log_trade_to_db, sym, "SELL", qty, limit_set, sell_reason, pnl_pct))

    @trading_loop.before_loop
    async def before_trading_loop(self):
        await self.wait_until_ready()

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
    discord_token = os.getenv("DISCORD_USER_TOKEN")

    if not discord_token:
        print("❌ CRITICAL: DISCORD_USER_TOKEN is missing from .env")
        exit()

    try:
        import webull
        webull.__version__ = "1.0.0"
        from webull.core.client import ApiClient
        from webull.trade.trade_client import TradeClient

        api_client = ApiClient(app_key, app_secret, "us")
        api_client.add_endpoint("us", "api.sandbox.webull.com")
        trade_client = TradeClient(api_client)

        if test_webull_connection(trade_client, account_id):
            client = WatcherClient(api_client, trade_client, account_id)
            client.run(discord_token)
    except ImportError as e:
        print(f"❌ Error: {e}")