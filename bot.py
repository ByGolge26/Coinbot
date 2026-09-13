import os, time, json, uuid, threading, hmac, hashlib
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
import pandas as pd
import numpy as np

try:
    import psycopg
except ImportError:
    psycopg = None

BINANCE_TR_BASE = os.getenv('BINANCE_TR_BASE', 'https://www.binance.tr').rstrip('/')
BINANCE_MARKET_BASE = os.getenv('BINANCE_MARKET_BASE', 'https://api.binance.me').rstrip('/')
GROQ_CHAT = 'https://api.groq.com/openai/v1/chat/completions'
TR_ZONE = ZoneInfo('Europe/Istanbul')


def money(v):
    try: return round(float(v), 2)
    except Exception: return 0.0

def dec_floor(value, increment):
    v, inc = Decimal(str(value)), Decimal(str(increment))
    if inc <= 0: return float(v)
    return float((v / inc).to_integral_value(rounding=ROUND_DOWN) * inc)

class BinanceTRClient:
    def __init__(self):
        self.api_key = os.getenv('BINANCE_API_KEY', '').strip()
        self.api_secret = os.getenv('BINANCE_API_SECRET', '').strip()
        self.timeout = max(5, int(os.getenv('BINANCE_HTTP_TIMEOUT', '20')))
        self.recv_window = min(60000, max(1000, int(os.getenv('BINANCE_RECV_WINDOW', '5000'))))
        self.time_offset_ms = 0

    @property
    def configured(self):
        return bool(self.api_key and self.api_secret)

    def _encode(self, params):
        from urllib.parse import urlencode
        return urlencode([(k, v) for k, v in params.items() if v is not None], doseq=True)

    def _signed_params(self, params=None):
        p = dict(params or {})
        p.setdefault('recvWindow', self.recv_window)
        p['timestamp'] = int(time.time() * 1000) + self.time_offset_ms
        query = self._encode(p)
        p['signature'] = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        return p

    def sync_time(self):
        r = requests.get(BINANCE_TR_BASE + '/open/v1/common/time', timeout=self.timeout)
        r.raise_for_status()
        j = r.json()
        if j.get('code', 0) != 0:
            raise RuntimeError(j.get('msg', 'Binance server time error'))
        self.time_offset_ms = int(j['timestamp']) - int(time.time() * 1000)
        return j['timestamp']

    def public(self, path, params=None, market=False):
        base = BINANCE_MARKET_BASE if market else BINANCE_TR_BASE
        r = requests.get(base + path, params=params or {}, timeout=self.timeout,
                         headers={'User-Agent':'Coinbot-V22'})
        if not r.ok:
            try: detail = r.json()
            except Exception: detail = r.text
            raise RuntimeError(f'Binance HTTP {r.status_code}: {str(detail)[:600]}')
        j = r.json()
        if isinstance(j, dict) and j.get('code', 0) not in (0, None):
            raise RuntimeError(f"Binance API {j.get('code')}: {j.get('msg')}")
        return j

    def signed(self, method, path, params=None):
        if not self.configured:
            raise RuntimeError('BINANCE_API_KEY / BINANCE_API_SECRET eksik')
        try:
            self.sync_time()
        except Exception:
            pass
        p = self._signed_params(params)
        headers = {'X-MBX-APIKEY': self.api_key, 'User-Agent':'Coinbot-V22'}
        if method.upper() == 'GET':
            r = requests.get(BINANCE_TR_BASE + path, params=p, headers=headers, timeout=self.timeout)
        else:
            r = requests.post(BINANCE_TR_BASE + path, data=p, headers=headers, timeout=self.timeout)
        if not r.ok:
            try: detail = r.json()
            except Exception: detail = r.text
            raise RuntimeError(f'Binance TR HTTP {r.status_code}: {str(detail)[:800]}')
        j = r.json()
        if isinstance(j, dict) and j.get('code', 0) not in (0, None):
            raise RuntimeError(f"Binance TR API {j.get('code')}: {j.get('msg') or j.get('message')}")
        return j

    def symbols(self):
        return self.public('/open/v1/common/symbols').get('data', {}).get('list', [])

    def account(self):
        return self.signed('GET', '/open/v1/account/spot').get('data', {})

    def try_available(self):
        for a in self.account().get('accountAssets', []):
            if a.get('asset') == 'TRY':
                return float(a.get('free') or 0)
        return 0.0

    def candles(self, symbol, interval='5m', limit=1000):
        # Main (symbol type 1) market data is documented by Binance TR on api.binance.me.
        # Symbols use the underscore-free MBX form for this endpoint.
        market_symbol = symbol.replace('_', '')
        j = self.public('/api/v1/klines', {'symbol': market_symbol, 'interval': interval, 'limit': min(1000, limit)}, market=True)
        rows = j.get('data', j if isinstance(j, list) else [])
        if not rows:
            raise RuntimeError(f'{symbol}: mum verisi boş')
        df = pd.DataFrame(rows, columns=['start','open','high','low','close','volume','close_time','quote_volume','trades','taker_base','taker_quote','ignore'])
        for c in ['open','high','low','close','volume','quote_volume']:
            df[c] = pd.to_numeric(df[c], errors='coerce')
        df['start'] = pd.to_numeric(df['start'], errors='coerce')
        return df.dropna().sort_values('start').reset_index(drop=True)

    def order_market_buy(self, symbol, quote_try):
        return self.signed('POST', '/open/v1/orders', {
            'symbol': symbol, 'side': 0, 'type': 2,
            'quoteOrderQty': f'{quote_try:.2f}', 'clientId': 'CB-' + uuid.uuid4().hex[:20]
        })

    def order_market_sell(self, symbol, qty):
        return self.signed('POST', '/open/v1/orders', {
            'symbol': symbol, 'side': 1, 'type': 2,
            'quantity': f'{qty:.12f}', 'clientId': 'CB-' + uuid.uuid4().hex[:20]
        })

    def order_detail(self, order_id=None, client_id=None):
        p = {'orderId': order_id} if order_id is not None else {'clientId': client_id}
        return self.signed('GET', '/open/v1/orders/detail', p).get('data', {})

