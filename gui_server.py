"""
gui_server.py  —  Drop this file next to your main bot file.
Run it alongside the bot:  python gui_server.py
It reads the same SQLite DB, JSON files, and controls the bot process.
"""

import sqlite3
import json
import os
import subprocess
import signal
import threading
import time
from datetime import datetime
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_sock import Sock

# ── paths must match your bot config ──────────────────────────────────────────
DATABASE        = "ai_trading.db"
SIGNAL_FILE     = "signal_history.json"
OPEN_SIGNALS_FILE = "open_signals.json"
PERFORMANCE_FILE  = "performance_data.json"
BOT_SCRIPT      = "ai_trading_bot_v4.py"   # ← updated to match your filename
# ──────────────────────────────────────────────────────────────────────────────

app  = Flask(__name__, static_folder=".", static_url_path="/static")
CORS(app)
sock = Sock(app)

# ── Serve the GUI HTML directly (fixes file:// CORS issues) ──────────────────
@app.route("/")
def index():
    return app.send_static_file("trading_gui.html")

bot_process   = None   # subprocess handle
ws_clients    = []     # connected WebSocket clients
gui_log       = []     # in-memory log ring (last 100 lines)
bot_lock      = threading.Lock()

# ── helpers ───────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return []

def add_log(level, msg):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "level": level, "msg": msg}
    gui_log.append(entry)
    if len(gui_log) > 100:
        gui_log.pop(0)
    broadcast({"type": "log", "data": entry})

def broadcast(payload):
    dead = []
    for ws in ws_clients:
        try:
            ws.send(json.dumps(payload))
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.remove(ws)

def bot_is_running():
    global bot_process
    return bot_process is not None and bot_process.poll() is None

# ── bot process watcher (streams stdout → GUI log) ────────────────────────────

def watch_bot_output(proc):
    for line in iter(proc.stdout.readline, b""):
        text = line.decode("utf-8", errors="replace").rstrip()
        if text:
            level = "err" if "ERROR" in text.upper() else \
                    "warn" if "WARN" in text.upper() or "SKIP" in text.upper() else \
                    "acc"
            add_log(level, text)
    add_log("warn", "Bot process ended")
    broadcast({"type": "bot_status", "running": False})

# ═══════════════════════════════════════════════════════════════════════════════
# REST ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/status")
def status():
    return jsonify({
        "bot_running": bot_is_running(),
        "mt5_ok":      os.path.exists(DATABASE),   # simple proxy
        "time_utc":    datetime.utcnow().strftime("%H:%M:%S"),
    })

# ── scoreboard ────────────────────────────────────────────────────────────────
@app.route("/api/scoreboard")
def scoreboard():
    db = get_db()
    rows = db.execute(
        "SELECT result, COUNT(*) as cnt FROM signals WHERE signal != 'HOLD' GROUP BY result"
    ).fetchall()
    counts = {r["result"]: r["cnt"] for r in rows}

    sym_rows = db.execute("""
        SELECT symbol,
               SUM(CASE WHEN result='WIN'  THEN 1 ELSE 0 END) wins,
               SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) losses,
               COUNT(*) total
        FROM   signals WHERE signal != 'HOLD'
        GROUP  BY symbol
    """).fetchall()
    db.close()

    w = counts.get("WIN", 0)
    l = counts.get("LOSS", 0)
    r = counts.get("RUNNING", 0)
    e = counts.get("EXPIRED", 0)
    total = w + l
    wr = round(w / total * 100, 1) if total else 0.0

    return jsonify({
        "wins": w, "losses": l, "running": r, "expired": e,
        "win_rate": wr,
        "symbols": [dict(row) for row in sym_rows]
    })

# ── recent signals ────────────────────────────────────────────────────────────
@app.route("/api/signals")
def signals():
    db  = get_db()
    rows = db.execute(
        "SELECT * FROM signals ORDER BY id DESC LIMIT 50"
    ).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

# ── open signals ──────────────────────────────────────────────────────────────
@app.route("/api/open_signals")
def open_signals():
    return jsonify(load_json(OPEN_SIGNALS_FILE))

