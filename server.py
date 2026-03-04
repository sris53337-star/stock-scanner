from flask import Flask, jsonify
from flask_cors import CORS
import yfinance as yf
import pandas as pd

app = Flask(__name__)
CORS(app)

def compute_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

@app.route("/scan/<ticker>")
def scan(ticker):
    try:
        df = yf.download(ticker, period="7d", interval="15m", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna()

        if len(df) < 5:
            df = yf.download(ticker, period="60d", interval="1d", progress=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.dropna()

        df['EMA9']  = compute_ema(df['Close'], 9)
        df['EMA21'] = compute_ema(df['Close'], 21)
        df = df.dropna()

        last = df.iloc[-1]
        prev = df.iloc[-2]

        signal = None
        if prev['EMA9'] <= prev['EMA21'] and last['EMA9'] > last['EMA21']:
            signal = "BULLISH"
        elif prev['EMA9'] >= prev['EMA21'] and last['EMA9'] < last['EMA21']:
            signal = "BEARISH"

        return jsonify({
            "ticker":  ticker,
            "price":   round(float(last['Close']), 2),
            "ema9":    round(float(last['EMA9']),  2),
            "ema21":   round(float(last['EMA21']), 2),
            "signal":  signal,
            "history": [round(float(x), 2) for x in df['Close'].tail(20).tolist()]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/ping")
def ping():
    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
