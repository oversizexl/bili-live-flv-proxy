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
    rooms_status = []
    all_online = True
    for rid in ROOM_IDS:
        url = get_cached_url(rid)
        rooms_status.append({"id": rid, "online": url is not None})
        if url is None:
            all_online = False
    status = "ALL ONLINE" if all_online else "SOME OFFLINE"
    status_color = "#22c55e" if all_online else "#f59e0b"
    status_icon = "&#9679;" if all_online else "&#9679;"

    room_rows = "".join([
        f'<div class="room-row">'
        f'<div class="room-info">'
        f'<div class="room-header"><span class="room-dot" style="background:{"#22c55e" if r["online"] else "#ef4444"}"></span>'
        f'<span class="room-label">Room {r["id"]}</span>'
        f'<span class="room-status {"online" if r["online"] else "offline"}">{"ONLINE" if r["online"] else "OFFLINE"}</span>'
        f'</div>'
        f'<div class="url-row">'
        f'<div class="url-box" id="url-{r["id"]}"></div>'
        f'<button class="copy-btn" data-rid="{r["id"]}">Copy</button>'
        f'</div></div>'
        f'</div>'
        for r in rooms_status
    ])

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
    background: {status_color}15;
    color: {status_color};
    border: 1px solid {status_color}30;
  }}
  .status-badge::before {{
    content: '';
    width: 6px; height: 6px;
    border-radius: 50%;
    background: {status_color};
    box-shadow: 0 0 6px {status_color}80;
  }}
  .room-row {{
    display: flex;
    align-items: flex-start;
    gap: 12px;
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
  .room-info {{
    flex: 1;
    min-width: 0;
  }}
  .room-header {{
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
  }}
  .room-dot {{
    width: 8px; height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }}
  .room-dot[style*="22c55e"] {{
    box-shadow: 0 0 6px #22c55e60;
  }}
  .room-dot[style*="ef4444"] {{
    box-shadow: 0 0 6px #ef444460;
  }}
  .room-label {{
    font-size: 14px;
    font-weight: 600;
    color: #e6edf3;
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
  .url-row {{
    display: flex;
    align-items: stretch;
    gap: 8px;
    margin-top: 6px;
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
  .copy-btn svg {{
    width: 14px; height: 14px;
    opacity: 0.7;
    flex-shrink: 0;
  }}
  .info {{
    text-align: center;
    font-size: 11px;
    color: #484f58;
    margin-top: 20px;
    line-height: 1.7;
  }}
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <h1>Bilibili Live FLV Proxy</h1>
    <div class="status-badge">{status}</div>
  </div>
  {room_rows}
  <div class="info">
    Add these URLs to your radio app &middot; CDN signature auto-refreshes every {CACHE_TTL//60} min
  </div>
</div>
<script>
(function() {{
  document.querySelectorAll('.url-row').forEach(row => {{
    const urlBox = row.querySelector('.url-box');
    const btn = row.querySelector('.copy-btn');
    const rid = btn.dataset.rid;
    const fullUrl = window.location.origin + '/live/' + rid + '.flv';
    urlBox.textContent = fullUrl;

    btn.addEventListener('click', () => {{
      navigator.clipboard.writeText(fullUrl).then(() => {{
        btn.textContent = 'Copied';
        btn.classList.add('copied');
        setTimeout(() => {{
          btn.textContent = 'Copy';
          btn.classList.remove('copied');
        }}, 1800);
      }});
    }});
  }});
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