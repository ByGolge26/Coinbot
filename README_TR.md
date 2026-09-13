# Coinbot V21 - Binance Spot

Bu sürüm Coinbase yerine Binance Spot REST API kullanır. Teknik tarama + Groq AI + risk motoru + kalıcı PostgreSQL durum kaydı içerir.

## Güvenlik
İlk kurulumda `BINANCE_TESTNET=true`, `LIVE_TRADING=false`, `DRY_RUN=true` bırakın. Binance API anahtarını sadece Render Environment Variables içine koyun. Çekim/withdrawal yetkisi vermeyin.

## Binance değişkenleri
- BINANCE_API_KEY
- BINANCE_API_SECRET
- BINANCE_TESTNET=true/false
- LIVE_TRADING=true/false
- LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY_TRADING (yalnızca canlı işlem için)
- DATABASE_URL

## Mevcut risk sınırları
Minimum alış 1000 TL, minimum zarar 100 TL, minimum kâr 150 TL, pozisyon başına maksimum %25, aktif sermaye %75.

## Kritik düzeltmeler
- `NoneType.get` kaynaklı AI parse çökmesi güvenli parser ile düzeltildi.
- AI bozulduğunda açık pozisyonların çıkış yönetimi AI'dan bağımsızdır.
- Groq strict JSON schema kaldırıldı; küçük JSON object kullanılıyor ve fallback model var.
- Binance market order sonrası gerçek `executedQty` ve `cummulativeQuoteQty` ile pozisyon kaydı tutuluyor.
- 5xx/timeout sonrası emir başarısız varsayılmadan `clientOrderId` ile sorgulanıyor.
- Binance LOT_SIZE/MIN_NOTIONAL filtreleri uygulanıyor.
- Binance imzalı isteklerde HMAC-SHA256 ve percent-encoded payload kullanılıyor.

Canlı para için önce Binance Spot Testnet ve ardından çok küçük tutarla doğrulama yapılmalıdır.
