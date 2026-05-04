#!/usr/bin/env python3
"""C4Coins Auto Faucet Bot - Web Edition (Port 8080)"""

import os, re, time, json, random, logging, threading
from datetime import datetime
from pathlib import Path
from collections import Counter
import requests, cv2, numpy as np, pytesseract
from flask import Flask, request, jsonify, Response

BASE_URL = "https://feyorra.top"
PORT = int(os.environ.get("PORT", 8080))
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA_DIR / "config.json"
STATS_FILE = DATA_DIR / "stats.json"
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("c4coins")

# ============================================================
# STATE
# ============================================================

class State:
    def __init__(self):
        self.running = False
        self.paused = False
        self.total_earned = 0.0
        self.total_claims = 0
        self.total_failed = 0
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
        self.max_logs = 500
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
                json.dump({"date": datetime.now().strftime("%Y-%m-%d"),
                           "earned": self.total_earned, "claims": self.total_claims}, f, indent=2)
        except: pass

    def add_log(self, msg, level="info"):
        ts = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self.logs.append({"time": ts, "msg": msg, "level": level})
            if len(self.logs) > self.max_logs:
                self.logs = self.logs[-self.max_logs:]
        log.info("[%s] %s", level.upper(), msg)

    def set_status(self, s):
        with self._lock:
            self.status = s

    def record_earned(self, amt, msg):
        with self._lock:
            self.total_earned += amt
            self.total_claims += 1
            self.last_msg = msg
            self.last_time = datetime.now().strftime("%H:%M:%S")
        self.add_log("+%.4f Coins | %s" % (amt, msg))
        self.save_stats()

    @property
    def uptime(self):
        if not self.uptime_start:
            return "0s"
        s = int(time.time() - self.uptime_start)
        d, s = divmod(s, 86400)
        h, s = divmod(s, 3600)
        m, s = divmod(s, 60)
        p = []
        if d: p.append("%dd" % d)
        if h: p.append("%dh" % h)
        if m: p.append("%dm" % m)
        p.append("%ds" % s)
        return " ".join(p)

    def get_logs(self):
        with self._lock:
            return list(self.logs)

    def snap(self):
        with self._lock:
            return {
                "running": self.running, "paused": self.paused, "status": self.status,
                "balance": self.balance, "earned": round(self.total_earned, 4),
                "claims": self.total_claims, "failed": self.total_failed,
                "last_msg": self.last_msg, "last_time": self.last_time, "uptime": self.uptime,
                "cap_ok": self.cap_ok, "cap_fail": self.cap_fail, "reconnects": self.reconnects,
                "has_cookie": bool(self.cookie)
            }

S = State()

# ============================================================
# HTTP HELPERS
# ============================================================

def _headers(referer):
    """Build request headers with cookie string."""
    return {
        "User-Agent": S.ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
        "Cookie": S.cookie,
    }

def _post_headers(referer):
    h = _headers(referer)
    h["Origin"] = BASE_URL
    h["Content-Type"] = "application/x-www-form-urlencoded"
    return h

def get_page(url, referer, timeout=30):
    """GET a page, return (text, is_ok, reason). is_ok: True=ok, False=expired, None=network error."""
    try:
        r = requests.get(url, headers=_headers(referer), timeout=timeout, allow_redirects=False)
        if r.status_code in (301, 302, 307, 308):
            loc = r.headers.get("Location", "")
            # Any redirect away from expected path = session expired
            if "/login" in loc or loc.endswith(BASE_URL + "/"):
                return None, False, "Session expired (redirect ke %s)" % loc
            # Follow redirect once (some pages do 307 -> self)
            target = loc if loc.startswith("http") else BASE_URL + loc
            r2 = requests.get(target, headers=_headers(referer), timeout=timeout, allow_redirects=False)
            if r2.status_code in (301, 302, 307, 308):
                return None, False, "Session expired (double redirect)"
            if r2.status_code != 200:
                return None, False, "HTTP %d" % r2.status_code
            return r2.text, True, "OK"
        if r.status_code != 200:
            return None, False, "HTTP %d" % r.status_code
        return r.text, True, "OK"
    except requests.Timeout:
        return None, None, "Timeout"
    except requests.ConnectionError:
        return None, None, "Connection lost"
    except requests.RequestException as e:
        return None, None, str(e)[:80]

# ============================================================
# CAPTCHA SOLVER
# ============================================================

