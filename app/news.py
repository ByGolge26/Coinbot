import requests
from datetime import datetime, timezone
from urllib.parse import quote
from .data import NEWS_NAMES

GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"

POSITIVE = {
    "beat","beats","growth","surge","rally","profit","record","upgrade",
    "partnership","approval","launch","strong","bullish","inflow","adoption",
    "artış","kar","kâr","rekor","yükseliş","anlaşma","onay","güçlü"
}
NEGATIVE = {
    "loss","miss","misses","decline","drop","fall","lawsuit","downgrade",
    "investigation","fraud","hack","exploit","warning","bearish","outflow",
    "zarar","düşüş","dava","soruşturma","hack","risk","zayıf"
}

def _sentiment(text):
    words = set(text.lower().replace(",", " ").replace(".", " ").split())
    pos = len(words & POSITIVE)
    neg = len(words & NEGATIVE)
    if pos > neg:
        label = "POZİTİF"
    elif neg > pos:
        label = "NEGATİF"
    else:
        label = "NÖTR"
    return label, pos, neg

def fetch_news(symbol, maxrecords=8):
    query = NEWS_NAMES.get(symbol, symbol.replace(".IS",""))
    params = {
        "query": query,
        "mode": "artlist",
        "maxrecords": maxrecords,
        "timespan": "3d",
        "sort": "datedesc",
        "format": "json"
    }
    try:
        r = requests.get(GDELT, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        articles = data.get("articles", [])
    except Exception as e:
        return {"symbol": symbol, "articles": [], "sentiment": "VERİ YOK",
                "score": 0, "error": str(e)}

    out=[]
    pos=neg=0
    for a in articles:
        title = a.get("title","")
        label,p,n = _sentiment(title)
        pos += p; neg += n
        out.append({
            "title": title,
            "url": a.get("url",""),
            "domain": a.get("domain",""),
            "seen": a.get("seendate",""),
            "sentiment": label
        })
    net = pos-neg
    score = max(-100, min(100, net*15))
    label = "POZİTİF" if score >= 20 else "NEGATİF" if score <= -20 else "NÖTR"
    return {"symbol":symbol,"articles":out,"sentiment":label,"score":score,"error":None}
