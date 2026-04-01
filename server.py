from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import yfinance as yf
import pandas as pd
import requests as req
import os
import threading
import time
import gc
import json
from datetime import datetime

app = Flask(__name__)
CORS(app, origins="*", supports_credentials=False)

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SHEETS_URL       = "https://script.google.com/macros/s/AKfycbxAIhJDoCRwhZ0f9_cSDikU9bmr7nR2ja5q5SfBendSNhlx99G4ngUG5EIe3ahjH7gUIQ/exec"

CAPITAL        = 5000
RISK_PCT       = 5
BROKERAGE      = 10
MIN_CONFLUENCE = 6
MIN_ATR_PCT    = 0.3
COOLDOWN_MINS  = 120
NIFTY_TTL      = 300

sent_signals  = {}
active_trades = {}
eod_sent_date = ""
_signal_times = {}
_nifty_cache  = {"trend": "NEUTRAL", "ts": 0}
_pdh_cache    = {}

TRADES_FILE       = "active_trades.json"
SIGNAL_TIMES_FILE = "signal_times.json"

def save_trades():
    try:
        with open(TRADES_FILE, "w") as f:
            json.dump(active_trades, f)
    except:
        pass

def load_trades():
    try:
        with open(TRADES_FILE, "r") as f:
            data = json.load(f)
            active_trades.update(data)
            print(f"Restored {len(data)} active trades")
    except:
        pass

def save_signal_times():
    try:
        with open(SIGNAL_TIMES_FILE, "w") as f:
            json.dump(_signal_times, f)
    except:
        pass

def load_signal_times():
    try:
        with open(SIGNAL_TIMES_FILE, "r") as f:
            data = json.load(f)
            _signal_times.update(data)
            print(f"Restored {len(data)} signal times")
    except:
        pass

load_trades()
load_signal_times()


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram: TOKEN or CHAT_ID missing!")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = req.post(url, json={
            "chat_id":                  TELEGRAM_CHAT_ID,
            "text":                     message,
            "parse_mode":               "HTML",
            "disable_web_page_preview": True
        }, timeout=10)
        print(f"Telegram sent: {r.status_code}")
    except Exception as e:
        print(f"Telegram error: {e}")

def notify_restart():
    import time as _t
    _t.sleep(10)
    trades = len(active_trades)
    trade_lines = ""
    for tk, tr in active_trades.items():
        trade_lines += f"\n• {tk.replace('.NS','')} {tr['signal']} @ Rs.{tr['entry']} | SL Rs.{tr['sl']} | T Rs.{tr['target']}"
    msg = (
        "<b>🔄 SCANNER RESTARTED</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "✅ Server back online\n"
        f"📊 Open trades: {trades}"
        + (trade_lines if trade_lines else "\nNo open trades")
        + "\n━━━━━━━━━━━━━━━━━━━━━\n"
        "⏰ Auto-scan starts in 2.5 mins"
    )
    send_telegram(msg)

threading.Thread(target=notify_restart, daemon=True).start()

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
    tr = high_low.combine(high_close, max).combine(low_close, max)
    return tr.rolling(window=period).mean()

def compute_vwap(df):
    tp = (df['High'] + df['Low'] + df['Close']) / 3
    return (tp * df['Volume']).cumsum() / df['Volume'].cumsum()

def compute_macd_hist(series):
    ema12  = series.ewm(span=12, adjust=False).mean()
    ema26  = series.ewm(span=26, adjust=False).mean()
    macd   = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd - signal

def compute_cci(df, period=20):
    tp = (df['High'] + df['Low'] + df['Close']) / 3
    ma = tp.rolling(window=period).mean()
    md = tp.rolling(window=period).apply(lambda x: abs(x - x.mean()).mean())
    return (tp - ma) / (0.015 * md)

def compute_bb(series, period=20):
    ma    = series.rolling(window=period).mean()
    std   = series.rolling(window=period).std()
    upper = ma + 2 * std
    lower = ma - 2 * std
    width = (upper - lower) / ma
    return upper, lower, width

