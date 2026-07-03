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

    room_rows = "".join([
        f'<div class="room-row"><span class="room-dot" style="background:{"#22c55e" if r["online"] else "#ef4444"}"></span>'
        f'Room {r["id"]} · {"ONLINE" if r["online"] else "OFFLINE"}'
        f'<div class="url-box" id="url-{r["id"]}"></div></div>'
        for r in rooms_status
    ])

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bilibili Live FLV Proxy</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    background: #0a0f18;
    color: #e0e0e0;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
  }}
  .card {{
    background: #141b2d;
    border: 1px solid #1e2a3a;
    border-radius: 16px;
    padding: 40px;
    max-width: 600px;
    width: 90%;
    text-align: center;
  }}
  h1 {{ font-size: 20px; font-weight: 700; margin-bottom: 8px; }}
  .status-badge {{
    display: inline-block;
    padding: 4px 12px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.5px;
    background: {status_color}22;
    color: {status_color};
    border: 1px solid {status_color}44;
    margin-bottom: 20px;
  }}
  .room-row {{
    background: #0a0f18;
    border: 1px solid #1e2a3a;
    border-radius: 10px;
    padding: 14px 16px;
    margin: 10px 0;
    text-align: left;
    font-size: 13px;
    color: #6b7280;
  }}
  .room-dot {{
    display: inline-block;
    width: 8px; height: 8px;
    border-radius: 50%;
    margin-right: 6px;
  }}
  .url-box {{
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 12px;
    margin-top: 8px;
    color: #a0c4ff;
    word-break: break-all;
    user-select: all;
  }}
  .copy-btn {{
    background: #1e2a3a;
    color: #e0e0e0;
    border: 1px solid #2a3a4a;
    border-radius: 8px;
    padding: 10px 24px;
    font-size: 14px;
    cursor: pointer;
    margin-top: 16px;
    transition: background .2s;
  }}
  .copy-btn:hover {{ background: #2a3a4a; }}
  .info {{ font-size: 12px; color: #6b7280; line-height: 1.6; margin-top: 16px; }}
  a {{ color: #a0c4ff; }}
</style>
</head>
<body>
<div class="card">
  <h1>Bilibili Live FLV Proxy</h1>
  <div class="status-badge">{status}</div>
  {room_rows}
  <div class="info">
    Add these URLs to your radio app.<br>
    Auto-refreshes CDN signature every {CACHE_TTL//60} min.
  </div>
</div>
<script>
  document.querySelectorAll('.url-box').forEach(el => {{
    const rid = el.id.replace('url-','');
    const origin = window.location.origin;
    const path = '/live/' + rid + '.flv';
    const fullUrl = origin + path;
    el.textContent = fullUrl;

    const btn = document.createElement('button');
    btn.className = 'copy-btn';
    btn.textContent = 'Copy';
    btn.onclick = () => {{
      navigator.clipboard.writeText(fullUrl);
      btn.textContent = 'Copied!';
      setTimeout(() => btn.textContent = 'Copy', 2000);
    }};
    el.parentElement.appendChild(btn);
  }});
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