from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf
import pandas as pd
import requests as req
import os
import threading
import time
import io
# matplotlib imported lazily inside functions
from datetime import datetime
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

app = Flask(__name__)
CORS(app, origins="*", supports_credentials=False)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SHEETS_URL       = "https://script.google.com/macros/s/AKfycbzu0aDb-n4re6qw_RtkkAYA-EbdhQcTnS9DoDd4wxhb4DTMKE89SUFxqtoeAa2mBx_V/exec"
CAPITAL          = 5000
RISK_PCT         = 2

# Email config
EMAIL_FROM     = os.environ.get("EMAIL_FROM", "")      # your gmail
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")  # gmail app password
EMAIL_TO       = os.environ.get("EMAIL_TO", "")        # recipient email

sent_signals  = {}
active_trades = {}
eod_sent      = False

# ── Helpers ───────────────────────────────────────────────────────────────────
def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        req.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       message,
            "parse_mode": "HTML"
        }, timeout=10)
    except:
        pass

def send_telegram_photo(image_bytes, caption=""):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMediaGroup"
        # Send as photo
        url2 = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        req.post(url2, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "caption": caption,
            "parse_mode": "HTML"
        }, files={"photo": ("chart.png", image_bytes, "image/png")}, timeout=15)
    except:
        pass

