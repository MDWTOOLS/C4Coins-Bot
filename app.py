#!/usr/bin/env python3
"""
C4Coins Auto Faucet Bot - Web Edition (Port 8080)
=================================================
Bot faucet otomatis feyorra.top sebagai web app.
Fitur: Set cookie, lihat log real-time, stats, control bot.

Run:
  python3 app.py

Env:
  PORT       - Port server (default: 8080)
  DATA_DIR   - Data directory (default: ./data)
"""

import os
import re
import sys
import time
import json
import random
import logging
import threading
from datetime import datetime
from pathlib import Path
from queue import Queue

import requests
import cv2
import numpy as np
import pytesseract
from flask import Flask, request, jsonify, send_file

# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://feyorra.top"
PORT = int(os.environ.get("PORT", 8080))
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_FILE = DATA_DIR / "config.json"
STATS_FILE = DATA_DIR / "stats.json"
LOG_FILE = DATA_DIR / "bot.log"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36"
)

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ]
)
log = logging.getLogger("c4coins")

# ============================================================
# GLOBAL STATE
# ============================================================

class BotState:
    def __init__(self):
        self.running = False
        self.paused = False
        self.total_earned = 0.0
        self.total_claims = 0
        self.last_claim_msg = ""
        self.last_claim_time = ""
        self.status = "Idle"
        self.balance = "N/A"
        self.uptime_start = None
        self.captcha_fails = 0
        self.captcha_solves = 0
        self.connections_lost = 0
        self.cookie = ""
        self.user_agent = DEFAULT_USER_AGENT
        self.log_queue = Queue(maxsize=200)
        self._lock = threading.Lock()
        self._load_config()
        self._load_stats()

    def _load_config(self):
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r") as f:
                    cfg = json.load(f)
                self.cookie = cfg.get("cookie", "")
                self.user_agent = cfg.get("user_agent", DEFAULT_USER_AGENT)
            except Exception:
                pass

    def _load_stats(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if STATS_FILE.exists():
            try:
                with open(STATS_FILE, "r") as f:
                    data = json.load(f)
                if data.get("date") == today:
                    self.total_earned = data.get("earned", 0.0)
                    self.total_claims = data.get("claims", 0)
            except Exception:
                pass

    def save_config(self):
        with open(CONFIG_FILE, "w") as f:
            json.dump({
                "cookie": self.cookie,
                "user_agent": self.user_agent,
            }, f, indent=2)

    def save_stats(self):
        with open(STATS_FILE, "w") as f:
            json.dump({
                "date": datetime.now().strftime("%Y-%m-%d"),
                "earned": self.total_earned,
                "claims": self.total_claims,
            }, f, indent=2)

    def add_log(self, msg: str, level: str = "info"):
        ts = datetime.now().strftime("%H:%M:%S")
        entry = {"time": ts, "msg": msg, "level": level}
        with self._lock:
            if self.log_queue.full():
                self.log_queue.get()
            self.log_queue.put(entry)
        lvl = logging.DEBUG if level == "debug" else logging.INFO if level == "info" else logging.WARNING if level == "warn" else logging.ERROR
        log.log(lvl, msg)

    def set_status(self, status: str):
        with self._lock:
            self.status = status

    def add_earned(self, amount: float, msg: str):
        with self._lock:
            self.total_earned += amount
            self.total_claims += 1
            self.last_claim_msg = msg
            self.last_claim_time = datetime.now().strftime("%H:%M:%S")
            self.add_log(f"+{amount:.4f} Coins - {msg}")
        self.save_stats()

    @property
    def uptime(self) -> str:
        if not self.uptime_start:
            return "0s"
        elapsed = int(time.time() - self.uptime_start)
        d, remainder = divmod(elapsed, 86400)
        h, remainder = divmod(remainder, 3600)
        m, s = divmod(remainder, 60)
        parts = []
        if d: parts.append(f"{d}d")
        if h: parts.append(f"{h}h")
        if m: parts.append(f"{m}m")
        parts.append(f"{s}s")
        return " ".join(parts)

    def get_logs(self) -> list:
        with self._lock:
            return list(self.log_queue.queue)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "paused": self.paused,
                "status": self.status,
                "balance": self.balance,
                "earned": self.total_earned,
                "claims": self.total_claims,
                "last_msg": self.last_claim_msg,
                "last_time": self.last_claim_time,
                "uptime": self.uptime,
                "captcha_solves": self.captcha_solves,
                "captcha_fails": self.captcha_fails,
                "connections_lost": self.connections_lost,
                "has_cookie": bool(self.cookie),
            }


