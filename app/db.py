import sqlite3
import os
from pathlib import Path

# Render persistent disk is mounted at /var/data in the cloud deployment.
# Local Windows development still uses the project directory.
DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).resolve().parent.parent)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB = DATA_DIR / "signals.db"


def init():
    con = sqlite3.connect(DB)
    con.execute('''CREATE TABLE IF NOT EXISTS signals(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, symbol TEXT, score INTEGER, signal TEXT,
        price REAL, stop REAL, target1 REAL, target2 REAL,
        news_score INTEGER, ai_score INTEGER
    )''')
    con.commit(); con.close()


def save(items):
    import datetime
    con = sqlite3.connect(DB)
    for x in items:
        if "price" not in x:
            continue
        con.execute(
            "INSERT INTO signals(ts,symbol,score,signal,price,stop,target1,target2,news_score,ai_score) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (datetime.datetime.now().isoformat(timespec="seconds"),
             x["symbol"], x["score"], x["signal"], x["price"], x["stop"],
             x["target1"], x["target2"], x.get("news", {}).get("score", 0),
             x.get("ai", {}).get("score", 0))
        )
    con.commit(); con.close()
