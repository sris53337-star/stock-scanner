from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf
import pandas as pd
import requests as req
import os
import threading
import time
import io
import json
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from datetime import datetime, timedelta

app = Flask(__name__)
CORS(app, origins="*", supports_credentials=False)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SHEETS_URL       = "https://script.google.com/macros/s/AKfycbzu0aDb-n4re6qw_RtkkAYA-EbdhQcTnS9DoDd4wxhb4DTMKE89SUFxqtoeAa2mBx_V/exec"
EMAIL_FROM       = os.environ.get("EMAIL_FROM", "")
EMAIL_PASSWORD   = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_TO         = os.environ.get("EMAIL_TO", "")

# Trading config
CAPITAL          = 5000
RISK_PCT         = 1.5       # 1.5% risk
BROKERAGE        = 10        # INDmoney intraday
LEVERAGE         = 1         # No leverage Month 1
MIN_CONFLUENCE   = 6         # Minimum score to alert

sent_signals     = {}
active_trades    = {}
eod_sent         = False

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        req.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def send_telegram_photo(image_bytes, caption=""):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        req.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "caption": caption,
            "parse_mode": "HTML"
        }, files={"photo": ("chart.png", image_bytes, "image/png")}, timeout=15)
    except Exception as e:
        print(f"Photo error: {e}")

def send_telegram_album(images, caption=""):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url   = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMediaGroup"
        files = {}
        media = []
        for i, img_bytes in enumerate(images):
            key = f"photo{i}"
            files[key] = (f"chart{i}.png", img_bytes, "image/png")
            item = {"type": "photo", "media": f"attach://{key}"}
            if i == 0:
                item["caption"]    = caption
                item["parse_mode"] = "HTML"
            media.append(item)
        req.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "media": json.dumps(media)
        }, files=files, timeout=20)
    except Exception as e:
        print(f"Album error: {e}")

# ── Email ─────────────────────────────────────────────────────────────────────
def send_email(subject, html_body, images=None):
    print(f"EMAIL DEBUG: FROM={EMAIL_FROM} TO={EMAIL_TO} PWD={'SET' if EMAIL_PASSWORD else 'MISSING'}")
    if not EMAIL_FROM or not EMAIL_PASSWORD or not EMAIL_TO:
        print("EMAIL DEBUG: Missing credentials!")
        return
    try:
        msg            = MIMEMultipart('related')
        msg['From']    = EMAIL_FROM
        msg['To']      = EMAIL_TO
        msg['Subject'] = subject
        alt = MIMEMultipart('alternative')
        msg.attach(alt)
        alt.attach(MIMEText(html_body, 'html'))
        if images:
            for i, img_bytes in enumerate(images):
                img = MIMEImage(img_bytes)
                img.add_header('Content-ID', f'<chart{i}>')
                img.add_header('Content-Disposition', 'inline', filename=f'chart{i}.png')
                msg.attach(img)
        print("EMAIL DEBUG: Connecting to Gmail SMTP...")
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(EMAIL_FROM, EMAIL_PASSWORD)
            smtp.send_message(msg)
            print("EMAIL DEBUG: Email sent successfully!")
    except Exception as e:
        print(f"EMAIL ERROR: {e}")

# ── Google Sheets ─────────────────────────────────────────────────────────────
def log_to_sheets(data):
    try:
        req.post(SHEETS_URL, json=data, timeout=10)
    except:
        pass

# ── Indicators ────────────────────────────────────────────────────────────────
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
    tp = (df['High'] + df['Low'] + df['Close']) / 3
    return (tp * df['Volume']).cumsum() / df['Volume'].cumsum()

def compute_adx(df, period=14):
    try:
        high  = df['High']
        low   = df['Low']
        close = df['Close']
        plus_dm  = high.diff()
        minus_dm = -low.diff()
        plus_dm[plus_dm < 0]   = 0
        minus_dm[minus_dm < 0] = 0
        tr = compute_atr(df, period)
        plus_di  = 100 * (plus_dm.ewm(span=period).mean()  / tr)
        minus_di = 100 * (minus_dm.ewm(span=period).mean() / tr)
        dx  = 100 * (abs(plus_di - minus_di) / (plus_di + minus_di))
        adx = dx.ewm(span=period).mean()
        return adx
    except:
        return pd.Series([0] * len(df), index=df.index)

