import os
import glob
import time
import hashlib
import threading
import requests
from fastapi import FastAPI
import uvicorn

app = FastAPI()

# ----------------- RENDER / UPTIME ENDPOINT -----------------
@app.get("/")
def ping_service():
    return {
        "status": "active",
        "node": "telemetry-sync",
        "time": time.strftime("%Y-%m-%d %H:%M:%S")
    }

# ----------------- BENCODE HANDLERS -----------------
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

# BTSCHOOL Client Identity (Transmission 3.00 Format)
CLIENT_PEER_ID = "-TR3000-" + hashlib.md5(b"telemetry_stream_worker").hexdigest()[:12]
CLIENT_HEADERS = {
    "User-Agent": "Transmission/3.00"
}

def sync_tracker_loop():
    time.sleep(8)
    while True:
        # Direct .torrent files read karega
        torrent_files = glob.glob("meta_cache/*.torrent")
        print(f"\n[{time.strftime('%H:%M:%S')}] Pulse sync: {len(torrent_files)} items found.")
        
        for t_file in torrent_files:
            try:
                with open(t_file, "rb") as f:
                    content = f.read()
                meta = bdecode(content)
                info = meta['info']
                raw_info = bencode(info)
                info_hash = hashlib.sha1(raw_info).digest()

                announce_target = meta.get('announce')
                if isinstance(announce_target, bytes):
                    announce_target = announce_target.decode('utf-8')

                if not announce_target:
                    continue

                # 100% Seeder payload (left=0 se recheck/storage bypass hota hai)
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

                resp = requests.get(announce_target, params=params, headers=CLIENT_HEADERS, timeout=20)
                file_label = info.get('name', os.path.basename(t_file))
                if isinstance(file_label, bytes):
                    file_label = file_label.decode('utf-8', errors='ignore')
                print(f"📡 [Synced] {file_label[:28]} | HTTP {resp.status_code}")
            except Exception as err:
                print(f"⚠️ Failed {os.path.basename(t_file)}: {err}")

        # Har 30 min (1800 sec) par standard interval announce
        time.sleep(1800)

@app.on_event("startup")
def start_sync():
    worker_th = threading.Thread(target=sync_tracker_loop, daemon=True)
    worker_th.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
