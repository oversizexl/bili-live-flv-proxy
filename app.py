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
ROOM_ID = int(os.environ.get("ROOM_ID", 27519423))
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
    room_id = ROOM_ID
    cached = get_cached_url(room_id)
    status = "ONLINE" if cached else "OFFLINE"
    proxy_url = f"{request.base_url}live/{room_id}.flv"

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
    max-width: 520px;
    width: 90%;
    text-align: center;
  }}
  h1 {{ font-size: 20px; font-weight: 700; margin-bottom: 8px; }}
  .status {{
    display: inline-block;
    width: 10px; height: 10px;
    border-radius: 50%;
    margin-right: 6px;
    background: {"#22c55e" if cached else "#ef4444"};
  }}
  .url-box {{
    background: #0a0f18;
    border: 1px solid #1e2a3a;
    border-radius: 10px;
    padding: 14px 16px;
    margin: 20px 0;
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 13px;
    word-break: break-all;
    color: #a0c4ff;
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
    margin-bottom: 16px;
    transition: background .2s;
  }}
  .copy-btn:hover {{ background: #2a3a4a; }}
  .info {{ font-size: 12px; color: #6b7280; line-height: 1.6; }}
  a {{ color: #a0c4ff; }}
</style>
</head>
<body>
<div class="card">
  <h1><span class="status"></span>Bilibili Live FLV Proxy</h1>
  <p style="color:#6b7280;font-size:13px;">Room {room_id} · {status}</p>
  <div class="url-box" id="url">{proxy_url}</div>
  <button class="copy-btn" onclick="navigator.clipboard.writeText(document.getElementById('url').textContent);this.textContent='Copied!';setTimeout(()=>this.textContent='Copy URL',2000)">Copy URL</button>
  <div class="info">
    Add this URL to your radio app.<br>
    Auto-refreshes CDN signature every {CACHE_TTL//60} min.
    <br><br>
    <a href="/live/{room_id}.flv" target="_blank">Open FLV stream →</a>
  </div>
</div>
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
    return {"status": "ok", "room": ROOM_ID}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)