state = BotState()
bot_thread = None

# ============================================================
# HTTP HELPERS
# ============================================================

def make_session() -> requests.Session:
    s = requests.Session()
    adapter = requests.adapters.HTTPAdapter(max_retries=3, pool_connections=5, pool_maxsize=5)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def headers_get(cookie: str, ua: str) -> dict:
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"{BASE_URL}/dashboard",
        "Cookie": cookie,
    }


def headers_post(cookie: str, ua: str) -> dict:
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"{BASE_URL}/faucet",
        "Cookie": cookie,
        "Origin": BASE_URL,
        "Content-Type": "application/x-www-form-urlencoded",
    }


def headers_img(cookie: str, ua: str) -> dict:
    return {
        "User-Agent": ua,
        "Accept": "image/*;q=0.8",
        "Referer": f"{BASE_URL}/faucet",
        "Cookie": cookie,
    }


# ============================================================
# CAPTCHA SOLVER
# ============================================================

def solve_captcha(image_bytes: bytes) -> str | None:
    try:
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return None

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

        kernel = np.ones((2, 2), np.uint8)
        cleaned = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel)

        contours, _ = cv2.findContours(cleaned, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        boxes = []
        for c in contours:
            x, y, w, h = cv2.boundingRect(c)
            if w > 4 and h > 10:
                boxes.append((x, y, w, h))
        boxes.sort(key=lambda b: b[0])

        if len(boxes) < 2:
            return None

        ocr_config = r"--oem 3 --psm 10 -c tessedit_char_whitelist=0123456789"
        result = ""

        for i, (x, y, w, h) in enumerate(boxes):
            if i == 0:
                continue
            roi = cleaned[y:y+h, x:x+w]
            roi = cv2.copyMakeBorder(roi, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=0)
            roi = cv2.bitwise_not(roi)
            roi = cv2.resize(roi, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
            _, roi = cv2.threshold(roi, 150, 255, cv2.THRESH_BINARY)

            text = pytesseract.image_to_string(roi, config=ocr_config).strip()
            if text.isdigit():
                result += text
                if len(result) == 4:
                    break

        return result if len(result) == 4 else None
    except Exception as e:
        log.error("Captcha error: %s", e)
        state.add_log(f"Captcha error: {e}", "error")
        return None


# ============================================================
# HTML PARSERS
# ============================================================

def parse_success(html: str) -> str | None:
    patterns = [
        r'title:\s*[\'"]([^\'"]+)[\'"]',
        r"([\d\.]+\s+Coins\s+has been added to your balance)",
        r"([\d\.]+\s+[A-Z]+\s+added to[^\']+)",
    ]
    for p in patterns:
        m = re.search(p, html, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def parse_wait(html: str) -> int:
    m = re.search(r"let wait = (\d+)", html)
    return int(m.group(1)) if m else 180


def parse_balance(html: str) -> str | None:
    m = re.search(r"<p>(.*?)</p>", html)
    return m.group(1) if m else None


# ============================================================
# PICK-A-BOX
# ============================================================

def play_pickabox(session: requests.Session, hdrs: dict, rounds: int = 5):
    state.add_log("Playing Pick-a-Box...")
    for r in range(1, rounds + 1):
        if not state.running:
            break
        try:
            resp = session.get(f"{BASE_URL}/pickabox", headers=hdrs, timeout=30)
            page = resp.text

            csrf = re.search(r'name="csrf_token_name" value="([^"]+)"', page)
            tok = re.search(r'name="token" value="([^"]+)"', page)
            grd = re.search(r'name="game_guard" value="([^"]+)"', page)

            if not all([csrf, tok, grd]):
                continue

            box = random.randint(1, 3)
            ph = hdrs.copy()
            ph["Content-Type"] = "application/x-www-form-urlencoded"
            ph["Origin"] = BASE_URL
            ph["Referer"] = f"{BASE_URL}/pickabox"

            session.post(f"{BASE_URL}/pickabox/play", data={
                "csrf_token_name": csrf.group(1),
                "token": tok.group(1),
                "game_guard": grd.group(1),
                "bet_amount": 1,
                "selected_box": box,
            }, headers=ph, timeout=30)

            if r < rounds:
                time.sleep(2)
        except requests.RequestException:
            break

    try:
        dr = session.get(f"{BASE_URL}/dashboard", headers=hdrs, timeout=30)
        b = parse_balance(dr.text)
        if b:
            state.balance = b
    except requests.RequestException:
        pass


# ============================================================
# BOT MAIN LOOP
# ============================================================

def bot_main_loop():
    """Loop utama bot - jalan di thread terpisah."""
    session = make_session()
    cookie = state.cookie
    ua = state.user_agent
    hdrs = headers_get(cookie, ua)

    state.add_log("Bot started")
    state.uptime_start = time.time()

    while state.running:
        if state.paused:
            time.sleep(1)
            continue

        try:
            # Validasi session
            state.set_status("Checking session...")
            try:
                resp = session.get(f"{BASE_URL}/dashboard", headers=hdrs, timeout=30)
                if "Dashboard" not in resp.text:
                    state.set_status("Session expired!")
                    state.add_log("Session expired! Update cookie.", "error")
                    state.running = False
                    break

                bal = parse_balance(resp.text)
                if bal:
                    state.balance = bal
                state.set_status("Session OK")
                state.add_log("Session valid")
            except requests.RequestException as e:
                state.set_status(f"Connection error")
                state.connections_lost += 1
                state.add_log(f"Connection error: {e}", "error")
                time.sleep(5)
                session = make_session()
                continue

            # Faucet page
            state.set_status("Loading faucet...")
            resp = session.get(f"{BASE_URL}/faucet", headers=hdrs, timeout=30)
            page = resp.text

            if "Daily limit" in page or ("limit" in page and "Ready" not in page):
                state.set_status("Daily limit reached!")
                state.add_log("Daily limit tercapai. Bot berhenti.", "warn")
                break

            if "complete shortlink" in page:
                state.set_status("Shortlink required!")
                state.add_log("Selesaikan misi shortlink dulu.", "warn")
                break

            if "Ready To Claim" in page:
                csrf = re.search(r'name="csrf_token_name" id="token" value="([^"]+)"', page)
                tok = re.search(r'name="token" value="([^"]+)"', page)
                img = re.search(r'<img id="Imageid" src="([^"]+)"', page)
                fld = re.search(
                    r'<input type="number" class="form-control border border-dark mb-3" name="([^"]+)"',
                    page,
                )

                if not all([csrf, tok, img, fld]):
                    state.set_status("Form parse failed")
                    state.add_log("Form parse failed, retry...", "warn")
                    time.sleep(2)
                    continue

                # Download captcha
                state.set_status("Downloading captcha...")
                img_resp = session.get(
                    img.group(1),
                    headers=headers_img(cookie, ua),
                    timeout=30,
                )

                if len(img_resp.content) < 100:
                    state.set_status("Playing Pick-a-Box...")
                    play_pickabox(session, hdrs)
                    time.sleep(2)
                    continue

                # Solve captcha
                state.set_status("Solving captcha...")
                digits = solve_captcha(img_resp.content)

                if not digits:
                    state.captcha_fails += 1
                    state.add_log("Captcha gagal di-solve", "warn")
                    time.sleep(1)
                    continue

                state.captcha_solves += 1
                state.add_log(f"Captcha solved: {digits}", "debug")

                # Submit claim
                state.set_status("Submitting claim...")
                session.post(
                    f"{BASE_URL}/faucet/verify",
                    data={
                        "csrf_token_name": csrf.group(1),
                        "token": tok.group(1),
                        fld.group(1): digits,
                    },
                    headers=headers_post(cookie, ua),
                    allow_redirects=False,
                    timeout=30,
                )

                time.sleep(2)

                # Cek hasil
                resp = session.get(f"{BASE_URL}/faucet", headers=hdrs, timeout=30)
                msg = parse_success(resp.text)

                if msg:
                    amt = re.search(r"([\d\.]+)\s+Coins", msg)
                    amount = float(amt.group(1)) if amt else 0.001
                    state.add_earned(amount, msg)

                    try:
                        dr = session.get(f"{BASE_URL}/dashboard", headers=hdrs, timeout=30)
                        b = parse_balance(dr.text)
                        if b:
                            state.balance = b
                    except requests.RequestException:
                        pass

                    wait = parse_wait(resp.text)
                    state.set_status(f"Cooldown {wait}s")
                    state.add_log(f"Cooldown {wait}s...")

                    for _ in range(wait):
                        if not state.running:
                            break
                        time.sleep(1)
                else:
                    state.add_log("Claim gagal, coba lagi...", "warn")
                    time.sleep(2)
            else:
                wait = parse_wait(page)
                state.set_status(f"Waiting {wait}s")
                state.add_log(f"Waiting {wait}s...")

                for _ in range(wait):
                    if not state.running:
                        break
                    time.sleep(1)

        except requests.ConnectionError:
            state.connections_lost += 1
            state.set_status("Reconnecting...")
            state.add_log("Koneksi putus, reconnect...", "error")
            time.sleep(10)
            session = make_session()
            hdrs = headers_get(cookie, ua)
        except Exception as e:
            log.error("Loop error: %s", e)
            state.set_status(f"Error")
            state.add_log(f"Error: {e}", "error")
            time.sleep(5)

    state.set_status("Stopped")
    state.add_log("Bot stopped")


def start_bot():
    """Start bot di background thread."""
    global bot_thread
    if state.running:
        return {"ok": False, "msg": "Bot sudah jalan"}
    if not state.cookie:
        return {"ok": False, "msg": "Cookie belum di-set"}

    state.running = True
    state.paused = False
    bot_thread = threading.Thread(target=bot_main_loop, daemon=True)
    bot_thread.start()
    return {"ok": True, "msg": "Bot started"}


def stop_bot():
    """Stop bot."""
    global bot_thread
    state.running = False
    return {"ok": True, "msg": "Bot stopped"}


# ============================================================
# FLASK APP
# ============================================================

app = Flask(__name__)


@app.route("/")
def index():
    return send_file("index.html")


@app.route("/api/status")
def api_status():
    return jsonify(state.snapshot())


@app.route("/api/logs")
def api_logs():
    logs = state.get_logs()
    return jsonify(logs)


@app.route("/api/cookie", methods=["GET", "POST"])
def api_cookie():
    if request.method == "GET":
        return jsonify({"has_cookie": bool(state.cookie), "cookie_preview": state.cookie[:20] + "..." if state.cookie and len(state.cookie) > 20 else state.cookie or ""})
    elif request.method == "POST":
        data = request.get_json(force=True)
        cookie = data.get("cookie", "").strip()
        if not cookie:
            return jsonify({"ok": False, "msg": "Cookie wajib diisi"}), 400
        state.cookie = cookie
        state.save_config()
        return jsonify({"ok": True, "msg": "Cookie tersimpan!"})


@app.route("/api/bot/start", methods=["POST"])
def api_start():
    result = start_bot()
    return jsonify(result)


@app.route("/api/bot/stop", methods=["POST"])
def api_stop():
    result = stop_bot()
    return jsonify(result)


@app.route("/api/bot/pause", methods=["POST"])
def api_pause():
    state.paused = not state.paused
    state.set_status("Paused" if state.paused else "Resumed")
    state.add_log("Bot paused" if state.paused else "Bot resumed")
    return jsonify({"ok": True, "paused": state.paused})


@app.route("/api/bot/reset", methods=["POST"])
def api_reset():
    stop_bot()
    state.total_earned = 0.0
    state.total_claims = 0
    state.captcha_fails = 0
    state.captcha_solves = 0
    state.connections_lost = 0
    state.last_claim_msg = ""
    state.last_claim_time = ""
    state.balance = "N/A"
    if STATS_FILE.exists():
        STATS_FILE.unlink()
    if CONFIG_FILE.exists():
        CONFIG_FILE.unlink()
    state.cookie = ""
    return jsonify({"ok": True, "msg": "Bot reset"})


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    log.info("C4Coins Web Bot starting on port %d", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False)
