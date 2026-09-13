# Midas AI Trader v0.4 Cloud

Bilgisayar kapalıyken de çalışması hedeflenen, Render üzerinde 7/24 çalışan araştırma ve sinyal paneli.

## Özellikler
- Kripto, BIST ve ABD varlıkları
- Teknik analiz: EMA, RSI, MACD, ATR, hacim, momentum, destek/direnç
- GDELT haber araştırması + basit sentiment
- OpenAI değerlendirmesi (API anahtarı verilirse)
- Telegram sinyalleri
- Walk-forward backtest; komisyon + slippage
- SQLite sinyal geçmişi (Render persistent disk)
- 30 dakikalık otomatik tarama
- iPhone'dan web paneline erişim
- Midas/Midas Kripto'ya otomatik emir GÖNDERMEZ

## Render kurulumu
1. Bu klasörü GitHub reposuna yükleyin.
2. Render > New > Blueprint seçin ve repository'yi bağlayın.
3. `render.yaml` içindeki web service oluşturulur.
4. Environment variables ekranında `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `OPENAI_API_KEY` değerlerini girin. Bunları koda yazmayın.
5. Deploy tamamlanınca Render size `https://...onrender.com` adresi verir. Bu adresi iPhone Safari'de açın.

Render cron/worker yerine bu sürümde web service içindeki kontrollü scheduler kullanır; böylece aynı servis hem paneli hem taramayı sunar. 7/24 çalışma için seçilen planın sürekli çalışan web service olması gerekir.

## Yerel test
Windows'ta `start.bat` çalıştırın. Tarayıcı: http://127.0.0.1:8000

## Güvenlik
- Gerçek `.env` dosyasını GitHub'a yüklemeyin.
- Telegram bot tokenını paylaşmayın.
- OpenAI API anahtarını paylaşmayın.
- Bu sistem getiri garantisi vermez ve otomatik emir göndermez.
