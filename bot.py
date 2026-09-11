
import os, time, json, re, requests, pandas as pd, numpy as np
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PUBLIC_PRODUCTS = "https://api.coinbase.com/api/v3/brokerage/market/products"
PUBLIC_CANDLES = "https://api.coinbase.com/api/v3/brokerage/market/products/{product_id}/candles"
GROQ_CHAT = "https://api.groq.com/openai/v1/chat/completions"

class TradingBot:
    def __init__(self):
        self.running = True
        self.dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
        self.started_at = datetime.now(timezone.utc)

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

        self.max_positions = int(os.getenv("MAX_OPEN_POSITIONS", "20"))
        self.position_pct = min(0.25, float(os.getenv("POSITION_SIZE_PCT", "0.25")))
        self.active_capital_pct = min(1.0, max(0.05, float(os.getenv("ACTIVE_CAPITAL_PCT", "0.75"))))

        self.entry_score = max(4, int(os.getenv("ENTRY_SCORE", "4")))
        self.scan_limit = min(60, max(10, int(os.getenv("SCAN_LIMIT", "40"))))
        self.ai_candidates = min(10, max(3, int(os.getenv("AI_CANDIDATES", "5"))))
        self.ai_min_score = min(100, max(50, int(os.getenv("AI_MIN_SCORE", "70"))))
        self.ai_model = os.getenv("AI_MODEL", "groq/compound-mini")
        self.ai_fallback_model = os.getenv("AI_FALLBACK_MODEL", "openai/gpt-oss-20b")
        self.ai_cache_minutes = max(5, int(os.getenv("AI_CACHE_MINUTES", "15")))
        self.last_ai_at = {}
        self.allow_without_ai = os.getenv("ALLOW_WITHOUT_AI", "false").lower() == "true"
        self.groq_key = os.getenv("GROQ_API_KEY", "").strip()

        self.positions = {}
        self.history = []
        self.signals = {}
        self.ai_reviews = {}
        self.last_check = None
        self.last_error = None
        self.scanned_count = 0
        self.ai_review_count = 0
        self.data_source = "Coinbase public market data"
        self.last_scan_ms = 0
        self.last_trade_block_reason = "Henüz tarama yapılmadı."

        self.trade_stats = {
            "buy_count": 0,
            "sell_count": 0,
            "total_buy_try": 0.0,
            "total_sell_try": 0.0,
            "wins": 0,
            "losses": 0,
            "daily_realized_pnl": 0.0,
            "day": datetime.now(ZoneInfo("Europe/Istanbul")).date().isoformat(),
        }

    def now_tr(self):
        return datetime.now(ZoneInfo("Europe/Istanbul"))

    def runtime(self):
        seconds = max(0, int((datetime.now(timezone.utc) - self.started_at).total_seconds()))
        d, rem = divmod(seconds, 86400)
        h, rem = divmod(rem, 3600)
        m, s = divmod(rem, 60)
        return {
            "uptime_text": f"{d} gün {h:02d} saat {m:02d} dk {s:02d} sn",
            "started_at_tr": self.started_at.astimezone(ZoneInfo("Europe/Istanbul")).strftime("%d.%m.%Y %H:%M:%S"),
            "now_tr": self.now_tr().strftime("%d.%m.%Y %H:%M:%S")
        }

    def _reset_daily_stats(self):
        today = self.now_tr().date().isoformat()
        if self.trade_stats["day"] != today:
            self.trade_stats = {
                "buy_count": 0, "sell_count": 0, "total_buy_try": 0.0,
                "total_sell_try": 0.0, "wins": 0, "losses": 0,
                "daily_realized_pnl": 0.0, "day": today
            }

    def settings(self):
        return {
            "risk": self.risk, "tp": self.tp, "sl": self.sl, "trailing": self.trailing,
            "max_positions": self.max_positions, "position_pct": self.position_pct,
            "active_capital_pct": self.active_capital_pct, "entry_score": self.entry_score,
            "scan_limit": self.scan_limit, "min_buy_try": self.min_buy_try,
            "min_loss_try": self.min_loss_try, "min_profit_try": self.min_profit_try,
            "ai_candidates": self.ai_candidates, "ai_min_score": self.ai_min_score,
            "ai_model": self.ai_model, "ai_provider": "Groq Compound Mini (web search)", "ai_cache_minutes": self.ai_cache_minutes
        }

    def update_settings(self, d):
        for k, attr in [("risk","risk"),("tp","tp"),("sl","sl"),("trailing","trailing")]:
            if k in d:
                v = float(d[k]) / 100.0
                if not 0 < v <= 0.25:
                    raise ValueError(f"{k} % 0-25 arasında olmalı")
                setattr(self, attr, v)
        if "max_positions" in d:
            self.max_positions = max(1, min(20, int(d["max_positions"])))
        if "position_pct" in d:
            v = float(d["position_pct"]) / 100.0
            if not 0 < v <= 0.25:
                raise ValueError("Pozisyon büyüklüğü %1-%25 arasında olmalı")
            self.position_pct = v
        if "active_capital_pct" in d:
            v = float(d["active_capital_pct"]) / 100.0
            if not 0.05 <= v <= 1.0:
                raise ValueError("Aktif sermaye %5-%100 arasında olmalı")
            self.active_capital_pct = v
        if "entry_score" in d:
            self.entry_score = max(4, min(5, int(d["entry_score"])))
        if "scan_limit" in d:
            self.scan_limit = max(10, min(60, int(d["scan_limit"])))
        if "ai_candidates" in d:
            self.ai_candidates = max(3, min(10, int(d["ai_candidates"])))
        if "ai_min_score" in d:
            self.ai_min_score = max(50, min(100, int(d["ai_min_score"])))
        if "min_buy_try" in d:
            self.min_buy_try = max(1000.0, float(d["min_buy_try"]))
        if "min_loss_try" in d:
            self.min_loss_try = max(100.0, float(d["min_loss_try"]))
        if "min_profit_try" in d:
            self.min_profit_try = max(150.0, float(d["min_profit_try"]))

    def get_products(self):
        r = requests.get(
            PUBLIC_PRODUCTS,
            params={"limit": 100, "product_type": "SPOT", "get_tradability_status": "true"},
            timeout=20,
            headers={"User-Agent": "YatirimBot/9.0"}
        )
        r.raise_for_status()
        products = r.json().get("products", [])
        stable = {
            "USDC","USDT","DAI","PYUSD","USDS","USDG","EURC","GUSD","TUSD",
            "USDP","FDUSD","BUSD"
        }
        out = []
        for p in products:
            pid = p.get("product_id", "")
            base = p.get("base_currency_id", "")
            if not pid.endswith("-USD") or not base or base in stable:
                continue
            if p.get("is_disabled") or p.get("trading_disabled") or p.get("view_only"):
                continue
            try:
                vol = float(p.get("volume_24h") or p.get("approximate_quote_24h_volume") or 0)
                chg = float(str(p.get("price_percentage_change_24h", "0")).replace("%",""))
                price = float(p.get("price") or p.get("mid_market_price") or 0)
            except Exception:
                continue
            if price <= 0:
                continue
            out.append({
                "symbol": pid, "volume24": vol, "change24": chg,
                "price": price, "name": p.get("base_name") or base
            })
        out.sort(key=lambda x: x["volume24"], reverse=True)
        return out[:self.scan_limit]

    def fetch_candles(self, symbol, granularity="FIVE_MINUTE", minutes=5, limit=200):
        end = int(time.time())
        start = end - (minutes * limit * 60)
        r = requests.get(
            PUBLIC_CANDLES.format(product_id=symbol),
            params={"start": str(start), "end": str(end), "granularity": granularity, "limit": min(350, limit)},
            timeout=18, headers={"User-Agent":"YatirimBot/9.0"}
        )
        r.raise_for_status()
        rows = r.json().get("candles", [])
        if len(rows) < 60:
            raise RuntimeError(f"{symbol}: yetersiz {granularity} veri")
        df = pd.DataFrame(rows)
        for c in ["open","high","low","close","volume"]:
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
        df = self.calc_indicators(self.fetch_candles(symbol, granularity, minutes))
        x = df.iloc[-1]
        return {
            "price": float(x.close),
            "ema20": float(x.ema20),
            "ema50": float(x.ema50),
            "rsi": float(x.rsi),
            "macd": float(x.macd),
            "macds": float(x.macds),
            "volume_ratio": float(x.volume / x.v20) if x.v20 else 0.0,
            "trend": bool(x.ema20 > x.ema50)
        }

    def technical_analyze(self, product):
        symbol = product["symbol"]
        tf5 = self.timeframe_snapshot(symbol, "FIVE_MINUTE", 5)
        tf15 = self.timeframe_snapshot(symbol, "FIFTEEN_MINUTE", 15)
        tf1h = self.timeframe_snapshot(symbol, "ONE_HOUR", 60)

        # Daha fazla kaliteli fırsat yakalamak için giriş bandı genişletildi.
        # AI ikinci kapı olarak riskli adayları eleyecek.
        rsi_ok = 45 <= tf5["rsi"] <= 72
        trend_ok = tf5["trend"]
        macd_ok = tf5["macd"] > tf5["macds"]
        volume_ok = tf5["volume_ratio"] >= 0.80
        momentum_ok = product["change24"] > -2.0
        tf15_ok = tf15["trend"] and tf15["macd"] > tf15["macds"]
        tf1h_ok = tf1h["trend"] and tf1h["macd"] > tf1h["macds"]

        checks = {
            "5dk Trend": trend_ok,
            "5dk RSI": rsi_ok,
            "5dk MACD": macd_ok,
            "5dk Hacim": volume_ok,
            "24s Momentum": momentum_ok,
            "15dk Onay": tf15_ok,
            "1s Onay": tf1h_ok,
        }
        # Core 5 conditions + higher timeframe confirmation.
        core_score = sum([trend_ok, rsi_ok, macd_ok, volume_ok, momentum_ok])
        mtf_score = sum([tf15_ok, tf1h_ok])
        technical_score = core_score + mtf_score

        # Adaptif giriş: 5/7 teknik puan yeterli, ancak trend + MACD
        # ve en az bir üst zaman dilimi onayı şart. AI son kapıdır.
        buy = (
            trend_ok and macd_ok and
            core_score >= max(4, self.entry_score) and
            technical_score >= 5 and
            (tf15_ok or tf1h_ok)
        )
        return {
            "symbol": symbol,
            "name": product["name"],
            "price": tf5["price"],
            "rsi": tf5["rsi"],
            "change24": product["change24"],
            "volume_ratio": tf5["volume_ratio"],
            "score": technical_score,
            "core_score": core_score,
            "checks": checks,
            "decision": "AL ADAYI" if buy else "BEKLE",
            "tf5": tf5, "tf15": tf15, "tf1h": tf1h,
            "reasons": [
                f"5dk trend {'uygun' if trend_ok else 'zayıf'}",
                f"5dk RSI {tf5['rsi']:.1f} {'uygun' if rsi_ok else 'uygun değil'}",
                f"5dk MACD {'pozitif' if macd_ok else 'negatif'}",
                f"5dk hacim {tf5['volume_ratio']:.2f}x {'uygun' if volume_ok else 'zayıf'}",
                f"24s momentum {product['change24']:+.2f}%",
                f"15dk {'onay' if tf15_ok else 'onay yok'}",
                f"1s {'onay' if tf1h_ok else 'onay yok'}"
            ],
            "time": self.now_tr().strftime("%H:%M:%S")
        }

    def _compact_ai_result(self, parsed, sources=None):
        """Normalize a model result into the panel's small, predictable shape."""
        sources = sources or []
        score = max(0, min(100, int(float(parsed.get("score", 0)))))
        decision = parsed.get("decision", "BEKLE")
        if decision not in ("AL", "BEKLE"):
            decision = "BEKLE"
        risk = parsed.get("risk", "ORTA")
        if risk not in ("DÜŞÜK", "ORTA", "YÜKSEK"):
            risk = "ORTA"
        return {
            "score": score,
            "decision": decision,
            "risk": risk,
            "summary": str(parsed.get("summary", ""))[:500],
            "positive": [str(x)[:160] for x in (parsed.get("positive") or [])[:2]],
            "negative": [str(x)[:160] for x in (parsed.get("negative") or [])[:2]],
            "sources": sources[:3]
        }

    def _ai_request(self, payload, headers, timeout=60):
        r = requests.post(GROQ_CHAT, headers=headers, json=payload, timeout=timeout)
        if not r.ok:
            try:
                err = r.json().get("error", {})
                detail = err.get("message") if isinstance(err, dict) else str(err)
            except Exception:
                detail = r.text.strip()
            raise RuntimeError(f"Groq HTTP {r.status_code}: {detail[:500]}")
        return r.json()

    def _parse_ai_message(self, data):
        msg = ((data.get("choices") or [{}])[0].get("message") or {})
        out = (msg.get("content") or "").strip()
        if "```" in out:
            out = out.replace("```json", "").replace("```", "").strip()
        match = re.search(r"\{.*\}", out, re.S)
        if not match:
            raise ValueError("Groq AI geçerli JSON döndürmedi")
        return json.loads(match.group(0)), msg

    def _ai_fallback(self, c, failure_text):
        """Use a small text-only model if Compound is rejected (e.g. 413)."""
        prompt = (
            "Kripto işlem filtresi. Web araması kullanma; yalnızca verilen veriyi değerlendir. "
            f"Coin {c['symbol']} ({c['name']}), fiyat {c['price']:.8g} USD, 24s {c['change24']:+.2f}%, "
            f"RSI {c['rsi']:.1f}, hacim {c['volume_ratio']:.2f}x, teknik {c['score']}/7. "
            "Güçlü yükseliş yapısı ve makul risk varsa AL; aksi halde BEKLE. "
            "Yalnızca şu JSON: "
            '{"score":0,"decision":"AL|BEKLE","risk":"DÜŞÜK|ORTA|YÜKSEK",'
            '"summary":"en fazla 2 kısa Türkçe cümle","positive":[],"negative":[]}'
        )
        payload = {
            "model": self.ai_fallback_model,
            "messages": [
                {"role": "system", "content": "Türkçe yanıt ver. Sadece geçerli JSON döndür."},
                {"role": "user", "content": prompt}
            ],
            "max_completion_tokens": 300,
            "temperature": 0.1,
            "response_format": {"type": "json_object"}
        }
        data = self._ai_request(payload, {
            "Authorization": f"Bearer {self.groq_key}",
            "Content-Type": "application/json"
        }, timeout=45)
        parsed, _ = self._parse_ai_message(data)
        result = self._compact_ai_result(parsed)
        result["summary"] = (result["summary"] + " • Web araştırması yedek AI ile atlandı.").strip()
        return result

    def ai_research(self, candidates):
        """
        V15: Compound Mini önce çok küçük bir web araştırması yapar. 413/400/422
        gibi model/tool kaynaklı hatalarda işlem tamamen kilitlenmez; küçük bir
        text-only GPT-OSS 20B JSON çağrısına düşer. Böylece AI servisi geçici
        olarak sorun çıkarsa panel sonsuza kadar 'Yeni işlem açılmadı' durumunda
        kalmaz. Fallback web doğrulaması yapmaz, bu nedenle yüksek riskli bir
        adayda yine teknik/AI kapısı korunur.
        """
        if not self.groq_key:
            msg = "GROQ_API_KEY yok. Render Environment Variables'a ekle."
            decision = "AI YOK" if self.allow_without_ai else "AI ONAYI GEREKLİ"
            return {c["symbol"]: {
                "score": 0, "decision": decision, "risk": "Bilinmiyor",
                "summary": msg + (" AI kapısı atlandı." if self.allow_without_ai else " Yeni işlem açılmadı."),
                "positive": [], "negative": [], "sources": []
            } for c in candidates}

        reviews = {}
        now_ts = time.time()
        for c in candidates:
            cached = self.ai_reviews.get(c["symbol"])
            cached_at = self.last_ai_at.get(c["symbol"], 0)
            if cached and (now_ts - cached_at) < self.ai_cache_minutes * 60:
                reviews[c["symbol"]] = cached
                continue

            prompt = (
                "Kripto AL filtresi. Güncel web aramasıyla son 72 saatte yalnızca ciddi risk/katalizörleri kontrol et. "
                f"Coin {c['symbol']} ({c['name']}), fiyat {c['price']:.8g} USD, 24s {c['change24']:+.2f}%, "
                f"RSI5 {c['rsi']:.1f}, hacim5 {c['volume_ratio']:.2f}x, teknik {c['score']}/7. "
                "Hack, exploit, delist, büyük unlock/arzdaki artış, ağ sorunu veya ciddi negatif haber varsa BEKLE. "
                "Teknik yapı ve haber görünümü uygunsa AL. "
                "Yalnızca şu JSON'u döndür: "
                '{"score":0,"decision":"AL|BEKLE","risk":"DÜŞÜK|ORTA|YÜKSEK",'
                '"summary":"en fazla 2 kısa Türkçe cümle","positive":["en fazla 2"],"negative":["en fazla 2"]}'
            )
            payload = {
                "model": self.ai_model,
                "messages": [
                    {"role": "system", "content": "Türkçe yanıt ver. Sadece geçerli JSON döndür."},
                    {"role": "user", "content": prompt}
                ],
                "max_completion_tokens": 350,
                "temperature": 0.1,
                "compound_custom": {"tools": {"enabled_tools": ["web_search"]}}
            }
            try:
                data = self._ai_request(payload, {
                    "Authorization": f"Bearer {self.groq_key}",
                    "Content-Type": "application/json",
                    "Groq-Model-Version": "latest"
                })
                parsed, msg = self._parse_ai_message(data)
                sources = []
                for tool in (msg.get("executed_tools") or []):
                    if not isinstance(tool, dict):
                        continue
                    for item in (tool.get("search_results") or [])[:3]:
                        if isinstance(item, dict):
                            url = item.get("url") or item.get("link")
                            title = item.get("title") or item.get("name") or "Web kaynağı"
                            if url:
                                sources.append({"title": str(title)[:120], "url": str(url)[:500]})
                result = self._compact_ai_result(parsed, sources)
                result["summary"] = result["summary"] or "Web araştırması tamamlandı."
                reviews[c["symbol"]] = result
                self.last_ai_at[c["symbol"]] = now_ts
                self.ai_reviews[c["symbol"]] = result
            except Exception as e:
                # 413/400/422 dahil: küçük fallback çağrısı.
                try:
                    result = self._ai_fallback(c, str(e))
                    result["fallback"] = True
                    result["fallback_reason"] = str(e)[:220]
                    reviews[c["symbol"]] = result
                    self.last_ai_at[c["symbol"]] = now_ts
                    self.ai_reviews[c["symbol"]] = result
                except Exception as e2:
                    reviews[c["symbol"]] = {
                        "score": 0, "decision": "AI HATASI", "risk": "Bilinmiyor",
                        "summary": f"AI araştırması başarısız: {type(e).__name__}: {e}; yedek AI: {type(e2).__name__}: {e2}",
                        "positive": [], "negative": [], "sources": []
                    }
        return reviews

    def open_sim(self, s):
        if s["symbol"] in self.positions:
            return False

        # Aktif sermaye havuzu ve pozisyon başına sınır.
        market_value = sum(float(p.get("last", p["entry"])) * float(p["qty"]) for p in self.positions.values())
        equity = self.balance_try + market_value
        active_budget = equity * self.active_capital_pct
        active_used = sum(float(p.get("buy_amount_try", 0)) for p in self.positions.values())
        remaining_budget = max(0.0, active_budget - active_used)

        buy_amount = min(self.balance_try, equity * self.position_pct, remaining_budget)
        if buy_amount + 1e-9 < self.min_buy_try:
            return False

        # AI kapısı.
        ai = self.ai_reviews.get(s["symbol"], {})
        if not self.allow_without_ai:
            if ai.get("decision") != "AL" or ai.get("score", 0) < self.ai_min_score:
                return False

        entry = s["price"]
        qty = buy_amount / max(entry, 1e-12)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        self.balance_try -= buy_amount
        self._reset_daily_stats()
        self.trade_stats["buy_count"] += 1
        self.trade_stats["total_buy_try"] += buy_amount

        self.positions[s["symbol"]] = {
            "symbol": s["symbol"], "name": s["name"],
            "entry": entry, "qty": qty, "buy_amount_try": buy_amount,
            "stop": entry, "target": entry, "peak": entry,
            "last": entry, "unrealized": 0, "score": s["score"],
            "ai_score": ai.get("score", 0), "ai_risk": ai.get("risk", ""),
            "opened": now
        }

        self.history.insert(0, {
            "type": "OPEN", "symbol": s["symbol"], "price": entry,
            "qty": qty, "buy_try": buy_amount, "sell_try": None, "pnl": 0,
            "reason": f"AL • Teknik {s['score']}/7 • AI {ai.get('score',0)}/100",
            "time": now
        })
        return True

    def manage_positions(self):
        for symbol, p in list(self.positions.items()):
            try:
                product = {"symbol": symbol, "name": p.get("name", symbol), "change24": 0}
                s = self.technical_analyze(product)
                price = s["price"]
                p["last"] = price
                p["peak"] = max(p["peak"], price)
                current_value = price * p["qty"]
                pnl = current_value - p["buy_amount_try"]
                p["unrealized"] = pnl
                p["stop"] = p["entry"]  # informational
                p["target"] = p["entry"]

                loss_hit = pnl <= -self.min_loss_try
                profit_hit = pnl >= self.min_profit_try

                # Once +150 TL has been reached, allow a trailing lock,
                # but never sell below +150 TL.
                trailing_lock = False
                if float(p.get("peak_pnl", 0)) < pnl:
                    p["peak_pnl"] = pnl
                if p.get("peak_pnl", 0) >= self.min_profit_try:
                    lock = max(self.min_profit_try, p["peak_pnl"] * 0.75)
                    trailing_lock = self.min_profit_try <= pnl <= lock

                if loss_hit or profit_hit or trailing_lock:
                    sell_try = current_value
                    realized = sell_try - p["buy_amount_try"]
                    self.balance_try += sell_try
                    self.realized_pnl += realized
                    self._reset_daily_stats()

                    self.trade_stats["sell_count"] += 1
                    self.trade_stats["total_sell_try"] += sell_try
                    self.trade_stats["daily_realized_pnl"] += realized
                    if realized > 0:
                        self.trade_stats["wins"] += 1
                    elif realized < 0:
                        self.trade_stats["losses"] += 1

                    reason = (
                        f"ZARAR EŞİĞİ (-{self.min_loss_try:.0f} TL)" if loss_hit else
                        f"KÂR EŞİĞİ (+{self.min_profit_try:.0f} TL)" if profit_hit else
                        f"TRAILING (min +{self.min_profit_try:.0f} TL)"
                    )
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    self.history.insert(0, {
                        "type": "CLOSE", "symbol": symbol, "price": price, "qty": p["qty"],
                        "buy_try": p["buy_amount_try"], "sell_try": sell_try, "pnl": realized,
                        "reason": reason, "time": now
                    })
                    del self.positions[symbol]
            except Exception as e:
                self.last_error = f"Pozisyon yönetimi {symbol}: {type(e).__name__}: {e}"

    def tick(self):
        if not self.running:
            return
        self.last_check = self.now_tr().strftime("%d.%m.%Y %H:%M:%S")
        self.last_error = None

        try:
            products = self.get_products()
        except Exception as e:
            self.last_error = f"Coin listesi alınamadı: {type(e).__name__}: {e}"
            return

        results = []
        for product in products:
            try:
                results.append(self.technical_analyze(product))
            except Exception as e:
                pass

        # Fırsat havuzu: eski 6/7 filtresi çok sertti ve 24 saat hiç işlem
        # oluşmamasına yol açabiliyordu. Şimdi önce teknik olarak makul adayları
        # sıralıyor, son kararı AI + risk filtresine bırakıyoruz.
        results.sort(key=lambda x: (x["score"], x["core_score"], x["change24"], x["volume_ratio"]), reverse=True)
        self.scanned_count = len(results)

        eligible = [x for x in results if x["score"] >= 5 and x["core_score"] >= 4]
        if len(eligible) < self.ai_candidates:
            eligible = [x for x in results if x["score"] >= 4 and x["core_score"] >= 3]
        candidates = eligible[:self.ai_candidates]
        fresh_before = dict(self.last_ai_at)
        self.ai_reviews = self.ai_research(candidates)
        # Sayacı gerçekten araştırılan/yeni güncellenen adaylar için göster.
        self.ai_review_count = sum(1 for c in candidates if self.last_ai_at.get(c["symbol"], 0) != fresh_before.get(c["symbol"], 0))

        # AI sonucu panelde tüm adaylarda görünür.
        for x in results:
            if x["symbol"] in self.ai_reviews:
                x["ai"] = self.ai_reviews[x["symbol"]]
            else:
                x["ai"] = None
            x["final_decision"] = (
                "AL" if (x.get("ai") or {}).get("decision") == "AL"
                and (x.get("ai") or {}).get("score",0) >= self.ai_min_score
                else x["decision"]
            )

        self.signals = {x["symbol"]: x for x in results}

        self.manage_positions()

        # En yüksek teknik + AI skorlu adaylardan al.
        ranked_buy = [
            x for x in results
            if x["decision"] == "AL ADAYI"
            and (x.get("ai") or {}).get("decision") == "AL"
            and (x.get("ai") or {}).get("score",0) >= self.ai_min_score
            and (x.get("ai") or {}).get("risk") != "YÜKSEK"
        ]
        ranked_buy.sort(key=lambda x: ((x.get("ai") or {}).get("score",0), x["score"], x["change24"]), reverse=True)

        if not ranked_buy:
            ai_al = [x for x in results if (x.get("ai") or {}).get("decision") == "AL"]
            if not ai_al:
                self.last_trade_block_reason = f"Alım yok: AI {self.ai_min_score}+ skorlu AL adayı üretmedi."
            else:
                best = max((x.get("ai") or {}).get("score",0) for x in ai_al)
                self.last_trade_block_reason = f"Alım yok: en yüksek AI skoru {best}/100; gereken {self.ai_min_score}."
        else:
            self.last_trade_block_reason = f"{len(ranked_buy)} uygun AL adayı bulundu; pozisyon açma denendi."

        opened_count = 0
        for s in ranked_buy:
            if len(self.positions) >= self.max_positions or self.balance_try < self.min_buy_try:
                self.last_trade_block_reason = "Alım sırası durdu: aktif sermaye veya bakiye sınırı."
                break
            if self.open_sim(s):
                opened_count += 1
        if opened_count:
            self.last_trade_block_reason = f"{opened_count} sanal pozisyon açıldı."

        self.last_scan_ms = int(time.time()*1000)

    def status(self):
        self._reset_daily_stats()
        market_value = sum(float(p.get("last", p["entry"])) * float(p["qty"]) for p in self.positions.values())
        unrealized = sum(float(p.get("unrealized", 0)) for p in self.positions.values())
        equity = self.balance_try + market_value
        total_pnl = equity - self.initial_balance
        closed = self.trade_stats["sell_count"]
        win_rate = (self.trade_stats["wins"] / closed * 100) if closed else 0.0

        ranked = sorted(
            self.signals.values(),
            key=lambda x: (
                x.get("final_decision") == "AL",
                (x.get("ai") or {}).get("score",0),
                x.get("score",0),
                x.get("change24",0)
            ), reverse=True
        )

        return {
            "running": self.running, "dry_run": self.dry_run,
            "cash_try": round(self.balance_try,2), "balance_try": round(equity,2),
            "market_value_try": round(market_value,2), "pnl": round(total_pnl,2),
            "realized_pnl": round(self.realized_pnl,2), "unrealized_pnl": round(unrealized,2),
            "positions": list(self.positions.values()),
            "signals": ranked[:25],
            "scanned_count": self.scanned_count,
            "ai_review_count": self.ai_review_count,
            "last_check": self.last_check, "last_error": self.last_error, "trade_block_reason": self.last_trade_block_reason,
            "settings": self.settings(),
            "data_source": self.data_source,
            "entry_logic": "Adaptif teknik filtre (>=5/7, trend+MACD, en az 1 üst zaman dilimi) + Groq AI web araştırması + AI onayı",
            "exit_logic": f"Min alış {self.min_buy_try:.0f} TL • Min zarar -{self.min_loss_try:.0f} TL • Min kâr +{self.min_profit_try:.0f} TL",
            "runtime": self.runtime(),
            "daily_stats": {
                "day": self.trade_stats["day"],
                "daily_pnl": round(self.trade_stats["daily_realized_pnl"],2),
                "total_buy_try": round(self.trade_stats["total_buy_try"],2),
                "total_sell_try": round(self.trade_stats["total_sell_try"],2),
                "wins": self.trade_stats["wins"],
                "losses": self.trade_stats["losses"],
                "win_rate": round(win_rate,1)
            },
            "history": self.history[:30]
        }
