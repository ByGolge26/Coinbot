YATIRIM BOTU V12 - GROQ 400 HATASI DÜZELTİLDİ

V12, V11'de Groq Compound API'ye gönderilen geçersiz `citation_options`
alanını kaldırır. Groq'un Compound API'si web arama kaynaklarını yanıt içinde
otomatik olarak döndürür; ayrı bir citation_options alanı göndermek gerekmez.

Ayrıca HTTP hata gövdesi artık panel/log tarafında ayrıntılı gösterilir.

AI:
- Model: groq/compound-mini
- Built-in web search: web_search
- AI adayları: 8
- Minimum AI skoru: 70
- AI çalışmazsa yeni işlem açılmaz.

ÖNEMLİ ÜCRET NOTU:
Groq'un güncel dokümantasyonunda Compound Mini'nin yerleşik web araması
ayrı bir araç ücretiyle listelenmektedir. Bu nedenle "ücretsiz AI" ifadesi
model/API ücretsiz kotası anlamına gelebilir; web araması sınırsız ücretsiz
değildir. Gerçek kullanım ücretleri Groq hesabındaki güncel fiyatlandırmaya
göre kontrol edilmelidir.

Render Environment Variables:
GROQ_API_KEY = Groq API anahtarın
AI_MODEL = groq/compound-mini
AI_CANDIDATES = 8
AI_MIN_SCORE = 70
AI_CACHE_MINUTES = 15
ALLOW_WITHOUT_AI = false
DRY_RUN = true

Kurulum:
1. GitHub'da V12 dosyalarını mevcut proje ile değiştir.
2. Commit/push yap.
3. Render -> Manual Deploy -> Deploy latest commit.
4. GROQ_API_KEY'in Environment Variables altında bulunduğunu kontrol et.
5. DRY_RUN=true kalsın.
