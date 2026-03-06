from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf
import pandas as pd
import requests as req
import os
import threading
import time
from datetime import datetime

app = Flask(__name__)
CORS(app, origins="*", supports_credentials=False)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SHEETS_URL       = "https://script.google.com/macros/s/AKfycbzu0aDb-n4re6qw_RtkkAYA-EbdhQcTnS9DoDd4wxhb4DTMKE89SUFxqtoeAa2mBx_V/exec"
CAPITAL          = 5000
RISK_PCT         = 2
active_trades    = {}  # tracks open trades for SL/Target monitoring
eod_sent         = False

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        req.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
    except:
        pass

def log_to_sheets(data):
    try:
        req.post(SHEETS_URL, json=data, timeout=10)
    except:
        pass

def compute_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def compute_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(window=period).mean()
    loss = -delta.clip(upper=0).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def compute_atr(df, period=14):
    high_low = df['High'] - df['Low']
    high_close = (df['High'] - df['Close'].shift()).abs()
    low_close = (df['Low'] - df['Close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

def compute_vwap(df):
    typical_price = (df['High'] + df['Low'] + df['Close']) / 3
    return (typical_price * df['Volume']).cumsum() / df['Volume'].cumsum()

def calculate_position_size(entry, sl):
    risk_amount   = CAPITAL * (RISK_PCT / 100)
    risk_per_share = abs(entry - sl)
    if risk_per_share == 0:
        return 0, 0, 0, 0
    shares     = int(risk_amount / risk_per_share)
    cost       = round(shares * entry, 2)
    max_loss   = round(shares * risk_per_share, 2)
    max_gain   = round(max_loss * 2, 2)
    return shares, cost, max_loss, max_gain

def load_watchlist():
    try:
        with open("watchlist.txt", "r") as f:
            return [line.strip() for line in f if line.strip() and not line.startswith('#')]
    except:
        return ["ITC.NS", "RELIANCE.NS", "INFY.NS"]

sent_signals = {}

# ── Trade Monitor Thread ──────────────────────────────────────────────────────
def monitor_trades():
    global eod_sent
    while True:
        try:
            now = datetime.now()
            ist_hour   = (now.hour + 5) % 24
            ist_minute = (now.minute + 30) % 60
            if ist_minute >= 60:
                ist_hour = (ist_hour + 1) % 24

            # EOD summary at 3:35 PM IST
            if ist_hour == 15 and ist_minute >= 35 and not eod_sent:
                send_eod_summary()
                eod_sent = True

            # Reset EOD flag at midnight
            if ist_hour == 0 and ist_minute == 0:
                eod_sent = False
                sent_signals.clear()
                active_trades.clear()

            # Check active trades
            for ticker in list(active_trades.keys()):
                trade = active_trades[ticker]
                try:
                    df = yf.download(ticker, period="1d", interval="5m", progress=False)
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    if df.empty:
                        continue
                    current_price = float(df['Close'].iloc[-1])
                    entry  = trade['entry']
                    sl     = trade['sl']
                    target = trade['target']
                    signal = trade['signal']
                    shares = trade['shares']

                    result     = None
                    exit_price = None

                    if signal == 'BULLISH':
                        if current_price >= target:
                            result = 'TARGET HIT ✅'
                            exit_price = target
                        elif current_price <= sl:
                            result = 'SL HIT ❌'
                            exit_price = sl
                    else:
                        if current_price <= target:
                            result = 'TARGET HIT ✅'
                            exit_price = target
                        elif current_price >= sl:
                            result = 'SL HIT ❌'
                            exit_price = sl

                    # Force exit at 3:20 PM IST
                    if ist_hour == 15 and ist_minute >= 20 and not result:
                        result = 'MANUAL EXIT 📤'
                        exit_price = current_price

                    if result:
                        pnl = round(shares * (exit_price - entry) * (1 if signal == 'BULLISH' else -1) - 40, 2)
                        emoji = "🎯" if "TARGET" in result else "🛑" if "SL" in result else "📤"
                        msg = (
                            f"{emoji} <b>{result}</b>\n"
                            f"📌 <b>{ticker}</b>\n\n"
                            f"Entry:  ₹{entry}\n"
                            f"Exit:   ₹{exit_price}\n"
                            f"Shares: {shares}\n\n"
                            f"💰 Net P&L: <b>₹{pnl}</b>\n"
                            f"(after ₹40 brokerage)"
                        )
                        send_telegram(msg)
                        log_to_sheets({
                            "action": "update_result",
                            "ticker": ticker,
                            "result": result,
                            "exit_price": exit_price,
                            "pnl": pnl
                        })
                        del active_trades[ticker]
                except:
                    pass
        except:
            pass
        time.sleep(300)  # check every 5 minutes

def send_eod_summary():
    try:
        watchlist = load_watchlist()
        bullish = [t for t, d in sent_signals.items() if d.get('signal') == 'BULLISH']
        bearish = [t for t, d in sent_signals.items() if d.get('signal') == 'BEARISH']
        total   = len(sent_signals)

        msg = (
            f"📊 <b>EOD SUMMARY</b> — {datetime.now().strftime('%d %b %Y')}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚀 BULLISH signals: {len(bullish)}\n"
            f"⚠️ BEARISH signals: {len(bearish)}\n"
            f"📈 Total signals:   {total}\n"
            f"🔍 Stocks scanned:  {len(watchlist)}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
        )
        if bullish:
            msg += f"🚀 Bullish: {', '.join([t.replace('.NS','') for t in bullish])}\n"
        if bearish:
            msg += f"⚠️ Bearish: {', '.join([t.replace('.NS','') for t in bearish])}\n"
        if total == 0:
            msg += "😴 No strong signals today\n"
        msg += f"━━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"⏰ Market closed. See you tomorrow 9:15 AM!"
        send_telegram(msg)
    except:
        pass

# Start monitor thread
monitor_thread = threading.Thread(target=monitor_trades, daemon=True)
monitor_thread.start()

@app.route("/watchlist")
def get_watchlist():
    return jsonify(load_watchlist())

@app.route("/scan/<ticker>")
def scan(ticker):
    try:
        df = yf.download(ticker, period="7d", interval="15m", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna()

        if len(df) < 30:
            return jsonify({"error": "Not enough data"}), 404

        df['EMA9']    = compute_ema(df['Close'], 9)
        df['EMA21']   = compute_ema(df['Close'], 21)
        df['RSI']     = compute_rsi(df['Close'], 14)
        df['ATR']     = compute_atr(df, 14)
        df['VWAP']    = compute_vwap(df)
        df['VOL_AVG'] = df['Volume'].rolling(window=20).mean()
        df = df.dropna()

        last = df.iloc[-1]
        prev = df.iloc[-2]

        price     = float(last['Close'])
        ema9      = float(last['EMA9'])
        ema21     = float(last['EMA21'])
        rsi       = round(float(last['RSI']), 2)
        atr       = float(last['ATR'])
        vwap      = float(last['VWAP'])
        volume    = int(last['Volume'])
        vol_avg   = int(last['VOL_AVG'])
        vol_ratio = round(volume / vol_avg, 2) if vol_avg > 0 else 0

        ema_bullish = float(prev['EMA9']) <= float(prev['EMA21']) and ema9 > ema21
        ema_bearish = float(prev['EMA9']) >= float(prev['EMA21']) and ema9 < ema21
        vol_confirm = vol_ratio >= 2.0
        above_vwap  = price > vwap
        rsi_bull_ok = 45 <= rsi <= 65
        rsi_bear_ok = 35 <= rsi <= 55

        bull_score = sum([vol_confirm, above_vwap, rsi_bull_ok])
        bear_score = sum([vol_confirm, not above_vwap, rsi_bear_ok])

        signal = None
        if ema_bullish and vol_confirm and above_vwap and rsi_bull_ok:
            signal = "BULLISH"
        elif ema_bearish and vol_confirm and not above_vwap and rsi_bear_ok:
            signal = "BEARISH"
        elif ema_bullish and bull_score >= 2:
            signal = "WEAK_BULLISH"
        elif ema_bearish and bear_score >= 2:
            signal = "WEAK_BEARISH"

        sl_points     = round(atr * 1.0, 2)
        target_points = round(atr * 2.0, 2)

        if signal in ("BULLISH", "WEAK_BULLISH"):
            entry  = round(price, 2)
            sl     = round(price - sl_points, 2)
            target = round(price + target_points, 2)
        elif signal in ("BEARISH", "WEAK_BEARISH"):
            entry  = round(price, 2)
            sl     = round(price + sl_points, 2)
            target = round(price - target_points, 2)
        else:
            entry = sl = target = None

        # ── Send alert for strong signals only ───────────────────────────────
        if signal in ("BULLISH", "BEARISH") and entry and sl:
            signal_key = f"{ticker}_{signal}"
            if sent_signals.get(signal_key, {}).get('price') != round(price):
                shares, cost, max_loss, max_gain = calculate_position_size(entry, sl)
                sent_signals[signal_key] = {'signal': signal, 'price': round(price)}

                emoji = "🚀" if signal == "BULLISH" else "⚠️"
                msg = (
                    f"{emoji} <b>INTRADAY {signal}</b>\n"
                    f"📌 <b>{ticker}</b> @ ₹{round(price,2)}\n\n"
                    f"✅ Entry:  ₹{entry}\n"
                    f"🛑 SL:     ₹{sl}\n"
                    f"🎯 Target: ₹{target}\n\n"
                    f"💰 <b>POSITION SIZE:</b>\n"
                    f"Capital:   ₹{CAPITAL}\n"
                    f"Risk:      ₹{max_loss} ({RISK_PCT}%)\n"
                    f"Shares:    {shares}\n"
                    f"Cost:      ₹{cost}\n"
                    f"Max Loss:  ₹{max_loss}\n"
                    f"Max Gain:  ₹{max_gain}\n"
                    f"Brokerage: ₹40\n\n"
                    f"📊 RSI: {rsi} | Vol: {vol_ratio}x\n"
                    f"📈 VWAP: {'Above ✅' if above_vwap else 'Below ❌'}\n"
                    f"⚡ ATR: ₹{round(atr,2)}\n\n"
                    f"⏰ Exit by 3:20 PM IST"
                )
                send_telegram(msg)

                # Log to Google Sheets
                now = datetime.now()
                log_to_sheets({
                    "date":      now.strftime("%d-%b-%Y"),
                    "time":      now.strftime("%H:%M"),
                    "ticker":    ticker,
                    "signal":    signal,
                    "entry":     entry,
                    "sl":        sl,
                    "target":    target,
                    "shares":    shares,
                    "cost":      cost,
                    "max_loss":  max_loss,
                    "max_gain":  max_gain,
                    "rsi":       rsi,
                    "vol_ratio": vol_ratio
                })

                # Add to active trades monitor
                active_trades[ticker] = {
                    'signal': signal, 'entry': entry,
                    'sl': sl, 'target': target, 'shares': shares
                }

        return jsonify({
            "ticker":     ticker,
            "price":      round(price, 2),
            "ema9":       round(ema9, 2),
            "ema21":      round(ema21, 2),
            "rsi":        rsi,
            "vwap":       round(vwap, 2),
            "atr":        round(atr, 2),
            "volume":     volume,
            "vol_avg":    vol_avg,
            "vol_ratio":  vol_ratio,
            "above_vwap": above_vwap,
            "signal":     signal,
            "score":      bull_score if ema_bullish else bear_score,
            "entry":      entry,
            "sl":         sl,
            "target":     target,
            "history":    [round(float(x), 2) for x in df['Close'].tail(20).tolist()]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/backtest/<ticker>")
def backtest(ticker):
    try:
        period = request.args.get("period", "3mo")
        df = yf.download(ticker, period=period, interval="1d", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna()
        if len(df) < 30:
            return jsonify({"error": "Not enough data"}), 404
        return jsonify({
            "dates":   [str(d)[:10] for d in df.index.tolist()],
            "opens":   [round(float(x),2) for x in df['Open'].tolist()],
            "highs":   [round(float(x),2) for x in df['High'].tolist()],
            "lows":    [round(float(x),2) for x in df['Low'].tolist()],
            "closes":  [round(float(x),2) for x in df['Close'].tolist()],
            "volumes": [int(x) for x in df['Volume'].tolist()],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/ping")
def ping():
    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