# ── signal history (json file) ────────────────────────────────────────────────
@app.route("/api/history")
def history():
    return jsonify(load_json(SIGNAL_FILE)[-50:])

# ── log ───────────────────────────────────────────────────────────────────────
@app.route("/api/log")
def get_log():
    return jsonify(gui_log[-80:])

@app.route("/api/log/clear", methods=["POST"])
def clear_log():
    gui_log.clear()
    return jsonify({"ok": True})

# ── bot control ───────────────────────────────────────────────────────────────
@app.route("/api/bot/start", methods=["POST"])
def start_bot():
    global bot_process
    with bot_lock:
        if bot_is_running():
            return jsonify({"ok": False, "msg": "Already running"})
        try:
            bot_process = subprocess.Popen(
                ["python", BOT_SCRIPT],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            threading.Thread(target=watch_bot_output, args=(bot_process,), daemon=True).start()
            add_log("acc", f"Bot started (PID {bot_process.pid})")
            broadcast({"type": "bot_status", "running": True})
            return jsonify({"ok": True, "pid": bot_process.pid})
        except Exception as e:
            add_log("err", f"Failed to start bot: {e}")
            return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/bot/stop", methods=["POST"])
def stop_bot():
    global bot_process
    with bot_lock:
        if not bot_is_running():
            return jsonify({"ok": False, "msg": "Bot not running"})
        try:
            bot_process.send_signal(signal.SIGTERM)
            bot_process.wait(timeout=5)
        except Exception:
            bot_process.kill()
        add_log("warn", "Bot stopped by user")
        broadcast({"type": "bot_status", "running": False})
        bot_process = None
        return jsonify({"ok": True})

# ── filter overrides (written to a JSON file the bot can read) ────────────────
FILTER_FILE = "gui_filters.json"

@app.route("/api/filters", methods=["GET"])
def get_filters():
    try:
        with open(FILTER_FILE) as f:
            return jsonify(json.load(f))
    except Exception:
        defaults = {
            "killzone": True, "calendar": True, "spread": True,
            "volatility": True, "market_quality": True,
            "regime": True, "mtf": True, "telegram": True
        }
        return jsonify(defaults)

@app.route("/api/filters", methods=["POST"])
def set_filters():
    data = request.json
    with open(FILTER_FILE, "w") as f:
        json.dump(data, f, indent=2)
    add_log("info", f"Filters updated: {data}")
    broadcast({"type": "filters", "data": data})
    return jsonify({"ok": True})

# ── clear open signals ────────────────────────────────────────────────────────
@app.route("/api/open_signals/clear", methods=["POST"])
def clear_open():
    with open(OPEN_SIGNALS_FILE, "w") as f:
        json.dump([], f)
    add_log("warn", "Open signals cleared via GUI")
    return jsonify({"ok": True})

# ─── WebSocket (live push) ────────────────────────────────────────────────────
@sock.route("/ws")
def websocket(ws):
    ws_clients.append(ws)
    add_log("info", "GUI connected via WebSocket")
    # send current state immediately
    ws.send(json.dumps({"type": "bot_status", "running": bot_is_running()}))
    try:
        while True:
            msg = ws.receive(timeout=30)
            if msg is None:
                break
            data = json.loads(msg)
            if data.get("type") == "ping":
                ws.send(json.dumps({"type": "pong"}))
    except Exception:
        pass
    finally:
        if ws in ws_clients:
            ws_clients.remove(ws)

# ── push new DB rows every 5 s ────────────────────────────────────────────────
def db_poller():
    last_id = 0
    while True:
        time.sleep(5)
        try:
            db   = get_db()
            rows = db.execute(
                "SELECT * FROM signals WHERE id > ? ORDER BY id", (last_id,)
            ).fetchall()
            db.close()
            for row in rows:
                last_id = max(last_id, row["id"])
                broadcast({"type": "new_signal", "data": dict(row)})
        except Exception:
            pass

threading.Thread(target=db_poller, daemon=True).start()

# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    add_log("acc", "GUI server started on http://localhost:5000")
    print("=" * 50)
    print("  AI Trading Bot GUI Server")
    print("  Open  →  trading_gui.html  in your browser")
    print("  API   →  http://localhost:5000")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=False)
