import os
import glob
import time
import hashlib
import threading
import sqlite3
import re
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI
import uvicorn
import psutil

app = FastAPI()

# =====================================================================
# CONFIGURATION & CREDENTIALS
# =====================================================================
TG_BOT_TOKEN = "8403175184:AAFLZY6yHWNb8DxvZ8ZmdZIKw6ZuTebc8aA"
TG_CHAT_ID = "6519226948"

TRACKER_INTERVAL_SECONDS = 1800  # 30 mins
JOB_INTERVAL_SECONDS = 1800      # 30 mins

DB_FILE = "jobs_cache.db"

# =====================================================================
# DEDUPLICATION DATABASE (ZERO-REPEAT ALERTS)
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

# =====================================================================
# TELEGRAM DISPATCHER (HTML MODE - PLATFORM LINE REMOVED)
# =====================================================================
def send_telegram_alert(title, company, location, link, stipend, posted_time):
    loc_clean = location.strip()
    if "work from home" in loc_clean.lower():
        loc_badge = "🏠 Work From Home"
    elif "hybrid" in loc_clean.lower():
        loc_badge = f"🏢 Hybrid ({loc_clean})"
    else:
        loc_badge = f"🏢 In-Office ({loc_clean})"

    msg = (
        f"🎯 <b>New Matched Opening!</b>\n\n"
        f"🏢 <b>Company:</b> {company}\n"
        f"💼 <b>Role:</b> {title}\n"
        f"📍 <b>Location:</b> {loc_badge}\n"
        f"💰 <b>Stipend/CTC:</b> {stipend}\n"
        f"⏳ <b>Posted:</b> {posted_time}\n\n"
        f'🔗 <a href="{link}"><b>Apply on Internshala</b></a>'
    )
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }

    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=20)
            return r.status_code == 200
        except requests.exceptions.RequestException:
            if attempt < 2:
                time.sleep(2)
                continue
            return False

# =====================================================================
# BIT TORRENT BENCODE PARSER
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

# =====================================================================
# THREAD 1: BTSCHOOL TRACKER RE-ANNOUNCE LOOP
# =====================================================================
def sync_tracker_loop():
    time.sleep(5)
    session = requests.Session()
    session.headers.update(CLIENT_HEADERS)

    while True:
        torrent_files = glob.glob("meta_cache/*.torrent")
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
# THREAD 2: INTERNSHALA JOB HUNTER LOOP
# =====================================================================
DOMAIN_PATTERNS = [
    r"\b(ai|ml|genai|llm|rag|nlp|mlops)\b",
    r"artificial\s+intelligence",
    r"machine\s+learning",
    r"deep\s+learning",
    r"computer\s+vision",
    r"data\s+scienc(e|tist)",
    r"data\s+analy(st|tics)",
    r"data\s+engineer(ing)?",
    r"python\b",
    r"prompt\s+engineer",
    r"applied\s+scientist"
]

GENERIC_TECH_PATTERNS = [
    r"\bsde\b",
    r"software\s+developer",
    r"software\s+engineer",
    r"backend\s+developer",
    r"backend\s+engineer",
    r"full\s*stack"
]

EXCLUDE_PATTERNS = [
    r"video\s+(creator|editor|making)",
    r"instructor", r"trainer", r"teaching", r"tutor",
    r"sales\b", r"\bbda\b", r"\bbde\b", r"business\s+development",
    r"telecaller", r"executive\s+assistant", r"secretary",
    r"event\s+management", r"graphic\s+design", r"content\s+writ",
    r"\bphp\b", r"laravel", r"wordpress", r"\.net\b", r"\bc#\b",
    r"java\s+developer"
]

ALLOWED_TECH_SKILLS = {
    "python", "machine learning", "deep learning", "artificial intelligence", 
    "data science", "fastapi", "flask", "django", "pytorch", "tensorflow", 
    "opencv", "nlp", "langchain", "langgraph", "llm", "rag", "sql"
}

def is_fresh_listing(posted_time: str) -> bool:
    pt = posted_time.lower().strip()
    fresh_signals = [
        "just now", "few hours", "hour ago", "hours ago",
        "today", "yesterday", "1 day ago", "2 days ago",
        "early applicant"
    ]
    return any(sig in pt for sig in fresh_signals)

def is_valid_match(title: str, snippet_text: str, stipend: str) -> bool:
    title_lower = title.lower()
    snippet_lower = snippet_text.lower()
    stipend_lower = stipend.lower()

    if "unpaid" in stipend_lower or "unpaid" in snippet_lower:
        return False

    for exc in EXCLUDE_PATTERNS:
        if re.search(exc, title_lower):
            return False

    for dom in DOMAIN_PATTERNS:
        if re.search(dom, title_lower):
            return True

    is_generic = any(re.search(gen, title_lower) for gen in GENERIC_TECH_PATTERNS)
    if is_generic:
        if any(tech in snippet_lower for tech in ALLOWED_TECH_SKILLS):
            return True

    return False