def detect_candle_pattern(df):
    """Detect bullish/bearish candlestick patterns"""
    try:
        last = df.iloc[-1]
        prev = df.iloc[-2]
        o, h, l, c = last['Open'], last['High'], last['Low'], last['Close']
        po, ph, pl, pc = prev['Open'], prev['High'], prev['Low'], prev['Close']
        body      = abs(c - o)
        prev_body = abs(pc - po)
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        total_range = h - l if h != l else 0.001

        # Bullish patterns
        # Hammer
        if (lower_wick > body * 2 and upper_wick < body * 0.5
                and c > o and lower_wick > total_range * 0.6):
            return "BULLISH", "Hammer"

        # Bullish Engulfing
        if (c > o and pc < po and c > po and o < pc
                and body > prev_body):
            return "BULLISH", "Bullish Engulfing"

        # Morning Star (3 candles)
        if len(df) >= 3:
            prev2 = df.iloc[-3]
            if (prev2['Close'] < prev2['Open'] and
                    abs(pc - po) < abs(prev2['Close'] - prev2['Open']) * 0.3 and
                    c > o and c > (prev2['Open'] + prev2['Close']) / 2):
                return "BULLISH", "Morning Star"

        # Piercing Line
        if (pc < po and c > o and
                o < pc and c > (po + pc) / 2):
            return "BULLISH", "Piercing Line"

        # Bearish patterns
        # Shooting Star
        if (upper_wick > body * 2 and lower_wick < body * 0.5
                and c < o and upper_wick > total_range * 0.6):
            return "BEARISH", "Shooting Star"

        # Bearish Engulfing
        if (c < o and pc > po and c < po and o > pc
                and body > prev_body):
            return "BEARISH", "Bearish Engulfing"

        # Evening Star
        if len(df) >= 3:
            prev2 = df.iloc[-3]
            if (prev2['Close'] > prev2['Open'] and
                    abs(pc - po) < abs(prev2['Close'] - prev2['Open']) * 0.3 and
                    c < o and c < (prev2['Open'] + prev2['Close']) / 2):
                return "BEARISH", "Evening Star"

        # Dark Cloud Cover
        if (pc > po and c < o and
                o > pc and c < (po + pc) / 2):
            return "BEARISH", "Dark Cloud Cover"

        return "NEUTRAL", "No Pattern"
    except:
        return "NEUTRAL", "No Pattern"

