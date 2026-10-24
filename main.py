import os
import glob
import time
import hashlib
import threading
import requests
from fastapi import FastAPI
import uvicorn

app = FastAPI()

@app.get("/")
def health():
    return {
        "status": "online",
        "service": "Telemetry Node",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
    }

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

PEER_ID = "-TR3000-" + hashlib.md5(b"telemetry_node_prod").hexdigest()[:12]
HEADERS = {"User-Agent": "Transmission/3.00"}

def announce_cycle():
    time.sleep(10)
    while True:
        torrent_files = glob.glob("torrents/*.torrent")
        print(f"[{time.strftime('%H:%M:%S')}] Found {len(torrent_files)} torrents to announce.")
        
        for tpath in torrent_files:
            try:
                with open(tpath, "rb") as f:
                    content = f.read()
                meta = bdecode(content)
                info = meta['info']
                raw_info = bencode(info)
                info_hash = hashlib.sha1(raw_info).digest()

                announce_url = meta.get('announce')
                if isinstance(announce_url, bytes):
                    announce_url = announce_url.decode('utf-8')

                if not announce_url:
                    continue

                params = {
                    "info_hash": info_hash,
                    "peer_id": PEER_ID,
                    "port": "51413",
                    "uploaded": "0",
                    "downloaded": "0",
                    "left": "0",  # 100% complete seeder status
                    "corrupt": "0",
                    "compact": "1",
                    "numwant": "0",
                    "event": "started"
                }

                res = requests.get(announce_url, params=params, headers=HEADERS, timeout=15)
                name = info.get('name', os.path.basename(tpath))
                if isinstance(name, bytes):
                    name = name.decode('utf-8', errors='ignore')
                print(f"✅ Seeding Synced: {name[:30]} | HTTP {res.status_code}")
            except Exception as e:
                print(f"❌ Announce error for {tpath}: {e}")

        # Har 30 min (1800 sec) par cycle repeat hoga
        time.sleep(1800)

@app.on_event("startup")
def start_worker():
    t = threading.Thread(target=announce_cycle, daemon=True)
    t.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