class TradingBot:
    def __init__(self):
        self.lock = threading.Lock(); self.running = True
        self.started_at = datetime.now(timezone.utc)
        self.live_trading = os.getenv('LIVE_TRADING','false').lower() == 'true'
        self.live_confirm = os.getenv('LIVE_CONFIRM','').strip() == 'I_UNDERSTAND_REAL_MONEY_TRADING'
        self.dry_run = not (self.live_trading and self.live_confirm)
        self.mode = 'LIVE' if not self.dry_run else 'DRY RUN'
        self.initial_balance = float(os.getenv('STARTING_TRY_BALANCE','5000'))
        self.balance_try = self.initial_balance
        self.realized_pnl = 0.0
        self.risk = float(os.getenv('RISK_PER_TRADE','0.01'))
        self.tp = float(os.getenv('TAKE_PROFIT','0.025'))
        self.sl = float(os.getenv('STOP_LOSS','0.01'))
        self.trailing = float(os.getenv('TRAILING_STOP','0.01'))
        self.min_buy_try = max(1000.0, float(os.getenv('MIN_BUY_TRY','1000')))
        self.min_loss_try = max(100.0, float(os.getenv('MIN_LOSS_TRY','100')))
        self.min_profit_try = max(150.0, float(os.getenv('MIN_PROFIT_TRY','150')))
        self.max_daily_loss_try = max(100.0, float(os.getenv('MAX_DAILY_LOSS_TRY','200')))
        self.max_consecutive_losses = max(1, int(os.getenv('MAX_CONSECUTIVE_LOSSES','3')))
        self.loss_cooldown_minutes = max(5, int(os.getenv('LOSS_COOLDOWN_MINUTES','30')))
        self.max_positions = max(1, min(20, int(os.getenv('MAX_OPEN_POSITIONS','20'))))
        self.position_pct = min(.25, max(.01, float(os.getenv('POSITION_SIZE_PCT','0.25'))))
        self.active_capital_pct = min(1.0, max(.05, float(os.getenv('ACTIVE_CAPITAL_PCT','0.75'))))
        self.entry_score = max(4, min(5, int(os.getenv('ENTRY_SCORE','4'))))
        self.scan_limit = min(60, max(10, int(os.getenv('SCAN_LIMIT','40'))))
        self.ai_candidates = min(10, max(3, int(os.getenv('AI_CANDIDATES','8'))))
        self.ai_min_score = min(100, max(50, int(os.getenv('AI_MIN_SCORE','70'))))
        self.ai_model = os.getenv('AI_MODEL','llama-3.1-8b-instant').strip()
        self.ai_fallback_model = os.getenv('AI_FALLBACK_MODEL','').strip()
        self.groq_key = os.getenv('GROQ_API_KEY','').strip()
        self.allow_without_ai = os.getenv('ALLOW_WITHOUT_AI','false').lower() == 'true'
        self.scan_interval_seconds = max(120, int(os.getenv('SCAN_INTERVAL_SECONDS','300')))
        self.positions={}; self.history=[]; self.signals={}; self.ai_reviews={}; self.last_ai_at={}
        self.last_check=None; self.last_error=None; self.last_ai_error=None; self.last_trade_block_reason='Henüz tarama yapılmadı.'
        self.last_groq_status='Hazır'; self.last_ai_request_at=None; self.ai_batch_size=0; self.scanned_count=0; self.ai_review_count=0
        self.last_scan_ms=0; self.consecutive_losses=0; self.cooldown_until=0; self.pending_symbols=set(); self.next_scan_at=0
        self.database_url=''; self.trade_stats={'buy_count':0,'sell_count':0,'total_buy_try':0.0,'total_sell_try':0.0,'wins':0,'losses':0,'daily_realized_pnl':0.0,'day':self.now_tr_date()}
        self.binance = BinanceTRClient(); self._load_state()
        if self.live_trading and not self.live_confirm: self.last_error='LIVE_TRADING=true ama LIVE_CONFIRM eksik; canlı emirler kapalı.'
        if self.live_trading and not self.binance.configured: self.last_error='Canlı mod için BINANCE_API_KEY ve BINANCE_API_SECRET gerekli.'
        if self.live_trading and not self.database_url: self.last_error='Canlı mod için DATABASE_URL gerekli.'

    def now_tr(self): return datetime.now(TR_ZONE)
    def now_tr_date(self): return self.now_tr().date().isoformat()
    def runtime(self):
        sec=max(0,int((datetime.now(timezone.utc)-self.started_at).total_seconds())); d,rem=divmod(sec,86400); h,rem=divmod(rem,3600); m,s=divmod(rem,60)
        return {'uptime_text':f'{d} gün {h:02d} saat {m:02d} dk {s:02d} sn','started_at_tr':self.started_at.astimezone(TR_ZONE).strftime('%d.%m.%Y %H:%M:%S'),'now_tr':self.now_tr().strftime('%d.%m.%Y %H:%M:%S')}

    def _load_state(self):
        self.database_url=os.getenv('DATABASE_URL','').strip()
        if not self.database_url or psycopg is None: return
        try:
            with psycopg.connect(self.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute('CREATE TABLE IF NOT EXISTS bot_state (id INTEGER PRIMARY KEY, payload JSONB NOT NULL, updated_at TIMESTAMPTZ DEFAULT NOW())')
                    cur.execute('SELECT payload FROM bot_state WHERE id=1'); row=cur.fetchone()
                    if row:
                        d=row[0] or {}; self.positions=d.get('positions',{}); self.history=d.get('history',[]); self.trade_stats.update(d.get('trade_stats',{})); self.realized_pnl=float(d.get('realized_pnl',0)); self.consecutive_losses=int(d.get('consecutive_losses',0)); self.cooldown_until=float(d.get('cooldown_until',0)); self.initial_balance=float(d.get('initial_balance',self.initial_balance))
        except Exception as e: self.last_error=f'DB yükleme hatası: {type(e).__name__}: {e}'

    def _save_state(self):
        if not self.database_url or psycopg is None: return
        payload={'positions':self.positions,'history':self.history[:150],'trade_stats':self.trade_stats,'realized_pnl':self.realized_pnl,'consecutive_losses':self.consecutive_losses,'cooldown_until':self.cooldown_until,'initial_balance':self.initial_balance}
        try:
            with psycopg.connect(self.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute('CREATE TABLE IF NOT EXISTS bot_state (id INTEGER PRIMARY KEY, payload JSONB NOT NULL, updated_at TIMESTAMPTZ DEFAULT NOW())')
                    cur.execute('INSERT INTO bot_state (id,payload) VALUES (1,%s) ON CONFLICT (id) DO UPDATE SET payload=EXCLUDED.payload,updated_at=NOW()', (json.dumps(payload,ensure_ascii=False),))
        except Exception as e: self.last_error=f'DB kayıt hatası: {type(e).__name__}: {e}'

    def _reset_daily(self):
        today=self.now_tr_date()
        if self.trade_stats.get('day') != today:
            self.trade_stats.update({'buy_count':0,'sell_count':0,'total_buy_try':0.0,'total_sell_try':0.0,'wins':0,'losses':0,'daily_realized_pnl':0.0,'day':today}); self.consecutive_losses=0

    def settings(self):
        return {'risk':self.risk,'tp':self.tp,'sl':self.sl,'trailing':self.trailing,'max_positions':self.max_positions,'position_pct':self.position_pct,'active_capital_pct':self.active_capital_pct,'entry_score':self.entry_score,'scan_limit':self.scan_limit,'min_buy_try':self.min_buy_try,'min_loss_try':self.min_loss_try,'min_profit_try':self.min_profit_try,'ai_candidates':self.ai_candidates,'ai_min_score':self.ai_min_score,'ai_model':self.ai_model,'ai_provider':'Groq • web kapalı','scan_interval_seconds':self.scan_interval_seconds,'max_daily_loss_try':self.max_daily_loss_try,'max_consecutive_losses':self.max_consecutive_losses,'loss_cooldown_minutes':self.loss_cooldown_minutes,'live_trading':self.live_trading,'mode':self.mode,'allow_without_ai':self.allow_without_ai}

    def update_settings(self,d):
        for k,a in [('risk','risk'),('tp','tp'),('sl','sl'),('trailing','trailing')]:
            if k in d:
                v=float(d[k])/100
                if not 0<v<=.25: raise ValueError(f'{k} % 0-25 arasında olmalı')
                setattr(self,a,v)
        if 'max_positions' in d:self.max_positions=max(1,min(20,int(d['max_positions'])))
        if 'position_pct' in d:self.position_pct=min(.25,max(.01,float(d['position_pct'])/100))
        if 'active_capital_pct' in d:self.active_capital_pct=min(1,max(.05,float(d['active_capital_pct'])/100))
        if 'entry_score' in d:self.entry_score=max(4,min(5,int(d['entry_score'])))
        if 'scan_limit' in d:self.scan_limit=max(10,min(60,int(d['scan_limit'])))
        if 'ai_candidates' in d:self.ai_candidates=max(3,min(10,int(d['ai_candidates'])))
        if 'ai_min_score' in d:self.ai_min_score=max(50,min(100,int(d['ai_min_score'])))
        if 'min_buy_try' in d:self.min_buy_try=max(1000,float(d['min_buy_try']))
        if 'min_loss_try' in d:self.min_loss_try=max(100,float(d['min_loss_try']))
        if 'min_profit_try' in d:self.min_profit_try=max(150,float(d['min_profit_try']))

    def get_products(self):
        items=self.binance.symbols(); out=[]; stable={'USDT','USDC','FDUSD','BUSD','USDS'}
        for p in items:
            try:
                if p.get('type') != 1 or p.get('quoteAsset') != 'TRY' or not p.get('spotTradingEnable'): continue
                base=p.get('baseAsset',''); sym=p.get('symbol','')
                if not base or base in stable or not sym: continue
                filters={x.get('filterType'):x for x in p.get('filters',[])}
                out.append({'symbol':sym,'name':base,'base':base,'quote':'TRY','filters':filters})
            except Exception: continue
        # Rank TRY pairs by 24h quote volume when Binance market data exposes the ticker endpoint.
        try:
            import json as _json
            syms=[x['symbol'].replace('_','') for x in out]
            tj=self.binance.public('/api/v3/ticker/24hr', {'symbols': _json.dumps(syms, separators=(',',':'))}, market=True)
            tickers=tj if isinstance(tj,list) else tj.get('data',[])
            by={str(t.get('symbol','')):t for t in tickers if isinstance(t,dict)}
            for x in out:
                t=by.get(x['symbol'].replace('_',''),{})
                x['volume24']=float(t.get('quoteVolume') or 0)
                x['change24_ticker']=float(t.get('priceChangePercent') or 0)
                x['last_price']=float(t.get('lastPrice') or 0)
            out.sort(key=lambda x:(x.get('volume24',0), x.get('last_price',0)), reverse=True)
        except Exception:
            out.sort(key=lambda x:x['symbol'])
        return out[:self.scan_limit]

    def fetch_candles(self,symbol,limit=1000): return self.binance.candles(symbol,'5m',limit)

    def indicators(self,df):
        d=df.copy(); d['ema20']=d.close.ewm(span=20,adjust=False).mean(); d['ema50']=d.close.ewm(span=50,adjust=False).mean()
        delta=d.close.diff(); gain=delta.clip(lower=0).rolling(14).mean(); loss=(-delta.clip(upper=0)).rolling(14).mean(); rs=gain/loss.replace(0,np.nan); d['rsi']=100-(100/(1+rs))
        e12=d.close.ewm(span=12,adjust=False).mean(); e26=d.close.ewm(span=26,adjust=False).mean(); d['macd']=e12-e26; d['macds']=d.macd.ewm(span=9,adjust=False).mean(); d['v20']=d.volume.rolling(20).mean(); return d

    def snapshot(self,df):
        x=df.iloc[-1]; vr=float(x.volume/x.v20) if pd.notna(x.v20) and x.v20 else 0.0
        return {'price':float(x.close),'ema20':float(x.ema20),'ema50':float(x.ema50),'rsi':float(x.rsi) if pd.notna(x.rsi) else 50.0,'macd':float(x.macd),'macds':float(x.macds),'volume_ratio':vr,'trend':bool(x.ema20>x.ema50)}

    def technical_analyze(self,p):
        df=self.indicators(self.fetch_candles(p['symbol']))
        if len(df)<120: raise RuntimeError('yetersiz mum verisi')
        # Build 15m/1h from the same 5m feed, reducing API load and avoiding 120+ calls per scan.
        ix=pd.to_datetime(df['start'],unit='ms',utc=True); r=df.set_index(ix)
        a5=self.snapshot(df); r15=r.resample('15min').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna(); r1h=r.resample('1h').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
        a15=self.snapshot(self.indicators(r15)); a1h=self.snapshot(self.indicators(r1h))
        chg24=((a5['price']/float(df.iloc[max(0,len(df)-289)]['close']))-1)*100 if len(df)>289 else 0.0
        core=sum([a5['trend'],45<=a5['rsi']<=70,a5['macd']>a5['macds'],a5['volume_ratio']>=.8,chg24>-2])
        tf15=a15['trend'] and a15['macd']>a15['macds']; tf1h=a1h['trend'] and a1h['macd']>a1h['macds']; score=core+tf15+tf1h
        buy=a5['trend'] and a5['macd']>a5['macds'] and core>=max(4,self.entry_score) and score>=5 and (tf15 or tf1h)
        return {'symbol':p['symbol'],'name':p['name'],'price':a5['price'],'rsi':a5['rsi'],'change24':chg24,'volume_ratio':a5['volume_ratio'],'score':score,'core_score':core,'checks':{'5dk Trend':a5['trend'],'5dk RSI':45<=a5['rsi']<=70,'5dk MACD':a5['macd']>a5['macds'],'5dk Hacim':a5['volume_ratio']>=.8,'24s Momentum':chg24>-2,'15dk Onay':tf15,'1s Onay':tf1h},'decision':'AL ADAYI' if buy else 'BEKLE','tf5':a5,'tf15':a15,'tf1h':a1h,'reasons':[f"5dk trend {'uygun' if a5['trend'] else 'zayıf'}",f"RSI {a5['rsi']:.1f}",f"MACD {'pozitif' if a5['macd']>a5['macds'] else 'negatif'}",f"Hacim {a5['volume_ratio']:.2f}x",f"24s {chg24:+.2f}%",f"15dk {'onay' if tf15 else 'onay yok'}",f"1s {'onay' if tf1h else 'onay yok'}"], 'time':self.now_tr().strftime('%H:%M:%S')}

    def _parse_ai(self,text):
        try: return json.loads(text)
        except Exception:
            import re; m=re.search(r'\{.*\}',text or '',re.S)
            if not m: raise ValueError('AI geçerli JSON döndürmedi')
            return json.loads(m.group(0))

    def _ai_request(self,payload,model=None):
        if not self.groq_key: raise RuntimeError('GROQ_API_KEY yok')
        payload=dict(payload); payload['model']=model or self.ai_model
        r=requests.post(GROQ_CHAT,headers={'Authorization':'Bearer '+self.groq_key,'Content-Type':'application/json'},json=payload,timeout=45)
        if not r.ok:
            try: msg=r.json().get('error',{}).get('message',r.text)
            except Exception: msg=r.text
            raise RuntimeError(f'Groq HTTP {r.status_code}: {str(msg)[:700]}')
        self.last_groq_status='200 OK'; return r.json()

    def ai_research(self,cands):
        if not cands: return {}
        compact=[{'symbol':c['symbol'],'price':round(c['price'],6),'change24':round(c['change24'],2),'rsi':round(c['rsi'],1),'volume_ratio':round(c['volume_ratio'],2),'technical_score':c['score'],'core_score':c['core_score']} for c in cands]
        prompt=('Kripto spot teknik filtre. Web kullanma. Yalnızca verilen veriyi değerlendir. Her sembol için 0-100 score, decision AL/BEKLE, risk DÜŞÜK/ORTA/YÜKSEK ve kısa summary üret. Sadece JSON döndür. Yapı: {"reviews":[{"symbol":"BTC_TRY","score":75,"decision":"AL","risk":"ORTA","summary":"..."}]}. Tüm sembolleri döndür. Veri:\n'+json.dumps(compact,ensure_ascii=False,separators=(',',':')))
        payload={'messages':[{'role':'system','content':'Türkçe yanıt ver. Yalnızca geçerli JSON üret.'},{'role':'user','content':prompt}],'temperature':0.1,'max_completion_tokens':600,'response_format':{'type':'json_object'}}
        self.ai_batch_size=len(cands); self.last_ai_request_at=self.now_tr().strftime('%d.%m.%Y %H:%M:%S')
        try:
            data=self._ai_request(payload); content=((data.get('choices') or [{}])[0].get('message') or {}).get('content',''); obj=self._parse_ai(content); items={x.get('symbol'):x for x in obj.get('reviews',[]) if isinstance(x,dict)}; out={}
            for c in cands:
                x=items.get(c['symbol'],{}); dec=x.get('decision','BEKLE') if x.get('decision') in ('AL','BEKLE') else 'BEKLE'; risk=x.get('risk','ORTA') if x.get('risk') in ('DÜŞÜK','ORTA','YÜKSEK') else 'ORTA';
                try: sc=max(0,min(100,int(float(x.get('score',0)))))
                except: sc=0
                out[c['symbol']]={'score':sc,'decision':dec,'risk':risk,'summary':str(x.get('summary',''))[:300],'positive':[],'negative':[],'sources':[]}; self.last_ai_at[c['symbol']]=time.time()
            self.last_ai_error=None; return out
        except Exception as e:
            self.last_ai_error=str(e)[:900]; self.last_groq_status='AI hatası'
            return {c['symbol']:{'score':0,'decision':'AI HATASI','risk':'Bilinmiyor','summary':self.last_ai_error,'positive':[],'negative':[],'sources':[]} for c in cands}

    def _available_try(self):
        return self.binance.try_available() if not self.dry_run else self.balance_try

    def _buy_amount_try(self):
        available=self._available_try(); market_value=sum(float(p.get('last',p.get('entry',0)))*float(p.get('qty',0)) for p in self.positions.values()); equity=available+market_value; active_budget=equity*self.active_capital_pct; active_used=sum(float(p.get('buy_amount_try',0)) for p in self.positions.values()); remaining=max(0,active_budget-active_used); amount=min(available,equity*self.position_pct,remaining); return amount if amount>=self.min_buy_try else 0.0

    def _open(self,s):
        symbol=s['symbol'];
        if symbol in self.positions or symbol in self.pending_symbols:return False
        self._reset_daily()
        if self.trade_stats['daily_realized_pnl']<=-self.max_daily_loss_try:return False
        if time.time()<self.cooldown_until:return False
        if self.consecutive_losses>=self.max_consecutive_losses:return False
        ai=self.ai_reviews.get(symbol,{})
        if not self.allow_without_ai and (ai.get('decision')!='AL' or ai.get('score',0)<self.ai_min_score): return False
        amount=self._buy_amount_try()
        if amount<self.min_buy_try: self.last_trade_block_reason=f'Alım yok: minimum {self.min_buy_try:.0f} TL veya aktif sermaye limiti nedeniyle yeterli bakiye yok.'; return False
        self.pending_symbols.add(symbol)
        try:
            if self.dry_run:
                price=s['price']; qty=amount/max(price,1e-12); order_id='SIM-'+uuid.uuid4().hex[:12]; fee=0.0; filled=amount
            else:
                resp=self.binance.order_market_buy(symbol,amount); oid=(resp.get('data') or {}).get('orderId');
                if not oid: raise RuntimeError(str(resp))
                time.sleep(.5); order=self.binance.order_detail(order_id=oid); status=int(order.get('status',0)); qty=float(order.get('executedQty') or 0); filled=float(order.get('executedQuoteQty') or 0); price=float(order.get('executedPrice') or 0); fee=0.0
                if qty<=0 or status not in (1,2): raise RuntimeError(f'Emir dolmadı: status={status}')
                if price<=0: price=filled/qty
                order_id=str(oid)
            self.positions[symbol]={'symbol':symbol,'name':s['name'],'entry':price,'qty':qty,'buy_amount_try':filled,'last':price,'unrealized':0.0,'peak_pnl':0.0,'peak_price':price,'ai_score':ai.get('score',0),'ai_risk':ai.get('risk',''),'opened':self.now_tr().strftime('%d.%m.%Y %H:%M:%S'),'order_id':order_id,'fee_try':fee}
            self.trade_stats['buy_count']+=1; self.trade_stats['total_buy_try']+=filled; self.history.insert(0,{'type':'OPEN','symbol':symbol,'price':price,'qty':qty,'buy_try':filled,'sell_try':None,'pnl':0,'reason':f"AL • Teknik {s['score']}/7 • AI {ai.get('score',0)}/100 • {self.mode}",'time':self.now_tr().strftime('%d.%m.%Y %H:%M:%S'),'order_id':order_id}); self._save_state(); self.last_trade_block_reason=f"{'Canlı' if not self.dry_run else 'Sanal'} alım açıldı: {symbol} • {filled:.2f} TL"; return True
        finally:self.pending_symbols.discard(symbol)

    def _close(self,symbol,p,reason):
        price=float(p.get('last',p.get('entry',0))); qty=float(p.get('qty',0)); buy=float(p.get('buy_amount_try',0))
        if qty<=0: raise RuntimeError(f'{symbol}: geçersiz satış miktarı')
        if not self.dry_run:
            # Re-read balance so a dust/fee-adjusted quantity cannot create a rejected sell.
            asset=symbol.split('_')[0]; actual=self.binance.signed('GET','/open/v1/account/spot/asset',{'asset':asset}).get('data',{}); free=float(actual.get('free') or 0); qty=min(qty,free)
            f=self.binance.symbols(); meta=next((x for x in f if x.get('symbol')==symbol),{}); filt={x.get('filterType'):x for x in meta.get('filters',[])}; step=float((filt.get('LOT_SIZE') or {}).get('stepSize') or '0.00000001'); minq=float((filt.get('LOT_SIZE') or {}).get('minQty') or '0'); qty=dec_floor(qty,step)
            if qty<minq: raise RuntimeError(f'{symbol}: satış miktarı LOT_SIZE altında')
            resp=self.binance.order_market_sell(symbol,qty); oid=(resp.get('data') or {}).get('orderId');
            if not oid: raise RuntimeError(str(resp))
            time.sleep(.5); order=self.binance.order_detail(order_id=oid); sold_qty=float(order.get('executedQty') or 0); sold_value=float(order.get('executedQuoteQty') or 0); price=float(order.get('executedPrice') or price); qty=sold_qty; sell_value=sold_value
        else: sell_value=price*qty; oid='SIM-'+uuid.uuid4().hex[:12]
        pnl=sell_value-buy; self.realized_pnl+=pnl; self.trade_stats['sell_count']+=1; self.trade_stats['total_sell_try']+=sell_value; self.trade_stats['daily_realized_pnl']+=pnl
        if pnl>=0:self.trade_stats['wins']+=1; self.consecutive_losses=0
        else:self.trade_stats['losses']+=1; self.consecutive_losses+=1; self.cooldown_until=time.time()+self.loss_cooldown_minutes*60
        self.history.insert(0,{'type':'CLOSE','symbol':symbol,'price':price,'qty':qty,'buy_try':buy,'sell_try':sell_value,'pnl':pnl,'reason':reason,'time':self.now_tr().strftime('%d.%m.%Y %H:%M:%S'),'order_id':oid}); self.positions.pop(symbol,None); self._save_state()

    def manage_positions(self):
        for symbol,p in list(self.positions.items()):
            try:
                s=self.technical_analyze({'symbol':symbol,'name':p.get('name',symbol)}); p['last']=s['price']; p['peak_price']=max(float(p.get('peak_price',p['entry'])),p['last']); pnl=(p['last']*p['qty']-p['buy_amount_try']); p['unrealized']=pnl; p['peak_pnl']=max(float(p.get('peak_pnl',0)),pnl)
                if pnl<=-self.min_loss_try:self._close(symbol,p,f'ZARAR EŞİĞİ (-{self.min_loss_try:.0f} TL)')
                elif pnl>=self.min_profit_try:self._close(symbol,p,f'KÂR HEDEFİ (+{pnl:.0f} TL)')
                elif p['peak_pnl']>=self.min_profit_try and pnl<=p['peak_pnl']*(1-self.trailing):self._close(symbol,p,f'TRAILING (+{pnl:.0f} TL)')
            except Exception as e:self.last_error=f'Pozisyon yönetimi {symbol}: {type(e).__name__}: {e}'

    def tick(self,force=False):
        if not self.running or (not force and time.time()<self.next_scan_at) or not self.lock.acquire(blocking=False): return
        try:
            self.last_check=self.now_tr().strftime('%d.%m.%Y %H:%M:%S'); self.last_error=None; self.last_ai_error=None; self._reset_daily()
            if self.live_trading and not self.binance.configured: self.last_error='Binance API anahtarı eksik'; return
            products=self.get_products(); rough=[]
            # First pass: one 5m request per symbol. This is the key fix versus the old Coinbase/451 path.
            for p in products:
                try:
                    x=self.technical_analyze(p); rough.append(x)
                except Exception: continue
            rough.sort(key=lambda x:(x['score'],x['core_score'],x['change24'],x['volume_ratio']),reverse=True); results=rough[:self.scan_limit]; self.scanned_count=len(results)
            eligible=[x for x in results if x['decision']=='AL ADAYI' and x['score']>=5][:self.ai_candidates]; self.ai_reviews=self.ai_research(eligible); self.ai_review_count=len(eligible)
            for x in results:
                x['ai']=self.ai_reviews.get(x['symbol']); x['final_decision']='AL' if x.get('ai',{}).get('decision')=='AL' and x.get('ai',{}).get('score',0)>=self.ai_min_score else x['decision']
            self.signals={x['symbol']:x for x in results}; self.manage_positions()
            ranked=[x for x in results if x['decision']=='AL ADAYI' and (self.allow_without_ai or (x.get('ai',{}).get('decision')=='AL' and x.get('ai',{}).get('score',0)>=self.ai_min_score)) and x.get('ai',{}).get('risk')!='YÜKSEK']; ranked.sort(key=lambda x:(x.get('ai',{}).get('score',0),x['score'],x['change24']),reverse=True)
            if not ranked:self.last_trade_block_reason=('Alım yok: AI başarısız • '+self.last_ai_error[:250]) if self.last_ai_error else f'Alım yok: AI {self.ai_min_score}+ skorlu aday yok.'
            opened=0
            for s in ranked:
                if len(self.positions)>=self.max_positions: break
                if self._open(s): opened+=1
            if opened:self.last_trade_block_reason=f'{opened} pozisyon açıldı.'
            self._save_state(); self.last_scan_ms=int(time.time()*1000)
        finally:self.next_scan_at=time.time()+self.scan_interval_seconds; self.lock.release()

    def status(self):
        self._reset_daily()
        if self.dry_run:
            market=sum(float(p.get('last',p.get('entry',0)))*float(p.get('qty',0)) for p in self.positions.values()); cash=self.balance_try; equity=cash+market
        else:
            try: cash=self.binance.try_available(); market=sum(float(p.get('last',p.get('entry',0)))*float(p.get('qty',0)) for p in self.positions.values()); equity=cash+market
            except Exception as e: cash=market=equity=0; self.last_error=f'Canlı bakiye okunamadı: {e}'
        total_pnl=equity-self.initial_balance; closed=self.trade_stats['sell_count']; wr=self.trade_stats['wins']/closed*100 if closed else 0
        ranked=sorted(self.signals.values(),key=lambda x:(x.get('final_decision')=='AL',x.get('ai',{}).get('score',0),x.get('score',0)),reverse=True)[:self.ai_candidates]
        return {'running':self.running,'dry_run':self.dry_run,'live_trading':self.live_trading,'mode':self.mode,'balance_try':equity,'cash_try':cash,'pnl':total_pnl,'positions':list(self.positions.values()),'signals':ranked,'scanned_count':self.scanned_count,'ai_review_count':self.ai_review_count,'runtime':self.runtime(),'last_check':self.last_check,'last_error':self.last_error,'last_ai_error':self.last_ai_error,'last_groq_status':self.last_groq_status,'ai_batch_size':self.ai_batch_size,'trade_block_reason':self.last_trade_block_reason,'history':self.history[:30],'settings':self.settings(),'daily_stats':{**self.trade_stats,'win_rate':wr},'risk_guard':{'daily_loss_limit_try':self.max_daily_loss_try,'consecutive_losses':self.consecutive_losses,'max_consecutive_losses':self.max_consecutive_losses},'data_source':'Binance TR symbols + Binance market klines','entry_logic':'EMA20>EMA50 + RSI + MACD + hacim + momentum + 15dk/1s onay + AI','exit_logic':f'min zarar {self.min_loss_try:.0f} TL • min kâr {self.min_profit_try:.0f} TL • trailing'}
