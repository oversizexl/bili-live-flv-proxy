#!/usr/bin/env python3
"""
Bilibili Live FLV Proxy — Hugging Face Space
固定直链 → 自动刷新签名 → 透传 FLV 流数据
"""

import asyncio
import time
import threading
import urllib.request
import urllib.parse
import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, HTMLResponse, PlainTextResponse
import uvicorn

PORT = int(os.environ.get("PORT", 7860))
_room_ids_env = os.environ.get("ROOM_IDS", os.environ.get("ROOM_ID", "27519423"))
ROOM_IDS = [int(x.strip()) for x in _room_ids_env.split(",") if x.strip()]
DEFAULT_QN = int(os.environ.get("DEFAULT_QN", 80))
CACHE_TTL = 3000  # 50 分钟

API_URL = "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://live.bilibili.com/",
}

_cache = {}
_cache_lock = threading.Lock()

app = FastAPI(title="Bilibili Live FLV Proxy")


def fetch_flv_url(room_id: int, qn: int = DEFAULT_QN) -> str | None:
    params = urllib.parse.urlencode({
        "room_id": room_id,
        "protocol": "0,1",
        "format": "0,1,2",
        "codec": "0,1",
        "qn": qn,
        "platform": "web",
        "ptype": "8",
    })
    req = urllib.request.Request(f"{API_URL}?{params}", headers=HEADERS)
    data = json.loads(urllib.request.urlopen(req, timeout=10).read())
    playurl = data["data"]["playurl_info"]["playurl"]

    for stream in playurl["stream"]:
        if stream["protocol_name"] != "http_stream":
            continue
        for fmt in stream["format"]:
            if fmt["format_name"] != "flv":
                continue
            for codec in fmt["codec"]:
                if codec["codec_name"] != "avc":
                    continue
                if codec["url_info"]:
                    info = codec["url_info"][0]
                    return info["host"] + codec["base_url"] + info["extra"]
    return None


