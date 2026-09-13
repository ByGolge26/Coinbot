# Yatırım Botu V19

V19, V18'den canlı işlem mimarisine geçiş için hazırlanmıştır.

## Mimari
- Coinbase Advanced Trade REST API
- CDP JWT authentication
- Groq `openai/gpt-oss-20b`
- Web araştırması kapalı
- Teknik filtre + AI onayı + risk motoru
- Preview Order -> gerçek market order
- Gerçek bakiye/fill/fee takibi
- PostgreSQL state persistence
- Günlük zarar ve ardışık kayıp koruması

## Güvenlik
ZIP varsayılan olarak DRY RUN'dur. Canlı moda geçmek için Render'da `LIVE_TRADING=true`, `LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY_TRADING`, `DRY_RUN=false` ayarlanmalıdır.

Coinbase anahtarında View + Trade kullan. Transfer/withdraw yetkisi verme.

## Kurulum
Detaylar `KURULUM_V19_CANLI.txt` dosyasındadır.
