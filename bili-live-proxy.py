#!/usr/bin/env python3
"""
Bilibili Live FLV Streaming Proxy
提供固定直链 → 自动刷新签名 → 透传 FLV 流数据

用法: python3 bili-live-proxy.py
固定链接: http://localhost:8765/live/27519423.flv

App 直接播放这个固定 URL 即可，代理会处理签名过期问题。
"""

import http.server
import json
import socket
import threading
import time
import urllib.request
import urllib.parse

PORT = 8765
ROOM_ID = 27519423
DEFAULT_QN = 80  # 流畅
API_URL = "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://live.bilibili.com/",
}

# 缓存: room_id -> {url, expires_at}
_cache = {}
_cache_lock = threading.Lock()
CACHE_TTL = 3000  # 50 分钟, CDN 签名有效期约 60 分钟


def fetch_flv_url(room_id, qn=DEFAULT_QN):
    """从 Bilibili API 获取最新签名 FLV URL"""
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


def get_cached_url(room_id):
    """获取缓存的 CDN URL，过期自动刷新"""
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


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if not self.path.startswith("/live/"):
            self._text(404, "Usage: /live/<room_id>.flv\n")
            return

        path = self.path.rstrip("/")
        room_id_str = path.split("/")[-1].replace(".flv", "").replace(".m3u8", "")
        try:
            room_id = int(room_id_str)
        except ValueError:
            self._text(400, "Invalid room_id\n")
            return

        cdn_url = get_cached_url(room_id)
        if cdn_url is None:
            self._text(502, "Failed to fetch stream. Room may be offline.\n")
            return

        print(f"[{self.log_date_time_string()}] PROXY room={room_id} -> CDN")

        try:
            req = urllib.request.Request(cdn_url, headers={
                "User-Agent": HEADERS["User-Agent"],
                "Referer": "https://live.bilibili.com/",
            })
            with urllib.request.urlopen(req, timeout=30) as upstream:
                self.send_response(200)
                self.send_header("Content-Type", "video/x-flv")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()

                # 流式透传
                while True:
                    chunk = upstream.read(65536)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break
        except Exception as e:
            # 客户端断开时静默, 其他错误打日志
            if not isinstance(e, (BrokenPipeError, ConnectionResetError)):
                print(f"[!] Stream error: {e}")

    def _text(self, code, msg):
        body = msg.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # 用上面自己的 print

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            pass


def main():
    print(f" Bilibili Live FLV Streaming Proxy")
    print(f" Fixed URL: http://localhost:{PORT}/live/{ROOM_ID}.flv")
    print()
    print(f"[*] Testing room {ROOM_ID}...")
    url = get_cached_url(ROOM_ID)
    if url:
        print(f"[+] Live stream OK.")
        print(f"[+] Add this to your radio app: http://localhost:{PORT}/live/{ROOM_ID}.flv")
    else:
        print("[!] Warning: Room may be offline.")

    # 允许多个并发连接
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), ProxyHandler)
    server.daemon_threads = True
    print(f"[*] Listening on port {PORT}...")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
