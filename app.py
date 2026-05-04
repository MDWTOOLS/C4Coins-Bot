#!/usr/bin/env python3
"""C4Coins Auto Faucet Bot - Web Edition (Port 8080)"""

import os, re, time, json, random, logging, threading
from datetime import datetime
from pathlib import Path
import requests, cv2, numpy as np, pytesseract
from flask import Flask, request, jsonify, Response

BASE_URL = "https://feyorra.top"
PORT = int(os.environ.get("PORT", 8080))
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA_DIR / "config.json"
STATS_FILE = DATA_DIR / "stats.json"
LOG_FILE = DATA_DIR / "bot.log"
DEFAULT_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("c4coins")
log.setLevel(logging.DEBUG)
log.addHandler(logging.FileHandler(LOG_FILE, encoding="utf-8"))

class State:
    def __init__(self):
        self.running = False
        self.paused = False
        self.total_earned = 0.0
        self.total_claims = 0
        self.last_msg = ""
        self.last_time = ""
        self.status = "Idle"
        self.balance = "N/A"
        self.uptime_start = None
        self.cap_ok = 0
        self.cap_fail = 0
        self.reconnects = 0
        self.cookie = ""
        self.ua = DEFAULT_UA
        self.logs = []
        self.max_logs = 300
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE) as f:
                    c = json.load(f)
                self.cookie = c.get("cookie", "")
                self.ua = c.get("ua", DEFAULT_UA)
            except: pass
        if STATS_FILE.exists():
            try:
                with open(STATS_FILE) as f:
                    d = json.load(f)
                if d.get("date") == datetime.now().strftime("%Y-%m-%d"):
                    self.total_earned = d.get("earned", 0.0)
                    self.total_claims = d.get("claims", 0)
            except: pass

    def save_cfg(self):
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump({"cookie": self.cookie, "ua": self.ua}, f, indent=2)
        except: pass

    def save_stats(self):
        try:
            with open(STATS_FILE, "w") as f:
                json.dump({"date": datetime.now().strftime("%Y-%m-%d"), "earned": self.total_earned, "claims": self.total_claims}, f, indent=2)
        except: pass

    def log(self, msg, level="info"):
        ts = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self.logs.append({"time": ts, "msg": msg, "level": level})
            if len(self.logs) > self.max_logs:
                self.logs = self.logs[-self.max_logs:]
        lm = {"debug": logging.DEBUG, "info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}
        log.log(lm.get(level, logging.INFO), msg)

    def set_status(self, s):
        with self._lock:
            self.status = s

    def earned(self, amt, msg):
        with self._lock:
            self.total_earned += amt
            self.total_claims += 1
            self.last_msg = msg
            self.last_time = datetime.now().strftime("%H:%M:%S")
        self.log("+%.4f Coins - %s" % (amt, msg))
        self.save_stats()

    @property
    def uptime(self):
        if not self.uptime_start: return "0s"
        s = int(time.time() - self.uptime_start)
        d, s = divmod(s, 86400); h, s = divmod(s, 3600); m, s = divmod(s, 60)
        p = []
        if d: p.append("%dd" % d)
        if h: p.append("%dh" % h)
        if m: p.append("%dm" % m)
        p.append("%ds" % s)
        return " ".join(p)

    def get_logs(self):
        with self._lock: return list(self.logs)

    def snap(self):
        with self._lock:
            return {"running": self.running, "paused": self.paused, "status": self.status,
                "balance": self.balance, "earned": self.total_earned, "claims": self.total_claims,
                "last_msg": self.last_msg, "last_time": self.last_time, "uptime": self.uptime,
                "cap_ok": self.cap_ok, "cap_fail": self.cap_fail, "reconnects": self.reconnects,
                "has_cookie": bool(self.cookie)}

S = State()

def mk_sess():
    s = requests.Session()
    a = requests.adapters.HTTPAdapter(max_retries=3, pool_connections=5, pool_maxsize=5)
    s.mount("https://", a); s.mount("http://", a)
    return s

def h_get(ck, ua):
    return {"User-Agent": ua, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9", "Referer": BASE_URL+"/dashboard", "Cookie": ck}

def h_post(ck, ua):
    return {"User-Agent": ua, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9", "Referer": BASE_URL+"/faucet", "Cookie": ck,
            "Origin": BASE_URL, "Content-Type": "application/x-www-form-urlencoded"}

def h_img(ck, ua):
    return {"User-Agent": ua, "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": BASE_URL+"/faucet", "Cookie": ck}

def solve_cap(data):
    try:
        if len(data) < 50: return None
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None: return None
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        _, t = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        c = cv2.morphologyEx(t, cv2.MORPH_OPEN, np.ones((2,2), np.uint8))
        cnts, _ = cv2.findContours(c, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes = sorted([(cv2.boundingRect(x), x) for x in cnts if cv2.boundingRect(x)[2] > 4 and cv2.boundingRect(x)[3] > 10], key=lambda b: b[0][0])
        if len(boxes) < 2: return None
        cfg = r"--oem 3 --psm 10 -c tessedit_char_whitelist=0123456789"
        res = ""
        for i, (b, _) in enumerate(boxes):
            if i == 0: continue
            x,y,w,h = b
            roi = c[y:y+h, x:x+w]
            if roi.size == 0: continue
            roi = cv2.copyMakeBorder(roi,10,10,10,10,cv2.BORDER_CONSTANT,value=0)
            roi = cv2.bitwise_not(roi)
            roi = cv2.resize(roi, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
            _, roi = cv2.threshold(roi, 150, 255, cv2.THRESH_BINARY)
            txt = pytesseract.image_to_string(roi, config=cfg).strip()
            if txt.isdigit(): res += txt
            if len(res) == 4: break
        return res if len(res) == 4 else None
    except Exception as e:
        log.error("Cap error: %s", e)
        return None

def parse_ok(h):
    for p in [r'title:\s*[\'"]([^\'"]+)[\'"]', r"([\d\.]+\s+Coins\s+has been added to your balance)", r"([\d\.]+\s+[A-Z]+\s+added to[^\']+)"]:
        m = re.search(p, h, re.I)
        if m: return m.group(1)
    return None

def parse_wait(h):
    m = re.search(r"let wait = (\d+)", h)
    return int(m.group(1)) if m else 180

def parse_bal(h):
    m = re.search(r"<p>(.*?)</p>", h)
    return m.group(1) if m else None

def pick_a_box(sess, hdrs):
    S.log("Memulai Pick-a-Box game...")
    for r in range(1, 6):
        if not S.running: break
        try:
            pg = sess.get(BASE_URL+"/pickabox", headers=hdrs, timeout=30).text
            csrf = re.search(r'name="csrf_token_name" value="([^"]+)"', pg)
            tok = re.search(r'name="token" value="([^"]+)"', pg)
            grd = re.search(r'name="game_guard" value="([^"]+)"', pg)
            if not all([csrf, tok, grd]): continue
            ph = hdrs.copy(); ph["Content-Type"] = "application/x-www-form-urlencoded"; ph["Origin"] = BASE_URL; ph["Referer"] = BASE_URL+"/pickabox"
            sess.post(BASE_URL+"/pickabox/play", data={"csrf_token_name":csrf.group(1),"token":tok.group(1),"game_guard":grd.group(1),"bet_amount":1,"selected_box":random.randint(1,3)}, headers=ph, timeout=30)
            S.log("Pick-a-Box round %d/5 selesai" % r, "debug")
            if r < 5: time.sleep(2)
        except: break
    try:
        b = parse_bal(sess.get(BASE_URL+"/dashboard", headers=hdrs, timeout=30).text)
        if b: S.balance = b
    except: pass
    S.log("Pick-a-Box selesai, balance: %s" % (S.balance or "N/A"))

def bot_loop():
    sess = mk_sess()
    ck, ua, hdrs = S.cookie, S.ua, h_get(S.cookie, S.ua)
    S.log("Bot dimulai")
    S.uptime_start = time.time()
    attempt = 0

    while S.running:
        if S.paused:
            time.sleep(1)
            continue
        try:
            attempt += 1
            S.set_status("Memeriksa session...")
            S.log("[Attempt #%d] Memeriksa session..." % attempt)

            try:
                resp = sess.get(BASE_URL+"/dashboard", headers=hdrs, timeout=30)
                if resp.status_code != 200:
                    S.log("Dashboard HTTP %d, retry..." % resp.status_code, "warn")
                    time.sleep(10); sess = mk_sess(); continue
                if "dashboard" not in resp.text.lower():
                    S.set_status("Session expired!")
                    S.log("Session expired! Silakan update cookie.", "error")
                    S.running = False; break
                bal = parse_bal(resp.text)
                if bal: S.balance = bal
                S.set_status("Session OK")
                S.log("Session valid | Balance: %s" % (bal or "N/A"))
            except requests.Timeout:
                S.reconnects += 1; S.log("Request timeout, reconnect...", "error"); time.sleep(5); sess = mk_sess(); continue
            except requests.RequestException as e:
                S.reconnects += 1; S.log("Koneksi error: %s" % str(e)[:80], "error"); time.sleep(5); sess = mk_sess(); continue

            S.set_status("Memuat faucet...")
            S.log("Memuat halaman faucet...")
            try:
                resp = sess.get(BASE_URL+"/faucet", headers=hdrs, timeout=30)
                page = resp.text
            except requests.RequestException as e:
                S.log("Gagal muat faucet: %s" % str(e)[:80], "error"); time.sleep(5); continue

            if "daily limit" in page.lower() or "limit reached" in page.lower():
                S.set_status("Daily limit!")
                S.log("Daily limit tercapai! Bot berhenti.", "warn"); break

            if "shortlink" in page.lower():
                S.set_status("Shortlink!")
                S.log("Shortlink diperlukan. Bot berhenti.", "warn"); break

            if "Ready To Claim" in page:
                S.log("Faucet siap claim, memparsing form...")
                csrf = re.search(r'name="csrf_token_name"[^>]*value="([^"]+)"', page)
                tok = re.search(r'name="token"[^>]*value="([^"]+)"', page)
                img = re.search(r'<img[^>]*id="Imageid"[^>]*src="([^"]+)"', page)
                if not img: img = re.search(r'<img[^>]*src="([^"]*captcha[^"]*)"', page, re.I)
                fld = re.search(r'<input[^>]*type="number"[^>]*name="([^"]+)"', page)

                if not all([csrf, tok, img, fld]):
                    S.log("Form parse gagal (csrf=%s tok=%s img=%s fld=%s), retry..." % ("Y" if csrf else "N","Y" if tok else "N","Y" if img else "N","Y" if fld else "N"), "warn")
                    time.sleep(3); continue

                S.log("Form berhasil diparse, mengunduh captcha...")
                S.set_status("Mengunduh captcha...")
                img_url = img.group(1)
                if not img_url.startswith("http"): img_url = BASE_URL + "/" + img_url.lstrip("/")
                try:
                    img_resp = sess.get(img_url, headers=h_img(ck, ua), timeout=30)
                    S.log("Captcha diunduh (%d bytes)" % len(img_resp.content), "debug")
                except requests.RequestException as e:
                    S.log("Gagal unduh captcha: %s" % str(e)[:60], "error"); time.sleep(3); continue

                if len(img_resp.content) < 100:
                    S.log("Captcha kosong, bermain Pick-a-Box...")
                    pick_a_box(sess, hdrs); time.sleep(2); continue

                S.set_status("Memecahkan captcha...")
                S.log("Memecahkan captcha (OCR)...")
                t0 = time.time()
                digits = solve_cap(img_resp.content)
                t1 = time.time() - t0

                if not digits:
                    S.cap_fail += 1
                    S.log("Captcha GAGAL (%.1fs) | Total fail: %d" % (t1, S.cap_fail), "warn")
                    time.sleep(2); continue

                S.cap_ok += 1
                S.log("Captcha BERHASIL: %s (%.1fs) | OK: %d | Fail: %d" % (digits, t1, S.cap_ok, S.cap_fail))

                S.set_status("Mengirim claim...")
                S.log("Mengirim claim dengan kode: %s..." % digits)
                try:
                    cr = sess.post(BASE_URL+"/faucet/verify",
                        data={"csrf_token_name": csrf.group(1), "token": tok.group(1), fld.group(1): digits},
                        headers=h_post(ck, ua), allow_redirects=False, timeout=30)
                    S.log("Claim response: HTTP %d" % cr.status_code, "debug")
                except requests.RequestException as e:
                    S.log("Claim gagal: %s" % str(e)[:60], "error"); time.sleep(3); continue

                time.sleep(2)

                try:
                    resp = sess.get(BASE_URL+"/faucet", headers=hdrs, timeout=30)
                    page = resp.text
                except: time.sleep(3); continue

                msg = parse_ok(page)
                if msg:
                    amt_m = re.search(r"([\d\.]+)\s+Coins", msg)
                    amount = float(amt_m.group(1)) if amt_m else 0.001
                    S.earned(amount, msg)
                    S.log("CLAIM BERHASIL! +%s coins | Total: %.4f | Claims: %d" % (("%.4f" % amount), S.total_earned, S.total_claims))

                    try:
                        b = parse_bal(sess.get(BASE_URL+"/dashboard", headers=hdrs, timeout=30).text)
                        if b: S.balance = b
                    except: pass

                    wait = parse_wait(page)
                    S.set_status("Cooldown %ds" % wait)
                    S.log("Cooldown %d detik... (balance: %s)" % (wait, S.balance or "N/A"))

                    for sec in range(wait):
                        if not S.running or S.paused: break
                        if sec % 10 == 0 and sec > 0:
                            S.log("Cooldown: %d/%d detik tersisa" % (wait - sec, wait), "debug")
                        time.sleep(1)
                    S.log("Cooldown selesai, claim berikutnya...")
                else:
                    if "incorrect" in page.lower() or "wrong" in page.lower():
                        S.log("Captcha SALAH! Coba lagi...", "error")
                    else:
                        S.log("Claim gagal, halaman tidak mengkonfirmasi. Retry...", "warn")
                    time.sleep(3)
            else:
                wait = parse_wait(page)
                S.set_status("Menunggu %ds" % wait)
                S.log("Faucet belum siap, menunggu %d detik..." % wait)
                for sec in range(wait):
                    if not S.running or S.paused: break
                    time.sleep(1)
                S.log("Selesai menunggu, mencoba claim lagi...")

        except requests.ConnectionError:
            S.reconnects += 1
            S.set_status("Reconnecting...")
            S.log("Koneksi terputus! Reconnect... (%d)" % S.reconnects, "error")
            time.sleep(10); sess = mk_sess(); hdrs = h_get(ck, ua)
        except Exception as e:
            log.error("Loop error: %s", e)
            S.set_status("Error"); S.log("Error: %s" % str(e)[:80], "error"); time.sleep(5)

    S.set_status("Stopped")
    S.log("Bot berhenti | Total claim: %d | Total earned: %.4f coins | Uptime: %s" % (S.total_claims, S.total_earned, S.uptime))

# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)

@app.route("/")
def index():
    return Response(HTML_PAGE, content_type="text/html")

@app.route("/health")
def health():
    return Response("OK")

@app.route("/api/status")
def api_status():
    return jsonify(S.snap())

@app.route("/api/logs")
def api_logs():
    return jsonify(S.get_logs())

@app.route("/api/cookie", methods=["GET","POST"])
def api_cookie():
    if request.method == "GET":
        c = S.cookie
        return jsonify({"has_cookie": bool(c), "preview": (c[:20]+"...") if c and len(c)>20 else c or ""})
    d = request.get_json(force=True, silent=True) or {}
    ck = str(d.get("cookie","")).strip()
    if not ck: return jsonify({"ok":False,"msg":"Cookie wajib diisi"}), 400
    S.cookie = ck; S.save_cfg(); S.log("Cookie diperbarui")
    return jsonify({"ok":True,"msg":"Cookie tersimpan!"})

@app.route("/api/bot/start", methods=["POST"])
def api_start():
    if S.running: return jsonify({"ok":False,"msg":"Bot sudah jalan"})
    if not S.cookie: return jsonify({"ok":False,"msg":"Cookie belum di-set"})
    S.running = True; S.paused = False; S.cap_fail = 0; S.cap_ok = 0; S.reconnects = 0
    threading.Thread(target=bot_loop, daemon=True).start()
    return jsonify({"ok":True,"msg":"Bot started"})

@app.route("/api/bot/stop", methods=["POST"])
def api_stop():
    S.running = False
    return jsonify({"ok":True,"msg":"Bot stopped"})

@app.route("/api/bot/pause", methods=["POST"])
def api_pause():
    S.paused = not S.paused
    S.set_status("Paused" if S.paused else "Resumed")
    S.log("Bot %s" % ("dijeda" if S.paused else "dilanjutkan"))
    return jsonify({"ok":True,"paused":S.paused})

@app.route("/api/bot/reset", methods=["POST"])
def api_reset():
    S.running = False; S.total_earned = 0; S.total_claims = 0; S.cap_fail = 0; S.cap_ok = 0
    S.reconnects = 0; S.last_msg = ""; S.last_time = ""; S.balance = "N/A"; S.logs.clear()
    for f in [STATS_FILE, CONFIG_FILE]:
        if f.exists(): f.unlink()
    S.cookie = ""; S.log("Bot direset")
    return jsonify({"ok":True,"msg":"Bot reset"})

# ============================================================
# HTML PAGE
# ============================================================

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>C4Coins Faucet Bot</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:#0a0c13;color:#c8cdd8;min-height:100vh}
.hdr{background:linear-gradient(180deg,#111827 0%,#0f1420 100%);border-bottom:1px solid rgba(255,255,255,.06);padding:14px 20px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.logo{display:flex;align-items:center;gap:12px}
.logo-icon{width:34px;height:34px;background:linear-gradient(135deg,#f59e0b,#ef4444);border-radius:9px;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:800;font-size:13px;letter-spacing:-0.5px}
.logo-text h1{font-size:16px;color:#f1f5f9;font-weight:700;line-height:1.2}
.logo-text span{font-size:10px;color:#4b5563;display:block}
.badge{display:inline-flex;align-items:center;gap:6px;padding:5px 14px;border-radius:20px;font-size:11px;font-weight:600;border:1px solid}
.badge-dot{width:7px;height:7px;border-radius:50%}
.b-on{background:rgba(34,197,94,.1);border-color:rgba(34,197,94,.3);color:#22c55e}
.b-on .badge-dot{background:#22c55e;animation:blink 1.5s infinite}
.b-off{background:rgba(75,85,99,.1);border-color:rgba(75,85,99,.2);color:#6b7280}
.b-off .badge-dot{background:#6b7280}
.b-err{background:rgba(239,68,68,.1);border-color:rgba(239,68,68,.3);color:#ef4444}
.b-err .badge-dot{background:#ef4444}
.b-warn{background:rgba(245,158,11,.1);border-color:rgba(245,158,11,.3);color:#f59e0b}
.b-warn .badge-dot{background:#f59e0b}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.25}}
.wrap{max-width:880px;margin:0 auto;padding:16px}
.card{background:#111827;border:1px solid rgba(255,255,255,.05);border-radius:14px;padding:18px;margin-bottom:14px}
.card-title{font-size:11px;font-weight:700;color:#4b5563;text-transform:uppercase;letter-spacing:.8px;margin-bottom:14px;display:flex;align-items:center;gap:6px}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.stat{text-align:center;padding:14px 8px;background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.04);border-radius:10px}
.stat .val{font-size:22px;font-weight:800;line-height:1.2}
.stat .lbl{font-size:10px;color:#4b5563;margin-top:4px;text-transform:uppercase;letter-spacing:.3px}
.c1 .val{color:#f59e0b}.c2 .val{color:#22c55e}.c3 .val{color:#3b82f6}.c4 .val{color:#8b5cf6}
.form-row{display:flex;gap:10px;align-items:flex-end}
.form-group{flex:1}
.form-group label{display:block;font-size:11px;color:#4b5563;margin-bottom:5px;font-weight:600}
.form-group input{width:100%;padding:10px 14px;background:#0a0c13;border:1px solid rgba(255,255,255,.08);border-radius:8px;color:#e5e7eb;font-size:12px;outline:none;font-family:'Courier New',monospace;transition:border .2s}
.form-group input:focus{border-color:#3b82f6}
.form-group input::placeholder{color:#374151}
.btn{padding:10px 18px;border:none;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;transition:all .15s;display:inline-flex;align-items:center;gap:6px;letter-spacing:.3px}
.btn:active{transform:scale(.97)}
.btn-go{background:linear-gradient(135deg,#22c55e,#16a34a);color:#fff}
.btn-go:hover{box-shadow:0 4px 15px rgba(34,197,94,.3)}
.btn-stop{background:linear-gradient(135deg,#ef4444,#dc2626);color:#fff}
.btn-stop:hover{box-shadow:0 4px 15px rgba(239,68,68,.3)}
.btn-pause{background:linear-gradient(135deg,#f59e0b,#d97706);color:#fff}
.btn-pause:hover{box-shadow:0 4px 15px rgba(245,158,11,.3)}
.btn-reset{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.08);color:#6b7280}
.btn-reset:hover{border-color:rgba(255,255,255,.15);color:#9ca3af}
.btn-save{background:linear-gradient(135deg,#3b82f6,#2563eb);color:#fff}
.btn-save:hover{box-shadow:0 4px 15px rgba(59,130,246,.3)}
.btns{display:flex;gap:8px;flex-wrap:wrap}
.cookie-info{margin-top:10px;padding:8px 12px;border-radius:8px;font-size:11px;font-family:'Courier New',monospace}
.cookie-ok{background:rgba(34,197,94,.06);border:1px solid rgba(34,197,94,.15);color:#22c55e}
.cookie-no{background:rgba(239,68,68,.06);border:1px solid rgba(239,68,68,.15);color:#ef4444}
.detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:12px}
.detail-grid .row{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid rgba(255,255,255,.03)}
.detail-grid .row:last-child{border-bottom:none}
.detail-grid .k{color:#4b5563}.detail-grid .v{font-weight:700}
.log-box{background:#080a10;border:1px solid rgba(255,255,255,.04);border-radius:10px;height:380px;overflow-y:auto;padding:10px;font-family:'Courier New',monospace;font-size:11px;line-height:1.6}
.log-box::-webkit-scrollbar{width:5px}
.log-box::-webkit-scrollbar-track{background:transparent}
.log-box::-webkit-scrollbar-thumb{background:rgba(255,255,255,.08);border-radius:3px}
.log-box::-webkit-scrollbar-thumb:hover{background:rgba(255,255,255,.15)}
.log-line{display:flex;gap:10px;padding:1px 0}
.log-ts{color:#374151;white-space:nowrap;flex-shrink:0;min-width:62px}
.log-msg{flex:1;word-break:break-word}
.log-msg.info{color:#6b7280}
.log-msg.debug{color:#374151}
.log-msg.warn{color:#f59e0b}
.log-msg.error{color:#ef4444}
.log-empty{text-align:center;color:#374151;padding:40px 20px;font-size:12px}
@media(max-width:640px){.grid4{grid-template-columns:repeat(2,1fr)}.wrap{padding:10px}.hdr{padding:10px 14px}.btns{flex-direction:column}.btns .btn{width:100%;justify-content:center}.form-row{flex-direction:column}.detail-grid{grid-template-columns:1fr}}
</style>
</head>
<body>

<div class="hdr">
  <div class="logo">
    <div class="logo-icon">C4</div>
    <div class="logo-text">
      <h1>C4Coins Faucet Bot</h1>
      <span>feyorra.top &middot; Auto Claim</span>
    </div>
  </div>
  <div id="badge" class="badge b-off">
    <div class="badge-dot"></div>
    <span id="badgeTxt">Idle</span>
  </div>
</div>

<div class="wrap">

  <div class="card">
    <div class="card-title">&#128200; Statistics</div>
    <div class="grid4">
      <div class="stat c1"><div class="val" id="sEarned">0.0000</div><div class="lbl">Earned</div></div>
      <div class="stat c2"><div class="val" id="sClaims">0</div><div class="lbl">Claims</div></div>
      <div class="stat c3"><div class="val" id="sBal">N/A</div><div class="lbl">Balance</div></div>
      <div class="stat c4"><div class="val" id="sUp">0s</div><div class="lbl">Uptime</div></div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">&#127873; Cookie</div>
    <div class="form-row">
      <div class="form-group">
        <label>ci_session dari feyorra.top</label>
        <input type="text" id="cookieIn" placeholder="Paste cookie di sini...">
      </div>
      <button class="btn btn-save" onclick="saveCookie()">&#128190; Save</button>
    </div>
    <div id="cookieInfo"></div>
  </div>

  <div class="card">
    <div class="card-title">&#9881; Controls</div>
    <div class="btns">
      <button class="btn btn-go" onclick="startBot()">&#9654; Start</button>
      <button class="btn btn-stop" onclick="stopBot()">&#9632; Stop</button>
      <button class="btn btn-pause" onclick="pauseBot()">&#10074;&#10074; Pause</button>
      <button class="btn btn-reset" onclick="resetBot()">&#8635; Reset</button>
    </div>
  </div>

  <div class="card">
    <div class="card-title">&#128202; Details</div>
    <div class="detail-grid">
      <div class="row"><span class="k">Captcha OK</span><span class="v" style="color:#22c55e" id="dCapOk">0</span></div>
      <div class="row"><span class="k">Captcha Fail</span><span class="v" style="color:#ef4444" id="dCapFail">0</span></div>
      <div class="row"><span class="k">Reconnects</span><span class="v" style="color:#f59e0b" id="dReconn">0</span></div>
      <div class="row"><span class="k">Last Claim</span><span class="v" style="color:#3b82f6" id="dLastClaim">-</span></div>
      <div class="row"><span class="k">Last Reward</span><span class="v" style="color:#8b5cf6" id="dLastReward">-</span></div>
      <div class="row"><span class="k">Status</span><span class="v" id="dStatus" style="color:#6b7280">Idle</span></div>
    </div>
  </div>

  <div class="card" style="padding-bottom:8px">
    <div class="card-title">&#128220; Activity Log</div>
    <div class="log-box" id="logBox"><div class="log-empty">Menunggu aktivitas bot...</div></div>
  </div>

</div>

<script>
var lastLen = 0;

function api(url, opts) {
  return fetch(url, opts).then(function(r) { return r.json(); }).catch(function(e) { console.error(e); return null; });
}

function esc(s) {
  var d = document.createElement('span');
  d.textContent = s;
  return d.innerHTML;
}

function updateStatus() {
  api('/api/status').then(function(d) {
    if (!d) return;
    var badge = document.getElementById('badge');
    var txt = document.getElementById('badgeTxt');
    txt.textContent = d.status;
    if (d.running && !d.paused) badge.className = 'badge b-on';
    else if (d.paused) badge.className = 'badge b-warn';
    else if (d.status.toLowerCase().indexOf('error') >= 0 || d.status.toLowerCase().indexOf('expired') >= 0) badge.className = 'badge b-err';
    else badge.className = 'badge b-off';

    document.getElementById('sEarned').textContent = d.earned.toFixed(4);
    document.getElementById('sClaims').textContent = d.claims;
    document.getElementById('sBal').textContent = d.balance;
    document.getElementById('sUp').textContent = d.uptime;
    document.getElementById('dCapOk').textContent = d.cap_ok;
    document.getElementById('dCapFail').textContent = d.cap_fail;
    document.getElementById('dReconn').textContent = d.reconnects;
    document.getElementById('dLastClaim').textContent = d.last_time || '-';
    document.getElementById('dLastReward').textContent = d.last_msg || '-';
    document.getElementById('dStatus').textContent = d.status;
  });
}

function updateLogs() {
  api('/api/logs').then(function(logs) {
    if (!logs || !logs.length) return;
    if (logs.length === lastLen) return;
    lastLen = logs.length;
    var box = document.getElementById('logBox');
    var html = '';
    for (var i = 0; i < logs.length; i++) {
      var l = logs[i];
      html += '<div class="log-line">';
      html += '<span class="log-ts">' + esc(l.time) + '</span>';
      html += '<span class="log-msg ' + l.level + '">' + esc(l.msg) + '</span>';
      html += '</div>';
    }
    box.innerHTML = html;
    box.scrollTop = box.scrollHeight;
  });
}

function loadCookie() {
  api('/api/cookie').then(function(d) {
    if (!d) return;
    var el = document.getElementById('cookieInfo');
    if (d.has_cookie) {
      el.className = 'cookie-info cookie-ok';
      el.textContent = 'Cookie aktif: ' + d.preview;
    } else {
      el.className = 'cookie-info cookie-no';
      el.textContent = 'Cookie belum di-set! Paste cookie dari feyorra.top.';
    }
  });
}

function saveCookie() {
  var v = document.getElementById('cookieIn').value.trim();
  if (!v) { alert('Cookie wajib diisi!'); return; }
  api('/api/cookie', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({cookie: v})
  }).then(function(d) {
    if (d && d.ok) {
      document.getElementById('cookieIn').value = '';
      loadCookie();
    } else {
      alert(d ? d.msg : 'Gagal menyimpan cookie');
    }
  });
}

function startBot() {
  api('/api/bot/start', {method: 'POST'}).then(function(d) {
    if (d && !d.ok) alert(d.msg);
  });
}

function stopBot() {
  api('/api/bot/stop', {method: 'POST'});
}

function pauseBot() {
  api('/api/bot/pause', {method: 'POST'});
}

function resetBot() {
  if (!confirm('Reset semua data dan cookie?')) return;
  api('/api/bot/reset', {method: 'POST'}).then(function() { loadCookie(); });
}

setInterval(updateStatus, 1500);
setInterval(updateLogs, 1500);
updateStatus();
updateLogs();
loadCookie();
</script>
</body>
</html>"""

if __name__ == "__main__":
    log.info("C4Coins Web Bot starting on port %d", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
