import os, time, json, re, uuid, threading
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
import pandas as pd
import numpy as np
import jwt

try:
    import psycopg
except ImportError:
    psycopg = None

from cryptography.hazmat.primitives import serialization

PUBLIC_PRODUCTS = "https://api.coinbase.com/api/v3/brokerage/market/products"
PUBLIC_CANDLES = "https://api.coinbase.com/api/v3/brokerage/market/products/{product_id}/candles"
GROQ_CHAT = "https://api.groq.com/openai/v1/chat/completions"
COINBASE_BASE = "https://api.coinbase.com"
FX_URL = "https://api.frankfurter.app/latest"
TR_ZONE = ZoneInfo("Europe/Istanbul")


def money(v):
    try:
        return round(float(v), 2)
    except Exception:
        return 0.0


def dec_floor(value, increment):
    v, inc = Decimal(str(value)), Decimal(str(increment))
    if inc <= 0:
        return float(v)
    return float((v / inc).to_integral_value(rounding=ROUND_DOWN) * inc)


class CoinbaseClient:
    def __init__(self):
        self.key_name = os.getenv("COINBASE_API_KEY_NAME", "").strip()
        self.private_key = os.getenv("COINBASE_API_PRIVATE_KEY", "").strip().replace("\\n", "\n")
        self.timeout = max(5, int(os.getenv("COINBASE_HTTP_TIMEOUT", "20")))

    @property
    def configured(self):
        return bool(self.key_name and self.private_key)

    def _private_key_obj(self):
        if not self.private_key.startswith("-----BEGIN"):
            raise ValueError("COINBASE_API_PRIVATE_KEY PEM formatında değil")
        return serialization.load_pem_private_key(self.private_key.encode(), password=None)

    def _jwt(self, method, path):
        if not self.configured:
            raise RuntimeError("Coinbase API anahtarı yapılandırılmamış")
        key = self._private_key_obj()
        alg = "EdDSA" if key.__class__.__name__.startswith("Ed25519") else "ES256"
        now = int(time.time())
        claims = {
            "sub": self.key_name,
            "iss": "cdp",
            "nbf": now,
            "exp": now + 120,
            "uri": f"{method.upper()} api.coinbase.com{path}",
        }
        return jwt.encode(
            claims, key, algorithm=alg,
            headers={"kid": self.key_name, "nonce": uuid.uuid4().hex},
        )

    def request(self, method, path, params=None, body=None):
        token = self._jwt(method, path)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "YatirimBot/20",
        }
        r = requests.request(
            method, COINBASE_BASE + path, params=params, json=body,
            headers=headers, timeout=self.timeout
        )
        if not r.ok:
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise RuntimeError(f"Coinbase HTTP {r.status_code}: {str(detail)[:700]}")
        return r.json()

    def accounts(self):
        return self.request(
            "GET", "/api/v3/brokerage/accounts", params={"limit": 250}
        ).get("accounts", [])

    def usd_available(self):
        for a in self.accounts():
            if a.get("currency") == "USD" and a.get("active", True):
                return float((a.get("available_balance") or {}).get("value") or 0)
        return 0.0

    def product(self, symbol):
        return self.request("GET", f"/api/v3/brokerage/products/{symbol}")

    def preview(self, product_id, side, order_configuration):
        return self.request(
            "POST", "/api/v3/brokerage/orders/preview",
            body={
                "product_id": product_id,
                "side": side,
                "order_configuration": order_configuration,
            },
        )

    def create_market_buy(self, product_id, quote_usd):
        return self.request(
            "POST", "/api/v3/brokerage/orders",
            body={
                "client_order_id": str(uuid.uuid4()),
                "product_id": product_id,
                "side": "BUY",
                "order_configuration": {
                    "market_market_ioc": {"quote_size": f"{quote_usd:.8f}"}
                },
            },
        )

    def create_market_sell(self, product_id, base_size):
        return self.request(
            "POST", "/api/v3/brokerage/orders",
            body={
                "client_order_id": str(uuid.uuid4()),
                "product_id": product_id,
                "side": "SELL",
                "order_configuration": {
                    "market_market_ioc": {"base_size": f"{base_size:.12f}"}
                },
            },
        )

    def order(self, order_id):
        return self.request(
            "GET", f"/api/v3/brokerage/orders/historical/{order_id}"
        ).get("order", {})


