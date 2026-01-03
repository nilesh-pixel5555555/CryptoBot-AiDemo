# main.py - CRYPTO AI BOT (No Partials + 2Hr Cycle)

import os
import ccxt
import pandas as pd
import numpy as np
import asyncio
import json
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Bot
from flask import Flask, jsonify, render_template_string
import threading
import time
import traceback 

# --- CONFIGURATION ---
try:
    from dotenv import load_dotenv 
    load_dotenv() 
except:
    pass

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Assets to monitor
CRYPTOS = [s.strip() for s in os.getenv("CRYPTOS", "BTC/USDT,ETH/USDT,SOL/USDT").split(',')]
TIMEFRAME_MAIN = "4h"  # Major Trend
TIMEFRAME_ENTRY = "1h" # Entry Precision

# Initialize Bot and Exchange
bot = Bot(token=TELEGRAM_BOT_TOKEN)
exchange = ccxt.kraken({'enableRateLimit': True, 'rateLimit': 2000})

# Global Stats & History
bot_stats = {
    "status": "initializing",
    "total_analyses": 0,
    "last_analysis": None,
    "monitored_assets": CRYPTOS,
    "uptime_start": datetime.now().isoformat(),
    "version": "V2.6 Premium Quant (2HR Cycle)"
}

trade_history = []
TRADE_FILE = "crypto_trade_history.json"

# =========================================================================
# === DATA PERSISTENCE ===
# =========================================================================

def load_history():
    global trade_history
    try:
        if os.path.exists(TRADE_FILE):
            with open(TRADE_FILE, 'r') as f:
                trade_history = json.load(f)
                print(f"📊 Loaded {len(trade_history)} trades")
    except Exception as e:
        print(f"⚠️ Load history failed: {e}")

def save_history():
    try:
        with open(TRADE_FILE, 'w') as f:
            json.dump(trade_history, f, indent=2)
    except Exception as e:
        print(f"⚠️ Save history failed: {e}")

# =========================================================================
# === HELPER FUNCTIONS ===
# =========================================================================

def calculate_cpr_levels(df_daily):
    """Calculates Daily Pivot Points."""
    if df_daily.empty or len(df_daily) < 2: return None
    prev_day = df_daily.iloc[-2]
    H, L, C = prev_day['high'], prev_day['low'], prev_day['close']
    PP = (H + L + C) / 3.0
    BC = (H + L) / 2.0
    TC = PP - BC + PP
    return {
        'PP': PP, 'TC': TC, 'BC': BC,
        'R1': 2*PP - L, 'S1': 2*PP - H,
        'R2': PP + (H - L), 'S2': PP - (H - L)
    }

