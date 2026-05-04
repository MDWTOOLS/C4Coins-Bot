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
import time
import json
import random
import logging
import threading
from datetime import datetime
from pathlib import Path

import requests
import cv2
import numpy as np
import pytesseract
from flask import Flask, request, jsonify, Response

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

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("c4coins")
log.setLevel(logging.DEBUG)
fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
fh.setLevel(logging.DEBUG)
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
log.addHandler(fh)

# ============================================================
# BOT STATE
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
        self.logs = []
        self.max_logs = 200
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
                self.add_log("Config loaded")
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
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump({"cookie": self.cookie, "user_agent": self.user_agent}, f, indent=2)
        except Exception as e:
            log.error("Save config error: %s", e)

    def save_stats(self):
        try:
            with open(STATS_FILE, "w") as f:
                json.dump({"date": datetime.now().strftime("%Y-%m-%d"), "earned": self.total_earned, "claims": self.total_claims}, f, indent=2)
        except Exception as e:
            log.error("Save stats error: %s", e)

    def add_log(self, msg, level="info"):
        ts = datetime.now().strftime("%H:%M:%S")
        entry = {"time": ts, "msg": msg, "level": level}
        with self._lock:
            self.logs.append(entry)
            if len(self.logs) > self.max_logs:
                self.logs = self.logs[-self.max_logs:]
        lm = {"debug": logging.DEBUG, "info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}
        log.log(lm.get(level, logging.INFO), msg)

    def set_status(self, s):
        with self._lock:
            self.status = s

    def add_earned(self, amount, msg):
        with self._lock:
            self.total_earned += amount
            self.total_claims += 1
            self.last_claim_msg = msg
            self.last_claim_time = datetime.now().strftime("%H:%M:%S")
        self.add_log("+%.4f Coins - %s" % (amount, msg))
        self.save_stats()

    @property
    def uptime(self):
        if not self.uptime_start:
            return "0s"
        e = int(time.time() - self.uptime_start)
        d, r = divmod(e, 86400)
        h, r = divmod(r, 3600)
        m, s = divmod(r, 60)
        p = []
        if d: p.append("%dd" % d)
        if h: p.append("%dh" % h)
        if m: p.append("%dm" % m)
        p.append("%ds" % s)
        return " ".join(p)

    def get_logs(self):
        with self._lock:
            return list(self.logs)

    def snapshot(self):
        with self._lock:
            return {
                "running": self.running, "paused": self.paused,
                "status": self.status, "balance": self.balance,
                "earned": self.total_earned, "claims": self.total_claims,
                "last_msg": self.last_claim_msg, "last_time": self.last_claim_time,
                "uptime": self.uptime,
                "captcha_solves": self.captcha_solves, "captcha_fails": self.captcha_fails,
                "connections_lost": self.connections_lost,
                "has_cookie": bool(self.cookie),
            }


state = BotState()

# ============================================================
# HTTP HELPERS
# ============================================================

def make_session():
    s = requests.Session()
    a = requests.adapters.HTTPAdapter(max_retries=3, pool_connections=5, pool_maxsize=5)
    s.mount("https://", a)
    s.mount("http://", a)
    return s

def hdr_get(ck, ua):
    return {"User-Agent": ua, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9", "Referer": BASE_URL + "/dashboard", "Cookie": ck}

def hdr_post(ck, ua):
    return {"User-Agent": ua, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9", "Referer": BASE_URL + "/faucet", "Cookie": ck, "Origin": BASE_URL, "Content-Type": "application/x-www-form-urlencoded"}

def hdr_img(ck, ua):
    return {"User-Agent": ua, "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8", "Referer": BASE_URL + "/faucet", "Cookie": ck}

# ============================================================
# CAPTCHA SOLVER
# ============================================================

def solve_captcha(image_bytes):
    try:
        if len(image_bytes) < 50:
            return None
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
            if roi.size == 0:
                continue
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
        return None

# ============================================================
# PARSERS
# ============================================================

def parse_success(html):
    for p in [r'title:\s*[\'"]([^\'"]+)[\'"]', r"([\d\.]+\s+Coins\s+has been added to your balance)", r"([\d\.]+\s+[A-Z]+\s+added to[^\']+)"]:
        m = re.search(p, html, re.IGNORECASE)
        if m:
            return m.group(1)
    return None

def parse_wait(html):
    m = re.search(r"let wait = (\d+)", html)
    return int(m.group(1)) if m else 180

def parse_balance(html):
    m = re.search(r"<p>(.*?)</p>", html)
    return m.group(1) if m else None

# ============================================================
# PICK-A-BOX
# ============================================================

def play_pickabox(sess, hdrs, rounds=5):
    state.add_log("Playing Pick-a-Box...")
    for r in range(1, rounds + 1):
        if not state.running:
            break
        try:
            resp = sess.get(BASE_URL + "/pickabox", headers=hdrs, timeout=30)
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
            ph["Referer"] = BASE_URL + "/pickabox"
            sess.post(BASE_URL + "/pickabox/play", data={"csrf_token_name": csrf.group(1), "token": tok.group(1), "game_guard": grd.group(1), "bet_amount": 1, "selected_box": box}, headers=ph, timeout=30)
            if r < rounds:
                time.sleep(2)
        except requests.RequestException:
            break
    try:
        dr = sess.get(BASE_URL + "/dashboard", headers=hdrs, timeout=30)
        b = parse_balance(dr.text)
        if b:
            state.balance = b
    except requests.RequestException:
        pass

# ============================================================
# BOT LOOP
# ============================================================

def bot_loop():
    sess = make_session()
    ck = state.cookie
    ua = state.user_agent
    hdrs = hdr_get(ck, ua)
    state.add_log("Bot started")
    state.uptime_start = time.time()

    while state.running:
        if state.paused:
            time.sleep(1)
            continue
        try:
            state.set_status("Checking session...")
            try:
                resp = sess.get(BASE_URL + "/dashboard", headers=hdrs, timeout=30)
                if resp.status_code != 200:
                    state.add_log("HTTP %d from dashboard" % resp.status_code, "error")
                    time.sleep(10)
                    sess = make_session()
                    continue
                if "dashboard" not in resp.text.lower():
                    state.set_status("Session expired!")
                    state.add_log("Session expired! Update cookie.", "error")
                    state.running = False
                    break
                bal = parse_balance(resp.text)
                if bal:
                    state.balance = bal
                state.set_status("Session OK")
                state.add_log("Session OK, balance: %s" % (bal or "N/A"))
            except requests.Timeout:
                state.connections_lost += 1
                state.add_log("Timeout", "error")
                time.sleep(5)
                sess = make_session()
                continue
            except requests.RequestException as e:
                state.connections_lost += 1
                state.add_log("Connection error: %s" % e, "error")
                time.sleep(5)
                sess = make_session()
                continue

            state.set_status("Loading faucet...")
            try:
                resp = sess.get(BASE_URL + "/faucet", headers=hdrs, timeout=30)
                page = resp.text
            except requests.RequestException as e:
                state.add_log("Faucet load error: %s" % e, "error")
                time.sleep(5)
                continue

            if "daily limit" in page.lower() or "limit reached" in page.lower():
                state.set_status("Daily limit!")
                state.add_log("Daily limit reached.", "warn")
                break

            if "shortlink" in page.lower():
                state.set_status("Shortlink!")
                state.add_log("Shortlink required.", "warn")
                break

            if "Ready To Claim" in page:
                csrf = re.search(r'name="csrf_token_name"[^>]*value="([^"]+)"', page)
                tok = re.search(r'name="token"[^>]*value="([^"]+)"', page)
                img = re.search(r'<img[^>]*id="Imageid"[^>]*src="([^"]+)"', page)
                if not img:
                    img = re.search(r'<img[^>]*src="([^"]*captcha[^"]*)"', page, re.IGNORECASE)
                fld = re.search(r'<input[^>]*type="number"[^>]*name="([^"]+)"', page)

                if not all([csrf, tok, img, fld]):
                    state.add_log("Form parse failed, retry...", "warn")
                    time.sleep(3)
                    continue

                state.set_status("Downloading captcha...")
                img_url = img.group(1)
                if not img_url.startswith("http"):
                    img_url = BASE_URL + "/" + img_url.lstrip("/")
                try:
                    img_resp = sess.get(img_url, headers=hdr_img(ck, ua), timeout=30)
                except requests.RequestException as e:
                    state.add_log("Captcha download error: %s" % e, "error")
                    time.sleep(3)
                    continue

                if len(img_resp.content) < 100:
                    state.add_log("Captcha too small, playing Pick-a-Box...")
                    play_pickabox(sess, hdrs)
                    time.sleep(2)
                    continue

                state.set_status("Solving captcha...")
                digits = solve_captcha(img_resp.content)
                if not digits:
                    state.captcha_fails += 1
                    state.add_log("Captcha failed (%d)" % state.captcha_fails, "warn")
                    time.sleep(2)
                    continue

                state.captcha_solves += 1
                state.add_log("Captcha solved: %s" % digits)

                state.set_status("Claiming...")
                try:
                    sess.post(BASE_URL + "/faucet/verify", data={"csrf_token_name": csrf.group(1), "token": tok.group(1), fld.group(1): digits}, headers=hdr_post(ck, ua), allow_redirects=False, timeout=30)
                except requests.RequestException as e:
                    state.add_log("Claim error: %s" % e, "error")
                    time.sleep(3)
                    continue

                time.sleep(2)

                try:
                    resp = sess.get(BASE_URL + "/faucet", headers=hdrs, timeout=30)
                    page = resp.text
                except requests.RequestException:
                    time.sleep(3)
                    continue

                msg = parse_success(page)
                if msg:
                    amt = re.search(r"([\d\.]+)\s+Coins", msg)
                    amount = float(amt.group(1)) if amt else 0.001
                    state.add_earned(amount, msg)
                    try:
                        dr = sess.get(BASE_URL + "/dashboard", headers=hdrs, timeout=30)
                        b = parse_balance(dr.text)
                        if b:
                            state.balance = b
                    except requests.RequestException:
                        pass
                    wait = parse_wait(page)
                    state.set_status("Cooldown %ds" % wait)
                    state.add_log("Cooldown %ds..." % wait)
                    for _ in range(wait):
                        if not state.running or state.paused:
                            break
                        time.sleep(1)
                else:
                    state.add_log("Claim failed, retry...", "warn")
                    time.sleep(3)
            else:
                wait = parse_wait(page)
                state.set_status("Waiting %ds" % wait)
                state.add_log("Waiting %ds..." % wait)
                for _ in range(wait):
                    if not state.running or state.paused:
                        break
                    time.sleep(1)

        except requests.ConnectionError:
            state.connections_lost += 1
            state.set_status("Reconnecting...")
            state.add_log("Connection lost, reconnect...", "error")
            time.sleep(10)
            sess = make_session()
            hdrs = hdr_get(ck, ua)
        except Exception as e:
            log.error("Loop error: %s", e)
            state.set_status("Error")
            state.add_log("Error: %s" % e, "error")
            time.sleep(5)

    state.set_status("Stopped")
    state.add_log("Bot stopped")

# ============================================================
# FLASK APP
# ============================================================

app = Flask(__name__)

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>C4Coins Faucet Bot</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,-apple-system,sans-serif;background:#0b0d14;color:#c8cdd8;min-height:100vh}
.hd{background:#0f1420;border-bottom:1px solid rgba(255,255,255,.06);padding:14px 20px;display:flex;align-items:center;justify-content:space-between}
.logo{display:flex;align-items:center;gap:10px}
.logo>div{width:32px;height:32px;background:linear-gradient(135deg,#f4a261,#e94560);border-radius:8px;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:800;font-size:14px}
.logo h1{font-size:16px;color:#e8ecf1}
.logo small{font-size:10px;color:#555;display:block}
.pill{display:flex;align-items:center;gap:6px;padding:5px 12px;border-radius:16px;font-size:11px;font-weight:600;border:1px solid}
.pill.on{background:rgba(0,180,100,.08);border-color:rgba(0,180,100,.25);color:#00b464}
.pill.off{background:rgba(100,100,120,.08);border-color:rgba(100,100,120,.25);color:#64647a}
.pill.err{background:rgba(220,50,50,.08);border-color:rgba(220,50,50,.25);color:#dc3232}
.pill.pau{background:rgba(244,162,97,.08);border-color:rgba(244,162,97,.25);color:#f4a261}
.dot{width:7px;height:7px;border-radius:50%}
.on .dot{background:#00b464;animation:p 2s infinite}
.off .dot{background:#64647a}
.err .dot{background:#dc3232}
.pau .dot{background:#f4a261}
@keyframes p{0%,100%{opacity:1}50%{opacity:.3}}
.ct{max-width:860px;margin:0 auto;padding:16px}
.cd{background:#12161f;border:1px solid rgba(255,255,255,.05);border-radius:12px;padding:16px;margin-bottom:14px}
.cd h3{font-size:12px;font-weight:600;color:#555;text-transform:uppercase;letter-spacing:.5px;margin-bottom:12px}
.sg{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.si{text-align:center;padding:10px 6px;background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.04);border-radius:8px}
.sv{font-size:20px;font-weight:700}.sv.a{color:#f4a261}.sv.b{color:#00b464}.sv.c{color:#00b4d8}.sv.d{color:#e94560}
.sl{font-size:10px;color:#555;margin-top:3px}
.fg{margin-bottom:0}.fg label{display:block;font-size:11px;color:#555;margin-bottom:4px}.fg input{width:100%;padding:9px 12px;background:rgba(0,0,0,.3);border:1px solid rgba(255,255,255,.08);border-radius:6px;color:#e8ecf1;font-size:12px;outline:none;font-family:monospace}.fg input:focus{border-color:#00b4d8}
.bt{padding:9px 16px;border:none;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;transition:.15s}
.bt:hover{transform:translateY(-1px)}.bt:active{transform:translateY(0)}
.bp{background:linear-gradient(135deg,#00b4d8,#0077b6);color:#fff}
.bs{background:linear-gradient(135deg,#00b464,#008651);color:#fff}
.bd{background:linear-gradient(135deg,#dc3232,#a02020);color:#fff}
.bw{background:linear-gradient(135deg,#f4a261,#d4793a);color:#fff}
.bo{background:transparent;border:1px solid rgba(255,255,255,.1);color:#8892a4}
.ct2{display:flex;gap:8px;flex-wrap:wrap}
.cr{display:flex;gap:8px;align-items:flex-end}.cr .fg{flex:1}
.cs{margin-top:8px;padding:7px 10px;border-radius:6px;font-size:10px;font-family:monospace}
.cs.y{background:rgba(0,180,100,.05);border:1px solid rgba(0,180,100,.15);color:#00b464}
.cs.n{background:rgba(220,50,50,.05);border:1px solid rgba(220,50,50,.15);color:#dc3232}
.lc{background:rgba(0,0,0,.3);border:1px solid rgba(255,255,255,.04);border-radius:8px;height:320px;overflow-y:auto;font-family:monospace;font-size:11px;padding:10px;line-height:1.5}
.lc::-webkit-scrollbar{width:5px}.lc::-webkit-scrollbar-thumb{background:rgba(255,255,255,.1);border-radius:2px}
.le{display:flex;gap:8px}.lt{color:#444;white-space:nowrap;flex-shrink:0}.lm{flex:1;word-break:break-all}
.lm.info{color:#8b95a8}.lm.debug{color:#555}.lm.warn{color:#f4a261}.lm.error{color:#e94560}
.dg{display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:12px}
.dg .k{color:#555}.dg .v{font-weight:600}
@media(max-width:640px){.sg{grid-template-columns:repeat(2,1fr)}.ct{padding:10px}.hd{padding:10px 14px;flex-wrap:wrap;gap:6px}.ct2{flex-direction:column}.ct2 .bt{width:100%;justify-content:center}.cr{flex-direction:column}}
</style>
</head>
<body>
<div class="hd">
<div class="logo"><div>C4</div><div><h1>C4Coins Faucet Bot</h1><small>feyorra.top</small></div></div>
<div id="sp" class="pill off"><div class="dot"></div><span id="st">Idle</span></div>
</div>
<div class="ct">
<div class="cd"><h3>Statistics</h3>
<div class="sg">
<div class="si"><div class="sv a" id="vE">0.0000</div><div class="sl">Coins Earned</div></div>
<div class="si"><div class="sv b" id="vC">0</div><div class="sl">Claims</div></div>
<div class="si"><div class="sv c" id="vB">N/A</div><div class="sl">Balance</div></div>
<div class="si"><div class="sv d" id="vU">0s</div><div class="sl">Uptime</div></div>
</div></div>
<div class="cd"><h3>Cookie</h3>
<div class="cr"><div class="fg"><label>ci_session dari feyorra.top</label><input id="ci" placeholder="Paste cookie..."></div><button class="bt bp" onclick="sC()">Save</button></div>
<div id="cs"></div></div>
<div class="cd"><h3>Controls</h3>
<div class="ct2">
<button class="bt bs" onclick="sB()">&#9654; Start</button>
<button class="bt bd" onclick="tB()">&#9632; Stop</button>
<button class="bt bw" onclick="pB()">&#10074;&#10074; Pause</button>
<button class="bt bo" onclick="rB()">&#8635; Reset</button>
</div></div>
<div class="cd"><h3>Details</h3>
<div class="dg">
<div><span class="k">Captcha OK: </span><span class="v" style="color:#00b464" id="vO">0</span></div>
<div><span class="k">Captcha Fail: </span><span class="v" style="color:#e94560" id="vF">0</span></div>
<div><span class="k">Reconnects: </span><span class="v" style="color:#f4a261" id="vR">0</span></div>
<div><span class="k">Last Claim: </span><span class="v" style="color:#00b4d8" id="vL">-</span></div>
</div></div>
<div class="cd" style="padding-bottom:6px"><h3>Activity Log</h3><div class="lc" id="lb"></div></div>
</div>
<script>
var ll=0;
function a(u,o){try{var r=fetch(u,o);return r.then(function(x){return x.json()})}catch(e){return Promise.resolve(null)}}
function e(s){var d=document.createElement('div');d.textContent=s;return d.innerHTML}
function rf(){
a('/api/status').then(function(d){if(!d)return;
var p=document.getElementById('sp'),t=document.getElementById('st');
t.textContent=d.status;
p.className='pill '+(d.running&&!d.paused?'on':d.paused?'pau':d.status.toLowerCase().match(/error|expired/)?'err':'off');
document.getElementById('vE').textContent=d.earned.toFixed(4);
document.getElementById('vC').textContent=d.claims;
document.getElementById('vB').textContent=d.balance;
document.getElementById('vU').textContent=d.uptime;
document.getElementById('vO').textContent=d.captcha_solves;
document.getElementById('vF').textContent=d.captcha_fails;
document.getElementById('vR').textContent=d.connections_lost;
document.getElementById('vL').textContent=d.last_time||'-';
})}
function rl(){
a('/api/logs').then(function(l){if(!l||ll===l.length)return;ll=l.length;
var b=document.getElementById('lb');b.innerHTML='';
for(var i=0;i<l.length;i++){var r=document.createElement('div');r.className='le';
r.innerHTML='<span class="lt">'+l[i].time+'</span><span class="lm '+l[i].level+'">'+e(l[i].msg)+'</span>';
b.appendChild(r)}b.scrollTop=b.scrollHeight})}
function lc(){a('/api/cookie').then(function(d){if(!d)return;
var el=document.getElementById('cs');
if(d.has_cookie){el.className='cs y';el.textContent='Cookie aktif: '+d.cookie_preview}
else{el.className='cs n';el.textContent='Cookie belum di-set!'}})}
function sC(){var v=document.getElementById('ci').value.trim();if(!v){alert('Cookie wajib diisi!');return}
a('/api/cookie',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cookie:v})}).then(function(d){if(d&&d.ok){document.getElementById('ci').value='';lc()}else{alert(d?d.msg:'Gagal')}})}
function sB(){a('/api/bot/start',{method:'POST'}).then(function(d){if(d&&!d.ok)alert(d.msg)})}
function tB(){a('/api/bot/stop',{method:'POST'})}
function pB(){a('/api/bot/pause',{method:'POST'})}
function rB(){if(!confirm('Reset semua data?'))return;a('/api/bot/reset',{method:'POST'}).then(function(){lc()})}
setInterval(rf,1500);setInterval(rl,2000);rf();rl();lc();
</script>
</body>
</html>"""


@app.route("/")
def index():
    return Response(HTML, content_type="text/html")

@app.route("/health")
def health():
    return Response("OK", content_type="text/plain")

@app.route("/api/status")
def api_status():
    return jsonify(state.snapshot())

@app.route("/api/logs")
def api_logs():
    return jsonify(state.get_logs())

@app.route("/api/cookie", methods=["GET", "POST"])
def api_cookie():
    if request.method == "GET":
        c = state.cookie
        p = c[:20] + "..." if c and len(c) > 20 else c or ""
        return jsonify({"has_cookie": bool(c), "cookie_preview": p})
    data = request.get_json(force=True, silent=True) or {}
    ck = str(data.get("cookie", "")).strip()
    if not ck:
        return jsonify({"ok": False, "msg": "Cookie wajib diisi"}), 400
    state.cookie = ck
    state.save_config()
    state.add_log("Cookie updated")
    return jsonify({"ok": True, "msg": "Cookie tersimpan!"})

@app.route("/api/bot/start", methods=["POST"])
def api_start():
    if state.running:
        return jsonify({"ok": False, "msg": "Bot sudah jalan"})
    if not state.cookie:
        return jsonify({"ok": False, "msg": "Cookie belum di-set"})
    state.running = True
    state.paused = False
    state.captcha_fails = 0
    state.captcha_solves = 0
    state.connections_lost = 0
    threading.Thread(target=bot_loop, daemon=True).start()
    return jsonify({"ok": True, "msg": "Bot started"})

@app.route("/api/bot/stop", methods=["POST"])
def api_stop():
    state.running = False
    return jsonify({"ok": True, "msg": "Bot stopped"})

@app.route("/api/bot/pause", methods=["POST"])
def api_pause():
    state.paused = not state.paused
    state.set_status("Paused" if state.paused else "Resumed")
    state.add_log("Bot paused" if state.paused else "Bot resumed")
    return jsonify({"ok": True, "paused": state.paused})

@app.route("/api/bot/reset", methods=["POST"])
def api_reset():
    state.running = False
    state.total_earned = 0.0
    state.total_claims = 0
    state.captcha_fails = 0
    state.captcha_solves = 0
    state.connections_lost = 0
    state.last_claim_msg = ""
    state.last_claim_time = ""
    state.balance = "N/A"
    state.logs.clear()
    for f in [STATS_FILE, CONFIG_FILE]:
        if f.exists():
            f.unlink()
    state.cookie = ""
    state.add_log("Bot reset")
    return jsonify({"ok": True, "msg": "Bot reset"})


if __name__ == "__main__":
    log.info("C4Coins Web Bot starting on port %d", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
