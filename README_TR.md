# Yatırım Botu V15

Bu sürüm V14'ün Groq AI hattını düzeltir. Compound Mini çağrısı güncel `latest` sürümü ve yalnızca `web_search` ile yapılır. 413/400/422 gibi çağrı hatalarında küçük `openai/gpt-oss-20b` JSON fallback devreye girer. Fallback web araması yapmaz; güvenlik nedeniyle AI kapısı yine korunur.

## Render Environment
- `GROQ_API_KEY`: mevcut Groq anahtarın
- `AI_MODEL`: `groq/compound-mini`
- `AI_FALLBACK_MODEL`: `openai/gpt-oss-20b`
- `AI_MIN_SCORE`: `70`
- `AI_CANDIDATES`: `8`
- `SCAN_LIMIT`: `40`
- `MIN_BUY_TRY`: `1000`
- `MIN_LOSS_TRY`: `100`
- `MIN_PROFIT_TRY`: `150`
- `POSITION_SIZE_PCT`: `0.25`
- `ACTIVE_CAPITAL_PCT`: `0.75` veya istediğin değer
- `DRY_RUN`: `true`

Panel artık alım yapılmadığında bunun nedenini ayrıca gösterir.