class TradingBot:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = True
        self.started_at = datetime.now(timezone.utc)

        self.live_trading = os.getenv("LIVE_TRADING", "false").lower() == "true"
        self.live_confirm = (
            os.getenv("LIVE_CONFIRM", "").strip()
            == "I_UNDERSTAND_REAL_MONEY_TRADING"
        )
        self.dry_run = not (self.live_trading and self.live_confirm)
        self.mode = "LIVE" if not self.dry_run else "DRY RUN"

        self.balance_try = float(os.getenv("STARTING_TRY_BALANCE", "5000"))
        self.initial_balance = self.balance_try
        self.realized_pnl = 0.0

        self.risk = float(os.getenv("RISK_PER_TRADE", "0.01"))
        self.tp = float(os.getenv("TAKE_PROFIT", "0.025"))
        self.sl = float(os.getenv("STOP_LOSS", "0.01"))
        self.trailing = float(os.getenv("TRAILING_STOP", "0.01"))

        self.min_buy_try = max(1000.0, float(os.getenv("MIN_BUY_TRY", "1000")))
        self.min_loss_try = max(100.0, float(os.getenv("MIN_LOSS_TRY", "100")))
        self.min_profit_try = max(150.0, float(os.getenv("MIN_PROFIT_TRY", "150")))
        self.max_daily_loss_try = max(100.0, float(os.getenv("MAX_DAILY_LOSS_TRY", "200")))
        self.max_consecutive_losses = max(1, int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3")))
        self.loss_cooldown_minutes = max(5, int(os.getenv("LOSS_COOLDOWN_MINUTES", "30")))

        self.max_positions = max(1, min(20, int(os.getenv("MAX_OPEN_POSITIONS", "20"))))
        self.position_pct = min(0.25, max(0.01, float(os.getenv("POSITION_SIZE_PCT", "0.25"))))
        self.active_capital_pct = min(1.0, max(0.05, float(os.getenv("ACTIVE_CAPITAL_PCT", "0.75"))))

        self.entry_score = max(4, min(5, int(os.getenv("ENTRY_SCORE", "4"))))
        self.scan_limit = min(60, max(10, int(os.getenv("SCAN_LIMIT", "40"))))
        self.ai_candidates = min(10, max(3, int(os.getenv("AI_CANDIDATES", "8"))))
        self.ai_min_score = min(100, max(50, int(os.getenv("AI_MIN_SCORE", "70"))))

        self.ai_model = os.getenv("AI_MODEL", "llama-3.1-8b-instant").strip()
        self.ai_fallback_model = os.getenv("AI_FALLBACK_MODEL", "").strip()
        self.ai_cache_minutes = max(5, int(os.getenv("AI_CACHE_MINUTES", "15")))
        self.groq_key = os.getenv("GROQ_API_KEY", "").strip()

        self.scan_interval_seconds = max(120, int(os.getenv("SCAN_INTERVAL_SECONDS", "300")))
        self.groq_cooldown_until = 0
        self.next_scan_at = 0

        self.positions = {}
        self.history = []
        self.signals = {}
        self.ai_reviews = {}
        self.last_ai_at = {}
        self.last_sell_at = {}

        self.last_check = None
        self.last_error = None
        self.last_ai_error = None
        self.last_trade_block_reason = "Henüz tarama yapılmadı."
        self.last_groq_status = "Hazır"
        self.last_ai_request_at = None
        self.ai_batch_size = 0
        self.scanned_count = 0
        self.ai_review_count = 0
        self.last_scan_ms = 0

        self.consecutive_losses = 0
        self.cooldown_until = 0
        self.pending_symbols = set()
        self.fx_rate = None
        self.fx_at = 0

        self.trade_stats = {
            "buy_count": 0, "sell_count": 0,
            "total_buy_try": 0.0, "total_sell_try": 0.0,
            "wins": 0, "losses": 0,
            "daily_realized_pnl": 0.0,
            "day": self.now_tr_date(),
        }

        self.cb = CoinbaseClient()
        self.database_url = ""
        self._load_state()

        if self.live_trading and not self.live_confirm:
            self.last_error = (
                "LIVE_TRADING=true ama LIVE_CONFIRM eksik; "
                "güvenlik nedeniyle canlı emirler kapalı."
            )
        if self.live_trading and not self.cb.configured:
            self.last_error = (
                "Canlı mod için COINBASE_API_KEY_NAME ve "
                "COINBASE_API_PRIVATE_KEY gerekli."
            )

    def now_tr(self):
        return datetime.now(TR_ZONE)

    def now_tr_date(self):
        return self.now_tr().date().isoformat()

    def runtime(self):
        seconds = max(
            0, int((datetime.now(timezone.utc) - self.started_at).total_seconds())
        )
        d, rem = divmod(seconds, 86400)
        h, rem = divmod(rem, 3600)
        m, s = divmod(rem, 60)
        return {
            "uptime_text": f"{d} gün {h:02d} saat {m:02d} dk {s:02d} sn",
            "started_at_tr": self.started_at.astimezone(TR_ZONE).strftime("%d.%m.%Y %H:%M:%S"),
            "now_tr": self.now_tr().strftime("%d.%m.%Y %H:%M:%S"),
        }

    def _load_state(self):
        self.database_url = os.getenv("DATABASE_URL", "").strip()
        if not self.database_url or psycopg is None:
            if self.live_trading:
                self.last_error = (
                    "Canlı mod için DATABASE_URL gerekli. "
                    "Render PostgreSQL bağlanmadan canlı işlem açılmayacak."
                )
            return
        try:
            with psycopg.connect(self.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "CREATE TABLE IF NOT EXISTS bot_state "
                        "(id INTEGER PRIMARY KEY, payload JSONB NOT NULL, "
                        "updated_at TIMESTAMPTZ DEFAULT NOW())"
                    )
                    cur.execute("SELECT payload FROM bot_state WHERE id=1")
                    row = cur.fetchone()
                    if not row:
                        return
                    data = row[0] or {}
                    self.positions = data.get("positions", {})
                    self.history = data.get("history", [])
                    self.trade_stats.update(data.get("trade_stats", {}))
                    self.realized_pnl = float(data.get("realized_pnl", 0))
                    self.consecutive_losses = int(data.get("consecutive_losses", 0))
                    self.cooldown_until = float(data.get("cooldown_until", 0))
                    self.last_sell_at = data.get("last_sell_at", {})
                    self.initial_balance = float(
                        data.get("initial_balance", self.initial_balance)
                    )
        except Exception as e:
            self.last_error = f"Veritabanı yükleme hatası: {type(e).__name__}: {e}"
            if self.live_trading:
                self.last_error += " • Canlı işlem güvenlik nedeniyle kapalı."

    def _save_state(self):
        if not self.database_url or psycopg is None:
            return
        payload = {
            "positions": self.positions,
            "history": self.history[:100],
            "trade_stats": self.trade_stats,
            "realized_pnl": self.realized_pnl,
            "consecutive_losses": self.consecutive_losses,
            "cooldown_until": self.cooldown_until,
            "last_sell_at": self.last_sell_at,
            "initial_balance": self.initial_balance,
        }
        try:
            with psycopg.connect(self.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "CREATE TABLE IF NOT EXISTS bot_state "
                        "(id INTEGER PRIMARY KEY, payload JSONB NOT NULL, "
                        "updated_at TIMESTAMPTZ DEFAULT NOW())"
                    )
                    cur.execute(
                        "INSERT INTO bot_state (id,payload) VALUES (1,%s) "
                        "ON CONFLICT (id) DO UPDATE SET "
                        "payload=EXCLUDED.payload, updated_at=NOW()",
                        (json.dumps(payload, ensure_ascii=False),),
                    )
        except Exception as e:
            self.last_error = f"Veritabanı kayıt hatası: {type(e).__name__}: {e}"

    def _reset_daily_stats(self):
        today = self.now_tr_date()
        if self.trade_stats.get("day") != today:
            self.trade_stats.update({
                "buy_count": 0, "sell_count": 0,
                "total_buy_try": 0.0, "total_sell_try": 0.0,
                "wins": 0, "losses": 0,
                "daily_realized_pnl": 0.0, "day": today,
            })
            self.consecutive_losses = 0

    def settings(self):
        return {
            "risk": self.risk, "tp": self.tp, "sl": self.sl,
            "trailing": self.trailing,
            "max_positions": self.max_positions,
            "position_pct": self.position_pct,
            "active_capital_pct": self.active_capital_pct,
            "entry_score": self.entry_score,
            "scan_limit": self.scan_limit,
            "min_buy_try": self.min_buy_try,
            "min_loss_try": self.min_loss_try,
            "min_profit_try": self.min_profit_try,
            "ai_candidates": self.ai_candidates,
            "ai_min_score": self.ai_min_score,
            "ai_model": self.ai_model,
            "ai_provider": "Groq JSON mode • web kapalı",
            "ai_cache_minutes": self.ai_cache_minutes,
            "scan_interval_seconds": self.scan_interval_seconds,
            "max_daily_loss_try": self.max_daily_loss_try,
            "max_consecutive_losses": self.max_consecutive_losses,
            "loss_cooldown_minutes": self.loss_cooldown_minutes,
            "live_trading": self.live_trading,
            "mode": self.mode,
        }

    def update_settings(self, d):
        for k, attr in [("risk", "risk"), ("tp", "tp"), ("sl", "sl"), ("trailing", "trailing")]:
            if k in d:
                v = float(d[k]) / 100
                if not 0 < v <= 0.25:
                    raise ValueError(f"{k} % 0-25 arasında olmalı")
                setattr(self, attr, v)
        if "max_positions" in d:
            self.max_positions = max(1, min(20, int(d["max_positions"])))
        if "position_pct" in d:
            self.position_pct = min(.25, max(.01, float(d["position_pct"]) / 100))
        if "active_capital_pct" in d:
            self.active_capital_pct = min(1, max(.05, float(d["active_capital_pct"]) / 100))
        if "entry_score" in d:
            self.entry_score = max(4, min(5, int(d["entry_score"])))
        if "scan_limit" in d:
            self.scan_limit = max(10, min(60, int(d["scan_limit"])))
        if "ai_candidates" in d:
            self.ai_candidates = max(3, min(10, int(d["ai_candidates"])))
        if "ai_min_score" in d:
            self.ai_min_score = max(50, min(100, int(d["ai_min_score"])))
        if "min_buy_try" in d:
            self.min_buy_try = max(1000, float(d["min_buy_try"]))
        if "min_loss_try" in d:
            self.min_loss_try = max(100, float(d["min_loss_try"]))
        if "min_profit_try" in d:
            self.min_profit_try = max(150, float(d["min_profit_try"]))

    def get_fx(self):
        if self.fx_rate and time.time() - self.fx_at < 1800:
            return self.fx_rate
        fixed = os.getenv("USDTRY_RATE", "").strip()
        if fixed:
            self.fx_rate = float(fixed)
            self.fx_at = time.time()
            return self.fx_rate
        r = requests.get(FX_URL, params={"from": "USD", "to": "TRY"}, timeout=10)
        r.raise_for_status()
        self.fx_rate = float(r.json()["rates"]["TRY"])
        self.fx_at = time.time()
        return self.fx_rate

    def get_products(self):
        r = requests.get(
            PUBLIC_PRODUCTS,
            params={
                "limit": 100,
                "product_type": "SPOT",
                "get_tradability_status": "true",
            },
            timeout=20,
            headers={"User-Agent": "YatirimBot/20"},
        )
        r.raise_for_status()
        products = r.json().get("products", [])
        stable = {
            "USDC","USDT","DAI","PYUSD","USDS","USDG","EURC",
            "GUSD","TUSD","USDP","FDUSD","BUSD"
        }
        out = []
        for p in products:
            pid = p.get("product_id", "")
            base = p.get("base_currency_id", "")
            if (
                not pid.endswith("-USD") or not base or base in stable
                or p.get("is_disabled") or p.get("trading_disabled")
                or p.get("view_only")
            ):
                continue
            try:
                vol = float(p.get("volume_24h") or p.get("approximate_quote_24h_volume") or 0)
                chg = float(str(p.get("price_percentage_change_24h", "0")).replace("%", ""))
                price = float(p.get("price") or p.get("mid_market_price") or 0)
            except Exception:
                continue
            if price <= 0:
                continue
            out.append({
                "symbol": pid, "volume24": vol, "change24": chg,
                "price": price, "name": p.get("base_name") or base,
            })
        out.sort(key=lambda x: x["volume24"], reverse=True)
        return out[:self.scan_limit]

    def fetch_candles(self, symbol, granularity="FIVE_MINUTE", minutes=5, limit=200):
        end = int(time.time())
        start = end - (minutes * limit * 60)
        r = requests.get(
            PUBLIC_CANDLES.format(product_id=symbol),
            params={
                "start": str(start), "end": str(end),
                "granularity": granularity, "limit": min(350, limit),
            },
            timeout=18,
            headers={"User-Agent": "YatirimBot/20"},
        )
        r.raise_for_status()
        rows = r.json().get("candles", [])
        if len(rows) < 60:
            raise RuntimeError(f"{symbol}: yetersiz {granularity} veri")
        df = pd.DataFrame(rows)
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["start"] = pd.to_numeric(df["start"], errors="coerce")
        return df.dropna().sort_values("start").reset_index(drop=True).tail(limit)

    def calc_indicators(self, df):
        df = df.copy()
        df["ema20"] = df.close.ewm(span=20, adjust=False).mean()
        df["ema50"] = df.close.ewm(span=50, adjust=False).mean()
        delta = df.close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        df["rsi"] = 100 - (100 / (1 + rs))
        e12 = df.close.ewm(span=12, adjust=False).mean()
        e26 = df.close.ewm(span=26, adjust=False).mean()
        df["macd"] = e12 - e26
        df["macds"] = df.macd.ewm(span=9, adjust=False).mean()
        df["v20"] = df.volume.rolling(20).mean()
        return df

    def timeframe_snapshot(self, symbol, granularity, minutes):
        df = self.calc_indicators(
            self.fetch_candles(symbol, granularity, minutes)
        )
        x = df.iloc[-1]
        volume_ratio = float(x.volume / x.v20) if pd.notna(x.v20) and x.v20 else 0.0
        return {
            "price": float(x.close),
            "ema20": float(x.ema20),
            "ema50": float(x.ema50),
            "rsi": float(x.rsi) if pd.notna(x.rsi) else 50.0,
            "macd": float(x.macd),
            "macds": float(x.macds),
            "volume_ratio": volume_ratio,
            "trend": bool(x.ema20 > x.ema50),
        }

    def technical_analyze(self, product):
        symbol = product["symbol"]
        tf5 = self.timeframe_snapshot(symbol, "FIVE_MINUTE", 5)
        tf15 = self.timeframe_snapshot(symbol, "FIFTEEN_MINUTE", 15)
        tf1h = self.timeframe_snapshot(symbol, "ONE_HOUR", 60)

        rsi_ok = 45 <= tf5["rsi"] <= 72
        trend_ok = tf5["trend"]
        macd_ok = tf5["macd"] > tf5["macds"]
        volume_ok = tf5["volume_ratio"] >= .80
        momentum_ok = product["change24"] > -2
        tf15_ok = tf15["trend"] and tf15["macd"] > tf15["macds"]
        tf1h_ok = tf1h["trend"] and tf1h["macd"] > tf1h["macds"]

        checks = {
            "5dk Trend": trend_ok, "5dk RSI": rsi_ok, "5dk MACD": macd_ok,
            "5dk Hacim": volume_ok, "24s Momentum": momentum_ok,
            "15dk Onay": tf15_ok, "1s Onay": tf1h_ok,
        }
        core = sum([trend_ok, rsi_ok, macd_ok, volume_ok, momentum_ok])
        score = core + sum([tf15_ok, tf1h_ok])
        buy = (
            trend_ok and macd_ok
            and core >= max(4, self.entry_score)
            and score >= 5
            and (tf15_ok or tf1h_ok)
        )

        return {
            "symbol": symbol, "name": product["name"], "price": tf5["price"],
            "rsi": tf5["rsi"], "change24": product["change24"],
            "volume_ratio": tf5["volume_ratio"], "score": score,
            "core_score": core, "checks": checks,
            "decision": "AL ADAYI" if buy else "BEKLE",
            "tf5": tf5, "tf15": tf15, "tf1h": tf1h,
            "reasons": [
                f"5dk trend {'uygun' if trend_ok else 'zayıf'}",
                f"5dk RSI {tf5['rsi']:.1f}",
                f"5dk MACD {'pozitif' if macd_ok else 'negatif'}",
                f"5dk hacim {tf5['volume_ratio']:.2f}x",
                f"24s momentum {product['change24']:+.2f}%",
                f"15dk {'onay' if tf15_ok else 'onay yok'}",
                f"1s {'onay' if tf1h_ok else 'onay yok'}",
            ],
            "time": self.now_tr().strftime("%H:%M:%S"),
        }

    def _compact_ai_result(self, parsed):
        try:
            score = max(0, min(100, int(float(parsed.get("score", 0)))))
        except Exception:
            score = 0
        decision = parsed.get("decision", "BEKLE")
        risk = parsed.get("risk", "ORTA")
        if decision not in ("AL", "BEKLE"):
            decision = "BEKLE"
        if risk not in ("DÜŞÜK", "ORTA", "YÜKSEK"):
            risk = "ORTA"
        return {
            "score": score, "decision": decision, "risk": risk,
            "summary": str(parsed.get("summary", ""))[:400],
            "positive": [], "negative": [], "sources": [],
        }

    def _parse_ai_json(self, text):
        text = (text or "").strip()
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            m = re.search(r"\{.*\}", text, re.S)
            if not m:
                raise ValueError("Groq geçerli JSON döndürmedi")
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else {}

    def _ai_request(self, payload, model=None):
        if time.time() < self.groq_cooldown_until:
            raise RuntimeError(
                f"Groq cooldown: {int(self.groq_cooldown_until-time.time())} sn"
            )
        if not self.groq_key:
            raise RuntimeError("GROQ_API_KEY yok")
        payload = dict(payload)
        payload["model"] = model or self.ai_model
        self.last_ai_request_at = self.now_tr().strftime("%d.%m.%Y %H:%M:%S")

        r = requests.post(
            GROQ_CHAT,
            headers={
                "Authorization": f"Bearer {self.groq_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=45,
        )
        if not r.ok:
            try:
                detail = r.json().get("error", {}).get("message", r.text)
            except Exception:
                detail = r.text
            if r.status_code == 429:
                self.groq_cooldown_until = time.time() + 60
            raise RuntimeError(f"Groq HTTP {r.status_code}: {str(detail)[:700]}")
        self.last_groq_status = "200 OK"
        return r.json()

    def ai_research(self, candidates):
        if not candidates:
            self.ai_batch_size = 0
            return {}

        if not self.groq_key:
            self.last_ai_error = "GROQ_API_KEY yok"
            return {
                c["symbol"]: {
                    "score": 0, "decision": "AI HATASI",
                    "risk": "Bilinmiyor", "summary": "GROQ_API_KEY yok.",
                    "positive": [], "negative": [], "sources": [],
                }
                for c in candidates
            }

        self.ai_batch_size = len(candidates)
        compact = [
            {
                "symbol": c["symbol"],
                "price": round(c["price"], 8),
                "change24": round(c["change24"], 2),
                "rsi": round(c["rsi"], 1),
                "volume_ratio": round(c["volume_ratio"], 2),
                "technical_score": c["score"],
                "core_score": c["core_score"],
            }
            for c in candidates
        ]

        # Intentionally use the simple JSON-object mode. The previous code used
        # a strict JSON schema plus reasoning-only parameters, which can produce
        # Groq HTTP 400 on models/endpoints that do not accept that combination.
        prompt = (
            "Kripto spot AL filtresi. Web kullanma. Sadece verilen teknik veriyi "
            "değerlendir. Her coin için score 0-100, decision AL veya BEKLE, "
            "risk DÜŞÜK/ORTA/YÜKSEK ve kısa summary üret. "
            "Sadece JSON döndür. Şu yapıyı kullan: "
            '{"reviews":[{"symbol":"BTC-USD","score":75,'
            '"decision":"AL","risk":"ORTA","summary":"..."}]}. '
            "Listedeki tüm semboller için bir kayıt döndür. Veri:\n"
            + json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        )

        payload = {
            "messages": [
                {"role": "system", "content": "Türkçe yanıt ver. Yalnızca geçerli JSON üret."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_completion_tokens": 700,
            "response_format": {"type": "json_object"},
        }

        try:
            data = self._ai_request(payload)
            message = ((data.get("choices") or [{}])[0].get("message") or {})
            parsed = self._parse_ai_json(message.get("content", ""))
            items = {
                x.get("symbol"): x
                for x in parsed.get("reviews", [])
                if isinstance(x, dict) and x.get("symbol")
            }

            out = {}
            for c in candidates:
                result = self._compact_ai_result(
                    items.get(
                        c["symbol"],
                        {
                            "score": 0, "decision": "BEKLE",
                            "risk": "ORTA",
                            "summary": "AI aday için karar döndürmedi.",
                        },
                    )
                )
                out[c["symbol"]] = result
                self.ai_reviews[c["symbol"]] = result
                self.last_ai_at[c["symbol"]] = time.time()

            self.last_ai_error = None
            return out

        except Exception as first_error:
            # Optional fallback model, but only if the user configured one.
            if self.ai_fallback_model and self.ai_fallback_model != self.ai_model:
                try:
                    data = self._ai_request(payload, model=self.ai_fallback_model)
                    message = ((data.get("choices") or [{}])[0].get("message") or {})
                    parsed = self._parse_ai_json(message.get("content", ""))
                    items = {
                        x.get("symbol"): x
                        for x in parsed.get("reviews", [])
                        if isinstance(x, dict) and x.get("symbol")
                    }
                    out = {}
                    for c in candidates:
                        result = self._compact_ai_result(
                            items.get(
                                c["symbol"],
                                {"score": 0, "decision": "BEKLE", "risk": "ORTA",
                                 "summary": "AI aday için karar döndürmedi."},
                            )
                        )
                        out[c["symbol"]] = result
                        self.ai_reviews[c["symbol"]] = result
                        self.last_ai_at[c["symbol"]] = time.time()
                    self.last_ai_error = None
                    self.last_groq_status = "200 OK • fallback"
                    return out
                except Exception as second_error:
                    self.last_ai_error = (
                        f"İlk AI: {first_error}; fallback: {second_error}"
                    )[:900]
            else:
                self.last_ai_error = str(first_error)[:900]

            self.last_groq_status = "AI hatası"
            return {
                c["symbol"]: {
                    "score": 0, "decision": "AI HATASI",
                    "risk": "Bilinmiyor",
                    "summary": f"AI araştırması başarısız: {self.last_ai_error}",
                    "positive": [], "negative": [], "sources": [],
                }
                for c in candidates
            }

    def _can_live_trade(self):
        if self.dry_run:
            return False, "DRY RUN"
        if not self.live_trading or not self.live_confirm:
            return False, "Canlı işlem güvenlik onayı eksik"
        if not self.cb.configured:
            return False, "Coinbase API anahtarı eksik"
        if not self.database_url:
            return False, "DATABASE_URL eksik"
        return True, ""

    def _buy_amount_usd(self):
        fx = self.get_fx()
        min_usd = self.min_buy_try / fx
        available = (
            self.cb.usd_available()
            if not self.dry_run
            else self.balance_try / fx
        )
        market_value = sum(
            float(p.get("last", p.get("entry", 0))) * float(p.get("qty", 0))
            for p in self.positions.values()
        )
        equity = available + market_value
        active_budget = equity * self.active_capital_pct
        active_used = sum(float(p.get("buy_usd", 0)) for p in self.positions.values())
        remaining = max(0, active_budget - active_used)
        amount = min(available, equity * self.position_pct, remaining)
        return amount if amount + 1e-9 >= min_usd else 0.0

    def _open(self, s):
        symbol = s["symbol"]
        if symbol in self.positions or symbol in self.pending_symbols:
            return False

        self._reset_daily_stats()
        if self.trade_stats["daily_realized_pnl"] <= -self.max_daily_loss_try:
            self.last_trade_block_reason = "Günlük zarar limiti doldu."
            return False
        if time.time() < self.cooldown_until:
            self.last_trade_block_reason = "Zarar sonrası cooldown aktif."
            return False
        if self.consecutive_losses >= self.max_consecutive_losses:
            self.last_trade_block_reason = "Arka arkaya zarar limiti doldu."
            return False

        ai = self.ai_reviews.get(symbol, {})
        if ai.get("decision") != "AL" or ai.get("score", 0) < self.ai_min_score:
            return False

        amount_usd = self._buy_amount_usd()
        if amount_usd <= 0:
            self.last_trade_block_reason = (
                f"Alım yapılamadı: minimum {self.min_buy_try:.0f} TL "
                "veya aktif sermaye limiti nedeniyle yeterli bakiye yok."
            )
            return False

        fx = self.get_fx()
        product = self.cb.product(symbol) if not self.dry_run else None

        if product:
            qinc = float(product.get("quote_increment") or "0.01")
            qmin = float(product.get("quote_min_size") or "0")
            amount_usd = dec_floor(amount_usd, qinc)
            if amount_usd < qmin:
                self.last_trade_block_reason = f"{symbol}: Coinbase minimum emir tutarı altında."
                return False
            prev = self.cb.preview(
                symbol, "BUY",
                {"market_market_ioc": {"quote_size": f"{amount_usd:.8f}"}},
            )
            if prev.get("success") is False or prev.get("error_response"):
                raise RuntimeError(
                    f"BUY preview reddetti: {prev.get('error_response') or prev}"
                )

        self.pending_symbols.add(symbol)
        try:
            if self.dry_run:
                price = s["price"]
                qty = amount_usd / max(price, 1e-12)
                filled_value = amount_usd
                fee = 0.0
                order_id = "SIM-" + uuid.uuid4().hex[:12]
            else:
                resp = self.cb.create_market_buy(symbol, amount_usd)
                order_id = (resp.get("success_response") or {}).get("order_id")
                if not resp.get("success") or not order_id:
                    raise RuntimeError(str(resp.get("error_response") or resp))

                order = {}
                for _ in range(15):
                    time.sleep(1)
                    order = self.cb.order(order_id)
                    if str(order.get("status", "")).upper() in {
                        "FILLED", "CANCELLED", "FAILED", "REJECTED"
                    }:
                        break

                qty = float(order.get("filled_size") or 0)
                filled_value = float(order.get("filled_value") or 0)
                fee = float(order.get("total_fees") or order.get("fee") or 0)
                price = filled_value / qty if qty > 0 else s["price"]
                if qty <= 0:
                    raise RuntimeError(
                        f"Emir dolmadı: {order.get('status')} "
                        f"{order.get('reject_message', '')}"
                    )
                amount_usd = filled_value + fee

            buy_try = amount_usd * fx
            self.positions[symbol] = {
                "symbol": symbol, "name": s["name"], "entry": price,
                "qty": qty, "buy_usd": amount_usd,
                "buy_amount_try": buy_try, "last": price,
                "unrealized": 0.0, "peak_pnl": 0.0,
                "peak_price": price,
                "ai_score": ai.get("score", 0),
                "ai_risk": ai.get("risk", ""),
                "opened": self.now_tr().strftime("%d.%m.%Y %H:%M:%S"),
                "order_id": order_id, "fee_usd": fee, "fx_rate": fx,
            }
            self.trade_stats["buy_count"] += 1
            self.trade_stats["total_buy_try"] += buy_try
            self.history.insert(0, {
                "type": "OPEN", "symbol": symbol, "price": price, "qty": qty,
                "buy_try": buy_try, "sell_try": None, "pnl": 0,
                "reason": f"AL • Teknik {s['score']}/7 • AI {ai.get('score',0)}/100 • {self.mode}",
                "time": self.now_tr().strftime("%d.%m.%Y %H:%M:%S"),
                "order_id": order_id,
            })
            self.last_trade_block_reason = (
                f"{'Canlı' if not self.dry_run else 'Sanal'} alım açıldı: "
                f"{symbol} • {buy_try:.2f} TL"
            )
            self._save_state()
            return True
        finally:
            self.pending_symbols.discard(symbol)

    def _close(self, symbol, p, reason):
        # FIX: the old version referenced an undefined variable `s` here.
        # That caused every sell path to fail with NameError and left positions open.
        fx = self.get_fx()
        price = float(p.get("last", p.get("entry", 0)))
        qty = float(p.get("qty", 0))

        if qty <= 0:
            raise RuntimeError(f"{symbol}: geçersiz satış miktarı")

        if not self.dry_run:
            product = self.cb.product(symbol)
            inc = float(product.get("base_increment") or "0.00000001")
            minsize = float(product.get("base_min_size") or "0")
            qty = dec_floor(qty, inc)
            if qty < minsize:
                raise RuntimeError(
                    f"{symbol}: satış miktarı base_min_size altında"
                )

            preview = self.cb.preview(
                symbol, "SELL",
                {"market_market_ioc": {"base_size": f"{qty:.12f}"}},
            )
            if preview.get("success") is False or preview.get("error_response"):
                raise RuntimeError(
                    f"SELL preview reddetti: "
                    f"{preview.get('error_response') or preview}"
                )

            resp = self.cb.create_market_sell(symbol, qty)
            oid = (resp.get("success_response") or {}).get("order_id")
            if not resp.get("success") or not oid:
                raise RuntimeError(str(resp.get("error_response") or resp))

            order = {}
            for _ in range(15):
                time.sleep(1)
                order = self.cb.order(oid)
                if str(order.get("status", "")).upper() in {
                    "FILLED", "CANCELLED", "FAILED", "REJECTED"
                }:
                    break

            filled = float(order.get("filled_size") or 0)
            value = float(order.get("filled_value") or 0)
            fee = float(order.get("total_fees") or order.get("fee") or 0)
            if filled <= 0:
                raise RuntimeError(
                    f"Satış dolmadı: {order.get('status')} "
                    f"{order.get('reject_message', '')}"
                )
            sell_usd = max(0.0, value - fee)
            sell_try = sell_usd * fx
            qty = filled
        else:
            sell_usd = price * qty
            sell_try = sell_usd * fx
            fee = 0.0
            oid = "SIM-" + uuid.uuid4().hex[:12]

        pnl = sell_try - float(p["buy_amount_try"])
        self.trade_stats["sell_count"] += 1
        self.trade_stats["total_sell_try"] += sell_try
        self.trade_stats["daily_realized_pnl"] += pnl
        self.realized_pnl += pnl

        if pnl > 0:
            self.trade_stats["wins"] += 1
            self.consecutive_losses = 0
        elif pnl < 0:
            self.trade_stats["losses"] += 1
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.max_consecutive_losses:
                self.cooldown_until = time.time() + self.loss_cooldown_minutes * 60

        self.history.insert(0, {
            "type": "CLOSE", "symbol": symbol, "price": price, "qty": qty,
            "buy_try": p["buy_amount_try"], "sell_try": sell_try,
            "pnl": pnl, "reason": reason,
            "time": self.now_tr().strftime("%d.%m.%Y %H:%M:%S"),
            "order_id": oid, "fee_usd": fee,
        })
        self.last_sell_at[symbol] = time.time()
        del self.positions[symbol]
        self._save_state()

    def manage_positions(self):
        for symbol, p in list(self.positions.items()):
            try:
                s = self.technical_analyze({
                    "symbol": symbol,
                    "name": p.get("name", symbol),
                    "change24": 0,
                })
                p["last"] = s["price"]
                p["peak_price"] = max(
                    float(p.get("peak_price", p["entry"])), p["last"]
                )

                value = p["last"] * p["qty"]
                pnl = (value - p["buy_usd"]) * self.get_fx()
                p["unrealized"] = pnl
                p["peak_pnl"] = max(float(p.get("peak_pnl", 0)), pnl)

                loss = pnl <= -self.min_loss_try
                profit = pnl >= self.min_profit_try
                trailing = False
                if p["peak_pnl"] >= self.min_profit_try:
                    trailing = pnl <= p["peak_pnl"] * (1 - self.trailing)

                if loss:
                    self._close(
                        symbol, p,
                        f"ZARAR EŞİĞİ (-{self.min_loss_try:.0f} TL)"
                    )
                elif profit or trailing:
                    self._close(
                        symbol, p,
                        f"KÂR/TRAILING (+{pnl:.0f} TL)"
                    )
            except Exception as e:
                self.last_error = (
                    f"Pozisyon yönetimi {symbol}: {type(e).__name__}: {e}"
                )

    def tick(self, force=False):
        if not self.running:
            return
        if not force and time.time() < self.next_scan_at:
            return
        if not self.lock.acquire(blocking=False):
            return

        try:
            self.last_check = self.now_tr().strftime("%d.%m.%Y %H:%M:%S")
            self.last_error = None
            self._reset_daily_stats()

            if self.live_trading and not self.cb.configured:
                self.last_error = "Coinbase API anahtarı eksik"
                return

            try:
                products = self.get_products()
            except Exception as e:
                self.last_error = (
                    f"Coin listesi alınamadı: {type(e).__name__}: {e}"
                )
                return

            results = []
            for product in products:
                try:
                    results.append(self.technical_analyze(product))
                except Exception:
                    continue

            results.sort(
                key=lambda x: (
                    x["score"], x["core_score"],
                    x["change24"], x["volume_ratio"]
                ),
                reverse=True,
            )
            self.scanned_count = len(results)

            eligible = [
                x for x in results
                if x["score"] >= 4 and x["core_score"] >= 3
            ][:self.ai_candidates]

            fresh_before = dict(self.last_ai_at)
            self.ai_reviews = self.ai_research(eligible)
            self.ai_review_count = sum(
                1 for c in eligible
                if self.last_ai_at.get(c["symbol"], 0)
                != fresh_before.get(c["symbol"], 0)
            )

            for x in results:
                x["ai"] = self.ai_reviews.get(x["symbol"])
                x["final_decision"] = (
                    "AL"
                    if x.get("ai", {}).get("decision") == "AL"
                    and x.get("ai", {}).get("score", 0) >= self.ai_min_score
                    else x["decision"]
                )

            self.signals = {x["symbol"]: x for x in results}

            # Always manage existing positions even if AI fails.
            self.manage_positions()

            ranked = [
                x for x in results
                if x["decision"] == "AL ADAYI"
                and x.get("ai", {}).get("decision") == "AL"
                and x.get("ai", {}).get("score", 0) >= self.ai_min_score
                and x.get("ai", {}).get("risk") != "YÜKSEK"
            ]
            ranked.sort(
                key=lambda x: (
                    x["ai"]["score"], x["score"],
                    x["change24"]
                ),
                reverse=True,
            )

            if not ranked:
                if self.last_ai_error:
                    self.last_trade_block_reason = (
                        f"Alım yok: AI başarısız • {self.last_ai_error[:250]}"
                    )
                else:
                    self.last_trade_block_reason = (
                        f"Alım yok: AI {self.ai_min_score}+ "
                        "skorlu güvenli AL adayı yok."
                    )

            opened = 0
            for s in ranked:
                if len(self.positions) >= self.max_positions:
                    break
                if self._open(s):
                    opened += 1

            if opened:
                self.last_trade_block_reason = (
                    f"{opened} {'canlı' if not self.dry_run else 'sanal'} "
                    "pozisyon açıldı."
                )

            self._save_state()
            self.last_scan_ms = int(time.time() * 1000)

        finally:
            self.next_scan_at = time.time() + self.scan_interval_seconds
            self.lock.release()

    def status(self):
        self._reset_daily_stats()

        if self.dry_run:
            market_value = sum(
                float(p.get("last", p.get("entry", 0))) * float(p.get("qty", 0))
                for p in self.positions.values()
            )
            equity = self.balance_try + market_value
            cash = self.balance_try
        else:
            try:
                cash = self.cb.usd_available() * self.get_fx()
                market_value = sum(
                    float(p.get("last", p.get("entry", 0)))
                    * float(p.get("qty", 0))
                    * self.get_fx()
                    for p in self.positions.values()
                )
                equity = cash + market_value
            except Exception as e:
                cash = market_value = equity = 0
                self.last_error = f"Canlı bakiye okunamadı: {e}"

        total_pnl = equity - self.initial_balance
        closed = self.trade_stats["sell_count"]
        win_rate = self.trade_stats["wins"] / closed * 100 if closed else 0

        ranked = sorted(
            self.signals.values(),
            key=lambda x: (
                x.get("final_decision") == "AL",
                x.get("ai", {}).get("score", 0),
                x.get("score", 0),
                x.get("change24", 0),
            ),
            reverse=True,
        )

        return {
            "running": self.running,
            "dry_run": self.dry_run,
            "live_trading": self.live_trading,
            "mode": self.mode,
            "cash_try": money(cash),
            "balance_try": money(equity),
            "market_value_try": money(market_value),
            "pnl": money(total_pnl),
            "realized_pnl": money(self.realized_pnl),
            "positions": list(self.positions.values()),
            "signals": ranked[:25],
            "scanned_count": self.scanned_count,
            "ai_review_count": self.ai_review_count,
            "last_check": self.last_check,
            "last_error": self.last_error,
            "trade_block_reason": self.last_trade_block_reason,
            "last_ai_error": self.last_ai_error,
            "groq_status": self.last_groq_status,
            "groq_cooldown_seconds": max(
                0, int(self.groq_cooldown_until - time.time())
            ),
            "last_ai_request_at": self.last_ai_request_at,
            "ai_batch_size": self.ai_batch_size,
            "scan_interval_seconds": self.scan_interval_seconds,
            "settings": self.settings(),
            "data_source": (
                "Coinbase public market data + "
                "Coinbase Advanced Trade (canlı modda)"
            ),
            "entry_logic": (
                "Teknik filtre + Groq AI + risk motoru + "
                "gerçek bakiye kontrolü"
            ),
            "exit_logic": (
                f"Min alış {self.min_buy_try:.0f} TL • "
                f"Min zarar -{self.min_loss_try:.0f} TL • "
                f"Min kâr +{self.min_profit_try:.0f} TL • "
                f"trailing {self.trailing*100:.1f}%"
            ),
            "runtime": self.runtime(),
            "daily_stats": {
                "day": self.trade_stats["day"],
                "daily_pnl": money(self.trade_stats["daily_realized_pnl"]),
                "total_buy_try": money(self.trade_stats["total_buy_try"]),
                "total_sell_try": money(self.trade_stats["total_sell_try"]),
                "wins": self.trade_stats["wins"],
                "losses": self.trade_stats["losses"],
                "win_rate": round(win_rate, 1),
            },
            "risk_guard": {
                "daily_loss_limit_try": self.max_daily_loss_try,
                "consecutive_losses": self.consecutive_losses,
                "max_consecutive_losses": self.max_consecutive_losses,
                "cooldown_seconds": max(
                    0, int(self.cooldown_until - time.time())
                ),
            },
            "history": self.history[:30],
        }
