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

@app.post("/api/start")
def start():
    bot.running = True
    bot.tick()
    return jsonify(ok=True, status=bot.status())

@app.post("/api/stop")
def stop():
    bot.running = False
    return jsonify(ok=True, status=bot.status())

@app.post("/api/scan")
def scan():
    bot.running = True
    bot.tick()
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
            bot.tick()
        except Exception as e:
            bot.last_error = f"{type(e).__name__}: {e}"
        time.sleep(60)

if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","10000")))