def compute_supertrend(df, period=10, multiplier=3.0):
    try:
        hl2        = (df['High'] + df['Low']) / 2
        atr        = compute_atr(df, period)
        upper      = (hl2 + multiplier * atr).values
        lower      = (hl2 - multiplier * atr).values
        close      = df['Close'].values
        supertrend = [True] * len(df)
        upper_band = upper.copy()
        lower_band = lower.copy()

        for i in range(1, len(df)):
            upper_band[i] = min(upper[i], upper_band[i-1]) if close[i-1] <= upper_band[i-1] else upper[i]
            lower_band[i] = max(lower[i], lower_band[i-1]) if close[i-1] >= lower_band[i-1] else lower[i]
            if supertrend[i-1] and close[i] < lower_band[i]:
                supertrend[i] = False
            elif not supertrend[i-1] and close[i] > upper_band[i]:
                supertrend[i] = True
            else:
                supertrend[i] = supertrend[i-1]

        import pandas as pd
        return pd.Series(supertrend, index=df.index)
    except:
        import pandas as pd
        return pd.Series([True] * len(df), index=df.index)

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
        last  = df.iloc[-1]
        prev  = df.iloc[-2]
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
    today  = datetime.utcnow().strftime("%Y-%m-%d")
    cached = _pdh_cache.get(ticker)
    if cached and cached[2] == today:
        return cached[0], cached[1]
    try:
        df = yf.download(ticker, period="5d", interval="1d", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna()
        if len(df) >= 2:
            pdh = float(df['High'].iloc[-2])
            pdl = float(df['Low'].iloc[-2])
            _pdh_cache[ticker] = (pdh, pdl, today)
            del df
            return pdh, pdl
        del df
        return None, None
    except:
        return None, None

def get_nifty_trend():
    now = time.time()
    if now - _nifty_cache["ts"] < NIFTY_TTL:
        return _nifty_cache["trend"]
    try:
        df = yf.download("^NSEI", period="5d", interval="1h", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna()
        trend = "NEUTRAL"
        if len(df) >= 21:
            ema9  = compute_ema(df['Close'], 9)
            ema21 = compute_ema(df['Close'], 21)
            if float(ema9.iloc[-1]) > float(ema21.iloc[-1]):
                trend = "BULLISH"
            elif float(ema9.iloc[-1]) < float(ema21.iloc[-1]):
                trend = "BEARISH"
        del df
        _nifty_cache["trend"] = trend
        _nifty_cache["ts"]    = now
        return trend
    except:
        return _nifty_cache["trend"]

def load_watchlist():
    try:
        with open("watchlist.txt", "r") as f:
            return [l.strip() for l in f if l.strip() and not l.startswith('#')]
    except:
        return ["ITC.NS", "RELIANCE.NS", "INFY.NS"]

def get_ist():
    now      = datetime.utcnow()
    ist_mins = now.hour * 60 + now.minute + 330
    return (ist_mins // 60) % 24, ist_mins % 60

def monitor_trades():
    global eod_sent_date
    while True:
        try:
            ist_hour, ist_minute = get_ist()
            today = datetime.utcnow().strftime("%d-%b-%Y")

            if ist_hour == 15 and ist_minute >= 35 and eod_sent_date != today:
                send_eod_summary()
                eod_sent_date = today

            if ist_hour == 0 and ist_minute < 5:
                sent_signals.clear()
                active_trades.clear()
                _pdh_cache.clear()
                _signal_times.clear()
                save_signal_times()
                save_trades()

            for ticker in list(active_trades.keys()):
                trade = active_trades[ticker]
                df    = None
                try:
                    df = yf.download(ticker, period="1d", interval="5m", progress=False)
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    if df.empty:
                        continue
                    current_price = float(df['Close'].iloc[-1])
                    del df
                    df = None

                    entry  = trade['entry']
                    sl     = trade['sl']
                    target = trade['target']
                    signal = trade['signal']
                    shares = trade['shares']

                    result     = None
                    exit_price = None

                    if signal == 'BULLISH':
                        if current_price >= target:
                            result, exit_price = 'TARGET HIT', target
                        elif current_price <= sl:
                            result, exit_price = 'SL HIT', sl
                    else:
                        if current_price <= target:
                            result, exit_price = 'TARGET HIT', target
                        elif current_price >= sl:
                            result, exit_price = 'SL HIT', sl

                    if ist_hour == 15 and ist_minute >= 15 and not result:
                        result, exit_price = 'EOD EXIT 3:15PM', current_price

                    if result:
                        pnl   = round(shares * (exit_price - entry) *
                                      (1 if signal == 'BULLISH' else -1) - BROKERAGE, 2)
                        emoji = "🎯" if "TARGET" in result else "🛑" if "SL" in result else "🔚"
                        msg   = (
                            f"{emoji} <b>{result}</b>\n"
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
                            "result":     result,
                            "exit_price": exit_price,
                            "pnl":        pnl
                        })
                        del active_trades[ticker]
                        save_trades()
                except:
                    pass
                finally:
                    if df is not None:
                        del df

            gc.collect()
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
            f"📊 <b>EOD SUMMARY</b> — {datetime.utcnow().strftime('%d %b %Y')}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🟢 Bullish: {len(bullish)} | 🔴 Bearish: {len(bearish)}\n"
            f"📈 Total: {total} | Avg Score: {avg_score}/11\n"
            f"🔍 Scanned: {len(watchlist)} stocks\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
        )
        if bullish:
            msg += "🟢 " + ", ".join([t.replace('.NS','') for t in bullish]) + "\n"
        if bearish:
            msg += "🔴 " + ", ".join([t.replace('.NS','') for t in bearish]) + "\n"
        if total == 0:
            msg += "😴 No strong signals today\n"
        msg += "━━━━━━━━━━━━━━━━━━━━━\nMarket closed. See you tomorrow! 🌙"
        send_telegram(msg)
    except:
        pass

def delayed_start():
    time.sleep(5)
    monitor_thread = threading.Thread(target=monitor_trades, daemon=True)
    monitor_thread.start()

starter = threading.Thread(target=delayed_start, daemon=True)
starter.start()

@app.route("/scan/<ticker>")
def scan(ticker):
    df5 = None
    try:
        ist_hour, ist_minute = get_ist()
        too_early = (ist_hour == 9 and ist_minute < 30)
        too_late  = (ist_hour > 15) or (ist_hour == 15 and ist_minute >= 15)

        print(f"SCAN {ticker} | IST {ist_hour:02d}:{ist_minute:02d} | too_early={too_early} too_late={too_late}")

        df5 = yf.download(ticker, period="5d", interval="5m", progress=False)
        if isinstance(df5.columns, pd.MultiIndex):
            df5.columns = df5.columns.get_level_values(0)
        df5 = df5.dropna()
        if len(df5) < 5:
            return jsonify({"error": "Not enough 5min data"}), 404

        df5 = df5[['Open', 'High', 'Low', 'Close', 'Volume']].copy()

        df5['EMA5']     = compute_ema(df5['Close'], 5)
        df5['EMA10']    = compute_ema(df5['Close'], 10)
        df5['RSI']      = compute_rsi(df5['Close'], 14)
        df5['ATR']      = compute_atr(df5, 14)
        df5['VWAP']     = compute_vwap(df5)
        df5['ADX']      = compute_adx(df5, 14)
        df5['VOL_AVG']  = df5['Volume'].rolling(window=20).mean()
        df5['CCI']      = compute_cci(df5, 20)
        df5['MACD_H']   = compute_macd_hist(df5['Close'])
        df5['BB_U'], df5['BB_L'], df5['BB_W'] = compute_bb(df5['Close'], 20)
        df5['ST_BULL'] = compute_supertrend(df5, period=10, multiplier=3.0)
        df5 = df5.dropna()

        if len(df5) < 3:
            return jsonify({"error": "Not enough data after indicators"}), 404

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
        vol_avg    = int(last5['VOL_AVG']) if last5['VOL_AVG'] > 0 else 1
        vol_ratio  = round(volume / vol_avg, 2)
        cci_val    = round(float(last5['CCI']), 2)
        macd_hist  = round(float(last5['MACD_H']), 4)
        bb_upper   = round(float(last5['BB_U']), 2)
        bb_lower   = round(float(last5['BB_L']), 2)
        above_vwap   = price > vwap_val
        st_bullish   = bool(last5["ST_BULL"])

        last3_c = df5['Close'].iloc[-3:].values
        last3_o = df5['Open'].iloc[-3:].values
        history_tail = [round(float(x), 2) for x in df5['Close'].tail(20).tolist()]
        candle_dir, candle_name = detect_candle_pattern(df5)

        ema_bull = float(prev5['EMA5']) <= float(prev5['EMA10']) and ema5 > ema10
        ema_bear = float(prev5['EMA5']) >= float(prev5['EMA10']) and ema5 < ema10

        del df5
        df5 = None

        nifty_trend = get_nifty_trend()
        pdh, pdl    = get_pdh_pdl(ticker)

        if ema_bull:
            direction = "BULLISH"
        elif ema_bear:
            direction = "BEARISH"
        else:
            return jsonify({"ticker": ticker, "price": round(price,2), "signal": None, "score": 0, "message": "No EMA crossover"})

        good_time = (
            (ist_hour == 9  and ist_minute >= 30) or
            (ist_hour == 10) or
            (ist_hour == 11 and ist_minute <= 30) or
            (ist_hour == 13 and ist_minute >= 30) or
            (ist_hour == 14 and ist_minute <= 30)
        )

        scores = {}
        scores['ema_cross']   = True
        scores['nifty']       = (direction == nifty_trend or nifty_trend == "NEUTRAL")
        scores['volume']      = vol_ratio >= 1.5
        scores['vwap']        = (direction == "BULLISH" and above_vwap) or (direction == "BEARISH" and not above_vwap)
        scores['rsi']         = (direction == "BULLISH" and 40 <= rsi <= 70) or (direction == "BEARISH" and 30 <= rsi <= 65)
        scores['pdh_pdl']     = bool(pdh and pdl and ((direction == "BULLISH" and price > pdh) or (direction == "BEARISH" and price < pdl)))
        scores['candle']      = (direction == candle_dir)
        scores['macd_hist']   = (macd_hist > 0) if direction == "BULLISH" else (macd_hist < 0)
        scores['cci']         = (cci_val > 100) if direction == "BULLISH" else (cci_val < -100)
        scores['consec']      = bool(
            (direction == "BULLISH" and last3_c[-1] > last3_o[-1] and last3_c[-2] > last3_o[-2]) or
            (direction == "BEARISH" and last3_c[-1] < last3_o[-1] and last3_c[-2] < last3_o[-2])
        )
        scores['time_window']  = good_time
        scores['supertrend']   = st_bullish if direction == 'BULLISH' else not st_bullish

        total_score = sum(scores.values())

        print(f"CROSSOVER {ticker} | {direction} | score={total_score}/12 | "
              f"ema={scores['ema_cross']} nifty={scores['nifty']} "
              f"vol={scores['volume']}({vol_ratio}x) vwap={scores['vwap']} rsi={scores['rsi']}({rsi}) "
              f"pdh={scores['pdh_pdl']} candle={scores['candle']} macd={scores['macd_hist']}({macd_hist}) "
              f"cci={scores['cci']}({cci_val}) consec={scores['consec']} time={scores['time_window']}")

        atr_pct = (atr_val / price) * 100
        if atr_pct < MIN_ATR_PCT:
            return jsonify({"ticker": ticker, "price": round(price,2), "signal": direction,
                            "score": total_score, "message": f"ATR {atr_pct:.2f}% too small"})
        if vol_ratio < 0.5:
            return jsonify({"ticker": ticker, "price": round(price,2), "signal": direction,
                            "score": total_score, "message": f"Volume {vol_ratio}x too low"})

        if total_score < MIN_CONFLUENCE:
            return jsonify({"ticker": ticker, "price": round(price,2), "signal": direction,
                            "score": total_score, "message": f"Score {total_score}/12 below minimum {MIN_CONFLUENCE}"})

        entry = round(price, 2)
        sl    = round(price - atr_val * 2.1, 2) if direction == "BULLISH" else round(price + atr_val * 2.1, 2)

        if (ist_hour == 9 and ist_minute >= 30) or ist_hour == 10 or (ist_hour == 11 and ist_minute <= 30):
            rr_mult  = 5.0
            rr_label = "1:2"
        elif (ist_hour == 13 and ist_minute >= 30) or ist_hour == 14 or (ist_hour == 15 and ist_minute <= 15):
            rr_mult  = 2.5
            rr_label = "1:1"
        else:
            rr_mult  = 3.75
            rr_label = "1:1.5"

        target = round(price + atr_val * rr_mult, 2) if direction == "BULLISH" else round(price - atr_val * rr_mult, 2)

        if total_score >= 10:
            trade_capital = CAPITAL
            signal_grade  = "PERFECT"
            grade_emoji   = "🔥 PERFECT SIGNAL"
        elif total_score >= 9:
            trade_capital = CAPITAL * 0.75
            signal_grade  = "STRONG"
            grade_emoji   = "✅ STRONG SIGNAL"
        elif total_score >= 8:
            trade_capital = CAPITAL * 0.5
            signal_grade  = "MODERATE"
            grade_emoji   = "⚠️ MODERATE SIGNAL"
        else:
            trade_capital = CAPITAL * 0.25
            signal_grade  = "WEAK"
            grade_emoji   = "👀 WEAK SIGNAL"

        risk_amount    = trade_capital * (RISK_PCT / 100)
        risk_per_share = abs(entry - sl)
        if risk_per_share == 0:
            return jsonify({"error": "Zero risk per share"}), 400

        shares = min(int(risk_amount / risk_per_share), int(trade_capital / entry))
        if shares <= 0:
            shares = 1

        cost     = round(shares * entry, 2)
        max_loss = round(shares * risk_per_share + BROKERAGE, 2)
        max_gain = round(shares * abs(target - entry) - BROKERAGE, 2)
        if max_gain <= 0:
            return jsonify({"ticker": ticker, "price": round(price,2), "signal": direction,
                            "score": total_score, "message": "Brokerage exceeds max gain — skipped"})

        signal_key  = f"{ticker}_{direction}"
        now_ts      = time.time()
        last_fired  = _signal_times.get(signal_key, 0)
        cooldown_ok = (now_ts - last_fired) > (COOLDOWN_MINS * 60)

        print(f"GATE {ticker} | key_exists={signal_key in sent_signals} cooldown_ok={cooldown_ok} too_early={too_early} too_late={too_late} score={total_score}")

        if signal_key not in sent_signals and cooldown_ok and not too_early and not too_late:
            _signal_times[signal_key] = now_ts
            save_signal_times()
            sent_signals[signal_key] = {'signal': direction, 'score': total_score}

            tv_symbol  = ticker.replace('.NS', '')
            dir_arrow  = "BUY"  if direction == "BULLISH" else "SELL"
            dir_emoji  = "🟢"  if direction == "BULLISH" else "🔴"

            conf_lines = (
                f"{'✅' if scores['ema_cross']   else '❌'} EMA 5/10 Cross\n"
                f"{'✅' if scores['nifty']       else '❌'} Nifty {nifty_trend}\n"
                f"{'✅' if scores['volume']      else '❌'} Volume {vol_ratio}x\n"
                f"{'✅' if scores['vwap']        else '❌'} VWAP {'Above' if above_vwap else 'Below'}\n"
                f"{'✅' if scores['rsi']         else '❌'} RSI {rsi}\n"
                f"{'✅' if scores['pdh_pdl']     else '❌'} PDH/PDL Break\n"
                f"{'✅' if scores['candle']      else '❌'} {candle_name}\n"
                f"{'✅' if scores['macd_hist']   else '❌'} MACD Hist {macd_hist}\n"
                f"{'✅' if scores['cci']         else '❌'} CCI {cci_val}\n"
                f"{'✅' if scores['consec']      else '❌'} Consecutive Candles\n"
                f"{'✅' if scores['time_window'] else '❌'} Prime Time Window\n"
                f"{'✅' if scores['supertrend']  else '❌'} Supertrend {'BULL' if st_bullish else 'BEAR'}"
            )

            msg = (
                f"🤖 <b>DUAL EMA CROSSOVER SCANNER</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"{dir_emoji} <b>INTRADAY {direction}</b>\n"
                f"<b>{tv_symbol}</b> @ Rs.{round(price, 2)}\n\n"
                f"<b>CONFLUENCES ({total_score}/12):</b>\n"
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
                f"ATR: Rs.{atr_val} | RR: {rr_label} (SL=2.1x ATR)\n\n"
                f"<a href='https://www.tradingview.com/chart/?symbol=NSE:{tv_symbol}'>📊 View on TradingView</a>\n\n"
                f"⏰ Exit by 3:15 PM IST"
            )
            send_telegram(msg)

            now_utc = datetime.utcnow()
            log_to_sheets({
                "date":      now_utc.strftime("%d-%b-%Y"),
                "time":      now_utc.strftime("%H:%M"),
                "ticker":    ticker,
                "signal":    f"INTRADAY {direction}",
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
            save_trades()

        return jsonify({
            "ticker":      ticker,
            "price":       round(price, 2),
            "signal":      direction,
            "score":       total_score,
            "grade":       signal_grade,
            "rsi":         rsi,
            "adx":         adx_val,
            "vwap":        round(vwap_val, 2),
            "atr":         atr_val,
            "vol_ratio":   vol_ratio,
            "nifty_trend": nifty_trend,
            "candle":      candle_name,
            "scores":      {k: bool(v) for k, v in scores.items()},
            "entry":       entry,
            "sl":          sl,
            "target":      target,
            "history":     history_tail
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if df5 is not None:
            del df5
        gc.collect()

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/watchlist")
def get_watchlist():
    return jsonify(load_watchlist())

@app.route("/backtest/<ticker>")
def backtest(ticker):
    df = None
    try:
        period = request.args.get("period", "3mo")
        df = yf.download(ticker, period=period, interval="1d", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna()
        if len(df) < 30:
            return jsonify({"error": "Not enough data"}), 404
        result = {
            "dates":   [str(d)[:10] for d in df.index.tolist()],
            "opens":   [round(float(x), 2) for x in df['Open'].tolist()],
            "highs":   [round(float(x), 2) for x in df['High'].tolist()],
            "lows":    [round(float(x), 2) for x in df['Low'].tolist()],
            "closes":  [round(float(x), 2) for x in df['Close'].tolist()],
            "volumes": [int(x) for x in df['Volume'].tolist()],
        }
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if df is not None:
            del df
        gc.collect()

@app.route("/trades")
def get_trades():
    return jsonify({"active_trades": active_trades, "count": len(active_trades)})

@app.route("/add_trade", methods=["POST"])
def add_trade():
    try:
        data = request.get_json()
        ticker = data.get("ticker")
        if not ticker:
            return jsonify({"error": "ticker required"}), 400
        active_trades[ticker] = {
            "signal": data.get("signal"),
            "entry":  float(data.get("entry")),
            "sl":     float(data.get("sl")),
            "target": float(data.get("target")),
            "shares": int(data.get("shares"))
        }
        save_trades()
        return jsonify({"status": "added", "ticker": ticker, "trade": active_trades[ticker]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/ping")
def ping():
    return "ok"

_scan_running = False

def auto_scan_loop():
    global _scan_running
    time.sleep(150)
    print("Auto-scan loop started.")
    while True:
        try:
            ist_hour, ist_min = get_ist()
            market_open  = (ist_hour > 9) or (ist_hour == 9 and ist_min >= 30)
            market_close = (ist_hour > 15) or (ist_hour == 15 and ist_min >= 15)
            in_market    = market_open and not market_close

            if in_market and not _scan_running:
                _scan_running = True
                watchlist = load_watchlist()
                print(f"Auto-scan START: {len(watchlist)} stocks | IST {ist_hour:02d}:{ist_min:02d}")
                if ist_hour == 9 and 30 <= ist_min < 35:
                    trade_lines = ""
                    for tk, tr in active_trades.items():
                        trade_lines += f"\n• {tk.replace('.NS','')} {tr['signal']} @ Rs.{tr['entry']} | SL Rs.{tr['sl']} | T Rs.{tr['target']}"
                    open_msg = (
                        "<b>🟢 MARKET OPEN — READY TO TRADE</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📡 Scanning {len(watchlist)} stocks every 5 mins\n"
                        f"⏰ Auto-exit at 3:15 PM IST\n"
                        "━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📊 Open trades: {len(active_trades)}"
                        + (trade_lines if trade_lines else "\nNo open trades")
                    )
                    send_telegram(open_msg)
                for ticker in watchlist:
                    try:
                        with app.test_request_context():
                            scan(ticker)
                    except Exception as e:
                        print(f"Auto-scan error {ticker}: {e}")
                    time.sleep(2)
                gc.collect()
                _scan_running = False
                print("Auto-scan DONE")
            elif _scan_running:
                print("Auto-scan: previous cycle still running, skipping")
            else:
                print(f"Auto-scan: market closed | IST {ist_hour:02d}:{ist_min:02d}")
        except Exception as e:
            _scan_running = False
            print(f"Auto-scan loop error: {e}")
        time.sleep(300)

scan_thread = threading.Thread(target=auto_scan_loop, daemon=True)
scan_thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000, threaded=True)
