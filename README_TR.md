# Yatırım Botu V18

V18, V18 üzerinden hazırlanmıştır. Ana değişiklikler:

- Groq AI artık aday başına ayrı çağrı yapmak yerine adayları **tek toplu AI isteğinde** değerlendirir.
- `groq/compound-mini` için yalnızca `web_search` etkin bırakılır.
- Groq `429 Too Many Requests` geldiğinde `Retry-After` okunur ve bot otomatik bekler; aynı anda fallback çağrısı yapıp rate limit'i daha da büyütmez.
- Tarama varsayılan olarak 600 saniyedir (10 dakika). Bu, ücretsiz Groq limitleriyle daha uyumlu bir kullanım sağlar.
- Aynı anda iki tarama başlatılmasını önleyen kilit eklendi.
- `/api/start` artık uzun bir taramayı HTTP isteği içinde bekletmez; worker taramayı başlatır.
- AI 429 durumunda yeni işlem açılmaz. Güvenlik nedeniyle AI onayı yoksa alım yapılmaz.
- 413/400/422 gibi 429 olmayan AI hatalarında tek toplu küçük fallback çağrısı denenir.
- Panelde Groq durumu, cooldown ve son AI çağrısı gösterilir.

## Render Environment

Gerekli:
- `GROQ_API_KEY`: Groq anahtarın
- `DRY_RUN=true`

Önerilen:
- `AI_MODEL=groq/compound-mini`
- `AI_FALLBACK_MODEL=openai/gpt-oss-20b`
- `AI_MIN_SCORE=70`
- `AI_CANDIDATES=8`
- `SCAN_LIMIT=40`
- `SCAN_INTERVAL_SECONDS=600`
- `MIN_BUY_TRY=1000`
- `MIN_LOSS_TRY=100`
- `MIN_PROFIT_TRY=150`
- `POSITION_SIZE_PCT=0.25`
- `ACTIVE_CAPITAL_PCT=0.75`
- `ALLOW_WITHOUT_AI=false`

Gerçek para ile çalıştırmadan önce DRY RUN ile tarama, sanal AL ve sanal SAT zincirini doğrulayın. Kâr garantisi yoktur.


V18: Groq normal chat modeli kullanır, Groq Compound/web araştırması tamamen kapalıdır. AI yalnızca botun hesapladığı teknik veriyi değerlendirir. Varsayılan model: openai/gpt-oss-20b. ChatGPT Go aboneliği API anahtarı olarak kullanılamaz; GROQ_API_KEY gerekir. DRY_RUN=true ile test edilmelidir.
