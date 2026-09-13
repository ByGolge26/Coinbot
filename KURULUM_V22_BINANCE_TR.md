# Coinbot V22 – Binance TR

Bu sürüm Binance global `api.binance.com` yerine **Binance TR** hesap/emir API'sini kullanır. Binance TR dokümantasyonunda ana REST tabanı `https://www.binance.tr`, sembol listesi `/open/v1/common/symbols`, imzalı hesap bilgisi `/open/v1/account/spot` ve emir oluşturma `/open/v1/orders` olarak tanımlanıyor. Ana sembol tipi için piyasa mumları dokümanda `api.binance.me` üzerinden gösteriliyor.

## Render Environment Variables

Zorunlu:

- `BINANCE_API_KEY` = Binance TR API anahtarın
- `BINANCE_API_SECRET` = Binance TR API Secret
- `GROQ_API_KEY` = Groq anahtarı
- `DATABASE_URL` = Render PostgreSQL bağlantısı

Canlı işlem için ayrıca:

- `LIVE_TRADING` = `true`
- `LIVE_CONFIRM` = `I_UNDERSTAND_REAL_MONEY_TRADING`

**İlk kurulumda LIVE_TRADING kesinlikle `false` kalsın.** Önce Binance TR bağlantı testini ve birkaç DRY RUN taramasını kontrol et.

## Binance API anahtarı güvenliği

API anahtarında sadece gerekli Spot/Trade yetkilerini aç. **Withdraw/para çekme yetkisini açma.** IP kısıtlaması kullanabiliyorsan Render'ın sabit çıkış IP'si yoksa anahtarı yanlışlıkla kilitlememeye dikkat et.

## Varsayılan riskler

- Minimum alış: 1000 TL
- Minimum zarar çıkışı: 100 TL
- Minimum kâr çıkışı: 150 TL
- Pozisyon üst sınırı: %25
- Aktif sermaye: %75
- Maksimum açık işlem: 20
- Günlük zarar limiti: 200 TL
- Arka arkaya kayıp: 3
- Kayıp sonrası bekleme: 30 dk
- AI olmadan işlem: kapalı
- Tarama: 5 dk

## 451 hatasının çözümü

Eski bot Binance global API'sine (`api.binance.com`) istek attığı için 451 Restricted Location alıyordu. Bu V22'de hesap ve emir işlemleri Binance TR'ye taşındı. Binance TR'nin güncel dokümanı da `/open/v1/common/symbols`, `/open/v1/account/spot` ve `/open/v1/orders` uçlarını tanımlıyor.

## Önemli

Bu paket gerçek para modunu otomatik açmaz. Gerçek işlem, iki ayrı koşul birlikte sağlanırsa açılır: `LIVE_TRADING=true` ve `LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY_TRADING`.
