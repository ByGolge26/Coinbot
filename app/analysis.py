import numpy as np
import pandas as pd

def indicators(df):
    x = df.copy()
    c,h,l,v = x["Close"], x["High"], x["Low"], x["Volume"]
    x["EMA20"] = c.ewm(span=20, adjust=False).mean()
    x["EMA50"] = c.ewm(span=50, adjust=False).mean()
    x["EMA200"] = c.ewm(span=200, adjust=False).mean()
    d = c.diff()
    gain = d.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    x["RSI"] = 100 - (100 / (1 + rs))
    e12 = c.ewm(span=12, adjust=False).mean()
    e26 = c.ewm(span=26, adjust=False).mean()
    x["MACD"] = e12 - e26
    x["MACDSignal"] = x["MACD"].ewm(span=9, adjust=False).mean()
    prev = c.shift(1)
    tr = pd.concat([(h-l),(h-prev).abs(),(l-prev).abs()], axis=1).max(axis=1)
    x["ATR"] = tr.rolling(14).mean()
    x["VolAvg20"] = v.rolling(20).mean()
    x["Momentum20"] = c.pct_change(20) * 100
    x["Resistance20"] = h.rolling(20).max().shift(1)
    x["Support20"] = l.rolling(20).min().shift(1)
    return x

def score_row(r):
    trend = 0
    momentum = 0
    volume = 0
    structure = 0
    quality = 0
    reasons = []

    if r.Close > r.EMA20:
        trend += 8; reasons.append("Fiyat EMA20 üzerinde")
    if r.EMA20 > r.EMA50:
        trend += 8; reasons.append("EMA20 > EMA50")
    if r.EMA50 > r.EMA200:
        trend += 9; reasons.append("EMA50 > EMA200")

    if r.MACD > r.MACDSignal:
        momentum += 10; reasons.append("MACD pozitif")
    if 50 <= r.RSI <= 68:
        momentum += 6; reasons.append("RSI dengeli")
    elif r.RSI > 70:
        momentum -= 4; reasons.append("RSI aşırı alım bölgesinde")
    if r.Momentum20 > 0:
        momentum += 4; reasons.append("20 günlük momentum pozitif")

    if r.Volume > r.VolAvg20:
        volume = 15; reasons.append("Hacim ortalamanın üzerinde")
    elif r.Volume > 0.8 * r.VolAvg20:
        volume = 8

    if r.Close > r.Support20 * 1.03:
        structure += 7
    if r.Close > r.Resistance20:
        structure += 8; reasons.append("20 günlük direnç kırılımı")

    atr_pct = float(r.ATR / r.Close * 100) if r.Close else 99
    if atr_pct < 3:
        quality += 12
    elif atr_pct < 6:
        quality += 8
    elif atr_pct < 10:
        quality += 4
    if 45 <= r.RSI <= 68:
        quality += 8
    if r.Close > r.EMA50:
        quality += 5

    score = round((trend + momentum + volume + structure + quality))
    if r.RSI > 75:
        score -= 8
    if atr_pct > 10:
        score -= 8
    return max(0, min(100, score)), reasons[:7], atr_pct

def analyze_frame(df, index=-1):
    x = indicators(df)
    r = x.iloc[index]
    if pd.isna(r.EMA200) or pd.isna(r.ATR):
        raise ValueError("Yeterli geçmiş veri yok")

    score, reasons, atr_pct = score_row(r)
    signal = "AL ADAYI" if score >= 78 else "İZLE" if score >= 58 else "BEKLE"
    close = float(r.Close)
    atr = float(r.ATR)
    stop = max(0.0, close - 1.5 * atr)
    risk = max(close - stop, 1e-12)
    target1 = close + 2.0 * risk
    target2 = close + 3.0 * risk
    regime = (
        "YÜKSELİŞ" if close > r.EMA50 and r.EMA50 > r.EMA200
        else "DÜŞÜŞ" if close < r.EMA50 and r.EMA50 < r.EMA200
        else "YATAY/KARARSIZ"
    )
    return {
        "score": int(score),
        "signal": signal,
        "price": close,
        "rsi": float(r.RSI),
        "momentum20": float(r.Momentum20),
        "atr_pct": atr_pct,
        "ema20": float(r.EMA20),
        "ema50": float(r.EMA50),
        "ema200": float(r.EMA200),
        "support": float(r.Support20),
        "resistance": float(r.Resistance20),
        "stop": stop,
        "target1": target1,
        "target2": target2,
        "rr": 2.0,
        "regime": regime,
        "reasons": reasons
    }

def analyze(symbol, df):
    out = analyze_frame(df)
    out["symbol"] = symbol
    return out
