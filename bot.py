import os, time, json, re, math, uuid, threading, hmac, hashlib
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

import requests
import pandas as pd
import numpy as np
try:
    import psycopg
except ImportError:
    psycopg = None

BINANCE_MAIN = "https://api.binance.com"
BINANCE_TESTNET = "https://testnet.binance.vision"
GROQ_CHAT = "https://api.groq.com/openai/v1/chat/completions"
FX_URL = "https://api.frankfurter.app/latest"
TR_ZONE = ZoneInfo("Europe/Istanbul")


def money(v):
    return round(float(v), 2)


def dec_floor(value, increment):
    v = Decimal(str(value)); inc = Decimal(str(increment))
    if inc <= 0: return float(v)
    return float((v / inc).to_integral_value(rounding=ROUND_DOWN) * inc)


class BinanceClient:
    """Binance Spot REST client using HMAC-SHA256 signed endpoints."""
    def __init__(self):
        self.api_key = os.getenv("BINANCE_API_KEY", "").strip()
        self.api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
        self.testnet = os.getenv("BINANCE_TESTNET", "false").lower() == "true"
        self.base = BINANCE_TESTNET if self.testnet else BINANCE_MAIN
        self.timeout = int(os.getenv("BINANCE_HTTP_TIMEOUT", "15"))
        self.recv_window = min(60000, max(1000, int(os.getenv("BINANCE_RECV_WINDOW", "5000"))))
        self.time_offset_ms = 0
        self.exchange_info_cache = None
        self.exchange_info_at = 0

    @property
    def configured(self):
        return bool(self.api_key and self.api_secret)

    def _timestamp(self):
        return int(time.time() * 1000) + self.time_offset_ms

    def sync_time(self):
        r = requests.get(self.base + "/api/v3/time", timeout=self.timeout)
        r.raise_for_status()
        server = int(r.json()["serverTime"])
        self.time_offset_ms = server - int(time.time() * 1000)
        return server

    def _signed_params(self, params):
        p = dict(params or {})
        p.setdefault("timestamp", self._timestamp())
        p.setdefault("recvWindow", self.recv_window)
        # Binance currently requires percent-encoding before HMAC for signed requests.
        payload = urlencode(p, doseq=True, encoding="utf-8", safe="-_.~")
        sig = hmac.new(self.api_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        p["signature"] = sig
        return p

    def request(self, method, path, params=None, signed=False):
        headers = {"User-Agent": "Coinbot-V21", "X-MBX-APIKEY": self.api_key} if self.api_key else {"User-Agent": "Coinbot-V21"}
        p = self._signed_params(params) if signed else (params or {})
        url = self.base + path
        try:
            r = requests.request(method, url, params=p, headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            raise RuntimeError(f"Binance ağ hatası: {e}")
        if not r.ok:
            try: detail = r.json()
            except Exception: detail = r.text
            code = detail.get("code") if isinstance(detail, dict) else r.status_code
            msg = detail.get("msg") if isinstance(detail, dict) else str(detail)
            if r.status_code in (418, 429):
                retry = r.headers.get("Retry-After", "?")
                raise RuntimeError(f"Binance HTTP {r.status_code} / {code}: {msg} • Retry-After={retry}s")
            raise RuntimeError(f"Binance HTTP {r.status_code} / {code}: {msg}")
        try: return r.json()
        except Exception: raise RuntimeError(f"Binance geçersiz JSON: {r.text[:500]}")

    def exchange_info(self, force=False):
        if not force and self.exchange_info_cache and time.time() - self.exchange_info_at < 900:
            return self.exchange_info_cache
        data = self.request("GET", "/api/v3/exchangeInfo")
        self.exchange_info_cache = data; self.exchange_info_at = time.time()
        return data

    def symbol_info(self, symbol):
        for s in self.exchange_info().get("symbols", []):
            if s.get("symbol") == symbol: return s
        raise RuntimeError(f"Binance sembolü bulunamadı: {symbol}")

    def ticker_24hr(self):
        return self.request("GET", "/api/v3/ticker/24hr")

    def klines(self, symbol, interval="5m", limit=200):
        return self.request("GET", "/api/v3/klines", params={"symbol": symbol, "interval": interval, "limit": min(1000, limit)})

    def account(self):
        if not self.configured: raise RuntimeError("BINANCE_API_KEY / BINANCE_API_SECRET eksik")
        return self.request("GET", "/api/v3/account", params={"omitZeroBalances": "true"}, signed=True)

    def balance(self, asset):
        for b in self.account().get("balances", []):
            if b.get("asset") == asset:
                return float(b.get("free", 0)) + float(b.get("locked", 0))
        return 0.0

    def free_balance(self, asset):
        for b in self.account().get("balances", []):
            if b.get("asset") == asset: return float(b.get("free", 0))
        return 0.0

    def order(self, symbol, order_id=None, client_order_id=None):
        p = {"symbol": symbol}
        if order_id is not None: p["orderId"] = order_id
        elif client_order_id: p["origClientOrderId"] = client_order_id
        else: raise ValueError("orderId veya clientOrderId gerekli")
        return self.request("GET", "/api/v3/order", params=p, signed=True)

    def create_market_buy(self, symbol, quote_qty, client_order_id):
        p = {"symbol": symbol, "side": "BUY", "type": "MARKET", "quoteOrderQty": f"{quote_qty:.8f}", "newOrderRespType": "FULL", "newClientOrderId": client_order_id}
        return self.request("POST", "/api/v3/order", params=p, signed=True)

    def create_market_sell(self, symbol, quantity, client_order_id):
        p = {"symbol": symbol, "side": "SELL", "type": "MARKET", "quantity": f"{quantity:.12f}", "newOrderRespType": "FULL", "newClientOrderId": client_order_id}
        return self.request("POST", "/api/v3/order", params=p, signed=True)


class TradingBot:
    def __init__(self):
        self.lock = threading.Lock(); self.running = True; self.started_at = datetime.now(timezone.utc)
        self.live_trading = os.getenv("LIVE_TRADING", "false").lower() == "true"
        self.live_confirm = os.getenv("LIVE_CONFIRM", "") == "I_UNDERSTAND_REAL_MONEY_TRADING"
        self.dry_run = not (self.live_trading and self.live_confirm)
        self.mode = "LIVE" if not self.dry_run else "DRY RUN"
        self.quote_asset = os.getenv("QUOTE_ASSET", "USDT").upper()
        self.balance_try = float(os.getenv("STARTING_TRY_BALANCE", "5000")); self.initial_balance = self.balance_try
        self.realized_pnl = 0.0; self.fx_rate = None; self.fx_at = 0.0
        self.risk = float(os.getenv("RISK_PER_TRADE", "0.01")); self.tp = float(os.getenv("TAKE_PROFIT", "0.025")); self.sl = float(os.getenv("STOP_LOSS", "0.01")); self.trailing = float(os.getenv("TRAILING_STOP", "0.01"))
        self.min_buy_try = max(1000.0, float(os.getenv("MIN_BUY_TRY", "1000"))); self.min_loss_try = max(100.0, float(os.getenv("MIN_LOSS_TRY", "100"))); self.min_profit_try = max(150.0, float(os.getenv("MIN_PROFIT_TRY", "150")))
        self.max_daily_loss_try = max(100.0, float(os.getenv("MAX_DAILY_LOSS_TRY", "200"))); self.max_consecutive_losses = max(1, int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))); self.loss_cooldown_minutes = max(5, int(os.getenv("LOSS_COOLDOWN_MINUTES", "30")))
        self.max_positions = int(os.getenv("MAX_OPEN_POSITIONS", "20")); self.position_pct = min(.25, float(os.getenv("POSITION_SIZE_PCT", ".25"))); self.active_capital_pct = min(1., max(.05, float(os.getenv("ACTIVE_CAPITAL_PCT", ".75"))))
        self.entry_score = max(4, int(os.getenv("ENTRY_SCORE", "4"))); self.scan_limit = min(60, max(10, int(os.getenv("SCAN_LIMIT", "40")))); self.ai_candidates = min(10, max(3, int(os.getenv("AI_CANDIDATES", "8")))); self.ai_min_score = min(100, max(50, int(os.getenv("AI_MIN_SCORE", "70"))))
        self.ai_model = os.getenv("AI_MODEL", "openai/gpt-oss-20b"); self.ai_fallback_model = os.getenv("AI_FALLBACK_MODEL", "llama-3.1-8b-instant"); self.ai_cache_minutes = max(5, int(os.getenv("AI_CACHE_MINUTES", "15"))); self.allow_without_ai = os.getenv("ALLOW_WITHOUT_AI", "false").lower() == "true"; self.groq_key = os.getenv("GROQ_API_KEY", "").strip()
        self.positions = {}; self.history = []; self.signals = {}; self.ai_reviews = {}; self.last_ai_at = {}; self.last_check = None; self.last_error = None; self.last_ai_error = None; self.last_trade_block_reason = "Henüz tarama yapılmadı."; self.scanned_count = 0; self.ai_review_count = 0; self.last_scan_ms = 0; self.next_scan_at = 0; self.scan_interval_seconds = max(120, int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))); self.groq_cooldown_until = 0; self.last_groq_status = "Hazır"; self.last_ai_request_at = None; self.ai_batch_size = 0; self.pending_symbols = set(); self.last_sell_at = {}; self.consecutive_losses = 0; self.cooldown_until = 0
        self.trade_stats={"buy_count":0,"sell_count":0,"total_buy_try":0.,"total_sell_try":0.,"wins":0,"losses":0,"daily_realized_pnl":0.,"day":self.now_tr_date()}
        self.bn = BinanceClient(); self._load_state()
        if self.live_trading and not self.live_confirm: self.last_error="LIVE_TRADING=true ama LIVE_CONFIRM eksik; canlı emirler güvenlik nedeniyle kapalı."
        if self.live_trading and not self.bn.configured: self.last_error="Canlı mod için BINANCE_API_KEY ve BINANCE_API_SECRET gerekli."
        if self.live_trading and not self.database_url: self.last_error="Canlı mod için DATABASE_URL gerekli."

    def now_tr(self): return datetime.now(TR_ZONE)
    def now_tr_date(self): return self.now_tr().date().isoformat()
    def runtime(self):
        sec=max(0,int((datetime.now(timezone.utc)-self.started_at).total_seconds())); d,rem=divmod(sec,86400); h,rem=divmod(rem,3600); m,s=divmod(rem,60)
        return {"uptime_text":f"{d} gün {h:02d} saat {m:02d} dk {s:02d} sn","started_at_tr":self.started_at.astimezone(TR_ZONE).strftime("%d.%m.%Y %H:%M:%S"),"now_tr":self.now_tr().strftime("%d.%m.%Y %H:%M:%S")}

    def _load_state(self):
        self.database_url=os.getenv("DATABASE_URL","").strip()
        if not self.database_url or psycopg is None:
            if self.live_trading: self.last_error="Canlı mod için DATABASE_URL gerekli."
            return
        try:
            with psycopg.connect(self.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute("CREATE TABLE IF NOT EXISTS bot_state_v21_binance (id INTEGER PRIMARY KEY, payload JSONB NOT NULL, updated_at TIMESTAMPTZ DEFAULT NOW())")
                    cur.execute("SELECT payload FROM bot_state_v21_binance WHERE id=1"); row=cur.fetchone()
                    if row:
                        data=row[0]; self.positions=data.get("positions",{}); self.history=data.get("history",[]); self.trade_stats=data.get("trade_stats",self.trade_stats); self.realized_pnl=float(data.get("realized_pnl",0)); self.consecutive_losses=int(data.get("consecutive_losses",0)); self.cooldown_until=float(data.get("cooldown_until",0)); self.last_sell_at=data.get("last_sell_at",{}); self.initial_balance=float(data.get("initial_balance",self.initial_balance))
        except Exception as e:
            self.last_error=f"Veritabanı yükleme hatası: {type(e).__name__}: {e}"

    def _save_state(self):
        if not self.database_url or psycopg is None: return
        payload={"positions":self.positions,"history":self.history[:100],"trade_stats":self.trade_stats,"realized_pnl":self.realized_pnl,"consecutive_losses":self.consecutive_losses,"cooldown_until":self.cooldown_until,"last_sell_at":self.last_sell_at,"initial_balance":self.initial_balance}
        try:
            with psycopg.connect(self.database_url) as conn:
                with conn.cursor() as cur: cur.execute("CREATE TABLE IF NOT EXISTS bot_state_v21_binance (id INTEGER PRIMARY KEY, payload JSONB NOT NULL, updated_at TIMESTAMPTZ DEFAULT NOW())"); cur.execute("INSERT INTO bot_state_v21_binance (id,payload) VALUES (1,%s) ON CONFLICT (id) DO UPDATE SET payload=EXCLUDED.payload, updated_at=NOW()",(json.dumps(payload,ensure_ascii=False),))
        except Exception as e: self.last_error=f"Veritabanı kayıt hatası: {type(e).__name__}: {e}"

    def _reset_daily_stats(self):
        today=self.now_tr_date()
        if self.trade_stats.get("day")!=today:
            self.trade_stats.update({"buy_count":0,"sell_count":0,"total_buy_try":0.,"total_sell_try":0.,"wins":0,"losses":0,"daily_realized_pnl":0.,"day":today}); self.consecutive_losses=0

    def settings(self):
        return {"risk":self.risk,"tp":self.tp,"sl":self.sl,"trailing":self.trailing,"max_positions":self.max_positions,"position_pct":self.position_pct,"active_capital_pct":self.active_capital_pct,"entry_score":self.entry_score,"scan_limit":self.scan_limit,"min_buy_try":self.min_buy_try,"min_loss_try":self.min_loss_try,"min_profit_try":self.min_profit_try,"ai_candidates":self.ai_candidates,"ai_min_score":self.ai_min_score,"ai_model":self.ai_model,"ai_provider":"Groq • web kapalı","ai_cache_minutes":self.ai_cache_minutes,"scan_interval_seconds":self.scan_interval_seconds,"max_daily_loss_try":self.max_daily_loss_try,"max_consecutive_losses":self.max_consecutive_losses,"loss_cooldown_minutes":self.loss_cooldown_minutes,"live_trading":self.live_trading,"mode":self.mode,"binance_testnet":self.bn.testnet,"quote_asset":self.quote_asset}

    def update_settings(self,d):
        for k,attr in [("risk","risk"),("tp","tp"),("sl","sl"),("trailing","trailing")]:
            if k in d:
                v=float(d[k])/100
                if not 0<v<=.25: raise ValueError(f"{k} % 0-25 arasında olmalı")
                setattr(self,attr,v)
        if "max_positions" in d:self.max_positions=max(1,min(20,int(d["max_positions"])))
        if "position_pct" in d:self.position_pct=min(.25,max(.01,float(d["position_pct"])/100))
        if "active_capital_pct" in d:self.active_capital_pct=min(1,max(.05,float(d["active_capital_pct"])/100))
        if "entry_score" in d:self.entry_score=max(4,min(5,int(d["entry_score"])))
        if "scan_limit" in d:self.scan_limit=max(10,min(60,int(d["scan_limit"])))
        if "ai_candidates" in d:self.ai_candidates=max(3,min(10,int(d["ai_candidates"])))
        if "ai_min_score" in d:self.ai_min_score=max(50,min(100,int(d["ai_min_score"])))
        if "min_buy_try" in d:self.min_buy_try=max(1000,float(d["min_buy_try"]))
        if "min_loss_try" in d:self.min_loss_try=max(100,float(d["min_loss_try"]))
        if "min_profit_try" in d:self.min_profit_try=max(150,float(d["min_profit_try"]))

    def get_fx(self):
        if self.fx_rate and time.time()-self.fx_at<900:return self.fx_rate
        try:
            r=requests.get(self.bn.base+"/api/v3/ticker/price",params={"symbol":self.quote_asset+"TRY"},timeout=8)
            if r.ok:
                rate=float(r.json()["price"]); self.fx_rate=rate; self.fx_at=time.time(); return rate
        except Exception: pass
        fixed=os.getenv("USDTRY_RATE","").strip()
        if fixed:
            self.fx_rate=float(fixed);self.fx_at=time.time();return self.fx_rate
        r=requests.get(FX_URL,params={"from":"USD","to":"TRY"},timeout=10);r.raise_for_status();rate=float(r.json()["rates"]["TRY"]);self.fx_rate=rate;self.fx_at=time.time();return rate

    def get_products(self):
        info=self.bn.exchange_info(); tradable={s["symbol"]:s for s in info.get("symbols",[]) if s.get("status")=="TRADING" and s.get("quoteAsset")==self.quote_asset and s.get("isSpotTradingAllowed",True)}
        tickers=self.bn.ticker_24hr(); out=[]
        for t in tickers:
            sym=t.get("symbol"); s=tradable.get(sym)
            if not s: continue
            try: price=float(t.get("lastPrice",0)); vol=float(t.get("quoteVolume",0)); chg=float(t.get("priceChangePercent",0))
            except Exception: continue
            if price<=0: continue
            base=s.get("baseAsset","")
            if base in {"USDT","USDC","BUSD","FDUSD","TUSD","USDP","DAI","USDS"}: continue
            out.append({"symbol":sym,"volume24":vol,"change24":chg,"price":price,"name":base,"base":base,"filters":s.get("filters",[])})
        out.sort(key=lambda x:x["volume24"],reverse=True); return out[:self.scan_limit]

    def _candles_df(self,symbol,interval,limit=200):
        rows=self.bn.klines(symbol,interval,limit); df=pd.DataFrame(rows,columns=["start","open","high","low","close","volume","close_time","quote_volume","trades","tb_base","tb_quote","ignore"])
        for c in ["open","high","low","close","volume"]: df[c]=pd.to_numeric(df[c],errors="coerce")
        return df.dropna().sort_values("start").reset_index(drop=True)

    def calc_indicators(self,df):
        df=df.copy(); df["ema20"]=df.close.ewm(span=20,adjust=False).mean(); df["ema50"]=df.close.ewm(span=50,adjust=False).mean(); delta=df.close.diff(); gain=delta.clip(lower=0).rolling(14).mean(); loss=(-delta.clip(upper=0)).rolling(14).mean(); rs=gain/loss.replace(0,np.nan); df["rsi"]=100-(100/(1+rs)); e12=df.close.ewm(span=12,adjust=False).mean(); e26=df.close.ewm(span=26,adjust=False).mean(); df["macd"]=e12-e26; df["macds"]=df.macd.ewm(span=9,adjust=False).mean(); df["v20"]=df.volume.rolling(20).mean(); return df

    def timeframe_snapshot(self,symbol,interval):
        df=self.calc_indicators(self._candles_df(symbol,interval,200)); x=df.iloc[-1]; return {"price":float(x.close),"ema20":float(x.ema20),"ema50":float(x.ema50),"rsi":float(x.rsi),"macd":float(x.macd),"macds":float(x.macds),"volume_ratio":float(x.volume/x.v20) if pd.notna(x.v20) and x.v20 else 0,"trend":bool(x.ema20>x.ema50)}

    def technical_analyze(self,product):
        sym=product["symbol"]; tf5=self.timeframe_snapshot(sym,"5m"); tf15=self.timeframe_snapshot(sym,"15m"); tf1h=self.timeframe_snapshot(sym,"1h")
        rsi_ok=45<=tf5["rsi"]<=72; trend_ok=tf5["trend"]; macd_ok=tf5["macd"]>tf5["macds"]; volume_ok=tf5["volume_ratio"]>=.80; momentum_ok=product["change24"]>-2; tf15_ok=tf15["trend"] and tf15["macd"]>tf15["macds"]; tf1h_ok=tf1h["trend"] and tf1h["macd"]>tf1h["macds"]
        checks={"5dk Trend":trend_ok,"5dk RSI":rsi_ok,"5dk MACD":macd_ok,"5dk Hacim":volume_ok,"24s Momentum":momentum_ok,"15dk Onay":tf15_ok,"1s Onay":tf1h_ok}; core=sum(checks[k] for k in ["5dk Trend","5dk RSI","5dk MACD","5dk Hacim","24s Momentum"]); score=core+int(tf15_ok)+int(tf1h_ok); buy=trend_ok and macd_ok and core>=max(4,self.entry_score) and score>=5 and (tf15_ok or tf1h_ok)
        return {"symbol":sym,"name":product["name"],"price":tf5["price"],"rsi":tf5["rsi"],"change24":product["change24"],"volume_ratio":tf5["volume_ratio"],"score":score,"core_score":core,"checks":checks,"decision":"AL ADAYI" if buy else "BEKLE","tf5":tf5,"tf15":tf15,"tf1h":tf1h,"reasons":[f"5dk trend {'uygun' if trend_ok else 'zayıf'}",f"5dk RSI {tf5['rsi']:.1f}",f"5dk MACD {'pozitif' if macd_ok else 'negatif'}",f"5dk hacim {tf5['volume_ratio']:.2f}x",f"24s momentum {product['change24']:+.2f}%",f"15dk {'onay' if tf15_ok else 'onay yok'}",f"1s {'onay' if tf1h_ok else 'onay yok'}"],"time":self.now_tr().strftime("%H:%M:%S")}

    def _compact_ai_result(self,parsed):
        parsed=parsed if isinstance(parsed,dict) else {}; raw_score=parsed.get("score",0)
        try: score=max(0,min(100,int(float(raw_score))))
        except Exception: score=0
        decision=str(parsed.get("decision","BEKLE")).upper(); risk=str(parsed.get("risk","ORTA")).upper(); decision=decision if decision in ("AL","BEKLE") else "BEKLE"; risk=risk if risk in ("DÜŞÜK","ORTA","YÜKSEK") else "ORTA"
        return {"score":score,"decision":decision,"risk":risk,"summary":str(parsed.get("summary",""))[:400],"positive":[] ,"negative":[],"sources":[]}

    def _ai_request(self,model,prompt):
        if time.time()<self.groq_cooldown_until: raise RuntimeError("Groq cooldown")
        self.last_ai_request_at=self.now_tr().strftime("%d.%m.%Y %H:%M:%S")
        payload={"model":model,"messages":[{"role":"system","content":"Türkçe yanıt ver. Sadece geçerli JSON nesnesi üret. Şu formatı kullan: {\"reviews\":[{\"symbol\":\"BTCUSDT\",\"score\":0,\"decision\":\"AL\",\"risk\":\"DÜŞÜK\",\"summary\":\"kısa\"}]}"},{"role":"user","content":prompt}],"max_completion_tokens":900,"temperature":0.1,"response_format":{"type":"json_object"}}
        r=requests.post(GROQ_CHAT,headers={"Authorization":f"Bearer {self.groq_key}","Content-Type":"application/json"},json=payload,timeout=35)
        if not r.ok:
            try: detail=(r.json().get("error") or {}).get("message",r.text)
            except Exception: detail=r.text
            if r.status_code==429:self.groq_cooldown_until=time.time()+60
            raise RuntimeError(f"Groq HTTP {r.status_code}: {str(detail)[:500]}")
        self.last_groq_status=f"200 OK • {model}"; return r.json()

    def _extract_ai_json(self,data):
        if not isinstance(data,dict): raise RuntimeError("Groq yanıtı nesne değil")
        choices=data.get("choices")
        if not isinstance(choices,list) or not choices or not isinstance(choices[0],dict): raise RuntimeError(f"Groq choices alanı geçersiz: {str(data)[:500]}")
        msg=choices[0].get("message")
        if not isinstance(msg,dict): raise RuntimeError(f"Groq message alanı geçersiz: {str(choices[0])[:500]}")
        content=msg.get("content")
        if not isinstance(content,str) or not content.strip(): raise RuntimeError("Groq boş content döndürdü")
        text=content.strip(); m=re.search(r"\{.*\}",text,re.S)
        try: parsed=json.loads(m.group(0) if m else text)
        except Exception as e: raise RuntimeError(f"Groq JSON ayrıştırılamadı: {e}")
        if not isinstance(parsed,dict) or not isinstance(parsed.get("reviews"),list): raise RuntimeError("Groq JSON içinde reviews listesi yok")
        return parsed

    def ai_research(self,candidates):
        if not candidates:return {}
        if not self.groq_key:
            msg="GROQ_API_KEY yok. AI zorunlu olduğu için yeni alım açılmadı." if not self.allow_without_ai else "GROQ_API_KEY yok; teknik mod izinli."
            return {c["symbol"]:{"score":0,"decision":"BEKLE","risk":"Bilinmiyor","summary":msg,"positive":[],"negative":[],"sources":[]} for c in candidates}
        compact=[{"symbol":c["symbol"],"price":round(c["price"],8),"change24":round(c["change24"],2),"rsi":round(c["rsi"],1),"volume_ratio":round(c["volume_ratio"],2),"technical_score":c["score"],"core_score":c["core_score"]} for c in candidates]
        prompt="Kripto spot AL filtresi. Web kullanma. Sadece verilen teknik veriyi değerlendir. Her coin için skor 0-100, karar AL/BEKLE, risk DÜŞÜK/ORTA/YÜKSEK ve en fazla 160 karakter özet ver. Sadece JSON üret. Veri="+json.dumps(compact,ensure_ascii=False,separators=(",",":"))
        self.ai_batch_size=len(candidates)
        models=[self.ai_model]+([self.ai_fallback_model] if self.ai_fallback_model and self.ai_fallback_model!=self.ai_model else [])
        last=None
        for model in models:
            try:
                parsed=self._extract_ai_json(self._ai_request(model,prompt)); items={x.get("symbol"):x for x in parsed.get("reviews",[]) if isinstance(x,dict) and x.get("symbol")}; out={}
                for c in candidates:
                    r=self._compact_ai_result(items.get(c["symbol"],{})); out[c["symbol"]]=r; self.ai_reviews[c["symbol"]]=r; self.last_ai_at[c["symbol"]]=time.time()
                self.last_ai_error=None; return out
            except Exception as e: last=str(e)
        self.last_ai_error=last or "Bilinmeyen AI hatası"; self.last_groq_status="AI hatası"; return {c["symbol"]:{"score":0,"decision":"BEKLE","risk":"Bilinmiyor","summary":f"AI araştırması başarısız: {self.last_ai_error}","positive":[],"negative":[],"sources":[]} for c in candidates}

    def _can_live_trade(self):
        if self.dry_run:return False,"DRY RUN"
        if not self.live_trading or not self.live_confirm:return False,"Canlı işlem güvenlik onayı eksik"
        if not self.bn.configured:return False,"BINANCE_API_KEY / BINANCE_API_SECRET eksik"
        if not self.database_url:return False,"DATABASE_URL eksik"
        return True,""

    def _filter_values(self,symbol):
        info=self.bn.symbol_info(symbol); f={x.get("filterType"):x for x in info.get("filters",[])}; lot=f.get("LOT_SIZE",{}); market_lot=f.get("MARKET_LOT_SIZE",lot); notional=f.get("NOTIONAL",f.get("MIN_NOTIONAL",{}));
        return {"base":info.get("baseAsset"),"quote":info.get("quoteAsset"),"step":float(market_lot.get("stepSize") or lot.get("stepSize") or 0),"min_qty":float(market_lot.get("minQty") or lot.get("minQty") or 0),"min_notional":float(notional.get("minNotional") or 0),"quote_order_qty_market_allowed":bool(info.get("quoteOrderQtyMarketAllowed",True))}

    def _buy_amount_quote(self):
        fx=self.get_fx(); min_quote=self.min_buy_try/fx; free=self.bn.free_balance(self.quote_asset) if not self.dry_run else self.balance_try/fx; equity=free+sum(p.get("last",0)*p.get("qty",0) for p in self.positions.values()); active_budget=equity*self.active_capital_pct; active_used=sum(p.get("buy_quote",0) for p in self.positions.values()); remaining=max(0,active_budget-active_used); amount=min(free,equity*self.position_pct,remaining); return amount if amount+1e-9>=min_quote else 0.0

    def _open(self,s):
        symbol=s["symbol"]
        if symbol in self.positions or symbol in self.pending_symbols:return False
        self._reset_daily_stats()
        if self.trade_stats["daily_realized_pnl"]<=-self.max_daily_loss_try or time.time()<self.cooldown_until or self.consecutive_losses>=self.max_consecutive_losses:return False
        ai=self.ai_reviews.get(symbol,{})
        if not (ai.get("decision")=="AL" and int(ai.get("score",0))>=self.ai_min_score and ai.get("risk")!="YÜKSEK") and not self.allow_without_ai:return False
        amount=self._buy_amount_quote()
        if amount<=0:return False
        fx=self.get_fx(); buy_try=amount*fx; self.pending_symbols.add(symbol)
        try:
            if self.dry_run:
                price=s["price"]; qty=amount/max(price,1e-12); filled_value=amount; fee=0; order_id="SIM-"+uuid.uuid4().hex[:12]
            else:
                flt=self._filter_values(symbol)
                if not flt["quote_order_qty_market_allowed"]: raise RuntimeError(f"{symbol}: Binance quoteOrderQty ile market alışa izin vermiyor")
                if amount<flt["min_notional"]: raise RuntimeError(f"{symbol}: minimum notional {flt['min_notional']} {self.quote_asset}")
                cid="CBOT"+uuid.uuid4().hex[:20]
                try: resp=self.bn.create_market_buy(symbol,amount,cid)
                except Exception as first:
                    # If Binance returned a 5xx/timeout, query the client order id before assuming failure.
                    try: resp=self.bn.order(symbol,client_order_id=cid)
                    except Exception: raise first
                order_id=str(resp.get("orderId") or resp.get("clientOrderId") or cid)
                order=resp
                if str(order.get("status","")).upper() not in {"FILLED","PARTIALLY_FILLED"} or not order.get("executedQty"):
                    for _ in range(10):
                        time.sleep(.7); order=self.bn.order(symbol,order_id=resp.get("orderId")) if resp.get("orderId") else self.bn.order(symbol,client_order_id=cid)
                        if str(order.get("status","")).upper() in {"FILLED","CANCELED","REJECTED","EXPIRED"}:break
                qty=float(order.get("executedQty") or 0); filled_value=float(order.get("cummulativeQuoteQty") or 0); fee=sum(float(x.get("commission",0)) for x in order.get("fills",[]) if x.get("commissionAsset")==self.quote_asset)
                if qty<=0 or filled_value<=0: raise RuntimeError(f"Alış gerçekleşmedi: status={order.get('status')} {order.get('msg','')}")
                price=filled_value/qty; buy_try=(filled_value+fee)*fx
            self.positions[symbol]={"symbol":symbol,"name":s["name"],"entry":price,"qty":qty,"buy_quote":filled_value+fee,"buy_usd":filled_value+fee,"buy_amount_try":buy_try,"last":price,"unrealized":0,"peak_pnl":0,"ai_score":ai.get("score",0),"ai_risk":ai.get("risk",""),"opened":self.now_tr().strftime("%d.%m.%Y %H:%M:%S"),"order_id":order_id,"fee_quote":fee,"fx_rate":fx}
            self.trade_stats["buy_count"]+=1; self.trade_stats["total_buy_try"]+=buy_try; self.history.insert(0,{"type":"OPEN","symbol":symbol,"price":price,"qty":qty,"buy_try":buy_try,"sell_try":None,"pnl":0,"reason":f"AL • Teknik {s['score']}/7 • AI {ai.get('score',0)}/100 • {self.mode}","time":self.now_tr().strftime("%d.%m.%Y %H:%M:%S"),"order_id":order_id}); self._save_state(); return True
        finally:self.pending_symbols.discard(symbol)

    def _close(self,symbol,p,reason):
        fx=self.get_fx(); price=float(p.get("last") or p.get("entry") or 0); qty=float(p.get("qty") or 0); oid=""
        if qty<=0: raise RuntimeError(f"{symbol}: satış miktarı geçersiz")
        if not self.dry_run:
            flt=self._filter_values(symbol); qty=dec_floor(qty,flt["step"])
            if qty<flt["min_qty"]: raise RuntimeError(f"{symbol}: satış miktarı minQty altında")
            if qty*price<flt["min_notional"]: raise RuntimeError(f"{symbol}: satış notional minNotional altında")
            cid="CBOT"+uuid.uuid4().hex[:20]
            try: resp=self.bn.create_market_sell(symbol,qty,cid)
            except Exception as first:
                try: resp=self.bn.order(symbol,client_order_id=cid)
                except Exception: raise first
            oid=str(resp.get("orderId") or resp.get("clientOrderId") or cid); order=resp
            if str(order.get("status","")).upper() not in {"FILLED","PARTIALLY_FILLED"}:
                for _ in range(10):
                    time.sleep(.7); order=self.bn.order(symbol,order_id=resp.get("orderId")) if resp.get("orderId") else self.bn.order(symbol,client_order_id=cid)
                    if str(order.get("status","")).upper() in {"FILLED","CANCELED","REJECTED","EXPIRED"}:break
            filled=float(order.get("executedQty") or 0); value=float(order.get("cummulativeQuoteQty") or 0); fee=sum(float(x.get("commission",0)) for x in order.get("fills",[]) if x.get("commissionAsset")==self.quote_asset)
            if filled<=0 or value<=0: raise RuntimeError(f"Satış gerçekleşmedi: status={order.get('status')} {order.get('msg','')}")
            qty=filled; price=value/filled; sell_quote=value-fee; sell_try=sell_quote*fx
        else:
            sell_quote=price*qty; sell_try=sell_quote*fx; fee=0; oid="SIM-"+uuid.uuid4().hex[:12]
        pnl=sell_try-p["buy_amount_try"]; self.trade_stats["sell_count"]+=1; self.trade_stats["total_sell_try"]+=sell_try; self.trade_stats["daily_realized_pnl"]+=pnl; self.realized_pnl+=pnl
        if pnl>0:self.trade_stats["wins"]+=1;self.consecutive_losses=0
        elif pnl<0:self.trade_stats["losses"]+=1;self.consecutive_losses+=1;self.cooldown_until=time.time()+self.loss_cooldown_minutes*60 if self.consecutive_losses>=self.max_consecutive_losses else 0
        self.history.insert(0,{"type":"CLOSE","symbol":symbol,"price":price,"qty":qty,"buy_try":p["buy_amount_try"],"sell_try":sell_try,"pnl":pnl,"reason":reason,"time":self.now_tr().strftime("%d.%m.%Y %H:%M:%S"),"order_id":oid,"fee_quote":fee}); self.last_sell_at[symbol]=time.time(); del self.positions[symbol]; self._save_state()

    def manage_positions(self):
        for symbol,p in list(self.positions.items()):
            try:
                product={"symbol":symbol,"name":p.get("name",symbol),"change24":0}; s=self.technical_analyze(product); p["last"]=s["price"]; p["peak_price"]=max(float(p.get("peak_price",p["entry"])),p["last"]); value=p["last"]*p["qty"]; pnl=(value-p.get("buy_quote",p.get("buy_usd",0)))*self.get_fx(); p["unrealized"]=pnl; p["peak_pnl"]=max(float(p.get("peak_pnl",0)),pnl); loss=pnl<=-self.min_loss_try; profit=pnl>=self.min_profit_try; trailing=p["peak_pnl"]>=self.min_profit_try and pnl<=p["peak_pnl"]*(1-self.trailing)
                if loss:self._close(symbol,p,f"ZARAR EŞİĞİ (-{self.min_loss_try:.0f} TL)")
                elif profit or trailing:self._close(symbol,p,f"KÂR/TRAILING (+{pnl:.0f} TL)")
            except Exception as e:self.last_error=f"Pozisyon yönetimi {symbol}: {type(e).__name__}: {e}"

    def tick(self,force=False):
        if not self.running:return
        if not force and time.time()<self.next_scan_at:return
        if not self.lock.acquire(blocking=False):return
        try:
            self.last_check=self.now_tr().strftime("%d.%m.%Y %H:%M:%S"); self.last_error=None; self._reset_daily_stats()
            if self.live_trading and not self.bn.configured:self.last_error="Binance API anahtarı eksik"; return
            try: products=self.get_products()
            except Exception as e:self.last_error=f"Binance coin listesi alınamadı: {type(e).__name__}: {e}"; return
            results=[]
            for product in products:
                try: results.append(self.technical_analyze(product))
                except Exception: pass
            results.sort(key=lambda x:(x["score"],x["core_score"],x["change24"]),reverse=True); self.scanned_count=len(results)
            # Existing positions must be managed independently of AI. A broken AI must never block exits.
            self.manage_positions()
            eligible=[x for x in results if x["score"]>=max(4,self.entry_score) and x["core_score"]>=3][:self.ai_candidates]
            fresh=dict(self.last_ai_at); self.ai_reviews=self.ai_research(eligible); self.ai_review_count=sum(1 for c in eligible if self.last_ai_at.get(c["symbol"],0)!=fresh.get(c["symbol"],0))
            for x in results:
                x["ai"]=self.ai_reviews.get(x["symbol"]); x["final_decision"]="AL" if x.get("ai",{}).get("decision")=="AL" and x.get("ai",{}).get("score",0)>=self.ai_min_score else x["decision"]
            self.signals={x["symbol"]:x for x in results}
            ranked=[x for x in results if x["decision"]=="AL ADAYI" and (x.get("ai",{}).get("decision")=="AL" and x.get("ai",{}).get("score",0)>=self.ai_min_score and x.get("ai",{}).get("risk")!="YÜKSEK" or self.allow_without_ai and not self.groq_key)]
            ranked.sort(key=lambda x:(x.get("ai",{}).get("score",0),x["score"],x["change24"]),reverse=True)
            if not ranked:self.last_trade_block_reason=f"Alım yok: AI {self.ai_min_score}+ skorlu güvenli AL adayı yok." if not self.allow_without_ai else "Alım yok: teknik olarak uygun aday bulunamadı."
            opened=0
            for s in ranked:
                if len(self.positions)>=self.max_positions:break
                if self._open(s):opened+=1
            if opened:self.last_trade_block_reason=f"{opened} {'canlı' if not self.dry_run else 'sanal'} pozisyon açıldı."
            self._save_state(); self.last_scan_ms=int(time.time()*1000)
        finally:self.next_scan_at=time.time()+self.scan_interval_seconds;self.lock.release()

    def status(self):
        self._reset_daily_stats(); fx=self.get_fx()
        if self.dry_run:
            market_value=sum(p.get("last",0)*p.get("qty",0) for p in self.positions.values())*fx; cash=self.balance_try; equity=cash+market_value
        else:
            try: cash=self.bn.free_balance(self.quote_asset)*fx; market_value=sum(p.get("last",0)*p.get("qty",0) for p in self.positions.values())*fx; equity=cash+market_value
            except Exception as e: cash=0;market_value=0;equity=0;self.last_error=f"Canlı bakiye okunamadı: {e}"
        total_pnl=equity-self.initial_balance; closed=self.trade_stats["sell_count"]; wr=self.trade_stats["wins"]/closed*100 if closed else 0
        ranked=sorted(self.signals.values(),key=lambda x:(x.get("final_decision")=="AL",x.get("ai",{}).get("score",0),x.get("score",0),x.get("change24",0)),reverse=True)
        return {"running":self.running,"dry_run":self.dry_run,"live_trading":self.live_trading,"mode":self.mode,"exchange":"Binance Spot","binance_testnet":self.bn.testnet,"cash_try":money(cash),"balance_try":money(equity),"market_value_try":money(market_value),"pnl":money(total_pnl),"realized_pnl":money(self.realized_pnl),"positions":list(self.positions.values()),"signals":ranked[:25],"scanned_count":self.scanned_count,"ai_review_count":self.ai_review_count,"last_check":self.last_check,"last_error":self.last_error,"trade_block_reason":self.last_trade_block_reason,"last_ai_error":self.last_ai_error,"groq_status":self.last_groq_status,"groq_cooldown_seconds":max(0,int(self.groq_cooldown_until-time.time())),"last_ai_request_at":self.last_ai_request_at,"ai_batch_size":self.ai_batch_size,"scan_interval_seconds":self.scan_interval_seconds,"settings":self.settings(),"data_source":"Binance Spot market data + Binance Spot trading API","entry_logic":"5dk/15dk/1s teknik filtre + Groq AI + risk motoru","exit_logic":f"Min alış {self.min_buy_try:.0f} TL • Min zarar -{self.min_loss_try:.0f} TL • Min kâr +{self.min_profit_try:.0f} TL • trailing {self.trailing*100:.1f}%","runtime":self.runtime(),"daily_stats":{"day":self.trade_stats["day"],"daily_pnl":money(self.trade_stats["daily_realized_pnl"]),"total_buy_try":money(self.trade_stats["total_buy_try"]),"total_sell_try":money(self.trade_stats["total_sell_try"]),"wins":self.trade_stats["wins"],"losses":self.trade_stats["losses"],"win_rate":round(wr,1)},"risk_guard":{"daily_loss_limit_try":self.max_daily_loss_try,"consecutive_losses":self.consecutive_losses,"max_consecutive_losses":self.max_consecutive_losses,"cooldown_seconds":max(0,int(self.cooldown_until-time.time()))},"history":self.history[:30]}
