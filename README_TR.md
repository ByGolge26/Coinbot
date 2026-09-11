YATIRIM BOTU V14 - GROQ 413 KESIN DUZELTME

V14, V13'te görülen "Groq HTTP 413: Request Entity Too Large" hatasına karşı
Groq isteğini küçültür ve web araştırmasını yalnızca gerekli adaylarda yapar.

Temel değişiklikler:
- Groq'a ham mum/dataframe gönderilmez.
- Her AI çağrısında yalnızca kısa teknik özet gönderilir.
- Sadece web_search etkinleştirilir; gereksiz Compound araçları kullanılmaz.
- Groq-Model-Version: 2025-07-23 kullanılır; daha hafif temel web araması tercih edilir.
- AI çıktısı en fazla kısa JSON olarak istenir.
- Kaynaklar panelde en fazla 3 adet tutulur.
- 413 dahil gerçek Groq hata mesajı panelde gösterilir.
- AI hatası varsa güvenlik gereği yeni işlem açılmaz.
- DRY_RUN=true olarak kalır.

Render Environment Variables:
GROQ_API_KEY = Groq API anahtarın
AI_MODEL = groq/compound-mini
AI_CANDIDATES = 8
AI_MIN_SCORE = 70
AI_CACHE_MINUTES = 15
ALLOW_WITHOUT_AI = false
DRY_RUN = true

Kurulum:
1. GitHub'daki mevcut bot dosyalarını V14 ZIP içindeki dosyalarla değiştir.
2. Commit/push yap.
3. Render -> Manual Deploy -> Deploy latest commit.
4. Render Environment'ta GROQ_API_KEY'in bulunduğunu kontrol et.
5. Panel başlığında "Yatırım Botu V14" görünmelidir.
6. DRY RUN açıkken önce tarama ve sanal işlem zincirini test et.

Not:
Groq'un güncel dokümanına göre HTTP 413, istek gövdesinin fazla büyük olduğunu belirtir.
V14 bu riski azaltmak için istemci tarafındaki AI payload'unu ciddi biçimde küçültür.
