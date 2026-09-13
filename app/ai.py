import os
import json
from dotenv import load_dotenv
load_dotenv()

def local_ai(technical, news):
    t = technical.get("score",0)
    n = news.get("score",0)
    risk = technical.get("atr_pct",99)
    final = round(max(0, min(100, t*0.72 + (n+100)*0.14)))
    if risk > 10:
        final = max(0, final-8)
    verdict = "POZİTİF" if final >= 75 else "TEMKİNLİ" if final >= 55 else "ZAYIF"
    return {
        "score": final,
        "verdict": verdict,
        "summary": f"Teknik skor {t}/100, haber skoru {n:+d}. Volatilite %{risk:.1f}. Yerel kural tabanlı değerlendirme kullanıldı.",
        "provider": "local"
    }

def ai_review(technical, news):
    key=os.getenv("OPENAI_API_KEY","").strip()
    if not key:
        return local_ai(technical, news)
    try:
        from openai import OpenAI
        client=OpenAI(api_key=key)
        model=os.getenv("OPENAI_MODEL","gpt-5.6")
        prompt = {
            "technical": technical,
            "news": {
                "sentiment": news.get("sentiment"),
                "score": news.get("score"),
                "articles": news.get("articles",[])[:6]
            }
        }
        resp=client.responses.create(
            model=model,
            input=[
                {"role":"system","content":"You are a cautious market research analyst. Do not promise returns. Assess evidence, contradictions and risk. Return JSON with score 0-100, verdict, summary, risks, catalysts."},
                {"role":"user","content":json.dumps(prompt, ensure_ascii=False, default=str)}
            ]
        )
        text=resp.output_text
        data=json.loads(text)
        data["provider"]="openai"
        return data
    except Exception as e:
        result=local_ai(technical,news)
        result["ai_error"]=str(e)
        return result
