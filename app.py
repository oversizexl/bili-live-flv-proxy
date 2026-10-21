#!/usr/bin/env python3
"""
Bilibili Live FLV Proxy — Hugging Face Space
固定直链 → 自动刷新签名 → 透传 FLV 流数据
"""

import asyncio
import time
import logging
import urllib.request
import urllib.parse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, HTMLResponse, PlainTextResponse, Response
import uvicorn

PORT = int(os.environ.get("PORT", 7860))
_room_ids_env = os.environ.get("ROOM_IDS", os.environ.get("ROOM_ID", "27519423"))
ROOM_IDS = [int(x.strip()) for x in _room_ids_env.split(",") if x.strip()]
DEFAULT_QN = int(os.environ.get("DEFAULT_QN", 80))
CACHE_TTL = 3000  # 50 分钟
CHUNK_SIZE = 65536
MAX_TAG_SIZE = 16 * 1024 * 1024
UPSTREAM_TIMEOUT = 30

API_URL = "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://live.bilibili.com/",
}

logger = logging.getLogger("bili-proxy")
logger.setLevel(logging.INFO)

_cache = {}
_url_locks = {}
_room_state = {}
_last_error_log = {}
# 每次读取都会占用一个线程直到有数据返回，独立线程池避免和其他请求互相排队
_io_executor = ThreadPoolExecutor(max_workers=64, thread_name_prefix="flv-io")

app = FastAPI(title="Bilibili Live FLV Proxy")


def fetch_flv_url(room_id: int, qn: int = DEFAULT_QN) -> tuple[str | None, int | None]:
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
    info = data.get("data") or {}
    status = info.get("live_status")
    playurl = (info.get("playurl_info") or {}).get("playurl")
    if playurl is None:
        return None, status

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
                    url_info = codec["url_info"][0]
                    return url_info["host"] + codec["base_url"] + url_info["extra"], status
    return None, status


def _parse_expires(url: str) -> float | None:
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        value = (query.get("expires") or query.get("deadline") or [None])[0]
        return float(value) if value else None
    except (TypeError, ValueError):
        return None


def _set_room_state(room_id: int, state: str) -> bool:
    if _room_state.get(room_id) == state:
        return False
    _room_state[room_id] = state
    return True


def _status_text(status: int | None) -> str:
    return {0: "未开播", 1: "直播中", 2: "轮播"}.get(status, f"未知({status})")


def _sig_left_text(url: str) -> str:
    deadline = _parse_expires(url)
    if not deadline:
        return "未知"
    return f"{max(int(deadline - time.time()), 0) // 60}分"


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}小时{minutes}分"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


