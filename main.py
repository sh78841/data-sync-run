import os
import glob
import time
import hashlib
import threading
import requests
from rich.live import Live
from rich.table import Table
from rich.console import Console

console = Console()

# ----------------- BENCODE LOGIC -----------------
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

# ----------------- TORRENT STATE -----------------
PEER_ID = "-TR3000-" + hashlib.md5(b"silent_worker").hexdigest()[:12]
HEADERS = {"User-Agent": "Transmission/3.00"}

torrents_state = []

def load_torrents():
    global torrents_state
    torrents_state = []
    files = glob.glob("torrents/*.torrent")
    for fpath in files:
        try:
            with open(fpath, "rb") as f:
                meta = bdecode(f.read())
            info_dict = meta['info']
            raw_info = bencode(info_dict)
            info_hash = hashlib.sha1(raw_info).digest()
            name = info_dict.get('name', os.path.basename(fpath))
            if isinstance(name, bytes):
                name = name.decode('utf-8', errors='ignore')
            
            announce_url = meta.get('announce', '')
            if isinstance(announce_url, bytes):
                announce_url = announce_url.decode('utf-8')
                
            torrents_state.append({
                "name": name[:38],
                "hash": info_hash,
                "url": announce_url,
                "status": "Ready",
                "last_announce": "-"
            })
        except Exception:
            pass

def generate_table():
    table = Table(title="[bold green]Transmission Seeding Node (100% Seed - No Download)[/bold green]")
    table.add_column("Torrent Name", style="cyan", no_wrap=True)
    table.add_column("Progress", style="bold green", justify="center")
    table.add_column("Tracker Status", style="magenta", justify="center")
    table.add_column("Last Announce", style="yellow", justify="right")

    for t in torrents_state:
        table.add_row(t["name"], "100.0%", t["status"], t["last_announce"])
    return table

def announce_cycle():
    while True:
        for t in torrents_state:
            try:
                params = {
                    "info_hash": t["hash"],
                    "peer_id": PEER_ID,
                    "port": "51413",
                    "uploaded": "0",
                    "downloaded": "0",
                    "left": "0",  # Tracker ko seeder batayega
                    "corrupt": "0",
                    "compact": "1",
                    "numwant": "0",
                    "event": "started"
                }
                res = requests.get(t["url"], params=params, headers=HEADERS, timeout=15)
                if res.status_code == 200:
                    t["status"] = "Seeding (HTTP 200)"
                else:
                    t["status"] = f"Error ({res.status_code})"
                t["last_announce"] = time.strftime("%H:%M:%S")
            except Exception:
                t["status"] = "Connection Timeout"
        time.sleep(1800)

if __name__ == "__main__":
    load_torrents()
    t = threading.Thread(target=announce_cycle, daemon=True)
    t.start()

    with Live(generate_table(), refresh_per_second=1) as live:
        while True:
            live.update(generate_table())
            time.sleep(1)
