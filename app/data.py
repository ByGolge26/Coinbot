import yfinance as yf
import pandas as pd

DEFAULT_SYMBOLS = [
    "BTC-USD","ETH-USD","SOL-USD","XRP-USD","DOGE-USD",
    "THYAO.IS","ASELS.IS","BIMAS.IS","TUPRS.IS","EREGL.IS",
    "AKBNK.IS","GARAN.IS","SISE.IS","KCHOL.IS","BRSAN.IS",
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","TSLA","AMD"
]

NEWS_NAMES = {
    "BTC-USD":"Bitcoin BTC crypto",
    "ETH-USD":"Ethereum ETH crypto",
    "SOL-USD":"Solana SOL crypto",
    "XRP-USD":"XRP Ripple crypto",
    "DOGE-USD":"Dogecoin DOGE crypto",
    "THYAO.IS":"Turkish Airlines THY",
    "ASELS.IS":"Aselsan ASELS",
    "BIMAS.IS":"BIM Birlesik Magazalar",
    "TUPRS.IS":"Tupras",
    "EREGL.IS":"Eregli Demir Celik",
    "AKBNK.IS":"Akbank",
    "GARAN.IS":"Garanti BBVA",
    "SISE.IS":"Sisecam",
    "KCHOL.IS":"Koc Holding",
    "BRSAN.IS":"Borusan",
    "AAPL":"Apple",
    "MSFT":"Microsoft",
    "NVDA":"Nvidia",
    "AMZN":"Amazon",
    "META":"Meta Platforms",
    "GOOGL":"Alphabet Google",
    "TSLA":"Tesla",
    "AMD":"AMD semiconductor"
}

def fetch(symbol, period="5y", interval="1d"):
    df = yf.download(
        symbol, period=period, interval=interval,
        auto_adjust=True, progress=False, threads=False
    )
    if df is None or df.empty:
        raise ValueError("Veri alınamadı")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    needed = ["Open","High","Low","Close","Volume"]
    for col in needed:
        if col not in df:
            raise ValueError(f"Eksik sütun: {col}")
    return df.dropna(subset=["Close"]).copy()