def fetch_data_safe(symbol, timeframe):
    """Robust fetcher with retries."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            if not exchange.markets: exchange.load_markets()
            market_id = exchange.market(symbol)['id']
            ohlcv = exchange.fetch_ohlcv(market_id, timeframe, limit=100)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            df['sma9'] = df['close'].rolling(9).mean()
            df['sma20'] = df['close'].rolling(20).mean()
            return df.dropna()
        except:
            time.sleep(2)
    return pd.DataFrame()

# =========================================================================
# === TRADING & TRACKING LOGIC ===
# =========================================================================

def record_trade(symbol, signal, entry, tp1, tp2, sl):
    """Save trade to history."""
    try:
        t = {
            "id": len(trade_history) + 1,
            "symbol": symbol,
            "signal": signal,
            "entry": float(entry),
            "tp1": float(tp1),
            "tp2": float(tp2),
            "sl": float(sl),
            "timestamp": datetime.now().isoformat(),
            "status": "ACTIVE",
            "outcome": None,
            "pnl_percent": 0.0
        }
        trade_history.append(t)
        save_history()
    except Exception as e:
        print(f"❌ Record trade error: {e}")

def check_trades():
    """Check active trades for TP/SL hits."""
    global trade_history
    updated = False
    
    for trade in trade_history:
        if trade['status'] == 'ACTIVE':
            try:
                # Use entry timeframe for checking
                df = fetch_data_safe(trade['symbol'], TIMEFRAME_ENTRY)
                if df.empty: continue
                
                current_price = float(df.iloc[-1]['close'])
                is_buy = "BUY" in trade['signal']
                new_status = None
                
                # --- WIN/LOSS LOGIC (STRICT - NO PARTIALS) ---
                if is_buy:
                    if current_price >= trade['tp2']:
                        new_status = 'TP2_HIT'; trade['outcome'] = 'WIN'
                    elif current_price >= trade['tp1']:
                        # TP1 is now a full WIN
                        new_status = 'TP1_HIT'; trade['outcome'] = 'WIN'
                    elif current_price <= trade['sl']:
                        new_status = 'SL_HIT'; trade['outcome'] = 'LOSS'
                else: # SELL
                    if current_price <= trade['tp2']:
                        new_status = 'TP2_HIT'; trade['outcome'] = 'WIN'
                    elif current_price <= trade['tp1']:
                        # TP1 is now a full WIN
                        new_status = 'TP1_HIT'; trade['outcome'] = 'WIN'
                    elif current_price >= trade['sl']:
                        new_status = 'SL_HIT'; trade['outcome'] = 'LOSS'
                
                if new_status:
                    trade['status'] = new_status
                    # Calculate % gain/loss
                    diff = (current_price - trade['entry']) / trade['entry'] * 100
                    if not is_buy: diff = -diff
                    trade['pnl_percent'] = diff
                    
                    msg = f"🔔 <b>UPDATE:</b> {trade['symbol']} hit {new_status} ({trade['outcome']})"
                    asyncio.run(bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode='HTML'))
                    updated = True
                    
                time.sleep(1) # Rate limit safety
                
            except Exception as e:
                print(f"Check error {trade['symbol']}: {e}")
    
    if updated: save_history()

def daily_report():
    """Generate Win/Loss report."""
    try:
        check_trades() # Update first
        
        now = datetime.now()
        last_24h = now - timedelta(hours=24)
        recent = [t for t in trade_history if datetime.fromisoformat(t['timestamp']) >= last_24h]
        
        if not recent:
            msg = "📊 <b>24H REPORT</b>\n\nNo trades in last 24h."
        else:
            wins = len([t for t in recent if t.get('outcome') == 'WIN'])
            losses = len([t for t in recent if t.get('outcome') == 'LOSS'])
            net_pct = sum([t.get('pnl_percent', 0) for t in recent])
            
            msg = (
                f"📊 <b>24H CRYPTO REPORT</b>\n\n"
                f"Signals: {len(recent)}\n"
                f"✅ Wins: {wins}\n"
                f"❌ Losses: {losses}\n"
                f"Net PnL: {'🟢' if net_pct >= 0 else '🔴'} {net_pct:+.2f}%"
            )
            
        asyncio.run(bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode='HTML'))
    except Exception as e:
        print(f"Report error: {e}")

# =========================================================================
# === SIGNAL GENERATION ===
# =========================================================================

def generate_and_send_signal(symbol):
    global bot_stats
    try:
        # 1. Fetch Multi-Timeframe Data
        df_4h = fetch_data_safe(symbol, TIMEFRAME_MAIN)
        df_1h = fetch_data_safe(symbol, TIMEFRAME_ENTRY)
        
        if not exchange.markets: exchange.load_markets()
        market_id = exchange.market(symbol)['id']
        ohlcv_d = exchange.fetch_ohlcv(market_id, '1d', limit=5)
        df_d = pd.DataFrame(ohlcv_d, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        cpr = calculate_cpr_levels(df_d)

        if df_4h.empty or df_1h.empty or cpr is None: return

        # 2. Extract Key Values
        price = df_4h.iloc[-1]['close']
        trend_4h = "BULLISH" if df_4h.iloc[-1]['sma9'] > df_4h.iloc[-1]['sma20'] else "BEARISH"
        trend_1h = "BULLISH" if df_1h.iloc[-1]['sma9'] > df_1h.iloc[-1]['sma20'] else "BEARISH"
        
        # 3. Master Signal Logic
        signal = "WAIT (Neutral)"
        emoji = "⏳"
        
        if trend_4h == "BULLISH" and trend_1h == "BULLISH" and price > cpr['PP']:
            signal = "STRONG BUY"
            emoji = "🚀"
        elif trend_4h == "BEARISH" and trend_1h == "BEARISH" and price < cpr['PP']:
            signal = "STRONG SELL"
            emoji = "🔻"

        if "BUY" not in signal and "SELL" not in signal:
            return

        # 4. Calculate Risk/Reward
        is_buy = "BUY" in signal
        tp1 = cpr['R1'] if is_buy else cpr['S1']
        tp2 = cpr['R2'] if is_buy else cpr['S2']
        sl = min(cpr['BC'], cpr['TC']) if is_buy else max(cpr['BC'], cpr['TC'])

        # 5. Record Trade
        record_trade(symbol, signal, price, tp1, tp2, sl)

        # 6. Send Message
        message = (
            f"╔════════════════════════════════╗\n"
            f"  🏆 <b>CRYPTO AI SIGNAL</b>\n"
            f"╚════════════════════════════════╝\n\n"
            f"<b>Asset:</b> {symbol}\n"
            f"<b>Price:</b> <code>{price:,.2f}</code>\n\n"
            f"--- 🚨 {emoji} <b>SIGNAL: {signal}</b> 🚨 ---\n\n"
            f"<b>📈 CONFLUENCE:</b>\n"
            f"• 4H: <code>{trend_4h}</code>\n"
            f"• 1H: <code>{trend_1h}</code>\n"
            f"• Pivot: {'Above' if price > cpr['PP'] else 'Below'} PP\n\n"
            f"<b>🎯 TARGETS:</b>\n"
            f"✅ TP1: <code>{tp1:,.2f}</code>\n"
            f"🔥 TP2: <code>{tp2:,.2f}</code>\n"
            f"🛑 SL: <code>{sl:,.2f}</code>\n\n"
            f"<i>Powered by Advanced CryptoBot</i>"
        )

        asyncio.run(bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='HTML'))
        
        bot_stats['total_analyses'] += 1
        bot_stats['last_analysis'] = datetime.now().isoformat()
        bot_stats['status'] = "operational"

    except Exception as e:
        print(f"❌ Analysis failed for {symbol}: {e}")

# =========================================================================
# === SCHEDULER & FLASK ===
# =========================================================================

def start_bot():
    print(f"🚀 Initializing {bot_stats['version']}...")
    load_history()
    
    scheduler = BackgroundScheduler()
    
    # --- ANALYSIS SCHEDULE (EVERY 2 HOURS) ---
    for s in CRYPTOS:
        # hour='*/2' = 00:00, 02:00, 04:00 ...
        scheduler.add_job(generate_and_send_signal, 'cron', hour='*/2', minute='0', args=[s.strip()])
    
    # Trade Checker (Every 15 mins to catch wins/losses quickly)
    scheduler.add_job(check_trades, 'cron', minute='15,30,45')
    
    # Daily Report (Every day at 9 AM)
    scheduler.add_job(daily_report, 'cron', hour='9', minute='0')
    
    scheduler.start()
    
    # Run initial analysis in thread so we don't wait 2 hours for first signal
    for s in CRYPTOS:
        threading.Thread(target=generate_and_send_signal, args=(s.strip(),)).start()

start_bot()

app = Flask(__name__)

@app.route('/')
def home():
    total = len(trade_history)
    wins = len([t for t in trade_history if t.get('outcome') == 'WIN'])
    losses = len([t for t in trade_history if t.get('outcome') == 'LOSS'])
    wr = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
    
    return render_template_string("""
        <body style="font-family:sans-serif; background:#0f172a; color:#f8fafc; text-align:center; padding-top:50px;">
            <div style="background:#1e293b; display:inline-block; padding:40px; border-radius:15px; border: 1px solid #334155;">
                <h1 style="color:#22d3ee;">AI Quant Dashboard</h1>
                <p style="font-size:1.2em;">Status: <span style="color:#4ade80;">Active</span></p>
                <hr style="border-color:#334155;">
                <div style="text-align:left; margin-top:20px;">
                    <p><b>Trades:</b> {{total}}</p>
                    <p><b>Wins:</b> {{wins}} <span style="color:#4ade80;">(TP1+TP2)</span></p>
                    <p><b>Losses:</b> {{losses}}</p>
                    <p><b>Win Rate:</b> {{wr}}%</p>
                </div>
                <hr style="border-color:#334155;">
                <p style="font-size:0.8em; color:#94a3b8;">{{t}}</p>
            </div>
        </body>
    """, total=total, wins=wins, losses=losses, wr=round(wr, 1), t=bot_stats['last_analysis'])

@app.route('/health')
def health(): return jsonify({"status": "healthy"}), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
