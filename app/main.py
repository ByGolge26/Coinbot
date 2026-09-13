import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from .data import DEFAULT_SYMBOLS, fetch
from .news import fetch_news
from .ai import ai_review
from .analysis import analyze
from .telegram import configured, send_signal
from .backtest import run
from .db import init
from .scanner import scan_all, build_item
from .version import VERSION, BUILD

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates"
TEMPLATES = Jinja2Templates(directory=str(TEMPLATE_DIR))
SCAN_MINUTES = int(os.getenv("SCAN_INTERVAL_MINUTES", "30"))

async def scheduler_loop():
    while True:
        try:
            await asyncio.to_thread(scan_all, True)
        except Exception as exc:
            print(f"[scheduler] scan failed: {exc}", flush=True)
        await asyncio.sleep(max(5, SCAN_MINUTES) * 60)

@asynccontextmanager
async def lifespan(app: FastAPI):
    init()
    task = asyncio.create_task(scheduler_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

app = FastAPI(title="Midas AI Trader v0.4 Cloud", lifespan=lifespan)

@app.get("/health")
async def health():
    return {"ok": True, "service": "midas-ai-trader", "version": VERSION, "build": BUILD, "scan_interval_minutes": SCAN_MINUTES}

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    # Do not block the homepage with a full market scan. Cached/latest data is preferable.
    rows = []
    for s in DEFAULT_SYMBOLS:
        try:
            rows.append(build_item(s))
        except Exception as exc:
            rows.append({"symbol": s, "score": 0, "combined_score": 0,
                         "signal": "VERİ YOK", "error": str(exc)})
    rows.sort(key=lambda x: x.get("combined_score", 0), reverse=True)
    good = [x for x in rows if x.get("signal") == "AL ADAYI"]
    return TEMPLATES.TemplateResponse(request=request, name="index.html",
        context={"rows": rows, "good": good, "count": len(rows),
                 "telegram": configured(), "scan_interval": SCAN_MINUTES})

@app.get("/api/scan")
async def scan():
    return JSONResponse(await asyncio.to_thread(scan_all, False))

@app.get("/api/news/{symbol}")
async def news(symbol: str):
    return fetch_news(symbol.upper())

@app.get("/api/backtest/{symbol}")
async def backtest(symbol: str):
    df = fetch(symbol.upper(), period="5y")
    return run(df)

@app.post("/api/telegram/{symbol}")
async def telegram(symbol: str):
    item = await asyncio.to_thread(build_item, symbol.upper())
    return send_signal(item)