def send_telegram_album(images, caption=""):
    """Send multiple images as album"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        import json
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMediaGroup"
        files = {}
        media = []
        for i, img_bytes in enumerate(images):
            key = f"photo{i}"
            files[key] = (f"chart{i}.png", img_bytes, "image/png")
            item = {"type": "photo", "media": f"attach://{key}"}
            if i == 0:
                item["caption"] = caption
                item["parse_mode"] = "HTML"
            media.append(item)
        req.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "media": json.dumps(media)
        }, files=files, timeout=20)
    except Exception as e:
        print(f"Album error: {e}")

def send_email(subject, html_body, images=None):
    """Send email with optional chart images"""
    if not EMAIL_FROM or not EMAIL_PASSWORD or not EMAIL_TO:
        return
    try:
        msg = MIMEMultipart('related')
        msg['From']    = EMAIL_FROM
        msg['To']      = EMAIL_TO
        msg['Subject'] = subject

        # HTML body
        alt = MIMEMultipart('alternative')
        msg.attach(alt)
        alt.attach(MIMEText(html_body, 'html'))

        # Attach charts as inline images
        if images:
            for i, img_bytes in enumerate(images):
                img = MIMEImage(img_bytes)
                img.add_header('Content-ID', f'<chart{i}>')
                img.add_header('Content-Disposition', 'inline',
                               filename=f'chart{i}.png')
                msg.attach(img)

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(EMAIL_FROM, EMAIL_PASSWORD)
            smtp.send_message(msg)
    except Exception as e:
        print(f"Email error: {e}")

def log_to_sheets(data):
    try:
        req.post(SHEETS_URL, json=data, timeout=10)
    except:
        pass

def compute_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def compute_rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(window=period).mean()
    loss  = -delta.clip(upper=0).rolling(window=period).mean()
    rs    = gain / loss
    return 100 - (100 / (1 + rs))

def compute_atr(df, period=14):
    high_low   = df['High'] - df['Low']
    high_close = (df['High'] - df['Close'].shift()).abs()
    low_close  = (df['Low']  - df['Close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

def compute_vwap(df):
    typical_price = (df['High'] + df['Low'] + df['Close']) / 3
    return (typical_price * df['Volume']).cumsum() / df['Volume'].cumsum()

def calculate_position_size(entry, sl):
    risk_amount    = CAPITAL * (RISK_PCT / 100)
    risk_per_share = abs(entry - sl)
    if risk_per_share == 0 or entry == 0:
        return 0, 0, 0, 0
    risk_based    = int(risk_amount / risk_per_share)
    capital_based = int(CAPITAL / entry)
    shares        = min(risk_based, capital_based)
    if shares <= 0:
        return 0, 0, 0, 0
    cost     = round(shares * entry, 2)
    max_loss = round(shares * risk_per_share, 2)
    max_gain = round(max_loss * 2, 2)
    return shares, cost, max_loss, max_gain

def load_watchlist():
    try:
        with open("watchlist.txt", "r") as f:
            return [line.strip() for line in f
                    if line.strip() and not line.startswith('#')]
    except:
        return ["ITC.NS", "RELIANCE.NS", "INFY.NS"]

# ── Chart Generator ───────────────────────────────────────────────────────────
def generate_chart(ticker, signal, entry, sl, target, interval="15m", period="5d", title="15 MIN"):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        df = yf.download(ticker, period=period, interval=interval, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna()
        if len(df) < 10:
            return None

        # Use last 40 candles
        df = df.tail(40)
        df['EMA9']  = compute_ema(df['Close'], 9)
        df['EMA21'] = compute_ema(df['Close'], 21)

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6),
                                        gridspec_kw={'height_ratios': [3, 1]},
                                        facecolor='#0a0a0a')
        ax1.set_facecolor('#0d0d0d')
        ax2.set_facecolor('#0d0d0d')

        # Draw candles
        for i, (idx, row) in enumerate(df.iterrows()):
            o, h, l, c = row['Open'], row['High'], row['Low'], row['Close']
            color = '#00ff88' if c >= o else '#ff4455'
            # Wick
            ax1.plot([i, i], [l, h], color=color, linewidth=0.8, alpha=0.7)
            # Body
            ax1.bar(i, abs(c - o), bottom=min(c, o),
                    color=color, alpha=0.85, width=0.6)

        # EMA lines
        ax1.plot(range(len(df)), df['EMA9'].values,
                 color='#58a6ff', linewidth=1.5, label='EMA9', alpha=0.9)
        ax1.plot(range(len(df)), df['EMA21'].values,
                 color='#ff8800', linewidth=1.5, label='EMA21', alpha=0.9)

        # Entry / SL / Target lines
        ax1.axhline(y=entry,  color='#58a6ff', linestyle='--', linewidth=1.2, alpha=0.8)
        ax1.axhline(y=sl,     color='#ff4455', linestyle='--', linewidth=1.2, alpha=0.8)
        ax1.axhline(y=target, color='#00ff88', linestyle='--', linewidth=1.2, alpha=0.8)

        # Labels on lines
        ax1.text(len(df)-1, target, f' TGT ₹{target}', color='#00ff88',
                 fontsize=7, va='bottom', fontfamily='monospace')
        ax1.text(len(df)-1, entry,  f' ENT ₹{entry}',  color='#58a6ff',
                 fontsize=7, va='bottom', fontfamily='monospace')
        ax1.text(len(df)-1, sl,     f' SL  ₹{sl}',     color='#ff4455',
                 fontsize=7, va='top',    fontfamily='monospace')

        # Volume bars
        for i, (idx, row) in enumerate(df.iterrows()):
            color = '#00ff8840' if row['Close'] >= row['Open'] else '#ff445540'
            ax2.bar(i, row['Volume'], color=color, width=0.6)

        # Styling
        sig_color = '#00ff88' if signal == 'BULLISH' else '#ff4455'
        sig_emoji = '🚀' if signal == 'BULLISH' else '⚠️'
        ax1.set_title(f'{sig_emoji} {ticker.replace(".NS","")} — {signal} | {title} CHART',
                      color=sig_color, fontsize=11, fontfamily='monospace',
                      fontweight='bold', pad=8)

        for ax in [ax1, ax2]:
            ax.tick_params(colors='#444', labelsize=7)
            ax.spines['bottom'].set_color('#222')
            ax.spines['top'].set_color('#222')
            ax.spines['left'].set_color('#222')
            ax.spines['right'].set_color('#222')
            ax.yaxis.label.set_color('#444')
            ax.xaxis.label.set_color('#444')

        ax1.tick_params(axis='x', labelbottom=False)
        ax1.set_ylabel('Price (₹)', color='#444', fontsize=8)
        ax2.set_ylabel('Volume', color='#444', fontsize=8)

        # X axis dates
        step = max(1, len(df)//6)
        ax2.set_xticks(range(0, len(df), step))
        ax2.set_xticklabels(
            [df.index[i].strftime('%d %b %H:%M') for i in range(0, len(df), step)],
            rotation=20, fontsize=6, color='#444'
        )

        ax1.legend(loc='upper left', facecolor='#111', edgecolor='#222',
                   labelcolor='white', fontsize=8)

        plt.tight_layout(pad=1.0)

        # Save to bytes
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=120,
                    facecolor='#0a0a0a', bbox_inches='tight')
        plt.close()
        buf.seek(0)
        return buf.read()

    except Exception as e:
        print(f"Chart error: {e}")
        return None

def generate_all_charts(ticker, signal, entry, sl, target):
    """Generate 4 charts: 5min, 15min, 1hr, Daily"""
    charts = []

    # Chart 1: 5 min (precise entry timing)
    c1 = generate_chart(ticker, signal, entry, sl, target,
                        interval="5m", period="2d", title="5 MIN")
    if c1: charts.append(c1)

    # Chart 2: 15 min (intraday detail)
    c2 = generate_chart(ticker, signal, entry, sl, target,
                        interval="15m", period="5d", title="15 MIN")
    if c2: charts.append(c2)

    # Chart 3: 1 hour (trend confirmation)
    c3 = generate_chart(ticker, signal, entry, sl, target,
                        interval="1h", period="1mo", title="1 HOUR")
    if c3: charts.append(c3)

    # Chart 4: Daily (big picture)
    c4 = generate_chart(ticker, signal, entry, sl, target,
                        interval="1d", period="3mo", title="DAILY")
    if c4: charts.append(c4)

    return charts

# ── Result Chart Generator ───────────────────────────────────────────────────
def generate_result_chart(ticker, signal, entry, sl, target, exit_price, result, pnl, shares):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        df = yf.download(ticker, period="5d", interval="15m", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna()
        if len(df) < 10:
            return None

        df = df.tail(50)
        df['EMA9']  = compute_ema(df['Close'], 9)
        df['EMA21'] = compute_ema(df['Close'], 21)

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6),
                                        gridspec_kw={'height_ratios': [3, 1]},
                                        facecolor='#0a0a0a')
        ax1.set_facecolor('#0d0d0d')
        ax2.set_facecolor('#0d0d0d')

        # Draw candles
        for i, (idx, row) in enumerate(df.iterrows()):
            o, h, l, c = row['Open'], row['High'], row['Low'], row['Close']
            color = '#00ff88' if c >= o else '#ff4455'
            ax1.plot([i, i], [l, h], color=color, linewidth=0.8, alpha=0.7)
            ax1.bar(i, abs(c - o), bottom=min(c, o),
                    color=color, alpha=0.85, width=0.6)

        # EMA lines
        ax1.plot(range(len(df)), df['EMA9'].values,
                 color='#58a6ff', linewidth=1.5, label='EMA9', alpha=0.9)
        ax1.plot(range(len(df)), df['EMA21'].values,
                 color='#ff8800', linewidth=1.5, label='EMA21', alpha=0.9)

        # Shade profit/loss zone
        is_win = "TARGET" in result
        zone_color = '#00ff8820' if is_win else '#ff445520'
        ax1.axhspan(min(entry, exit_price), max(entry, exit_price),
                    alpha=0.3, color='#00ff88' if is_win else '#ff4455')

        # Key levels
        ax1.axhline(y=entry,      color='#58a6ff', linestyle='--', linewidth=1.5, alpha=0.9)
        ax1.axhline(y=sl,         color='#ff4455', linestyle='--', linewidth=1.0, alpha=0.6)
        ax1.axhline(y=target,     color='#00ff88', linestyle='--', linewidth=1.0, alpha=0.6)
        ax1.axhline(y=exit_price, color='#ffcc00', linestyle='-',  linewidth=2.0, alpha=0.9)

        # Labels
        ax1.text(len(df)-1, target,     f' TGT ₹{target}',     color='#00ff88', fontsize=7, va='bottom', fontfamily='monospace')
        ax1.text(len(df)-1, entry,      f' ENT ₹{entry}',      color='#58a6ff', fontsize=7, va='bottom', fontfamily='monospace')
        ax1.text(len(df)-1, sl,         f' SL  ₹{sl}',         color='#ff4455', fontsize=7, va='top',    fontfamily='monospace')
        ax1.text(len(df)-1, exit_price, f' EXIT ₹{exit_price}',color='#ffcc00', fontsize=8, va='bottom', fontfamily='monospace', fontweight='bold')

        # P&L annotation in center
        pnl_color = '#00ff88' if pnl >= 0 else '#ff4455'
        pnl_text  = f"{'✅ PROFIT' if pnl >= 0 else '❌ LOSS'}  ₹{abs(pnl)}  ({shares} shares)"
        ax1.text(0.5, 0.97, pnl_text,
                 transform=ax1.transAxes,
                 color=pnl_color, fontsize=10, fontweight='bold',
                 ha='center', va='top', fontfamily='monospace',
                 bbox=dict(boxstyle='round,pad=0.3', facecolor='#111', edgecolor=pnl_color, alpha=0.8))

        # Volume bars
        for i, (idx, row) in enumerate(df.iterrows()):
            color = '#00ff8840' if row['Close'] >= row['Open'] else '#ff445540'
            ax2.bar(i, row['Volume'], color=color, width=0.6)

        # Title
        result_emoji = "🎯" if "TARGET" in result else "🛑" if "SL" in result else "📤"
        ax1.set_title(
            f'{result_emoji} {ticker.replace(".NS","")} — {result} | Net P&L: ₹{pnl}',
            color=pnl_color, fontsize=11, fontfamily='monospace',
            fontweight='bold', pad=8
        )

        for ax in [ax1, ax2]:
            ax.tick_params(colors='#444', labelsize=7)
            ax.spines['bottom'].set_color('#222')
            ax.spines['top'].set_color('#222')
            ax.spines['left'].set_color('#222')
            ax.spines['right'].set_color('#222')

        ax1.tick_params(axis='x', labelbottom=False)
        ax1.set_ylabel('Price (₹)', color='#444', fontsize=8)
        ax2.set_ylabel('Volume',    color='#444', fontsize=8)

        step = max(1, len(df)//6)
        ax2.set_xticks(range(0, len(df), step))
        ax2.set_xticklabels(
            [df.index[i].strftime('%d %b %H:%M') for i in range(0, len(df), step)],
            rotation=20, fontsize=6, color='#444'
        )
        ax1.legend(loc='upper left', facecolor='#111', edgecolor='#222',
                   labelcolor='white', fontsize=8)

        plt.tight_layout(pad=1.0)
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=120,
                    facecolor='#0a0a0a', bbox_inches='tight')
        plt.close()
        buf.seek(0)
        return buf.read()

    except Exception as e:
        print(f"Result chart error: {e}")
        return None

# ── Trade Monitor Thread ──────────────────────────────────────────────────────
def monitor_trades():
    global eod_sent
    while True:
        try:
            now = datetime.utcnow()
            ist_minutes = now.hour * 60 + now.minute + 330
            ist_hour    = (ist_minutes // 60) % 24
            ist_minute  = ist_minutes % 60

            if ist_hour == 15 and ist_minute >= 35 and not eod_sent:
                send_eod_summary()
                eod_sent = True

            if ist_hour == 0 and ist_minute < 5:
                eod_sent = False
                sent_signals.clear()
                active_trades.clear()

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

                    if ist_hour == 15 and ist_minute >= 20 and not result:
                        result = 'MANUAL EXIT 📤'
                        exit_price = current_price

                    if result:
                        pnl   = round(shares * (exit_price - entry) *
                                      (1 if signal == 'BULLISH' else -1) - 40, 2)
                        emoji = ("🎯" if "TARGET" in result
                                 else "🛑" if "SL" in result else "📤")

                        # Generate result chart
                        result_chart = generate_result_chart(
                            ticker, signal, entry, sl, target,
                            exit_price, result, pnl, shares
                        )
                        if result_chart:
                            send_telegram_photo(result_chart,
                                caption=f"{emoji} {ticker.replace('.NS','')} — {result} | P&L: ₹{pnl}")

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
                            "action":     "update_result",
                            "ticker":     ticker,
                            "result":     result,
                            "exit_price": exit_price,
                            "pnl":        pnl
                        })
                        del active_trades[ticker]
                except:
                    pass
        except:
            pass
        time.sleep(300)

def send_eod_summary():
    try:
        watchlist = load_watchlist()
        bullish   = [t for t, d in sent_signals.items() if d.get('signal') == 'BULLISH']
        bearish   = [t for t, d in sent_signals.items() if d.get('signal') == 'BEARISH']
        total     = len(sent_signals)

        msg = (
            f"📊 <b>EOD SUMMARY</b> — {datetime.utcnow().strftime('%d %b %Y')}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚀 BULLISH signals: {len(bullish)}\n"
            f"⚠️  BEARISH signals: {len(bearish)}\n"
            f"📈 Total signals:   {total}\n"
            f"🔍 Stocks scanned:  {len(watchlist)}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
        )
        if bullish:
            msg += "🚀 Bullish: " + ", ".join(
                [t.replace('.NS','').replace('_BULLISH','') for t in bullish]) + "\n"
        if bearish:
            msg += "⚠️  Bearish: " + ", ".join(
                [t.replace('.NS','').replace('_BEARISH','') for t in bearish]) + "\n"
        if total == 0:
            msg += "😴 No strong signals today\n"
        msg += (
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ Market closed. See you tomorrow 9:15 AM!"
        )
        send_telegram(msg)
    except:
        pass

monitor_thread = threading.Thread(target=monitor_trades, daemon=True)
monitor_thread.start()

# ── Routes ────────────────────────────────────────────────────────────────────
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

        price      = float(last['Close'])
        ema9       = float(last['EMA9'])
        ema21      = float(last['EMA21'])
        rsi        = round(float(last['RSI']), 2)
        atr_val    = round(float(last['ATR']), 2)
        vwap_val   = round(float(last['VWAP']), 2)
        volume     = int(last['Volume'])
        vol_avg    = int(last['VOL_AVG'])
        vol_ratio  = round(volume / vol_avg, 2) if vol_avg > 0 else 0
        above_vwap = price > vwap_val

        ema_bullish  = float(prev['EMA9']) <= float(prev['EMA21']) and ema9 > ema21
        ema_bearish  = float(prev['EMA9']) >= float(prev['EMA21']) and ema9 < ema21
        vol_ok       = vol_ratio >= 2.0
        rsi_bull_ok  = 47 <= rsi <= 63
        rsi_bear_ok  = 40 <= rsi <= 58

        bull_score = sum([vol_ok, above_vwap,     rsi_bull_ok])
        bear_score = sum([vol_ok, not above_vwap, rsi_bear_ok])

        signal = None
        if ema_bullish and vol_ok and above_vwap and rsi_bull_ok:
            signal = "BULLISH"
        elif ema_bearish and vol_ok and not above_vwap and rsi_bear_ok:
            signal = "BEARISH"
        elif ema_bullish and bull_score >= 2:
            signal = "WEAK_BULLISH"
        elif ema_bearish and bear_score >= 2:
            signal = "WEAK_BEARISH"

        if signal in ("BULLISH", "WEAK_BULLISH"):
            entry  = round(price, 2)
            sl     = round(price - atr_val, 2)
            target = round(price + atr_val * 2, 2)
        elif signal in ("BEARISH", "WEAK_BEARISH"):
            entry  = round(price, 2)
            sl     = round(price + atr_val, 2)
            target = round(price - atr_val * 2, 2)
        else:
            entry = sl = target = None

        if signal in ("BULLISH", "BEARISH") and entry and sl:
            signal_key = f"{ticker}_{signal}"

            # No new trades after 2:00 PM IST
            now_utc     = datetime.utcnow()
            ist_minutes = now_utc.hour * 60 + now_utc.minute + 330
            ist_hour    = (ist_minutes // 60) % 24
            too_late    = ist_hour >= 14

            if signal_key not in sent_signals and not too_late:
                shares, cost, max_loss, max_gain = calculate_position_size(entry, sl)
                net_gain  = max_gain - 40
                direction = "BUY" if signal == "BULLISH" else "SELL"
                emoji     = "🚀" if signal == "BULLISH" else "⚠️"
                tv_symbol = ticker.replace('.NS', '')

                sent_signals[signal_key] = {'signal': signal}

                # ── Generate 3 charts ────────────────────────────────────────
                charts = generate_all_charts(ticker, signal, entry, sl, target)

                # ── Text alert ───────────────────────────────────────────────
                msg = (
                    f"{emoji} <b>INTRADAY {signal}</b>\n"
                    f"📌 <b>{ticker}</b> @ ₹{round(price, 2)}\n\n"
                    f"✅ Entry:  ₹{entry}\n"
                    f"🛑 SL:     ₹{sl}\n"
                    f"🎯 Target: ₹{target}\n\n"
                    f"💰 <b>POSITION SIZE:</b>\n"
                    f"Action:    <b>{direction} {shares} shares</b>\n"
                    f"Cost:      ₹{cost}\n"
                    f"Max Loss:  ₹{max_loss}\n"
                    f"Max Gain:  ₹{max_gain}\n"
                    f"Brokerage: ₹40\n"
                    f"Net Gain:  ₹{net_gain}\n\n"
                    f"📊 RSI: {rsi} | Vol: {vol_ratio}x\n"
                    f"📈 VWAP: {'Above ✅' if above_vwap else 'Below ❌'}\n"
                    f"⚡ ATR: ₹{atr_val}\n\n"
                    f"📊 <a href='https://www.tradingview.com/chart/?symbol=NSE:{tv_symbol}'>View on TradingView</a>\n\n"
                    f"⏰ Exit by 3:20 PM IST"
                )

                # Send charts first as album, then text
                if charts:
                    send_telegram_album(charts, caption=f"{emoji} {ticker.replace('.NS','')} — {signal}")
                send_telegram(msg)

                # Send email alert
                tv_link = f"https://www.tradingview.com/chart/?symbol=NSE:{tv_symbol}"
                chart_tags = "".join([f'<img src="cid:chart{i}" style="width:100%;border-radius:8px;margin-bottom:8px">' for i in range(len(charts))])
                email_html = f"""
                <div style="background:#0a0a0a;color:#e0e0e0;font-family:monospace;padding:24px;border-radius:12px;max-width:600px">
                  <h2 style="color:{'#00ff88' if signal=='BULLISH' else '#ff4455'};margin-bottom:4px">
                    {'🚀' if signal=='BULLISH' else '⚠️'} INTRADAY {signal}
                  </h2>
                  <h3 style="color:#fff;margin-bottom:16px">{ticker} @ ₹{round(price,2)}</h3>
                  {chart_tags}
                  <table style="width:100%;border-collapse:collapse;margin:16px 0">
                    <tr>
                      <td style="padding:8px;background:#111;border:1px solid #222;color:#58a6ff">✅ ENTRY</td>
                      <td style="padding:8px;background:#111;border:1px solid #222;color:#58a6ff;font-weight:bold">₹{entry}</td>
                    </tr>
                    <tr>
                      <td style="padding:8px;background:#111;border:1px solid #222;color:#ff4455">🛑 STOP LOSS</td>
                      <td style="padding:8px;background:#111;border:1px solid #222;color:#ff4455;font-weight:bold">₹{sl}</td>
                    </tr>
                    <tr>
                      <td style="padding:8px;background:#111;border:1px solid #222;color:#00ff88">🎯 TARGET</td>
                      <td style="padding:8px;background:#111;border:1px solid #222;color:#00ff88;font-weight:bold">₹{target}</td>
                    </tr>
                  </table>
                  <div style="background:#111;border:1px solid #222;padding:12px;border-radius:8px;margin-bottom:16px">
                    <p style="color:#888;margin:0 0 8px">💰 POSITION SIZE</p>
                    <p style="margin:4px 0">Action: <b style="color:#fff">{direction} {shares} shares</b></p>
                    <p style="margin:4px 0">Cost: ₹{cost} | Max Loss: ₹{max_loss} | Net Gain: ₹{net_gain}</p>
                  </div>
                  <div style="background:#111;border:1px solid #222;padding:12px;border-radius:8px;margin-bottom:16px">
                    <p style="margin:4px 0">📊 RSI: {rsi} | Vol: {vol_ratio}x | ATR: ₹{atr_val}</p>
                    <p style="margin:4px 0">📈 VWAP: {'Above ✅' if above_vwap else 'Below ❌'}</p>
                  </div>
                  <a href="{tv_link}" style="display:block;background:#1a1a2e;color:#58a6ff;padding:10px;text-align:center;border-radius:8px;text-decoration:none;margin-bottom:16px">
                    📊 View on TradingView →
                  </a>
                  <p style="color:#555;font-size:11px;text-align:center">⚠️ Educational only. Not financial advice. Exit by 3:20 PM IST.</p>
                </div>
                """
                send_email(
                    subject=f"{'🚀' if signal=='BULLISH' else '⚠️'} {ticker.replace('.NS','')} {signal} @ ₹{round(price,2)}",
                    html_body=email_html,
                    images=charts
                )

                # Log to Google Sheets
                now = datetime.utcnow()
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

                active_trades[ticker] = {
                    'signal': signal,
                    'entry':  entry,
                    'sl':     sl,
                    'target': target,
                    'shares': shares
                }

        return jsonify({
            "ticker":     ticker,
            "price":      round(price, 2),
            "ema9":       round(ema9, 2),
            "ema21":      round(ema21, 2),
            "rsi":        rsi,
            "vwap":       vwap_val,
            "atr":        atr_val,
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
            "opens":   [round(float(x), 2) for x in df['Open'].tolist()],
            "highs":   [round(float(x), 2) for x in df['High'].tolist()],
            "lows":    [round(float(x), 2) for x in df['Low'].tolist()],
            "closes":  [round(float(x), 2) for x in df['Close'].tolist()],
            "volumes": [int(x) for x in df['Volume'].tolist()],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/test")
def test_alert():
    try:
        test_charts = generate_all_charts("SBIN.NS", "BULLISH", 812.0, 808.0, 820.0)
        msg = (
            f"🧪 <b>TEST ALERT — System Working!</b>\n"
            f"📌 <b>SBIN</b> @ ₹812.00\n\n"
            f"✅ Entry:  ₹812.00\n"
            f"🛑 SL:     ₹808.00\n"
            f"🎯 Target: ₹820.00\n\n"
            f"💰 <b>POSITION SIZE:</b>\n"
            f"Action:    <b>BUY 25 shares</b>\n"
            f"Cost:      ₹4,900\n"
            f"Max Loss:  ₹100\n"
            f"Max Gain:  ₹200\n"
            f"Net Gain:  ₹160\n\n"
            f"📊 RSI: 55.0 | Vol: 2.1x\n"
            f"📈 VWAP: Above ✅\n"
            f"⚡ ATR: ₹4.0\n\n"
            f"📊 <a href='https://www.tradingview.com/chart/?symbol=NSE:SBIN'>View on TradingView</a>\n\n"
            f"⏰ THIS IS A TEST — NOT A REAL SIGNAL"
        )
        if test_charts:
            send_telegram_album(test_charts,
                caption="🧪 TEST ALERT — SBIN BULLISH")
        send_telegram(msg)
        return jsonify({"status": "✅ Test alert sent!", "charts": len(test_charts)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/ping")
def ping():
    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