def get_cached_url(room_id: int) -> str | None:
    now = time.time()
    with _cache_lock:
        entry = _cache.get(room_id)
        if entry and entry["expires_at"] > now:
            return entry["url"]

    url = fetch_flv_url(room_id)
    if url is None:
        with _cache_lock:
            stale = _cache.get(room_id)
            if stale:
                return stale["url"]
        return None

    with _cache_lock:
        _cache[room_id] = {"url": url, "expires_at": now + CACHE_TTL}
    return url


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # 服务端环境变量作为初始种子，但前端可以自行添加删除房间
    server_rooms = ROOM_IDS

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bilibili Live FLV Proxy</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500&family=Outfit:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    background: #06090f;
    color: #cdd6e4;
    font-family: 'Outfit', -apple-system, sans-serif;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    background-image: radial-gradient(ellipse 80% 50% at 50% -20%, #1a2840 0%, transparent 60%);
  }}
  .card {{
    background: #0d1117;
    border: 1px solid #21262d;
    border-radius: 20px;
    padding: 36px 32px 28px;
    max-width: 640px;
    width: 92%;
    box-shadow: 0 0 0 1px rgba(255,255,255,0.03), 0 8px 32px rgba(0,0,0,0.4);
  }}
  .header {{
    text-align: center;
    margin-bottom: 24px;
  }}
  h1 {{
    font-size: 22px;
    font-weight: 600;
    letter-spacing: -0.3px;
    color: #e6edf3;
    margin-bottom: 10px;
  }}
  .badge-row {{
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
  }}
  .status-badge {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 5px 14px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.8px;
    text-transform: uppercase;
  }}
  .status-badge::before {{
    content: '';
    width: 6px; height: 6px;
    border-radius: 50%;
  }}
  .status-badge.live {{
    background: #22c55e15;
    color: #22c55e;
    border: 1px solid #22c55e30;
  }}
  .status-badge.live::before {{
    background: #22c55e;
    box-shadow: 0 0 6px #22c55e80;
  }}
  .status-badge.some-offline {{
    background: #f59e0b15;
    color: #f59e0b;
    border: 1px solid #f59e0b30;
  }}
  .status-badge.some-offline::before {{
    background: #f59e0b;
    box-shadow: 0 0 6px #f59e0b80;
  }}
  .status-badge.all-offline {{
    background: #ef444415;
    color: #ef4444;
    border: 1px solid #ef444430;
  }}
  .status-badge.all-offline::before {{
    background: #ef4444;
    box-shadow: 0 0 6px #ef444480;
  }}
  .add-row {{
    display: flex;
    gap: 8px;
    margin-bottom: 16px;
  }}
  .add-input {{
    flex: 1;
    font-family: 'JetBrains Mono', 'Outfit', monospace;
    font-size: 13px;
    padding: 10px 14px;
    background: #0a0e14;
    border: 1px solid #1a1f2b;
    border-radius: 8px;
    color: #e6edf3;
    outline: none;
    transition: border-color 0.2s;
  }}
  .add-input::placeholder {{
    color: #484f58;
  }}
  .add-input:focus {{
    border-color: #58a6ff;
  }}
  .add-btn {{
    font-family: 'Outfit', sans-serif;
    padding: 10px 18px;
    font-size: 13px;
    font-weight: 600;
    background: #238636;
    color: #fff;
    border: none;
    border-radius: 8px;
    cursor: pointer;
    white-space: nowrap;
    transition: background 0.18s;
  }}
  .add-btn:hover {{
    background: #2ea043;
  }}
  .add-btn:disabled {{
    background: #1a2a1e;
    color: #484f58;
    cursor: not-allowed;
  }}
  .room-list {{
    margin-bottom: 4px;
  }}
  .room-row {{
    display: flex;
    flex-direction: column;
    gap: 6px;
    background: #0d1117;
    border: 1px solid #1a1f2b;
    border-radius: 12px;
    padding: 14px 16px;
    margin: 8px 0;
    transition: border-color 0.2s;
  }}
  .room-row:hover {{
    border-color: #2a3344;
  }}
  .room-header {{
    display: flex;
    align-items: center;
    gap: 8px;
  }}
  .room-dot {{
    width: 8px; height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }}
  .room-dot.online {{
    background: #22c55e;
    box-shadow: 0 0 6px #22c55e60;
  }}
  .room-dot.offline {{
    background: #ef4444;
    box-shadow: 0 0 6px #ef444460;
  }}
  .room-label {{
    font-size: 14px;
    font-weight: 600;
    color: #e6edf3;
    flex: 1;
  }}
  .room-status {{
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.6px;
    text-transform: uppercase;
    padding: 2px 8px;
    border-radius: 4px;
  }}
  .room-status.online {{
    color: #22c55e;
    background: #22c55e14;
  }}
  .room-status.offline {{
    color: #ef4444;
    background: #ef444414;
  }}
  .room-del {{
    font-family: 'Outfit', sans-serif;
    font-size: 12px;
    padding: 4px 10px;
    background: transparent;
    color: #484f58;
    border: 1px solid #1a1f2b;
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.18s;
  }}
  .room-del:hover {{
    color: #ef4444;
    border-color: #ef444450;
    background: #ef444410;
  }}
  .url-row {{
    display: flex;
    align-items: stretch;
    gap: 8px;
  }}
  .url-box {{
    font-family: 'JetBrains Mono', 'SF Mono', monospace;
    font-size: 11.5px;
    font-weight: 400;
    color: #7d8590;
    background: #0a0e14;
    border: 1px solid #161b22;
    border-radius: 6px;
    padding: 8px 12px;
    word-break: break-all;
    user-select: all;
    line-height: 1.5;
    flex: 1;
    min-width: 0;
  }}
  .copy-btn {{
    font-family: 'Outfit', sans-serif;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    background: #1a1f2b;
    color: #7d8590;
    border: 1px solid #2a3344;
    border-radius: 8px;
    padding: 8px 14px;
    font-size: 12px;
    font-weight: 500;
    cursor: pointer;
    white-space: nowrap;
    flex-shrink: 0;
    transition: all 0.18s;
    letter-spacing: 0.3px;
  }}
  .copy-btn:hover {{
    background: #212838;
    color: #e6edf3;
    border-color: #4a5568;
  }}
  .copy-btn.copied {{
    background: #22c55e15;
    color: #22c55e;
    border-color: #22c55e40;
  }}
  .empty-hint {{
    text-align: center;
    font-size: 13px;
    color: #343942;
    padding: 20px 0;
  }}
  .info {{
    text-align: center;
    font-size: 11px;
    color: #484f58;
    margin-top: 16px;
    line-height: 1.7;
  }}
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <h1>Bilibili Live FLV Proxy</h1>
    <div class="badge-row">
      <div class="status-badge live" id="status-badge">LOADING</div>
    </div>
  </div>
  <div class="add-row">
    <input class="add-input" id="add-input" type="number" placeholder="Enter Bilibili room ID..." min="1" max="999999999">
    <button class="add-btn" id="add-btn">Add Room</button>
  </div>
  <div class="room-list" id="room-list">
    <div class="empty-hint">Loading rooms...</div>
  </div>
  <div class="info">
    CDN signature auto-refreshes every {CACHE_TTL//60} min &middot; Rooms saved in your browser
  </div>
</div>
<script>
(function() {{
  const STORAGE_KEY = 'bili_rooms';
  const SERVER_SEEDS = {json.dumps(server_rooms)};

  function loadRooms() {{
    let ids = [];
    try {{
      const raw = localStorage.getItem(STORAGE_KEY);
      ids = raw ? JSON.parse(raw) : [];
    }} catch(e) {{}}
    if (!Array.isArray(ids) || ids.length === 0) {{
      ids = SERVER_SEEDS;
    }}
    return [...new Set(ids.filter(id => Number.isInteger(id) && id > 0))];
  }}

  function saveRooms(ids) {{
    localStorage.setItem(STORAGE_KEY, JSON.stringify(ids));
  }}

  function buildUrl(rid) {{
    return window.location.origin + '/live/' + rid + '.flv';
  }}

  function render() {{
    const ids = loadRooms();
    const list = document.getElementById('room-list');
    const badge = document.getElementById('status-badge');

    if (ids.length === 0) {{
      list.innerHTML = '<div class="empty-hint">No rooms added yet. Enter a room ID above.</div>';
      badge.textContent = 'NO ROOMS';
      badge.className = 'status-badge all-offline';
      return;
    }}

    list.innerHTML = ids.map(rid => {{
      return '<div class="room-row" data-rid="' + rid + '">'
        + '<div class="room-header">'
        + '<span class="room-dot" id="dot-' + rid + '"></span>'
        + '<span class="room-label">Room ' + rid + '</span>'
        + '<span class="room-status" id="st-' + rid + '">...</span>'
        + '<button class="room-del" data-rid="' + rid + '">Remove</button>'
        + '</div>'
        + '<div class="url-row">'
        + '<div class="url-box">' + buildUrl(rid) + '</div>'
        + '<button class="copy-btn" data-rid="' + rid + '">Copy</button>'
        + '</div>'
        + '</div>';
    }}).join('');

    // Copy buttons
    list.querySelectorAll('.copy-btn').forEach(btn => {{
      btn.addEventListener('click', () => {{
        const rid = btn.dataset.rid;
        navigator.clipboard.writeText(buildUrl(rid)).then(() => {{
          btn.textContent = 'Copied';
          btn.classList.add('copied');
          setTimeout(() => {{
            btn.textContent = 'Copy';
            btn.classList.remove('copied');
          }}, 1800);
        }});
      }});
    }});

    // Delete buttons
    list.querySelectorAll('.room-del').forEach(btn => {{
      btn.addEventListener('click', () => {{
        const rid = Number(btn.dataset.rid);
        const ids = loadRooms().filter(id => id !== rid);
        saveRooms(ids);
        render();
      }});
    }});

    // Check live status
    updateStatuses();
  }}

  async function updateStatuses() {{
    const ids = loadRooms();
    let online = 0;
    let offline = 0;

    for (const rid of ids) {{
      const dot = document.getElementById('dot-' + rid);
      const st = document.getElementById('st-' + rid);
      if (!dot || !st) continue;

      try {{
        const resp = await fetch('/live/' + rid + '.flv', {{ method: 'HEAD' }});
        if (resp.ok) {{
          dot.className = 'room-dot online';
          st.textContent = 'ONLINE';
          st.className = 'room-status online';
          online++;
        }} else {{
          throw new Error();
        }}
      }} catch(e) {{
        dot.className = 'room-dot offline';
        st.textContent = 'OFFLINE';
        st.className = 'room-status offline';
        offline++;
      }}
    }}

    const badge = document.getElementById('status-badge');
    if (online > 0 && offline === 0) {{
      badge.textContent = 'ALL ONLINE';
      badge.className = 'status-badge live';
    }} else if (online > 0) {{
      badge.textContent = 'SOME OFFLINE';
      badge.className = 'status-badge some-offline';
    }} else if (ids.length > 0) {{
      badge.textContent = 'ALL OFFLINE';
      badge.className = 'status-badge all-offline';
    }}
  }}

  // Add room
  document.getElementById('add-btn').addEventListener('click', () => {{
    const input = document.getElementById('add-input');
    const val = parseInt(input.value, 10);
    if (!val || val < 1) return;
    const ids = loadRooms();
    if (ids.includes(val)) {{
      input.value = '';
      input.placeholder = 'Already added';
      setTimeout(() => input.placeholder = 'Enter Bilibili room ID...', 1500);
      return;
    }}
    ids.push(val);
    saveRooms(ids);
    input.value = '';
    render();
  }});

  document.getElementById('add-input').addEventListener('keydown', (e) => {{
    if (e.key === 'Enter') document.getElementById('add-btn').click();
  }});

  render();
}})();
</script>
</body>
</html>"""


@app.get("/live/{room_id}.flv")
async def proxy_flv(room_id: int):
    cdn_url = get_cached_url(room_id)
    if cdn_url is None:
        return PlainTextResponse("Room offline or fetch failed", status_code=502)

    async def stream():
        loop = asyncio.get_event_loop()

        def _fetch():
            req = urllib.request.Request(cdn_url, headers={
                "User-Agent": HEADERS["User-Agent"],
                "Referer": "https://live.bilibili.com/",
            })
            return urllib.request.urlopen(req, timeout=30)

        try:
            upstream = await loop.run_in_executor(None, _fetch)
            while True:
                chunk = await loop.run_in_executor(None, upstream.read, 65536)
                if not chunk:
                    break
                yield chunk
        except Exception:
            pass

    return StreamingResponse(
        stream(),
        media_type="video/x-flv",
        headers={
            "Cache-Control": "no-cache",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.get("/health")
async def health():
    return {"status": "ok", "rooms": ROOM_IDS}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)