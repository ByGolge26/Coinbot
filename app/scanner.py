import os
import time
from datetime import datetime, timezone
from .data import DEFAULT_SYMBOLS, fetch
from .analysis import analyze
from .news import fetch_news
from .ai import ai_review
from .db import save
from .telegram import send_signal, configured


def build_item(symbol):
    df = fetch(symbol, period="5y", interval="1d")
    tech = analyze(symbol, df)
    news = fetch_news(symbol)
    ai = ai_review(tech, news)
    tech["news"] = news
    tech["ai"] = ai
    tech["combined_score"] = round(
        tech["score"] * 0.72
        + (news.get("score", 0) + 100) * 0.14
        + ai.get("score", tech["score"]) * 0.14
    )
    return tech


def scan_all(notify=True):
    rows = []
    errors = []
    for symbol in DEFAULT_SYMBOLS:
        try:
            rows.append(build_item(symbol))
        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)})
            rows.append({
                "symbol": symbol, "score": 0, "combined_score": 0,
                "signal": "VERİ YOK", "error": str(exc)
            })

    rows.sort(key=lambda x: x.get("combined_score", 0), reverse=True)
    save(rows)

    notified = []
    if notify and configured():
        # Only notify for strong candidates. This avoids 23 Telegram messages every cycle.
        for item in rows:
            if item.get("signal") == "AL ADAYI" and item.get("combined_score", 0) >= 78:
                result = send_signal(item)
                if result.get("ok"):
                    notified.append(item["symbol"])

    return {
        "ok": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "rows": rows,
        "errors": errors,
        "notified": notified,
    }