def get_pdh_pdl(ticker):
    """Get previous day high and low"""
    try:
        df = yf.download(ticker, period="5d", interval="1d", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna()
        if len(df) >= 2:
            return float(df['High'].iloc[-2]), float(df['Low'].iloc[-2])
        return None, None
    except:
        return None, None

def get_nifty_trend():
    """Get Nifty 50 trend from 1HR chart"""
    try:
        df = yf.download("^NSEI", period="5d", interval="1h", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna()
        if len(df) < 21:
            return "NEUTRAL"
        df['EMA9']  = compute_ema(df['Close'], 9)
        df['EMA21'] = compute_ema(df['Close'], 21)
        last = df.iloc[-1]
        if float(last['EMA9']) > float(last['EMA21']):
            return "BULLISH"
        elif float(last['EMA9']) < float(last['EMA21']):
            return "BEARISH"
        return "NEUTRAL"
    except:
        return "NEUTRAL"

def calculate_position(entry, sl):
    """Calculate position size with 1.5% risk"""
    risk_amount    = CAPITAL * (RISK_PCT / 100)  # ₹75
    risk_per_share = abs(entry - sl)
    if risk_per_share == 0:
        return 0, 0, 0, 0
    risk_based    = int(risk_amount / risk_per_share)
    capital_based = int((CAPITAL * LEVERAGE) / entry)
    shares        = min(risk_based, capital_based)
    if shares <= 0:
        return 0, 0, 0, 0
    cost     = round(shares * entry, 2)
    max_loss = round(shares * risk_per_share + BROKERAGE, 2)
    max_gain = round(shares * risk_per_share * 2 - BROKERAGE, 2)
    return shares, cost, max_loss, max_gain

def load_watchlist():
    try:
        with open("watchlist.txt", "r") as f:
            return [l.strip() for l in f if l.strip() and not l.startswith('#')]
    except:
        return ["ITC.NS", "RELIANCE.NS", "INFY.NS"]

# ── Chart Generator ───────────────────────────────────────────────────────────
def generate_chart(ticker, signal, entry, sl, target,
                   interval="5m", period="2d", title="5 MIN",
                   ema_fast=5, ema_slow=10):
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

        df = df.tail(50)
        df['EMA_F'] = compute_ema(df['Close'], ema_fast)
        df['EMA_S'] = compute_ema(df['Close'], ema_slow)

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6),
                                        gridspec_kw={'height_ratios': [3, 1]},
                                        facecolor='#0a0a0a')
        ax1.set_facecolor('#0d0d0d')
        ax2.set_facecolor('#0d0d0d')

        for i, (idx, row) in enumerate(df.iterrows()):
            o, h, l, c = row['Open'], row['High'], row['Low'], row['Close']
            color = '#00ff88' if c >= o else '#ff4455'
            ax1.plot([i, i], [l, h], color=color, linewidth=0.8, alpha=0.7)
            ax1.bar(i, abs(c - o), bottom=min(c, o),
                    color=color, alpha=0.85, width=0.6)

        ax1.plot(range(len(df)), df['EMA_F'].values,
                 color='#58a6ff', linewidth=1.8,
                 label=f'EMA{ema_fast}', alpha=0.9)
        ax1.plot(range(len(df)), df['EMA_S'].values,
                 color='#ff8800', linewidth=1.8,
                 label=f'EMA{ema_slow}', alpha=0.9)

        ax1.axhline(y=entry,  color='#58a6ff', linestyle='--', linewidth=1.2)
        ax1.axhline(y=sl,     color='#ff4455', linestyle='--', linewidth=1.2)
        ax1.axhline(y=target, color='#00ff88', linestyle='--', linewidth=1.2)

        ax1.text(len(df)-1, target, f' TGT {target}', color='#00ff88',
                 fontsize=7, va='bottom', fontfamily='monospace')
        ax1.text(len(df)-1, entry,  f' ENT {entry}',  color='#58a6ff',
                 fontsize=7, va='bottom', fontfamily='monospace')
        ax1.text(len(df)-1, sl,     f' SL  {sl}',     color='#ff4455',
                 fontsize=7, va='top',    fontfamily='monospace')

        for i, (idx, row) in enumerate(df.iterrows()):
            color = '#00ff8840' if row['Close'] >= row['Open'] else '#ff445540'
            ax2.bar(i, row['Volume'], color=color, width=0.6)

        sig_color = '#00ff88' if signal == 'BULLISH' else '#ff4455'
        ax1.set_title(
            f'{ticker.replace(".NS","")} -- {signal} | {title}',
            color=sig_color, fontsize=11, fontfamily='monospace',
            fontweight='bold', pad=8
        )

        for ax in [ax1, ax2]:
            ax.tick_params(colors='#444', labelsize=7)
            for spine in ax.spines.values():
                spine.set_color('#222')

        ax1.tick_params(axis='x', labelbottom=False)
        ax1.set_ylabel('Price (Rs)', color='#444', fontsize=8)
        ax2.set_ylabel('Volume',     color='#444', fontsize=8)

        step = max(1, len(df)//6)
        ax2.set_xticks(range(0, len(df), step))
        fmt = '%d %b %H:%M' if interval not in ('1d', '1wk') else '%d %b'
        ax2.set_xticklabels(
            [df.index[i].strftime(fmt) for i in range(0, len(df), step)],
            rotation=20, fontsize=6, color='#444'
        )
        ax1.legend(loc='upper left', facecolor='#111',
                   edgecolor='#222', labelcolor='white', fontsize=8)

        plt.tight_layout(pad=1.0)
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=120,
                    facecolor='#0a0a0a', bbox_inches='tight')
        plt.close()
        buf.seek(0)
        return buf.read()
    except Exception as e:
        print(f"Chart error {title}: {e}")
        return None

def generate_all_charts(ticker, signal, entry, sl, target):
    charts = []
    c1 = generate_chart(ticker, signal, entry, sl, target,
                        interval="5m",  period="2d",  title="5 MIN",
                        ema_fast=5,  ema_slow=10)
    if c1: charts.append(c1)
    c2 = generate_chart(ticker, signal, entry, sl, target,
                        interval="15m", period="5d",  title="15 MIN",
                        ema_fast=9,  ema_slow=21)
    if c2: charts.append(c2)
    c3 = generate_chart(ticker, signal, entry, sl, target,
                        interval="1h",  period="1mo", title="1 HOUR",
                        ema_fast=9,  ema_slow=21)
    if c3: charts.append(c3)
    c4 = generate_chart(ticker, signal, entry, sl, target,
                        interval="1d",  period="3mo", title="DAILY",
                        ema_fast=20, ema_slow=50)
    if c4: charts.append(c4)
    return charts