def _solve_captcha(data):
    """Solve captcha using voting across 24+ OCR passes. Returns (digits, confidence)."""
    if len(data) < 50:
        return None, 0
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None, 0
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    all_results = []

    # Strategy 1: Full-image OCR with binary thresholds
    for th in [80, 100, 127, 150]:
        _, binary = cv2.threshold(g, th, 255, cv2.THRESH_BINARY)
        scaled = cv2.resize(binary, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
        scaled = cv2.medianBlur(scaled, 3)
        for psm in [6, 7, 8]:
            try:
                txt = pytesseract.image_to_string(scaled,
                    config=r"--oem 3 --psm %d -c tessedit_char_whitelist=0123456789" % psm).strip()
                digits = "".join(c for c in txt if c.isdigit())
                if len(digits) == 4:
                    all_results.append(digits)
            except: pass

    # Strategy 2: Inverted binary
    for th in [80, 100, 127, 150]:
        _, binary = cv2.threshold(g, th, 255, cv2.THRESH_BINARY_INV)
        scaled = cv2.resize(binary, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
        scaled = cv2.medianBlur(scaled, 3)
        for psm in [6, 7, 8]:
            try:
                txt = pytesseract.image_to_string(scaled,
                    config=r"--oem 3 --psm %d -c tessedit_char_whitelist=0123456789" % psm).strip()
                digits = "".join(c for c in txt if c.isdigit())
                if len(digits) == 4:
                    all_results.append(digits)
            except: pass

    # Strategy 3: Contour-based digit isolation
    try:
        _, bw = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        kernel = np.ones((2, 2), np.uint8)
        bw = cv2.morphologyEx(bw, cv2.MORPH_OPEN, kernel)
        cnts, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes = sorted([cv2.boundingRect(x) for x in cnts], key=lambda b: b[0])
        digit_boxes = [(x, y, w, h) for x, y, w, h in boxes if h > 8 and w > 2 and h / w < 5 and h * w > 40]
        if len(digit_boxes) >= 4:
            sel = digit_boxes
            if sel[0][0] < 10 and len(sel) > 4:
                sel = sel[1:]
            sel = sel[:4]
            cfg = r"--oem 3 --psm 10 -c tessedit_char_whitelist=0123456789"
            res = ""
            for x, y, w, h in sel:
                roi = bw[y:y + h, x:x + w]
                if roi.size == 0: continue
                if roi.mean() < 127:
                    roi = cv2.bitwise_not(roi)
                roi = cv2.copyMakeBorder(roi, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=0)
                roi = cv2.resize(roi, None, fx=5, fy=5, interpolation=cv2.INTER_CUBIC)
                _, roi = cv2.threshold(roi, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                txt = pytesseract.image_to_string(roi, config=cfg).strip()
                if txt.isdigit():
                    res += txt
                elif any(c.isdigit() for c in txt):
                    res += "".join(c for c in txt if c.isdigit())
                if len(res) >= 4: break
            if len(res) >= 4:
                all_results.append(res[:4])
    except: pass

    if not all_results:
        return None, 0
    votes = Counter(all_results)
    best, count = votes.most_common(1)[0]
    return best, count

# Shorthand that returns just the digits (for compatibility)
def solve_captcha(data):
    digits, confidence = _solve_captcha(data)
    return digits

# Full solver with confidence
def solve_captcha_confident(data, min_votes=2):
    """Solve captcha, only return result if confidence >= min_votes."""
    digits, confidence = _solve_captcha(data)
    if digits and confidence >= min_votes:
        return digits
    return None

# ============================================================
# PARSERS
# ============================================================

def parse_balance(html):
    """Extract balance from dashboard page."""
    for p in [r"<p>([\d.]+\s*Coins?)</p>",
              r">\s*([\d.]+)\s*Coins?\s*<",
              r"(?:Balance|Available|Earnings?)[^\d]{0,30}([\d.]+)",
              r"([\d.]+)\s*(?:Coins?|FEY|USD)"]:
        m = re.search(p, html, re.I)
        if m:
            val = m.group(1).strip()
            try:
                float(val)
                return val
            except: pass
    # Fallback: find any number followed by "Coins"
    all_m = re.findall(r"([\d.]+)\s*Coins?", html, re.I)
    for m in all_m:
        try:
            v = float(m)
            if v > 0:
                return m + " Coins"
        except: pass
    return None

def parse_wait(html):
    m = re.search(r"let wait = (\d+)", html)
    return int(m.group(1)) if m else None

def parse_form(page):
    """Extract form fields from faucet page."""
    csrf = re.search(r'name="csrf_token_name"[^>]*value="([^"]+)"', page)
    tok = re.search(r'name="token"[^>]*value="([^"]+)"', page)
    img = re.search(r'<img[^>]*id="Imageid"[^>]*src="([^"]+)"', page)
    if not img:
        img = re.search(r'<img[^>]*src="([^"]*captcha[^"]*)"', page, re.I)
    fld = re.search(r'<input[^>]*type="number"[^>]*name="([^"]+)"', page)
    return csrf, tok, img, fld

# ============================================================
# BOT LOOP
# ============================================================

def bot_loop():
    S.add_log("Bot dimulai", "info")
    S.uptime_start = time.time()
    attempt = 0
    cap_fail_streak = 0

    while S.running:
        if S.paused:
            time.sleep(1)
            continue
        try:
            attempt += 1

            # === STEP 1: CHECK SESSION (dashboard) ===
            S.set_status("Memeriksa session...")
            S.add_log("[Attempt #%d] Memeriksa session..." % attempt, "info")

            dash, ok, reason = get_page(BASE_URL + "/dashboard", BASE_URL + "/")
            if ok is False:
                S.set_status("Session Expired!")
                S.add_log("SESSION EXPIRED! %s" % reason, "error")
                S.add_log("Silakan update cookie baru dari feyorra.top", "error")
                S.running = False
                break
            elif ok is None:
                S.reconnects += 1
                S.add_log("Koneksi gagal: %s - Reconnect..." % reason, "error")
                S.set_status("Reconnecting...")
                time.sleep(5)
                continue

            bal = parse_balance(dash)
            if bal:
                S.balance = bal
            S.add_log("Session valid | Balance: %s" % (bal or "N/A"), "info")

            # === STEP 2: LOAD FAUCET ===
            S.set_status("Memuat faucet...")
            S.add_log("Memuat halaman faucet...", "info")

            page, ok, reason = get_page(BASE_URL + "/faucet", BASE_URL + "/dashboard")
            if ok is False:
                S.set_status("Session Expired!")
                S.add_log("Faucet gagal: %s - session expired!" % reason, "error")
                S.running = False
                break
            elif ok is None:
                S.add_log("Gagal muat faucet: %s" % reason, "error")
                time.sleep(5)
                continue

            # Check status
            if "daily limit" in page.lower() or "limit reached" in page.lower():
                S.set_status("Daily Limit!")
                S.add_log("DAILY LIMIT tercapai! Bot berhenti.", "warn")
                break
            if "shortlink" in page.lower() and "Ready To Claim" not in page:
                S.set_status("Shortlink Required")
                S.add_log("Shortlink diperlukan! Bot berhenti.", "warn")
                break

            if "Ready To Claim" not in page:
                wait = parse_wait(page)
                if wait:
                    S.set_status("Cooldown %ds" % wait)
                    S.add_log("Faucet belum siap, cooldown %d detik..." % wait, "info")
                    for sec in range(wait):
                        if not S.running or S.paused: break
                        if sec > 0 and sec % 15 == 0:
                            S.add_log("Cooldown: %d/%d detik tersisa" % (wait - sec, wait), "debug")
                        time.sleep(1)
                    continue
                else:
                    S.add_log("Faucet belum ready, reload 10 detik...", "warn")
                    time.sleep(10)
                    continue

            # === STEP 3: PARSE FORM ===
            S.add_log("Faucet siap! Parsing form...", "info")
            csrf, tok, img_m, fld = parse_form(page)
            if not all([csrf, tok, img_m, fld]):
                S.add_log("Form parse gagal (csrf=%s token=%s img=%s field=%s)" %
                          ("Y" if csrf else "N", "Y" if tok else "N",
                           "Y" if img_m else "N", "Y" if fld else "N"), "warn")
                time.sleep(5)
                continue
            S.add_log("Form OK", "debug")

            # === STEP 4: DOWNLOAD CAPTCHA ===
            S.set_status("Mengunduh captcha...")
            img_url = img_m.group(1)
            if not img_url.startswith("http"):
                img_url = BASE_URL + "/" + img_url.lstrip("/")
            try:
                img_r = requests.get(img_url, headers=_headers(BASE_URL + "/faucet"), timeout=30)
                S.add_log("Captcha diunduh (%d bytes)" % len(img_r.content), "debug")
            except requests.RequestException as e:
                S.add_log("Gagal unduh captcha: %s" % str(e)[:60], "error")
                time.sleep(3)
                continue

            if len(img_r.content) < 100:
                S.add_log("Captcha kosong, skip...", "warn")
                time.sleep(3)
                continue

            # === STEP 5: SOLVE CAPTCHA ===
            S.set_status("Memecahkan captcha...")
            S.add_log("Memecahkan captcha (OCR)...", "info")
            t0 = time.time()
            digits, confidence = _solve_captcha(img_r.content)
            t1 = time.time() - t0

            if not digits:
                S.cap_fail += 1
                cap_fail_streak += 1
                S.add_log("Captcha GAGAL (%.1fs) | Streak: %d | Total fail: %d" %
                          (t1, cap_fail_streak, S.cap_fail), "error")
                if cap_fail_streak >= 10:
                    S.add_log("10x captcha gagal berturut-turut! Bot berhenti.", "error")
                    S.set_status("Captcha Failed")
                    break
                time.sleep(2)
                continue

            if confidence < 2:
                S.add_log("Captcha rendah keyakinan: %s (votes=%d), reload captcha..." % (digits, confidence), "warn")
                S.cap_fail += 1
                cap_fail_streak += 1
                time.sleep(1)
                continue

            S.cap_ok += 1
            cap_fail_streak = 0
            S.add_log("Captcha BERHASIL: %s (%.1fs) | OK: %d | Fail: %d" %
                      (digits, t1, S.cap_ok, S.cap_fail), "info")

            # === STEP 6: SUBMIT CLAIM ===
            S.set_status("Mengirim claim...")
            S.add_log("Mengirim claim...", "info")
            try:
                cr = requests.post(BASE_URL + "/faucet/verify",
                    data={"csrf_token_name": csrf.group(1), "token": tok.group(1), fld.group(1): digits},
                    headers=_post_headers(BASE_URL + "/faucet"),
                    allow_redirects=False, timeout=30)
                S.add_log("Claim HTTP %d -> %s" % (cr.status_code, cr.headers.get("Location", "none")), "debug")

                if cr.status_code in (301, 302, 307):
                    loc = cr.headers.get("Location", "")
                    if "/login" in loc:
                        S.add_log("Redirect ke login - SESSION EXPIRED!", "error")
                        S.set_status("Session Expired!")
                        S.running = False
                        break
            except requests.RequestException as e:
                S.add_log("Claim error: %s" % str(e)[:60], "error")
                time.sleep(3)
                continue

            # === STEP 7: CHECK RESULT ===
            time.sleep(2)
            S.set_status("Memeriksa hasil...")

            page2, ok2, reason2 = get_page(BASE_URL + "/faucet", BASE_URL + "/faucet/verify")
            if ok2 is False:
                S.add_log("Gagal cek hasil: %s" % reason2, "error")
                time.sleep(3)
                continue
            if not page2:
                time.sleep(3)
                continue

            wait = parse_wait(page2)
            if wait:
                # SUCCESS!
                amt = 0.001
                amt_m = re.search(r"([\d.]+)\s*Coins?\s*has been added", page2, re.I)
                if amt_m:
                    try: amt = float(amt_m.group(1))
                    except: pass
                S.record_earned(amt, "Claim berhasil!")
                S.add_log("CLAIM BERHASIL! +%s coins | Total: %.4f | Claims: %d" %
                          (("%.4f" % amt), S.total_earned, S.total_claims))

                # Refresh balance
                dash2, _, _ = get_page(BASE_URL + "/dashboard", BASE_URL + "/faucet")
                if dash2:
                    bal2 = parse_balance(dash2)
                    if bal2:
                        S.balance = bal2

                # Cooldown
                S.set_status("Cooldown %ds" % wait)
                S.add_log("Cooldown %d detik... | Balance: %s" % (wait, S.balance), "info")
                for sec in range(wait):
                    if not S.running or S.paused: break
                    if sec > 0 and sec % 15 == 0:
                        S.add_log("Cooldown: %d/%d detik tersisa" % (wait - sec, wait), "debug")
                    time.sleep(1)
                S.add_log("Cooldown selesai, claim berikutnya...", "info")

            elif "Ready To Claim" in page2:
                S.total_failed += 1
                if "incorrect" in page2.lower() or "wrong" in page2.lower():
                    S.add_log("Captcha SALAH! (total salah: %d)" % S.total_failed, "error")
                else:
                    S.add_log("Claim gagal, masih Ready To Claim (gagal: %d)" % S.total_failed, "warn")
                time.sleep(3)
            else:
                S.add_log("Hasil tidak diketahui, reload...", "warn")
                time.sleep(5)

        except requests.ConnectionError:
            S.reconnects += 1
            S.set_status("Reconnecting...")
            S.add_log("Koneksi terputus! Reconnect #%d..." % S.reconnects, "error")
            time.sleep(10)
        except Exception as e:
            log.error("Loop error: %s", e, exc_info=True)
            S.set_status("Error")
            S.add_log("Error: %s" % str(e)[:80], "error")
            time.sleep(5)

    S.set_status("Stopped")
    S.add_log("Bot berhenti | Claims: %d | Earned: %.4f | Failed: %d | Uptime: %s" %
              (S.total_claims, S.total_earned, S.total_failed, S.uptime))

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

@app.route("/api/cookie", methods=["GET", "POST"])
def api_cookie():
    if request.method == "GET":
        c = S.cookie
        preview = (c[:30] + "...") if c and len(c) > 30 else (c or "")
        return jsonify({"has_cookie": bool(c), "preview": preview})
    d = request.get_json(force=True, silent=True) or {}
    ck = str(d.get("cookie", "")).strip()
    if not ck:
        return jsonify({"ok": False, "msg": "Cookie wajib diisi"}), 400
    S.cookie = ck
    S.save_cfg()
    S.add_log("Cookie diperbarui (%d karakter)" % len(ck))
    return jsonify({"ok": True, "msg": "Cookie tersimpan!"})

@app.route("/api/bot/start", methods=["POST"])
def api_start():
    if S.running:
        return jsonify({"ok": False, "msg": "Bot sudah jalan"})
    if not S.cookie:
        return jsonify({"ok": False, "msg": "Cookie belum di-set"})
    S.running = True
    S.paused = False
    threading.Thread(target=bot_loop, daemon=True).start()
    return jsonify({"ok": True, "msg": "Bot started"})

@app.route("/api/bot/stop", methods=["POST"])
def api_stop():
    S.running = False
    return jsonify({"ok": True, "msg": "Bot stopped"})

@app.route("/api/bot/pause", methods=["POST"])
def api_pause():
    S.paused = not S.paused
    S.set_status("Paused" if S.paused else "Resumed")
    S.add_log("Bot %s" % ("dijeda" if S.paused else "dilanjutkan"))
    return jsonify({"ok": True, "paused": S.paused})

@app.route("/api/bot/reset", methods=["POST"])
def api_reset():
    S.running = False
    S.total_earned = 0
    S.total_claims = 0
    S.total_failed = 0
    S.cap_fail = 0
    S.cap_ok = 0
    S.reconnects = 0
    S.last_msg = ""
    S.last_time = ""
    S.balance = "N/A"
    S.logs.clear()
    S.cookie = ""
    for f in [STATS_FILE, CONFIG_FILE]:
        if f.exists(): f.unlink()
    S.add_log("Bot direset")
    return jsonify({"ok": True, "msg": "Bot reset"})

# ============================================================
# HTML PAGE
# ============================================================

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>C4Coins Faucet Bot</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:#0b0e14;color:#c9d1d9;min-height:100vh}
.hdr{background:linear-gradient(135deg,#161b22 0%,#0d1117 100%);border-bottom:1px solid rgba(240,185,11,.15);padding:14px 20px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.logo{display:flex;align-items:center;gap:12px}
.logo-icon{width:36px;height:36px;background:linear-gradient(135deg,#f0b90b,#f8d12f);border-radius:10px;display:flex;align-items:center;justify-content:center;color:#0b0e14;font-weight:900;font-size:11px;letter-spacing:-.5px}
.logo-text h1{font-size:17px;color:#f0f6fc;font-weight:700;line-height:1.2}
.logo-text span{font-size:10px;color:#484f58;display:block;margin-top:1px}
.badge{display:inline-flex;align-items:center;gap:6px;padding:5px 14px;border-radius:20px;font-size:11px;font-weight:600;border:1px solid;transition:all .3s}
.badge-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.b-on{background:rgba(46,160,67,.12);border-color:rgba(46,160,67,.3);color:#3fb950}
.b-on .badge-dot{background:#3fb950;box-shadow:0 0 6px #3fb950;animation:pulse 1.5s infinite}
.b-off{background:rgba(110,118,129,.1);border-color:rgba(110,118,129,.2);color:#6e7681}
.b-off .badge-dot{background:#6e7681}
.b-err{background:rgba(248,81,73,.1);border-color:rgba(248,81,73,.3);color:#f85149}
.b-err .badge-dot{background:#f85149;box-shadow:0 0 6px #f85149}
.b-warn{background:rgba(210,153,34,.1);border-color:rgba(210,153,34,.3);color:#d29922}
.b-warn .badge-dot{background:#d29922;animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.wrap{max-width:900px;margin:0 auto;padding:16px}
.card{background:#161b22;border:1px solid rgba(240,246,252,.06);border-radius:12px;padding:18px;margin-bottom:14px}
.card:hover{border-color:rgba(240,246,252,.1)}
.card-title{font-size:11px;font-weight:700;color:#484f58;text-transform:uppercase;letter-spacing:.8px;margin-bottom:14px;display:flex;align-items:center;gap:8px}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.stat{text-align:center;padding:16px 8px;background:rgba(240,246,252,.02);border:1px solid rgba(240,246,252,.04);border-radius:10px}
.stat .val{font-size:22px;font-weight:800;line-height:1.2;font-variant-numeric:tabular-nums}
.stat .lbl{font-size:10px;color:#484f58;margin-top:5px;text-transform:uppercase;letter-spacing:.4px}
.c-earn .val{color:#f0b90b}.c-claim .val{color:#3fb950}.c-fail .val{color:#f85149}.c-bal .val{color:#58a6ff}
.form-row{display:flex;gap:10px;align-items:flex-end}
.form-group{flex:1}
.form-group label{display:block;font-size:11px;color:#484f58;margin-bottom:5px;font-weight:600}
.form-group textarea{width:100%;padding:10px 12px;background:#0d1117;border:1px solid rgba(240,246,252,.1);border-radius:8px;color:#c9d1d9;font-size:11px;outline:none;font-family:'Courier New',monospace;resize:vertical;min-height:42px;max-height:120px;transition:border .2s;line-height:1.4}
.form-group textarea:focus{border-color:#f0b90b}
.form-group textarea::placeholder{color:#30363d}
.btn{padding:10px 18px;border:none;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;transition:all .15s;display:inline-flex;align-items:center;gap:6px;letter-spacing:.3px;white-space:nowrap}
.btn:active{transform:scale(.97)}
.btn:disabled{opacity:.5;cursor:not-allowed;transform:none}
.btn-go{background:linear-gradient(135deg,#238636,#2ea043);color:#fff}
.btn-go:hover:not(:disabled){box-shadow:0 4px 12px rgba(46,160,67,.3)}
.btn-stop{background:linear-gradient(135deg,#da3633,#f85149);color:#fff}
.btn-stop:hover:not(:disabled){box-shadow:0 4px 12px rgba(248,81,73,.3)}
.btn-pause{background:linear-gradient(135deg,#9e6a03,#d29922);color:#fff}
.btn-pause:hover:not(:disabled){box-shadow:0 4px 12px rgba(210,153,34,.3)}
.btn-reset{background:rgba(110,118,129,.1);border:1px solid rgba(110,118,129,.2);color:#8b949e}
.btn-reset:hover:not(:disabled){border-color:rgba(110,118,129,.4);color:#c9d1d9}
.btn-save{background:linear-gradient(135deg,#1f6feb,#388bfd);color:#fff}
.btn-save:hover{box-shadow:0 4px 12px rgba(56,139,253,.3)}
.btns{display:flex;gap:8px;flex-wrap:wrap}
.cookie-info{margin-top:10px;padding:8px 12px;border-radius:8px;font-size:11px;font-family:'Courier New',monospace;word-break:break-all}
.cookie-ok{background:rgba(46,160,67,.06);border:1px solid rgba(46,160,67,.15);color:#3fb950}
.cookie-no{background:rgba(248,81,73,.06);border:1px solid rgba(248,81,73,.15);color:#f85149}
.detail-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;font-size:12px}
.detail-grid .row{display:flex;justify-content:space-between;padding:7px 10px;border-radius:6px;background:rgba(240,246,252,.02)}
.detail-grid .k{color:#484f58}.detail-grid .v{font-weight:700}
.log-box{background:#0d1117;border:1px solid rgba(240,246,252,.04);border-radius:10px;height:400px;overflow-y:auto;padding:8px;font-family:'JetBrains Mono','Fira Code','Courier New',monospace;font-size:11px;line-height:1.7}
.log-box::-webkit-scrollbar{width:5px}
.log-box::-webkit-scrollbar-track{background:transparent}
.log-box::-webkit-scrollbar-thumb{background:rgba(240,246,252,.06);border-radius:3px}
.log-box::-webkit-scrollbar-thumb:hover{background:rgba(240,246,252,.12)}
.log-line{display:flex;gap:10px;padding:2px 6px;border-radius:4px}
.log-line:hover{background:rgba(240,246,252,.03)}
.log-ts{color:#30363d;white-space:nowrap;flex-shrink:0;min-width:62px;font-size:10px}
.log-lv{width:8px;flex-shrink:0;display:flex;align-items:center}
.log-lv-dot{width:6px;height:6px;border-radius:50%}
.log-lv-dot.info{background:#484f58}
.log-lv-dot.debug{background:#21262d}
.log-lv-dot.warn{background:#d29922}
.log-lv-dot.error{background:#f85149}
.log-lv-dot.success{background:#3fb950}
.log-msg{flex:1;word-break:break-word}
.log-msg.info{color:#8b949e}
.log-msg.debug{color:#30363d;font-size:10px}
.log-msg.warn{color:#d29922}
.log-msg.error{color:#f85149}
.log-msg.success{color:#3fb950}
.log-empty{text-align:center;color:#21262d;padding:60px 20px;font-size:12px}
@media(max-width:640px){
  .grid4{grid-template-columns:repeat(2,1fr)}
  .detail-grid{grid-template-columns:1fr 1fr}
  .wrap{padding:10px}.hdr{padding:10px 14px}
  .btns{flex-direction:column}.btns .btn{width:100%;justify-content:center}
  .form-row{flex-direction:column}
  .log-box{height:300px;font-size:10px}
}
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
    <div class="card-title">&#128200; Statistik</div>
    <div class="grid4">
      <div class="stat c-earn"><div class="val" id="sEarned">0.0000</div><div class="lbl">Earned</div></div>
      <div class="stat c-claim"><div class="val" id="sClaims">0</div><div class="lbl">Claims</div></div>
      <div class="stat c-fail"><div class="val" id="sFailed">0</div><div class="lbl">Failed</div></div>
      <div class="stat c-bal"><div class="val" id="sBal">N/A</div><div class="lbl">Balance</div></div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">&#127873; Cookie</div>
    <div class="form-row">
      <div class="form-group">
        <label>Cookie dari feyorra.top</label>
        <textarea id="cookieIn" placeholder="Paste semua cookie dari feyorra.top di sini..."></textarea>
      </div>
      <button class="btn btn-save" onclick="saveCookie()">&#128190; Save</button>
    </div>
    <div id="cookieInfo"></div>
  </div>

  <div class="card">
    <div class="card-title">&#9881; Kontrol</div>
    <div class="btns">
      <button class="btn btn-go" onclick="startBot()">&#9654; Start</button>
      <button class="btn btn-stop" onclick="stopBot()">&#9632; Stop</button>
      <button class="btn btn-pause" onclick="pauseBot()">&#10074;&#10074; Pause</button>
      <button class="btn btn-reset" onclick="resetBot()">&#8635; Reset</button>
    </div>
  </div>

  <div class="card">
    <div class="card-title">&#128202; Detail</div>
    <div class="detail-grid">
      <div class="row"><span class="k">Captcha OK</span><span class="v" style="color:#3fb950" id="dCapOk">0</span></div>
      <div class="row"><span class="k">Captcha Fail</span><span class="v" style="color:#f85149" id="dCapFail">0</span></div>
      <div class="row"><span class="k">Reconnects</span><span class="v" style="color:#d29922" id="dReconn">0</span></div>
      <div class="row"><span class="k">Last Claim</span><span class="v" style="color:#58a6ff" id="dLastClaim">-</span></div>
      <div class="row"><span class="k">Last Reward</span><span class="v" style="color:#bc8cff" id="dLastReward">-</span></div>
      <div class="row"><span class="k">Uptime</span><span class="v" style="color:#f0b90b" id="dUptime">0s</span></div>
    </div>
  </div>

  <div class="card" style="padding-bottom:10px">
    <div class="card-title">&#128220; Activity Log</div>
    <div class="log-box" id="logBox"><div class="log-empty">Menunggu aktivitas bot...</div></div>
  </div>
</div>

<script>
var lastLen=0,autoScroll=true;
function api(u,o){return fetch(u,o).then(function(r){return r.json()}).catch(function(e){console.error(e);return null})}
function esc(s){var d=document.createElement('span');d.textContent=s;return d.innerHTML}

function updateStatus(){
  api('/api/status').then(function(d){
    if(!d)return;
    var b=document.getElementById('badge'),t=document.getElementById('badgeTxt');
    t.textContent=d.status;
    var s=d.status.toLowerCase();
    if(d.running&&!d.paused)b.className='badge b-on';
    else if(d.paused)b.className='badge b-warn';
    else if(s.indexOf('error')>=0||s.indexOf('expired')>=0||s.indexOf('fail')>=0)b.className='badge b-err';
    else b.className='badge b-off';
    document.getElementById('sEarned').textContent=d.earned.toFixed(4);
    document.getElementById('sClaims').textContent=d.claims;
    document.getElementById('sFailed').textContent=d.failed||0;
    document.getElementById('sBal').textContent=d.balance;
    document.getElementById('dCapOk').textContent=d.cap_ok;
    document.getElementById('dCapFail').textContent=d.cap_fail;
    document.getElementById('dReconn').textContent=d.reconnects;
    document.getElementById('dLastClaim').textContent=d.last_time||'-';
    document.getElementById('dLastReward').textContent=d.last_msg||'-';
    document.getElementById('dUptime').textContent=d.uptime;
  });
}

function updateLogs(){
  api('/api/logs').then(function(logs){
    if(!logs||!logs.length)return;
    if(logs.length===lastLen)return;
    lastLen=logs.length;
    var box=document.getElementById('logBox'),html='';
    for(var i=0;i<logs.length;i++){
      var l=logs[i],cls=l.level||'info';
      if(cls==='info'&&(l.msg.indexOf('BERHASIL')>=0||l.msg.indexOf('selesai')>=0))cls='success';
      if(cls==='info'&&l.msg.charAt(0)==='+')cls='success';
      html+='<div class="log-line">';
      html+='<span class="log-ts">'+esc(l.time)+'</span>';
      html+='<span class="log-lv"><span class="log-lv-dot '+cls+'"></span></span>';
      html+='<span class="log-msg '+cls+'">'+esc(l.msg)+'</span>';
      html+='</div>';
    }
    box.innerHTML=html;
    if(autoScroll)box.scrollTop=box.scrollHeight;
  });
}

document.addEventListener('DOMContentLoaded',function(){
  var box=document.getElementById('logBox');
  box.addEventListener('scroll',function(){autoScroll=(box.scrollHeight-box.scrollTop-box.clientHeight)<50});
});

function loadCookie(){
  api('/api/cookie').then(function(d){
    if(!d)return;
    var el=document.getElementById('cookieInfo');
    if(d.has_cookie){
      el.className='cookie-info cookie-ok';
      el.textContent='Cookie aktif: '+d.preview;
    }else{
      el.className='cookie-info cookie-no';
      el.textContent='Cookie belum di-set! Paste cookie dari feyorra.top.';
    }
  });
}

function saveCookie(){
  var v=document.getElementById('cookieIn').value.trim();
  if(!v){alert('Cookie wajib diisi!');return}
  api('/api/cookie',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cookie:v})})
  .then(function(d){
    if(d&&d.ok){document.getElementById('cookieIn').value='';loadCookie();alert('Cookie tersimpan! Klik Start.')}
    else alert(d?d.msg:'Gagal menyimpan cookie');
  });
}

function startBot(){api('/api/bot/start',{method:'POST'}).then(function(d){if(d&&!d.ok)alert(d.msg)})}
function stopBot(){api('/api/bot/stop',{method:'POST'})}
function pauseBot(){api('/api/bot/pause',{method:'POST'})}
function resetBot(){if(!confirm('Reset semua data?'))return;api('/api/bot/reset',{method:'POST'}).then(function(){loadCookie()})}

setInterval(updateStatus,1500);
setInterval(updateLogs,1500);
updateStatus();updateLogs();loadCookie();
</script>
</body>
</html>"""

if __name__ == "__main__":
    log.info("C4Coins Web Bot starting on port %d", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
