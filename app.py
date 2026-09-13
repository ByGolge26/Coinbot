import os, threading, time
from flask import Flask, jsonify, render_template, request
from bot import TradingBot

app = Flask(__name__)
bot = TradingBot()

@app.route("/")
def home():
    return render_template("index.html")

@app.get("/api/status")
def status():
    return jsonify(bot.status())

@app.get("/api/health")
def health():
    return jsonify(ok=True, mode=bot.mode, live_trading=bot.live_trading, dry_run=bot.dry_run)

@app.post("/api/coinbase-check")
def coinbase_check():
    try:
        if not bot.cb.configured:
            return jsonify(ok=False, error="COINBASE_API_KEY_NAME / COINBASE_API_PRIVATE_KEY eksik"), 400
        usd = bot.cb.usd_available()
        return jsonify(ok=True, usd_available=round(usd, 8), message="Coinbase view bağlantısı başarılı. Bu test emir göndermez.")
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400

@app.post("/api/start")
def start():
    bot.running = True
    bot.next_scan_at = 0
    return jsonify(ok=True, status=bot.status())

@app.post("/api/stop")
def stop():
    bot.running = False
    return jsonify(ok=True, status=bot.status())

@app.post("/api/scan")
def scan():
    bot.running = True
    threading.Thread(target=bot.tick, kwargs={"force": True}, daemon=True).start()
    return jsonify(ok=True, status=bot.status())

@app.post("/api/settings")
def settings():
    try:
        bot.update_settings(request.get_json(force=True) or {})
        return jsonify(ok=True, settings=bot.settings())
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400

def worker():
    while True:
        try:
            bot.tick(force=False)
        except Exception as e:
            bot.last_error = f"{type(e).__name__}: {e}"
        time.sleep(5)

if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