def scrape_internshala(url, category, session, headers):
    matches = 0
    try:
        res = session.get(url, headers=headers, timeout=20)
        if res.status_code != 200:
            print(f"[JOB-HUNTER] ⚠️ Failed {category}: HTTP {res.status_code}")
            return 0

        soup = BeautifulSoup(res.text, "html.parser")
        cards = soup.select(".individual_internship")

        for card in cards:
            link = ""
            if card.has_attr("data-href"):
                link = "https://internshala.com" + card["data-href"]
            else:
                a_tag = card.select_one("a.view_detail_button, h3 a, .job-title-href")
                if a_tag and a_tag.get("href"):
                    href = a_tag["href"]
                    link = href if href.startswith("http") else "https://internshala.com" + href

            if not link or link == "https://internshala.com":
                continue

            job_id_match = re.search(r'-(\d+)$', link)
            job_id = f"IS_{job_id_match.group(1)}" if job_id_match else f"IS_{link}"

            if is_job_seen(job_id):
                continue

            title_tag = card.select_one(".job-internship-name, .profile, h3 a")
            title = title_tag.get_text(strip=True) if title_tag else "N/A"

            company_tag = card.select_one(".company-name, .company_name, .heading_6_company")
            company = company_tag.get_text(strip=True) if company_tag else "N/A"
            company = re.sub(r'Actively hiring.*', '', company).strip()

            loc_tag = card.select_one(".locations, #location_names, .location_link")
            location = loc_tag.get_text(strip=True) if loc_tag else "Location Not Specified"

            stipend_tag = card.select_one(".stipend, .salary")
            stipend = stipend_tag.get_text(strip=True) if stipend_tag else "Not specified"

            posted_time = "Recently"
            status_tag = card.select_one(".status-success, .status-inactive, .posted_by_container, .status-info")
            if status_tag:
                posted_time = status_tag.get_text(" ", strip=True)

            card_full_text = card.get_text(" ", strip=True)

            if is_valid_match(title, card_full_text, stipend) and is_fresh_listing(posted_time):
                matches += 1
                print(f"[JOB-HUNTER] 🔥 Match: {title} @ {company} ({location})")
                sent = send_telegram_alert(title, company, location, link, stipend, posted_time)
                if sent:
                    print(f"[JOB-HUNTER] -> 📱 Telegram alert sent for {title}")
                    mark_job_seen(job_id, "Internshala", title)
                time.sleep(1.2)

    except Exception as e:
        print(f"[JOB-HUNTER] ⚠️ Scraper error in {category}: {e}")

    return matches

def job_hunter_loop():
    init_db()
    time.sleep(15)  # Initial container startup buffer
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    targets = [
        ("Internships", "https://internshala.com/internships/artificial-intelligence-ai,data-science,machine-learning,python-django-internship/"),
        ("Fresher Jobs", "https://internshala.com/jobs/artificial-intelligence-ai,data-science,machine-learning,python-django-jobs/")
    ]

    print("[JOB-HUNTER] Engine initialized.")
    while True:
        print(f"\n[JOB-HUNTER] [{time.strftime('%H:%M:%S')}] Checking latest listings...")
        for category, url in targets:
            scrape_internshala(url, category, session, headers)
        time.sleep(JOB_INTERVAL_SECONDS)

# =====================================================================
# THREAD 3: FASTAPI HEALTH & METRICS ENDPOINT (UptimeRobot / Cron-Job)
# =====================================================================
@app.api_route("/", methods=["GET", "HEAD"])
def ping_service():
    process = psutil.Process()
    ram_mb = process.memory_info().rss / (1024 * 1024)
    cpu_usage = psutil.cpu_percent(interval=None)

    total_tracked_jobs = 0
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM seen_jobs")
        total_tracked_jobs = c.fetchone()[0]
        conn.close()
    except Exception:
        pass

    return {
        "status": "active",
        "node": "telemetry-sync",
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "metrics": {
            "ram_usage_mb": round(ram_mb, 2),
            "cpu_percent": cpu_usage,
            "total_jobs_seen": total_tracked_jobs
        }
    }

# =====================================================================
# STARTUP DAEMON WORKERS
# =====================================================================
@app.on_event("startup")
def start_workers():
    t_tracker = threading.Thread(target=sync_tracker_loop, daemon=True)
    t_jobs = threading.Thread(target=job_hunter_loop, daemon=True)
    t_tracker.start()
    t_jobs.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
