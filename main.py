import os
import glob
import time
import hashlib
import threading
import sqlite3
import re
import gc
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, UploadFile, File, HTTPException
import uvicorn
import psutil

app = FastAPI()

START_TIME = time.time()
CURRENT_PUBLIC_IP = None

# =====================================================================
# CONFIGURATION
# =====================================================================
TG_BOT_TOKEN = "8403175184:AAFLZY6yHWNb8DxvZ8ZmdZIKw6ZuTebc8aA"
TG_CHAT_ID = "6519226948"

TRACKER_INTERVAL_SECONDS = 1800   # 30 mins
JOB_INTERVAL_SECONDS = 10800      # 3 Hours (10800s)
DB_FILE = "jobs_cache.db"
META_DIR = "meta_cache"

os.makedirs(META_DIR, exist_ok=True)

# =====================================================================
# DATABASE & RETENTION (2 DAYS PURGE)
# =====================================================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS seen_jobs (
            job_id TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            posted_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def cleanup_old_seen_jobs():
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM seen_jobs WHERE posted_at < datetime('now', '-2 days')")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB-CLEAN] Error: {e}")

def is_job_seen(job_id: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT 1 FROM seen_jobs WHERE job_id = ?", (job_id,))
    row = c.fetchone()
    conn.close()
    return row is not None

def mark_job_seen(job_id: str, source: str, title: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO seen_jobs (job_id, source, title) VALUES (?, ?, ?)", (job_id, source, title))
    conn.commit()
    conn.close()

def send_telegram_msg(msg: str):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass

# =====================================================================
# IP WATCHDOG THREAD
# =====================================================================
def get_outbound_ip():
    try:
        return requests.get("https://api.ipify.org", timeout=10).text.strip()
    except Exception:
        return None

def ip_watchdog_loop():
    global CURRENT_PUBLIC_IP
    time.sleep(10)
    while True:
        fresh_ip = get_outbound_ip()
        if fresh_ip and fresh_ip != CURRENT_PUBLIC_IP:
            old_ip = CURRENT_PUBLIC_IP
            CURRENT_PUBLIC_IP = fresh_ip
            if old_ip is None:
                send_telegram_msg(f"🚀 <b>Render Container Booted!</b>\nPublic IP: <code>{fresh_ip}</code>")
            else:
                send_telegram_msg(f"⚠️ <b>IP Changed!</b>\nOld: <code>{old_ip}</code>\nNew: <code>{fresh_ip}</code>\n<i>Verify tracker profile if needed.</i>")
        time.sleep(300) # Har 5 min me check

# =====================================================================
# BENCODE & TRACKER ANNOUNCE LOOP
# =====================================================================
def bdecode(data):
    def decode_item(idx):
        char = data[idx:idx+1]
        if char == b'i':
            idx += 1
            end = data.index(b'e', idx)
            return int(data[idx:end]), end + 1
        elif char == b'l':
            idx += 1
            res = []
            while data[idx:idx+1] != b'e':
                item, idx = decode_item(idx)
                res.append(item)
            return res, idx + 1
        elif char == b'd':
            idx += 1
            res = {}
            while data[idx:idx+1] != b'e':
                key, idx = decode_item(idx)
                val, idx = decode_item(idx)
                res[key.decode('utf-8', errors='ignore')] = val
            return res, idx + 1
        elif char.isdigit():
            colon = data.index(b':', idx)
            length = int(data[idx:colon])
            start = colon + 1
            return data[start:start+length], start + length
        else:
            raise ValueError(f"Invalid byte at index {idx}")
    res, _ = decode_item(0)
    return res

def bencode(data):
    if isinstance(data, int):
        return f"i{data}e".encode('ascii')
    elif isinstance(data, (str, bytes)):
        if isinstance(data, str):
            data = data.encode('utf-8')
        return f"{len(data)}:".encode('ascii') + data
    elif isinstance(data, list):
        return b"l" + b"".join(bencode(i) for i in data) + b"e"
    elif isinstance(data, dict):
        items = []
        for k in sorted(data.keys()):
            key_bytes = k.encode('utf-8') if isinstance(k, str) else k
            items.append(f"{len(key_bytes)}:".encode('ascii') + key_bytes)
            items.append(bencode(data[k]))
        return b"d" + b"".join(items) + b"e"
    raise ValueError(f"Unsupported type: {type(data)}")

CLIENT_PEER_ID = "-TR3000-" + hashlib.md5(b"telemetry_stream_worker").hexdigest()[:12]
CLIENT_HEADERS = {"User-Agent": "Transmission/3.00"}

def sync_tracker_loop():
    time.sleep(5)
    session = requests.Session()
    session.headers.update(CLIENT_HEADERS)

    while True:
        torrent_files = glob.glob(f"{META_DIR}/*.torrent")
        print(f"\n[TRACKER] [{time.strftime('%H:%M:%S')}] Announce batch started: {len(torrent_files)} items.")

        for t_file in torrent_files:
            try:
                with open(t_file, "rb") as f:
                    content = f.read()
                meta = bdecode(content)
                info = meta.get('info')
                if not info:
                    continue

                raw_info = bencode(info)
                info_hash = hashlib.sha1(raw_info).digest()

                announce_target = meta.get('announce')
                if isinstance(announce_target, bytes):
                    announce_target = announce_target.decode('utf-8')

                if not announce_target:
                    continue

                params = {
                    "info_hash": info_hash,
                    "peer_id": CLIENT_PEER_ID,
                    "port": "51413",
                    "uploaded": "0",
                    "downloaded": "0",
                    "left": "0",
                    "corrupt": "0",
                    "compact": "1",
                    "numwant": "0",
                    "event": "started"
                }

                resp = session.get(announce_target, params=params, timeout=15)
                file_label = info.get('name', os.path.basename(t_file))
                if isinstance(file_label, bytes):
                    file_label = file_label.decode('utf-8', errors='ignore')
                print(f"[TRACKER] 📡 [Synced] {file_label[:25]} | HTTP {resp.status_code}")
                time.sleep(1)

            except Exception as err:
                print(f"[TRACKER] ⚠️ Skip {os.path.basename(t_file)}: {err}")

        time.sleep(TRACKER_INTERVAL_SECONDS)

# =====================================================================
# JOB HUNTER (3 HOURS CYCLE + CLEAN MEMORY)
# =====================================================================
DOMAIN_PATTERNS = [r"\b(ai|ml|genai|llm|rag|nlp|mlops)\b", r"artificial\s+intelligence", r"machine\s+learning", r"deep\s+learning", r"computer\s+vision", r"data\s+scienc(e|tist)", r"data\s+analy(st|tics)", r"data\s+engineer(ing)?", r"python\b"]
GENERIC_TECH_PATTERNS = [r"\bsde\b", r"software\s+developer", r"software\s+engineer", r"backend\s+developer"]
EXCLUDE_PATTERNS = [r"instructor", r"trainer", r"sales\b", r"\bbda\b", r"telecaller", r"content\s+writ", r"\bphp\b", r"\.net\b"]
ALLOWED_TECH_SKILLS = {"python", "machine learning", "deep learning", "artificial intelligence", "data science", "fastapi", "flask", "django", "pytorch", "tensorflow", "opencv", "sql"}

def is_fresh_listing(posted_time: str) -> bool:
    pt = posted_time.lower().strip()
    return any(sig in pt for sig in ["just now", "few hours", "hour ago", "hours ago", "today", "yesterday", "1 day ago", "2 days ago", "early applicant"])

def is_valid_match(title: str, snippet_text: str, stipend: str) -> bool:
    t_low, s_low, st_low = title.lower(), snippet_text.lower(), stipend.lower()
    if "unpaid" in st_low or "unpaid" in s_low:
        return False
    if any(re.search(exc, t_low) for exc in EXCLUDE_PATTERNS):
        return False
    if any(re.search(dom, t_low) for dom in DOMAIN_PATTERNS):
        return True
    if any(re.search(gen, t_low) for gen in GENERIC_TECH_PATTERNS):
        return any(tech in s_low for tech in ALLOWED_TECH_SKILLS)
    return False

def scrape_internshala(url, category, session, headers):
    try:
        res = session.get(url, headers=headers, timeout=20)
        if res.status_code != 200:
            return
        soup = BeautifulSoup(res.text, "html.parser")
        cards = soup.select(".individual_internship")
        for card in cards:
            link = card.get("data-href")
            if not link:
                a_tag = card.select_one("a.view_detail_button, h3 a, .job-title-href")
                if a_tag and a_tag.get("href"):
                    link = a_tag["href"]
            if not link:
                continue
            link = link if link.startswith("http") else "https://internshala.com" + link

            job_id_match = re.search(r'-(\d+)$', link)
            job_id = f"IS_{job_id_match.group(1)}" if job_id_match else f"IS_{link}"
            if is_job_seen(job_id):
                continue

            title_tag = card.select_one(".job-internship-name, .profile, h3 a")
            title = title_tag.get_text(strip=True) if title_tag else "N/A"
            comp_tag = card.select_one(".company-name, .company_name, .heading_6_company")
            company = re.sub(r'Actively hiring.*', '', comp_tag.get_text(strip=True)) if comp_tag else "N/A"
            loc_tag = card.select_one(".locations, #location_names, .location_link")
            location = loc_tag.get_text(strip=True) if loc_tag else "Remote"
            stipend_tag = card.select_one(".stipend, .salary")
            stipend = stipend_tag.get_text(strip=True) if stipend_tag else "Not specified"
            posted_time = "Recently"
            status_tag = card.select_one(".status-success, .status-inactive, .posted_by_container, .status-info")
            if status_tag:
                posted_time = status_tag.get_text(" ", strip=True)

            if is_valid_match(title, card.get_text(" ", strip=True), stipend) and is_fresh_listing(posted_time):
                msg = f"🎯 <b>New Matched Opening!</b>\n\n🏢 <b>Company:</b> {company}\n💼 <b>Role:</b> {title}\n📍 <b>Location:</b> {location}\n💰 <b>Stipend/CTC:</b> {stipend}\n⏳ <b>Posted:</b> {posted_time}\n\n🔗 <a href='{link}'><b>Apply on Internshala</b></a>"
                send_telegram_msg(msg)
                mark_job_seen(job_id, "Internshala", title)
                time.sleep(1)

        del cards
        del soup
        del res
        gc.collect()
    except Exception as e:
        print(f"[JOB-HUNTER] Error: {e}")

def job_hunter_loop():
    init_db()
    time.sleep(15)
    session = requests.Session()
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36"}
    targets = [
        ("Internships", "https://internshala.com/internships/artificial-intelligence-ai,data-science,machine-learning,python-django-internship/"),
        ("Fresher Jobs", "https://internshala.com/jobs/artificial-intelligence-ai,data-science,machine-learning,python-django-jobs/")
    ]
    while True:
        cleanup_old_seen_jobs()
        for cat, url in targets:
            scrape_internshala(url, cat, session, headers)
        time.sleep(JOB_INTERVAL_SECONDS)

# =====================================================================
# TELEGRAM POLLER (ONLY /status)
# =====================================================================
def telegram_poller():
    offset = 0
    time.sleep(5)
    while True:
        try:
            url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates?offset={offset}&timeout=20"
            r = requests.get(url, timeout=25).json()
            if not r.get("ok"):
                time.sleep(5)
                continue

            for item in r.get("result", []):
                offset = item["update_id"] + 1
                msg = item.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id"))
                if chat_id != str(TG_CHAT_ID):
                    continue

                text = msg.get("text", "").strip()
                if text == "/status":
                    mem = psutil.Process().memory_info().rss / (1024 * 1024)
                    uptime_seconds = int(time.time() - START_TIME)
                    days = uptime_seconds // 86400
                    hours = (uptime_seconds % 86400) // 3600
                    minutes = (uptime_seconds % 3600) // 60
                    uptime_str = f"{days}d {hours}h {minutes}m"

                    send_telegram_msg(f"📊 <b>Container Health</b>\n\n🧠 <b>RAM Usage:</b> {mem:.2f} MB\n⏱️ <b>Uptime:</b> {uptime_str}")
        except Exception:
            time.sleep(5)

# =====================================================================
# FASTAPI ENDPOINTS (FOR CLI PUSH - NO RESTART)
# =====================================================================
@app.api_route("/", methods=["GET", "HEAD"])
def ping_service():
    mem = psutil.Process().memory_info().rss / (1024 * 1024)
    return {"status": "active", "ram_mb": round(mem, 2)}

@app.post("/upload-torrent")
async def upload_torrent(file: UploadFile = File(...)):
    if not file.filename.endswith(".torrent"):
        raise HTTPException(status_code=400, detail="Only .torrent files allowed")
    file_path = os.path.join(META_DIR, file.filename)
    with open(file_path, "wb") as buffer:
        content = await file.read()
        buffer.write(content)
    return {"status": "success", "file": file.filename}

@app.on_event("startup")
def start_all():
    threading.Thread(target=sync_tracker_loop, daemon=True).start()
    threading.Thread(target=job_hunter_loop, daemon=True).start()
    threading.Thread(target=ip_watchdog_loop, daemon=True).start()
    threading.Thread(target=telegram_poller, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
