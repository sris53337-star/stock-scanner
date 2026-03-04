from flask import Flask, jsonify
from flask_cors import CORS
import yfinance as yf
import pandas as pd
import os

app = Flask(__name__)
CORS(app)

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

def load_watchlist():
    try:
        with open("watchlist.txt", "r") as f:
            return [
                line.strip() for line in f
                if line.strip() and not line.startswith('#')
            ]
    except:
        return ["ITC.NS", "RELIANCE.NS", "INFY.NS"]

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
            df = yf.download(ticker, period="60d", interval="1d", progress=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.dropna()

        # ── All 5 Indicators ────────────────────────
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

        # ── 5 Conditions ────────────────────────────
        ema_bullish = float(prev['EMA9']) <= float(prev['EMA21']) and ema9 > ema21
        ema_bearish = float(prev['EMA9']) >= float(prev['EMA21']) and ema9 < ema21
        vol_confirm = vol_ratio >= 2.0
        above_vwap  = price > vwap
        rsi_bull_ok = 45 <= rsi <= 65
        rsi_bear_ok = 35 <= rsi <= 55

        # ── Score out of 3 ──────────────────────────
        bull_score = sum([vol_confirm, above_vwap, rsi_bull_ok])
        bear_score = sum([vol_confirm, not above_vwap, rsi_bear_ok])

        # ── Signal ──────────────────────────────────
        signal = None
        if ema_bullish and vol_confirm and above_vwap and rsi_bull_ok:
            signal = "BULLISH"
        elif ema_bearish and vol_confirm and not above_vwap and rsi_bear_ok:
            signal = "BEARISH"
        elif ema_bullish and bull_score >= 2:
            signal = "WEAK_BULLISH"
        elif ema_bearish and bear_score >= 2:
            signal = "WEAK_BEARISH"

        # ── Entry / SL / Target (1:2 RR via ATR) ───
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

@app.route("/ping")
def ping():
    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
