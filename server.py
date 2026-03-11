from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf
import pandas as pd
import requests as req
import os
import threading
import time
import json
from datetime import datetime

app = Flask(__name__)
CORS(app, origins="*", supports_credentials=False)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SHEETS_URL       = "https://script.google.com/macros/s/AKfycbzu0aDb-n4re6qw_RtkkAYA-EbdhQcTnS9DoDd4wxhb4DTMKE89SUFxqtoeAa2mBx_V/exec"

# Trading config
CAPITAL        = 5000
RISK_PCT       = 1.5   # 1.5% risk
BROKERAGE      = 10    # INDmoney intraday
LEVERAGE       = 1     # No leverage Month 1
MIN_CONFLUENCE = 6     # Minimum score to alert

sent_signals  = {}
active_trades = {}
eod_sent      = False

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram: TOKEN or CHAT_ID missing!")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = req.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       message,
            "parse_mode": "HTML"
        }, timeout=10)
        print(f"Telegram sent: {r.status_code}")
    except Exception as e:
        print(f"Telegram error: {e}")

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
        high     = df['High']
        low      = df['Low']
        plus_dm  = high.diff()
        minus_dm = -low.diff()
        plus_dm[plus_dm < 0]   = 0
        minus_dm[minus_dm < 0] = 0
        tr       = compute_atr(df, period)
        plus_di  = 100 * (plus_dm.ewm(span=period).mean()  / tr)
        minus_di = 100 * (minus_dm.ewm(span=period).mean() / tr)
        dx       = 100 * (abs(plus_di - minus_di) / (plus_di + minus_di))
        return dx.ewm(span=period).mean()
    except:
        return pd.Series([0] * len(df), index=df.index)

def detect_candle_pattern(df):
    try:
        last = df.iloc[-1]
        prev = df.iloc[-2]
        o,  h,  l,  c  = last['Open'], last['High'], last['Low'], last['Close']
        po, ph, pl, pc = prev['Open'], prev['High'], prev['Low'], prev['Close']
        body        = abs(c - o)
        prev_body   = abs(pc - po)
        upper_wick  = h - max(o, c)
        lower_wick  = min(o, c) - l
        total_range = h - l if h != l else 0.001

        if lower_wick > body * 2 and upper_wick < body * 0.5 and c > o and lower_wick > total_range * 0.6:
            return "BULLISH", "Hammer"
        if c > o and pc < po and c > po and o < pc and body > prev_body:
            return "BULLISH", "Bullish Engulfing"
        if len(df) >= 3:
            prev2 = df.iloc[-3]
            if (prev2['Close'] < prev2['Open'] and
                    abs(pc - po) < abs(prev2['Close'] - prev2['Open']) * 0.3 and
                    c > o and c > (prev2['Open'] + prev2['Close']) / 2):
                return "BULLISH", "Morning Star"
        if pc < po and c > o and o < pc and c > (po + pc) / 2:
            return "BULLISH", "Piercing Line"

        if upper_wick > body * 2 and lower_wick < body * 0.5 and c < o and upper_wick > total_range * 0.6:
            return "BEARISH", "Shooting Star"
        if c < o and pc > po and c < po and o > pc and body > prev_body:
            return "BEARISH", "Bearish Engulfing"
        if len(df) >= 3:
            prev2 = df.iloc[-3]
            if (prev2['Close'] > prev2['Open'] and
                    abs(pc - po) < abs(prev2['Close'] - prev2['Open']) * 0.3 and
                    c < o and c < (prev2['Open'] + prev2['Close']) / 2):
                return "BEARISH", "Evening Star"
        if pc > po and c < o and o > pc and c < (po + pc) / 2:
            return "BEARISH", "Dark Cloud Cover"

        return "NEUTRAL", "No Pattern"
    except:
        return "NEUTRAL", "No Pattern"

def get_pdh_pdl(ticker):
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

def load_watchlist():
    try:
        with open("watchlist.txt", "r") as f:
            return [l.strip() for l in f if l.strip() and not l.startswith('#')]
    except:
        return ["ITC.NS", "RELIANCE.NS", "INFY.NS"]

