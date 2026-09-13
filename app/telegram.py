import os
import requests
from dotenv import load_dotenv
load_dotenv()

def configured():
    return bool(os.getenv("TELEGRAM_BOT_TOKEN","").strip() and os.getenv("TELEGRAM_CHAT_ID","").strip())

def send_signal(item):
    token=os.getenv("TELEGRAM_BOT_TOKEN","").strip()
    chat=os.getenv("TELEGRAM_CHAT_ID","").strip()
    if not token or not chat:
        return {"ok":False,"error":"Telegram ayarlanmamış"}
    text=(
        f"🚨 Midas AI Trader Sinyali\n\n"
        f"{item['symbol']} • {item['signal']}\n"
        f"Puan: {item['score']}/100\n"
        f"Fiyat: {item['price']:.6f}\n"
        f"Stop: {item['stop']:.6f}\n"
        f"Hedef 1: {item['target1']:.6f}\n"
        f"Hedef 2: {item['target2']:.6f}\n"
        f"Risk/Rew.: 1:{item['rr']:.1f}\n"
        f"Rejim: {item['regime']}\n"
        f"AI: {item.get('ai',{}).get('verdict','-')} ({item.get('ai',{}).get('score','-')})\n\n"
        f"Bu bir araştırma sinyalidir, getiri garantisi değildir."
    )
    url=f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r=requests.post(url,json={"chat_id":chat,"text":text},timeout=15)
        r.raise_for_status()
        return {"ok":True}
    except Exception as e:
        return {"ok":False,"error":str(e)}