def generate_result_chart(ticker, signal, entry, sl, target,
                          exit_price, result, pnl, shares):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt

        df = yf.download(ticker, period="5d", interval="15m", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna().tail(50)
        df['EMA9']  = compute_ema(df['Close'], 9)
        df['EMA21'] = compute_ema(df['Close'], 21)

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6),
                                        gridspec_kw={'height_ratios': [3, 1]},
                                        facecolor='#0a0a0a')
        ax1.set_facecolor('#0d0d0d')
        ax2.set_facecolor('#0d0d0d')

        for i, (idx, row) in enumerate(df.iterrows()):
            o, h, l, c = row['Open'], row['High'], row['Low'], row['Close']
            color = '#00ff88' if c >= o else '#ff4455'
            ax1.plot([i, i], [l, h], color=color, linewidth=0.8, alpha=0.7)
            ax1.bar(i, abs(c - o), bottom=min(c, o),
                    color=color, alpha=0.85, width=0.6)

        ax1.plot(range(len(df)), df['EMA9'].values,
                 color='#58a6ff', linewidth=1.8, label='EMA9')
        ax1.plot(range(len(df)), df['EMA21'].values,
                 color='#ff8800', linewidth=1.8, label='EMA21')

        is_win = "TARGET" in result
        ax1.axhspan(min(entry, exit_price), max(entry, exit_price),
                    alpha=0.25, color='#00ff88' if is_win else '#ff4455')

        ax1.axhline(y=entry,      color='#58a6ff', linestyle='--', linewidth=1.5)
        ax1.axhline(y=sl,         color='#ff4455', linestyle='--', linewidth=1.0, alpha=0.6)
        ax1.axhline(y=target,     color='#00ff88', linestyle='--', linewidth=1.0, alpha=0.6)
        ax1.axhline(y=exit_price, color='#ffcc00', linestyle='-',  linewidth=2.0)

        ax1.text(len(df)-1, target,     f' TGT {target}',     color='#00ff88', fontsize=7, va='bottom', fontfamily='monospace')
        ax1.text(len(df)-1, entry,      f' ENT {entry}',      color='#58a6ff', fontsize=7, va='bottom', fontfamily='monospace')
        ax1.text(len(df)-1, sl,         f' SL  {sl}',         color='#ff4455', fontsize=7, va='top',    fontfamily='monospace')
        ax1.text(len(df)-1, exit_price, f' EXIT {exit_price}',color='#ffcc00', fontsize=8, va='bottom', fontfamily='monospace', fontweight='bold')

        pnl_color = '#00ff88' if pnl >= 0 else '#ff4455'
        pnl_text  = f"{'PROFIT' if pnl >= 0 else 'LOSS'}  Rs.{abs(pnl)}  ({shares} shares)"
        ax1.text(0.5, 0.97, pnl_text,
                 transform=ax1.transAxes, color=pnl_color,
                 fontsize=10, fontweight='bold', ha='center', va='top',
                 fontfamily='monospace',
                 bbox=dict(boxstyle='round,pad=0.3', facecolor='#111',
                           edgecolor=pnl_color, alpha=0.8))

        for i, (idx, row) in enumerate(df.iterrows()):
            color = '#00ff8840' if row['Close'] >= row['Open'] else '#ff445540'
            ax2.bar(i, row['Volume'], color=color, width=0.6)

        result_label = "TARGET" if "TARGET" in result else "SL" if "SL" in result else "EXIT"
        ax1.set_title(
            f'{ticker.replace(".NS","")} -- {result_label} | P&L: Rs.{pnl}',
            color=pnl_color, fontsize=11, fontfamily='monospace',
            fontweight='bold', pad=8
        )

        for ax in [ax1, ax2]:
            ax.tick_params(colors='#444', labelsize=7)
            for spine in ax.spines.values():
                spine.set_color('#222')

        ax1.tick_params(axis='x', labelbottom=False)
        ax1.set_ylabel('Price (Rs)', color='#444', fontsize=8)
        ax2.set_ylabel('Volume',     color='#444', fontsize=8)

        step = max(1, len(df)//6)
        ax2.set_xticks(range(0, len(df), step))
        ax2.set_xticklabels(
            [df.index[i].strftime('%d %b %H:%M') for i in range(0, len(df), step)],
            rotation=20, fontsize=6, color='#444'
        )
        ax1.legend(loc='upper left', facecolor='#111',
                   edgecolor='#222', labelcolor='white', fontsize=8)

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

# ── Trade Monitor ─────────────────────────────────────────────────────────────
def monitor_trades():
    global eod_sent
    while True:
        try:
            now         = datetime.utcnow()
            ist_mins    = now.hour * 60 + now.minute + 330
            ist_hour    = (ist_mins // 60) % 24
            ist_minute  = ist_mins % 60

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
                            result = 'TARGET HIT'
                            exit_price = target
                        elif current_price <= sl:
                            result = 'SL HIT'
                            exit_price = sl
                    else:
                        if current_price <= target:
                            result = 'TARGET HIT'
                            exit_price = target
                        elif current_price >= sl:
                            result = 'SL HIT'
                            exit_price = sl

                    if ist_hour == 15 and ist_minute >= 20 and not result:
                        result = 'MANUAL EXIT'
                        exit_price = current_price

                    if result:
                        pnl   = round(shares * (exit_price - entry) *
                                      (1 if signal == 'BULLISH' else -1) - BROKERAGE, 2)
                        result_label = "TARGET HIT" if "TARGET" in result else "SL HIT" if "SL" in result else "MANUAL EXIT"
                        emoji = "Target" if "TARGET" in result else "SL" if "SL" in result else "Exit"

                        result_chart = generate_result_chart(
                            ticker, signal, entry, sl, target,
                            exit_price, result, pnl, shares
                        )
                        if result_chart:
                            send_telegram_photo(result_chart,
                                caption=f"{emoji} {ticker.replace('.NS','')} -- {result_label} | P&L: Rs.{pnl}")

                        msg = (
                            f"<b>{result_label}</b>\n"
                            f"<b>{ticker}</b>\n\n"
                            f"Entry:  Rs.{entry}\n"
                            f"Exit:   Rs.{exit_price}\n"
                            f"Shares: {shares}\n\n"
                            f"Net P&L: <b>Rs.{pnl}</b>\n"
                            f"(incl. Rs.{BROKERAGE} brokerage)"
                        )
                        send_telegram(msg)
                        log_to_sheets({
                            "action":     "update_result",
                            "ticker":     ticker,
                            "result":     result_label,
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
        scores    = [d.get('score', 0) for d in sent_signals.values()]
        avg_score = round(sum(scores)/len(scores), 1) if scores else 0

        msg = (
            f"<b>EOD SUMMARY</b> -- {datetime.utcnow().strftime('%d %b %Y')}\n"
            f"------------------------\n"
            f"BULLISH signals: {len(bullish)}\n"
            f"BEARISH signals: {len(bearish)}\n"
            f"Total signals:   {total}\n"
            f"Avg score:       {avg_score}/8\n"
            f"Stocks scanned:  {len(watchlist)}\n"
            f"------------------------\n"
        )
        if bullish:
            msg += "Bullish: " + ", ".join([t.replace('.NS','') for t in bullish]) + "\n"
        if bearish:
            msg += "Bearish: " + ", ".join([t.replace('.NS','') for t in bearish]) + "\n"
        if total == 0:
            msg += "No strong signals today\n"
        msg += "------------------------\nMarket closed. See you tomorrow 9:15 AM!"
        send_telegram(msg)
    except:
        pass

monitor_thread = threading.Thread(target=monitor_trades, daemon=True)
monitor_thread.start()

# ── Scan Route ────────────────────────────────────────────────────────────────
@app.route("/scan/<ticker>")
def scan(ticker):
    try:
        # ── Time check ────────────────────────────────────────────────────────
        now_utc    = datetime.utcnow()
        ist_mins   = now_utc.hour * 60 + now_utc.minute + 330
        ist_hour   = (ist_mins // 60) % 24
        ist_minute = ist_mins % 60

        # No trades 9:15-9:30 AM and after 2:00 PM
        too_early = (ist_hour == 9 and ist_minute < 30)
        too_late  = ist_hour >= 14

        # ── 5 MIN data (signal) ───────────────────────────────────────────────
        df5 = yf.download(ticker, period="2d", interval="5m", progress=False)
        if isinstance(df5.columns, pd.MultiIndex):
            df5.columns = df5.columns.get_level_values(0)
        df5 = df5.dropna()
        if len(df5) < 20:
            return jsonify({"error": "Not enough 5min data"}), 404

        df5['EMA5']    = compute_ema(df5['Close'], 5)
        df5['EMA10']   = compute_ema(df5['Close'], 10)
        df5['RSI']     = compute_rsi(df5['Close'], 14)
        df5['ATR']     = compute_atr(df5, 14)
        df5['VWAP']    = compute_vwap(df5)
        df5['ADX']     = compute_adx(df5, 14)
        df5['VOL_AVG'] = df5['Volume'].rolling(window=20).mean()
        df5 = df5.dropna()

        last5 = df5.iloc[-1]
        prev5 = df5.iloc[-2]

        price      = float(last5['Close'])
        ema5       = float(last5['EMA5'])
        ema10      = float(last5['EMA10'])
        rsi        = round(float(last5['RSI']), 2)
        atr_val    = round(float(last5['ATR']), 2)
        vwap_val   = round(float(last5['VWAP']), 2)
        adx_val    = round(float(last5['ADX']), 2)
        volume     = int(last5['Volume'])
        vol_avg    = int(last5['VOL_AVG'])
        vol_ratio  = round(volume / vol_avg, 2) if vol_avg > 0 else 0
        above_vwap = price > vwap_val

        ema_bull_5m = float(prev5['EMA5']) <= float(prev5['EMA10']) and ema5 > ema10
        ema_bear_5m = float(prev5['EMA5']) >= float(prev5['EMA10']) and ema5 < ema10

        # ── 1 HR trend filter ─────────────────────────────────────────────────
        df1h = yf.download(ticker, period="1mo", interval="1h", progress=False)
        if isinstance(df1h.columns, pd.MultiIndex):
            df1h.columns = df1h.columns.get_level_values(0)
        df1h = df1h.dropna()
        hr_trend = "NEUTRAL"
        if len(df1h) >= 21:
            df1h['EMA9']  = compute_ema(df1h['Close'], 9)
            df1h['EMA21'] = compute_ema(df1h['Close'], 21)
            last1h = df1h.iloc[-1]
            if float(last1h['EMA9']) > float(last1h['EMA21']):
                hr_trend = "BULLISH"
            elif float(last1h['EMA9']) < float(last1h['EMA21']):
                hr_trend = "BEARISH"

        # ── Nifty trend ───────────────────────────────────────────────────────
        nifty_trend = get_nifty_trend()

        # ── PDH / PDL ─────────────────────────────────────────────────────────
        pdh, pdl = get_pdh_pdl(ticker)

        # ── Candlestick pattern ───────────────────────────────────────────────
        candle_dir, candle_name = detect_candle_pattern(df5)

        # ── Determine signal direction ────────────────────────────────────────
        if ema_bull_5m:
            direction = "BULLISH"
        elif ema_bear_5m:
            direction = "BEARISH"
        else:
            return jsonify({
                "ticker":  ticker,
                "price":   round(price, 2),
                "signal":  None,
                "score":   0,
                "message": "No EMA crossover"
            })

        # ── Confluence scoring ────────────────────────────────────────────────
        scores = {}

        # 1. EMA 5/10 crossover (MUST HAVE)
        scores['ema_cross'] = direction in ("BULLISH", "BEARISH")

        # 2. 1HR trend agrees
        scores['hr_trend'] = (
            (direction == "BULLISH" and hr_trend == "BULLISH") or
            (direction == "BEARISH" and hr_trend == "BEARISH")
        )

        # 3. Nifty agrees
        scores['nifty'] = (
            (direction == "BULLISH" and nifty_trend == "BULLISH") or
            (direction == "BEARISH" and nifty_trend == "BEARISH") or
            nifty_trend == "NEUTRAL"
        )

        # 4. Volume 2x+
        scores['volume'] = vol_ratio >= 2.0

        # 5. VWAP
        scores['vwap'] = (
            (direction == "BULLISH" and above_vwap) or
            (direction == "BEARISH" and not above_vwap)
        )

        # 6. RSI in range
        scores['rsi'] = (
            (direction == "BULLISH" and 47 <= rsi <= 63) or
            (direction == "BEARISH" and 40 <= rsi <= 58)
        )

        # 7. PDH/PDL break
        scores['pdh_pdl'] = False
        if pdh and pdl:
            scores['pdh_pdl'] = (
                (direction == "BULLISH" and price > pdh) or
                (direction == "BEARISH" and price < pdl)
            )

        # 8. Candlestick pattern
        scores['candle'] = (
            (direction == "BULLISH" and candle_dir == "BULLISH") or
            (direction == "BEARISH" and candle_dir == "BEARISH")
        )

        # 9. ADX strength
        scores['adx'] = adx_val >= 25

        total_score = sum(scores.values())

        # ── Entry / SL / Target ───────────────────────────────────────────────
        if direction == "BULLISH":
            entry  = round(price, 2)
            sl     = round(price - atr_val, 2)
            target = round(price + atr_val * 2, 2)
        else:
            entry  = round(price, 2)
            sl     = round(price + atr_val, 2)
            target = round(price - atr_val * 2, 2)

        # ── Position sizing based on score ────────────────────────────────────
        if total_score >= 8:
            trade_capital = CAPITAL        # ₹5,000
            signal_grade  = "PERFECT"
            grade_emoji   = "PERFECT SIGNAL"
        elif total_score == 7:
            trade_capital = CAPITAL * 0.5  # ₹2,500
            signal_grade  = "STRONG"
            grade_emoji   = "STRONG SIGNAL"
        elif total_score == 6:
            trade_capital = CAPITAL * 0.25 # ₹1,250
            signal_grade  = "MODERATE"
            grade_emoji   = "MODERATE SIGNAL"
        else:
            return jsonify({
                "ticker":  ticker,
                "price":   round(price, 2),
                "signal":  direction,
                "score":   total_score,
                "message": f"Score {total_score}/9 below minimum {MIN_CONFLUENCE}"
            })

        # Calculate position
        risk_amount    = trade_capital * (RISK_PCT / 100)
        risk_per_share = abs(entry - sl)
        if risk_per_share == 0:
            return jsonify({"error": "Zero risk per share"}), 400

        shares        = int(risk_amount / risk_per_share)
        capital_limit = int(trade_capital / entry)
        shares        = min(shares, capital_limit)
        if shares <= 0:
            shares = 1

        cost     = round(shares * entry, 2)
        max_loss = round(shares * risk_per_share + BROKERAGE, 2)
        max_gain = round(shares * risk_per_share * 2 - BROKERAGE, 2)
        net_gain = max_gain

        # Skip if brokerage kills profit
        if net_gain <= 0:
            return jsonify({
                "ticker":  ticker,
                "signal":  direction,
                "score":   total_score,
                "message": "Brokerage exceeds profit - skip trade!"
            })

        # ── Fire signal ───────────────────────────────────────────────────────
        signal_key = f"{ticker}_{direction}"
        if signal_key not in sent_signals and not too_early and not too_late:
            sent_signals[signal_key] = {
                'signal': direction,
                'score':  total_score
            }

            tv_symbol = ticker.replace('.NS', '')
            dir_arrow = "BUY" if direction == "BULLISH" else "SELL"

            # Confluence display
            conf_lines = (
                f"{'YES' if scores['ema_cross'] else 'NO '} EMA 5/10 Cross\n"
                f"{'YES' if scores['hr_trend']  else 'NO '} 1HR Trend {hr_trend}\n"
                f"{'YES' if scores['nifty']     else 'NO '} Nifty {nifty_trend}\n"
                f"{'YES' if scores['volume']    else 'NO '} Volume {vol_ratio}x\n"
                f"{'YES' if scores['vwap']      else 'NO '} VWAP {'Above' if above_vwap else 'Below'}\n"
                f"{'YES' if scores['rsi']       else 'NO '} RSI {rsi}\n"
                f"{'YES' if scores['pdh_pdl']   else 'NO '} PDH/PDL Break\n"
                f"{'YES' if scores['candle']    else 'NO '} {candle_name}\n"
                f"{'YES' if scores['adx']       else 'NO '} ADX {adx_val}"
            )

            msg = (
                f"<b>INTRADAY {direction}</b>\n"
                f"<b>{ticker}</b> @ Rs.{round(price,2)}\n\n"
                f"<b>CONFLUENCES ({total_score}/9):</b>\n"
                f"<code>{conf_lines}</code>\n\n"
                f"<b>{grade_emoji}</b>\n\n"
                f"Entry:  Rs.{entry}\n"
                f"SL:     Rs.{sl}\n"
                f"Target: Rs.{target}\n\n"
                f"<b>POSITION ({signal_grade}):</b>\n"
                f"Capital:   Rs.{trade_capital}\n"
                f"Action:    {dir_arrow} {shares} shares\n"
                f"Cost:      Rs.{cost}\n"
                f"Risk:      Rs.{max_loss}\n"
                f"Max Gain:  Rs.{max_gain}\n"
                f"Brokerage: Rs.{BROKERAGE}\n\n"
                f"ATR: Rs.{atr_val} | RR: 1:2\n\n"
                f"<a href='https://www.tradingview.com/chart/?symbol=NSE:{tv_symbol}'>View on TradingView</a>\n\n"
                f"Exit by 3:20 PM IST"
            )

            # Generate charts
            charts = generate_all_charts(ticker, direction, entry, sl, target)
            if charts:
                send_telegram_album(charts,
                    caption=f"{direction} {ticker.replace('.NS','')} | Score: {total_score}/9")
            send_telegram(msg)

            # Email
            chart_tags = "".join([
                f'<img src="cid:chart{i}" style="width:100%;border-radius:8px;margin-bottom:8px">'
                for i in range(len(charts))
            ])
            email_html = f"""
            <div style="background:#0a0a0a;color:#e0e0e0;font-family:monospace;padding:24px;border-radius:12px;max-width:600px">
              <h2 style="color:{'#00ff88' if direction=='BULLISH' else '#ff4455'}">
                INTRADAY {direction} | Score: {total_score}/9
              </h2>
              <h3>{ticker} @ Rs.{round(price,2)}</h3>
              {chart_tags}
              <pre style="background:#111;padding:12px;border-radius:8px">{conf_lines}</pre>
              <table style="width:100%;border-collapse:collapse;margin:16px 0">
                <tr><td style="padding:8px;background:#111;border:1px solid #222;color:#58a6ff">ENTRY</td>
                    <td style="padding:8px;background:#111;border:1px solid #222;font-weight:bold">Rs.{entry}</td></tr>
                <tr><td style="padding:8px;background:#111;border:1px solid #222;color:#ff4455">STOP LOSS</td>
                    <td style="padding:8px;background:#111;border:1px solid #222;font-weight:bold">Rs.{sl}</td></tr>
                <tr><td style="padding:8px;background:#111;border:1px solid #222;color:#00ff88">TARGET</td>
                    <td style="padding:8px;background:#111;border:1px solid #222;font-weight:bold">Rs.{target}</td></tr>
              </table>
              <p>{dir_arrow} {shares} shares | Cost: Rs.{cost} | Risk: Rs.{max_loss} | Gain: Rs.{max_gain}</p>
              <a href="https://www.tradingview.com/chart/?symbol=NSE:{tv_symbol}"
                 style="display:block;background:#1a1a2e;color:#58a6ff;padding:10px;text-align:center;border-radius:8px;text-decoration:none">
                View on TradingView
              </a>
            </div>
            """
            send_email(
                subject=f"INTRADAY {direction} {ticker.replace('.NS','')} @ Rs.{round(price,2)} | {total_score}/9",
                html_body=email_html,
                images=charts
            )

            # Log to sheets
            log_to_sheets({
                "date":       now_utc.strftime("%d-%b-%Y"),
                "time":       now_utc.strftime("%H:%M"),
                "ticker":     ticker,
                "signal":     direction,
                "score":      total_score,
                "grade":      signal_grade,
                "entry":      entry,
                "sl":         sl,
                "target":     target,
                "shares":     shares,
                "capital":    trade_capital,
                "cost":       cost,
                "max_loss":   max_loss,
                "max_gain":   max_gain,
                "rsi":        rsi,
                "adx":        adx_val,
                "vol_ratio":  vol_ratio,
                "hr_trend":   hr_trend,
                "nifty":      nifty_trend,
                "candle":     candle_name,
                "pdh_break":  scores['pdh_pdl']
            })

            active_trades[ticker] = {
                'signal': direction,
                'entry':  entry,
                'sl':     sl,
                'target': target,
                'shares': shares
            }

        return jsonify({
            "ticker":      ticker,
            "price":       round(price, 2),
            "signal":      direction,
            "score":       total_score,
            "grade":       signal_grade if total_score >= 6 else "WEAK",
            "ema5":        round(ema5, 2),
            "ema10":       round(ema10, 2),
            "rsi":         rsi,
            "adx":         adx_val,
            "vwap":        round(vwap_val, 2),
            "atr":         atr_val,
            "vol_ratio":   vol_ratio,
            "hr_trend":    hr_trend,
            "nifty_trend": nifty_trend,
            "candle":      candle_name,
            "scores":      scores,
            "entry":       entry,
            "sl":          sl,
            "target":      target,
            "history":     [round(float(x), 2) for x in df5['Close'].tail(20).tolist()]
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/watchlist")
def get_watchlist():
    return jsonify(load_watchlist())

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
        charts = generate_all_charts("SBIN.NS", "BULLISH", 812.0, 808.0, 820.0)
        msg = (
            f"<b>TEST ALERT -- System Working!</b>\n"
            f"<b>SBIN</b> @ Rs.812\n\n"
            f"<b>CONFLUENCES (8/9):</b>\n"
            f"<code>"
            f"YES EMA 5/10 Cross\n"
            f"YES 1HR Trend BULLISH\n"
            f"YES Nifty BULLISH\n"
            f"YES Volume 2.1x\n"
            f"YES VWAP Above\n"
            f"YES RSI 55\n"
            f"YES PDH Break\n"
            f"YES Bullish Engulfing\n"
            f"NO  ADX 22"
            f"</code>\n\n"
            f"<b>STRONG SIGNAL (8/9)</b>\n\n"
            f"Entry:  Rs.812\n"
            f"SL:     Rs.808\n"
            f"Target: Rs.820\n\n"
            f"BUY 12 shares | Cost: Rs.2,436\n"
            f"Risk: Rs.58 | Gain: Rs.86\n\n"
            f"THIS IS A TEST - NOT REAL SIGNAL"
        )
        if charts:
            send_telegram_album(charts, caption="TEST -- SBIN BULLISH | 8/9")
        send_telegram(msg)

        send_email(
            subject="TEST -- SBIN BULLISH @ Rs.812 | 8/9",
            html_body="<h2>Test Email Working!</h2><p>Stock Scanner system test successful!</p>",
            images=charts
        )

        return jsonify({"status": "Test alert sent!", "charts": len(charts)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/ping")
def ping():
    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000, threaded=True)