# ── Trade Monitor ─────────────────────────────────────────────────────────────
def monitor_trades():
    global eod_sent
    while True:
        try:
            now        = datetime.utcnow()
            ist_mins   = now.hour * 60 + now.minute + 330
            ist_hour   = (ist_mins // 60) % 24
            ist_minute = ist_mins % 60

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
                            result     = 'TARGET HIT'
                            exit_price = target
                        elif current_price <= sl:
                            result     = 'SL HIT'
                            exit_price = sl
                    else:
                        if current_price <= target:
                            result     = 'TARGET HIT'
                            exit_price = target
                        elif current_price >= sl:
                            result     = 'SL HIT'
                            exit_price = sl

                    if ist_hour == 15 and ist_minute >= 20 and not result:
                        result     = 'MANUAL EXIT'
                        exit_price = current_price

                    if result:
                        pnl          = round(shares * (exit_price - entry) *
                                             (1 if signal == 'BULLISH' else -1) - BROKERAGE, 2)
                        result_label = ("TARGET HIT" if "TARGET" in result
                                        else "SL HIT"     if "SL"     in result
                                        else "MANUAL EXIT")
                        emoji        = ("🎯" if "TARGET" in result
                                        else "🛑" if "SL" in result
                                        else "🔚")

                        msg = (
                            f"{emoji} <b>{result_label}</b>\n"
                            f"<b>{ticker.replace('.NS','')}</b>\n\n"
                            f"Entry:   Rs.{entry}\n"
                            f"Exit:    Rs.{exit_price}\n"
                            f"Shares:  {shares}\n\n"
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
        avg_score = round(sum(scores) / len(scores), 1) if scores else 0

        msg = (
            f"<b>EOD SUMMARY</b> -- {datetime.utcnow().strftime('%d %b %Y')}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"BULLISH signals: {len(bullish)}\n"
            f"BEARISH signals: {len(bearish)}\n"
            f"Total signals:   {total}\n"
            f"Avg score:       {avg_score}/9\n"
            f"Stocks scanned:  {len(watchlist)}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
        )
        if bullish:
            msg += "🟢 Bullish: " + ", ".join([t.replace('.NS', '') for t in bullish]) + "\n"
        if bearish:
            msg += "🔴 Bearish: " + ", ".join([t.replace('.NS', '') for t in bearish]) + "\n"
        if total == 0:
            msg += "No strong signals today\n"
        msg += "━━━━━━━━━━━━━━━━━━━━━\nMarket closed. See you tomorrow 9:15 AM! 🌙"
        send_telegram(msg)
    except:
        pass

monitor_thread = threading.Thread(target=monitor_trades, daemon=True)
monitor_thread.start()

# ── Scan Route ────────────────────────────────────────────────────────────────
@app.route("/scan/<ticker>")
def scan(ticker):
    try:
        now_utc    = datetime.utcnow()
        ist_mins   = now_utc.hour * 60 + now_utc.minute + 330
        ist_hour   = (ist_mins // 60) % 24
        ist_minute = ist_mins % 60

        # ✅ FIXED: block only after 3:15 PM (was blocking from 2:00 PM before!)
        too_early = (ist_hour == 9 and ist_minute < 30)
        too_late  = (ist_hour > 15) or (ist_hour == 15 and ist_minute >= 15)

        # ── 5 MIN data ────────────────────────────────────────────────────────
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

        price     = float(last5['Close'])
        ema5      = float(last5['EMA5'])
        ema10     = float(last5['EMA10'])
        rsi       = round(float(last5['RSI']), 2)
        atr_val   = round(float(last5['ATR']), 2)
        vwap_val  = round(float(last5['VWAP']), 2)
        adx_val   = round(float(last5['ADX']), 2)
        volume    = int(last5['Volume'])
        vol_avg   = int(last5['VOL_AVG'])
        vol_ratio = round(volume / vol_avg, 2) if vol_avg > 0 else 0
        above_vwap = price > vwap_val

        ema_bull_5m = float(prev5['EMA5']) <= float(prev5['EMA10']) and ema5 > ema10
        ema_bear_5m = float(prev5['EMA5']) >= float(prev5['EMA10']) and ema5 < ema10

        # ── 1 HR trend ────────────────────────────────────────────────────────
        df1h = yf.download(ticker, period="1mo", interval="1h", progress=False)
        if isinstance(df1h.columns, pd.MultiIndex):
            df1h.columns = df1h.columns.get_level_values(0)
        df1h     = df1h.dropna()
        hr_trend = "NEUTRAL"
        if len(df1h) >= 21:
            df1h['EMA9']  = compute_ema(df1h['Close'], 9)
            df1h['EMA21'] = compute_ema(df1h['Close'], 21)
            last1h = df1h.iloc[-1]
            if float(last1h['EMA9']) > float(last1h['EMA21']):
                hr_trend = "BULLISH"
            elif float(last1h['EMA9']) < float(last1h['EMA21']):
                hr_trend = "BEARISH"

        nifty_trend          = get_nifty_trend()
        pdh, pdl             = get_pdh_pdl(ticker)
        candle_dir, candle_name = detect_candle_pattern(df5)

        # ── Direction ─────────────────────────────────────────────────────────
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
        scores['ema_cross'] = True
        scores['hr_trend']  = (direction == hr_trend)
        scores['nifty']     = (direction == nifty_trend or nifty_trend == "NEUTRAL")
        scores['volume']    = vol_ratio >= 2.0
        scores['vwap']      = (direction == "BULLISH" and above_vwap) or (direction == "BEARISH" and not above_vwap)
        scores['rsi']       = (direction == "BULLISH" and 47 <= rsi <= 63) or (direction == "BEARISH" and 40 <= rsi <= 58)
        scores['pdh_pdl']   = False
        if pdh and pdl:
            scores['pdh_pdl'] = (direction == "BULLISH" and price > pdh) or (direction == "BEARISH" and price < pdl)
        scores['candle']    = (direction == candle_dir)
        scores['adx']       = adx_val >= 25

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

        # ── Grade ─────────────────────────────────────────────────────────────
        if total_score >= 8:
            trade_capital = CAPITAL
            signal_grade  = "PERFECT"
            grade_emoji   = "🔥 PERFECT SIGNAL"
        elif total_score == 7:
            trade_capital = CAPITAL * 0.5
            signal_grade  = "STRONG"
            grade_emoji   = "✅ STRONG SIGNAL"
        elif total_score == 6:
            trade_capital = CAPITAL * 0.25
            signal_grade  = "MODERATE"
            grade_emoji   = "⚠️ MODERATE SIGNAL"
        else:
            return jsonify({
                "ticker":  ticker,
                "price":   round(price, 2),
                "signal":  direction,
                "score":   total_score,
                "message": f"Score {total_score}/9 below minimum {MIN_CONFLUENCE}"
            })

        # ── Position sizing ───────────────────────────────────────────────────
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

        if max_gain <= 0:
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
            dir_emoji = "🟢" if direction == "BULLISH" else "🔴"

            conf_lines = (
                f"{'✅' if scores['ema_cross'] else '❌'} EMA 5/10 Cross\n"
                f"{'✅' if scores['hr_trend']  else '❌'} 1HR Trend {hr_trend}\n"
                f"{'✅' if scores['nifty']     else '❌'} Nifty {nifty_trend}\n"
                f"{'✅' if scores['volume']    else '❌'} Volume {vol_ratio}x\n"
                f"{'✅' if scores['vwap']      else '❌'} VWAP {'Above' if above_vwap else 'Below'}\n"
                f"{'✅' if scores['rsi']       else '❌'} RSI {rsi}\n"
                f"{'✅' if scores['pdh_pdl']   else '❌'} PDH/PDL Break\n"
                f"{'✅' if scores['candle']    else '❌'} {candle_name}\n"
                f"{'✅' if scores['adx']       else '❌'} ADX {adx_val}"
            )

            msg = (
                f"{dir_emoji} <b>INTRADAY {direction}</b>\n"
                f"<b>{tv_symbol}</b> @ Rs.{round(price, 2)}\n\n"
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
                f"<a href='https://www.tradingview.com/chart/?symbol=NSE:{tv_symbol}'>📊 View on TradingView</a>\n\n"
                f"⏰ Exit by 3:15 PM IST"
            )
            send_telegram(msg)

            log_to_sheets({
                "date":      now_utc.strftime("%d-%b-%Y"),
                "time":      now_utc.strftime("%H:%M"),
                "ticker":    ticker,
                "signal":    direction,
                "score":     total_score,
                "grade":     signal_grade,
                "entry":     entry,
                "sl":        sl,
                "target":    target,
                "shares":    shares,
                "capital":   trade_capital,
                "cost":      cost,
                "max_loss":  max_loss,
                "max_gain":  max_gain,
                "rsi":       rsi,
                "adx":       adx_val,
                "vol_ratio": vol_ratio,
                "hr_trend":  hr_trend,
                "nifty":     nifty_trend,
                "candle":    candle_name,
                "pdh_break": scores['pdh_pdl']
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

# ── Other Routes ──────────────────────────────────────────────────────────────
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
        msg = (
            "<b>🧪 TEST ALERT -- System Working!</b>\n"
            "<b>SBIN</b> @ Rs.812\n\n"
            "<b>CONFLUENCES (8/9):</b>\n"
            "<code>"
            "✅ EMA 5/10 Cross\n"
            "✅ 1HR Trend BULLISH\n"
            "✅ Nifty BULLISH\n"
            "✅ Volume 2.1x\n"
            "✅ VWAP Above\n"
            "✅ RSI 55\n"
            "✅ PDH Break\n"
            "✅ Bullish Engulfing\n"
            "❌ ADX 22"
            "</code>\n\n"
            "✅ <b>STRONG SIGNAL (8/9)</b>\n\n"
            "Entry:  Rs.812\n"
            "SL:     Rs.808\n"
            "Target: Rs.820\n\n"
            "BUY 12 shares | Cost: Rs.2,436\n"
            "Risk: Rs.58 | Gain: Rs.86\n\n"
            "<i>THIS IS A TEST - NOT A REAL SIGNAL</i>"
        )
        send_telegram(msg)
        return jsonify({"status": "Test alert sent to Telegram!"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/ping")
def ping():
    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000, threaded=True)
