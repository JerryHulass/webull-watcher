import json
import pandas as pd
import yfinance as yf
from datetime import timedelta
import re

# --- CONFIGURATION ---
JSON_FILE_PATH = "0dte_bot_chat2.json"  
ENTRY_DELAY_MINUTES = 1

# Proxy targets for underlying stock movement
PROFIT_TARGET_1_PCT = 0.0040  # +40% Option Move (0.4% stock move)
PROFIT_TARGET_2_PCT = 0.0045  # +45% Option Move (0.45% stock move)
STOP_LOSS_PCT = -0.0050       # -50% Option Move (-0.5% stock move)

def get_market_data():
    print("Fetching 1-minute market data for QQQ...")
    
    # Current Week (Set 1):
    # return yf.download("QQQ", period="7d", interval="1m")
    
    # Previous Week (Set 2):
    # return yf.download("QQQ", start="2026-08-28", end="2026-09-04", interval="1m")
    
    # 3rd Set:
    # return yf.download("QQQ", start="2026-08-21", end="2026-08-28", interval="1m")
    
    # 4th Set (Maximum Limit):
    return yf.download("QQQ", period="7d", interval="1m")

def parse_discord_json(file_path):
    print("Parsing Discord JSON signals...")
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    signals = []
    
    for msg in data.get('messages', []):
        # 1. Loop through all embeds in the message
        embeds = msg.get('embeds', [])
        for embed in embeds:
            # Combine the embed title and description into one searchable string
            embed_text = str(embed.get('title', '')) + " " + str(embed.get('description', ''))
            embed_text = embed_text.upper()
            
            if "DIAMOND" not in embed_text:
                continue
                
            # 2. Strict QQQ filter via Regex
            pattern = r'DIAMOND[\s\S]{1,60}?(LONG|SHORT)\s+(QQQ)'
            match = re.search(pattern, embed_text)
            
            if match:
                direction = match.group(1)
                ticker = match.group(2)
                opt_type = "CALL" if direction == "LONG" else "PUT"
                
                try:
                    signal_time = pd.to_datetime(msg['timestamp']).tz_convert('America/New_York')
                except TypeError:
                    signal_time = pd.to_datetime(msg['timestamp']).tz_localize('UTC').tz_convert('America/New_York')
                    
                # 3. Time Filter: Disregard anything before 9:30 AM EST
                if signal_time.time() < pd.Timestamp('09:30').time():
                    continue
                    
                signals.append({
                    "ticker": ticker,
                    "opt_type": opt_type,
                    "signal_time": signal_time
                })
                
    return pd.DataFrame(signals)

def run_backtest():
    market_data = get_market_data()
    # Call the new JSON parser
    signals_df = parse_discord_json(JSON_FILE_PATH)
    
    results = []
    
    for _, signal in signals_df.iterrows():
        ticker = signal['ticker']
        opt_type = signal['opt_type']
        entry_time = signal['signal_time'] + timedelta(minutes=ENTRY_DELAY_MINUTES)
        
        ticker_df = market_data.dropna()
        available_times = ticker_df.index[ticker_df.index >= entry_time]
        
        if available_times.empty:
            continue
            
        actual_entry_time = available_times[0]
        e_price = ticker_df.loc[actual_entry_time]['Close']
        entry_price = float(e_price.iloc[0] if isinstance(e_price, pd.Series) else e_price)
        
        end_of_day = actual_entry_time.replace(hour=16, minute=0, second=0)
        forward_data = ticker_df.loc[actual_entry_time:end_of_day]
        
        trade_result = "EXPIRED"
        exit_price = 0
        max_favorable_pct = 0.0
        max_adverse_pct = 0.0
        
        # Track which targets were crossed
        hit_30 = False
        hit_50 = False
        
        for current_time, row in forward_data.iterrows():
            c_price = row['Close']
            current_price = float(c_price.iloc[0] if isinstance(c_price, pd.Series) else c_price)
            
            if opt_type == "CALL":
                pct_change = (current_price - entry_price) / entry_price
            else:
                pct_change = (entry_price - current_price) / entry_price
                
            if pct_change > max_favorable_pct: max_favorable_pct = pct_change
            if pct_change < max_adverse_pct: max_adverse_pct = pct_change
            
            # Record hitting targets, but DO NOT break the loop
            if pct_change >= PROFIT_TARGET_1_PCT:
                hit_30 = True
            if pct_change >= PROFIT_TARGET_2_PCT:
                hit_50 = True
            
            # ONLY break if the hard stop loss is hit
            if pct_change <= STOP_LOSS_PCT:
                exit_price = current_price
                break
                
        # Determine the final result after the loop finishes
        if hit_50:
            trade_result = "RUNNER (50%+)"
        elif hit_30:
            trade_result = "TARGET HIT (30%)"
        elif pct_change <= STOP_LOSS_PCT:
            trade_result = "STOPPED OUT"
                
        results.append({
            "Ticker": ticker,
            "Type": opt_type,
            "Signal Time": signal['signal_time'].strftime("%Y-%m-%d %H:%M"),
            "Entry Price": round(entry_price, 2),
            "Result": trade_result,
            "Peak Profit": f"{max_favorable_pct*100:.2f}%",
            "Peak Drawdown": f"{max_adverse_pct*100:.2f}%"
        })
        
    results_df = pd.DataFrame(results)
    if results_df.empty:
        print("\nNo valid QQQ signals found during market hours in this date range.")
        return
        
    print("\n--- BACKTEST RESULTS ---")
    print(results_df.to_string(index=False))
    
    total_trades = len(results_df)
    # Win rate now counts both 30% hits and 50% runners as wins
    wins = len(results_df[(results_df['Result'] == 'TARGET HIT (30%)') | (results_df['Result'] == 'RUNNER (50%+)')])
    print(f"\nWin Rate: {wins}/{total_trades} ({wins/total_trades*100:.1f}%)")

if __name__ == "__main__":
    run_backtest()