def _fmt_bytes(count: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if count < 1024 or unit == "TB":
            return f"{int(count)}B" if unit == "B" else f"{count:.1f}{unit}"
        count /= 1024


async def get_cached_url(room_id: int, force: bool = False) -> str | None:
    entry = _cache.get(room_id)
    if not force and entry and entry["expires_at"] > time.time():
        return entry["url"]

    lock = _url_locks.setdefault(room_id, asyncio.Lock())
    async with lock:
        entry = _cache.get(room_id)
        if entry:
            # 10 秒内刚刷新过就直接复用，避免多个观众同时重连时重复请求 API
            if time.time() - entry["fetched_at"] < 10:
                return entry["url"]
            if not force and entry["expires_at"] > time.time():
                return entry["url"]

        loop = asyncio.get_running_loop()
        status = None
        fetch_failed = False
        try:
            url, status = await loop.run_in_executor(None, fetch_flv_url, room_id)
        except Exception as exc:
            url = None
            fetch_failed = True
            now = time.time()
            if now - _last_error_log.get(room_id, 0) >= 60:
                _last_error_log[room_id] = now
                logger.warning(
                    "房间=%s 获取播放地址失败 类型=%s 详情=%s",
                    room_id, type(exc).__name__, exc,
                )

        if url is None:
            if not fetch_failed and _set_room_state(room_id, "offline"):
                logger.info("房间=%s 未开播 状态=%s", room_id, _status_text(status))
            entry = _cache.get(room_id)
            return entry["url"] if entry else None

        if _set_room_state(room_id, "live"):
            logger.info("房间=%s 直播中", room_id)

        now = time.time()
        deadline = _parse_expires(url)
        expires_at = now + CACHE_TTL
        if deadline:
            # 保证新连接拿到的签名至少还有 5 分钟有效期
            expires_at = min(expires_at, deadline - 300)
        _cache[room_id] = {
            "url": url,
            "expires_at": expires_at,
            "fetched_at": now,
            "deadline": deadline,
        }
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

  function isSeed(rid) {{
    return SERVER_SEEDS.includes(rid);
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
      const seed = isSeed(rid);
      return '<div class="room-row" data-rid="' + rid + '">'
        + '<div class="room-header">'
        + '<span class="room-dot" id="dot-' + rid + '"></span>'
        + '<span class="room-label">Room ' + rid + (seed ? ' <span style="font-size:10px;color:#484f58;font-weight:400;">(default)</span>' : '') + '</span>'
        + '<span class="room-status" id="st-' + rid + '">...</span>'
        + (seed ? '' : '<button class="room-del" data-rid="' + rid + '">Remove</button>')
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


def _open_upstream(url: str):
    req = urllib.request.Request(url, headers={
        "User-Agent": HEADERS["User-Agent"],
        "Referer": "https://live.bilibili.com/",
    })
    return urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT)


class FlvStream:
    """把多次上游连接的 FLV 数据拼成一条连续流：后续连接去掉新的 FLV 头并接续时间戳。"""

    def __init__(self):
        self._last_ts = 0
        self._emitted_header = False
        self._passthrough = False
        self._start_connection()

    def next_connection(self):
        self._start_connection()

    @property
    def is_first(self) -> bool:
        return self._first

    @property
    def offset(self) -> int | None:
        return self._offset

    def _start_connection(self):
        self._buf = bytearray()
        self._header_done = False
        self._first = not self._emitted_header
        self._offset = 0 if self._first else None

    def feed(self, chunk: bytes) -> bytes:
        if self._passthrough:
            return chunk

        self._buf.extend(chunk)
        out = bytearray()

        if not self._header_done:
            if len(self._buf) < 3:
                return b""
            if self._buf[:3] != b"FLV":
                self._passthrough = True
                out.extend(self._buf)
                self._buf.clear()
                return bytes(out)
            if len(self._buf) < 13:
                return b""
            if self._first:
                out.extend(self._buf[:13])
                self._emitted_header = True
            del self._buf[:13]
            self._header_done = True

        while len(self._buf) >= 11:
            size = int.from_bytes(self._buf[1:4], "big")
            if size > MAX_TAG_SIZE:
                logger.warning("FLV tag 大小异常 %d，丢弃缓冲数据", size)
                self._buf.clear()
                break
            total = 11 + size + 4
            if len(self._buf) < total:
                break

            tag_type = self._buf[0]
            ts = int.from_bytes(self._buf[4:7], "big") | (self._buf[7] << 24)
            if self._offset is None:
                self._offset = self._last_ts + 1 - ts
            new_ts = (ts + self._offset) & 0xFFFFFFFF

            tag = bytearray(self._buf[:total])
            tag[4:7] = (new_ts & 0xFFFFFF).to_bytes(3, "big")
            tag[7] = new_ts >> 24
            del self._buf[:total]

            if not self._first and tag_type == 18:
                continue
            self._last_ts = new_ts
            out.extend(tag)

        return bytes(out)


@app.get("/live/{room_id}.flv")
@app.head("/live/{room_id}.flv")
async def proxy_flv(room_id: int, request: Request):
    cdn_url = await get_cached_url(room_id)
    if cdn_url is None:
        return PlainTextResponse("Room offline or fetch failed", status_code=502)

    headers = {
        "Cache-Control": "no-cache",
        "Access-Control-Allow-Origin": "*",
    }
    if request.method == "HEAD":
        return Response(status_code=200, headers=headers)

    client = request.client.host if request.client else "未知"
    logger.info(
        "房间=%s 客户端开始拉流 来源=%s 签名剩余=%s",
        room_id, client, _sig_left_text(cdn_url),
    )

    async def stream():
        loop = asyncio.get_running_loop()
        url = cdn_url
        flv = FlvStream()
        failures = 0
        started = time.monotonic()
        total_bytes = 0

        try:
            while True:
                upstream = None
                received = False
                error = None
                resumed = False
                conn_started = time.monotonic()
                conn_bytes = 0
                try:
                    upstream = await loop.run_in_executor(
                        _io_executor, _open_upstream, url
                    )
                    node = urllib.parse.urlparse(url).netloc
                    if flv.is_first:
                        logger.info("房间=%s 上游连接成功 节点=%s", room_id, node)
                    while True:
                        chunk = await loop.run_in_executor(
                            _io_executor, upstream.read1, CHUNK_SIZE
                        )
                        if not chunk:
                            break
                        received = True
                        conn_bytes += len(chunk)
                        data = flv.feed(chunk)
                        if data:
                            total_bytes += len(data)
                            yield data
                        if not resumed and not flv.is_first and flv.offset is not None:
                            logger.info(
                                "房间=%s 重连成功 节点=%s 时间戳偏移=%d毫秒",
                                room_id, node, flv.offset,
                            )
                            resumed = True
                except Exception as exc:
                    error = exc
                finally:
                    if upstream is not None:
                        try:
                            upstream.close()
                        except Exception:
                            pass

                # 上游结束或失败后刷新签名重连，对播放器保持同一个 HTTP 响应不中断
                if received:
                    if failures:
                        logger.info("房间=%s 连接已恢复 共尝试=%d次", room_id, failures)
                    failures = 0
                    logger.info(
                        "房间=%s 上游断开 原因=%s 时长=%s 流量=%s",
                        room_id,
                        f"异常({type(error).__name__})" if error else "正常结束",
                        _fmt_duration(time.monotonic() - conn_started),
                        _fmt_bytes(conn_bytes),
                    )
                    await asyncio.sleep(0.5)
                else:
                    failures += 1
                    delay = min(2 ** failures, 15)
                    if failures == 1 or failures % 5 == 0:
                        logger.warning(
                            "房间=%s 上游连接失败 类型=%s 详情=%s 已尝试=%d次 下次重试=%d秒",
                            room_id,
                            type(error).__name__ if error else "无数据",
                            error if error else "上游未返回数据",
                            failures,
                            delay,
                        )
                    await asyncio.sleep(delay)

                flv.next_connection()

                attempts = 0
                while True:
                    url = await get_cached_url(room_id, force=True)
                    if url:
                        if failures == 0:
                            logger.info(
                                "房间=%s 开始重连 签名剩余=%s",
                                room_id, _sig_left_text(url),
                            )
                        break
                    attempts += 1
                    if attempts % 12 == 1:
                        logger.warning("房间=%s 无可用播放地址 正在重试", room_id)
                    await asyncio.sleep(5)
        finally:
            logger.info(
                "房间=%s 客户端断开 总时长=%s 总流量=%s",
                room_id,
                _fmt_duration(time.monotonic() - started),
                _fmt_bytes(total_bytes),
            )

    return StreamingResponse(stream(), media_type="video/x-flv", headers=headers)


@app.get("/health")
async def health():
    return {"status": "ok", "rooms": ROOM_IDS}


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("服务启动 端口=%s 房间=%s", PORT, ROOM_IDS)
    uvicorn.run(app, host="0.0.0.0", port=PORT, access_log=False)