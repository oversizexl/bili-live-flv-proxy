# Bilibili Live FLV Proxy

把 Bilibili 直播间转成固定 FLV 直链。无需登录、无需 Cookie，自动处理 CDN 签名刷新。

## 为什么

Bilibili 直播流 FLV URL 带有 CDN 签名 (`sign`/`upsig`)，有效期约 60 分钟。一些第三方电台/播放器 App 不支持 302 跳转，每次过期后需要手动获取新链接。

这个代理项目提供一个**永久不变的 URL**，背后自动从 Bilibili API 获取最新签名地址并透传 FLV 流数据。

## 使用

部署后，访问首页获取你的固定直链：

```
https://your-space.hf.space/live/27519423.flv
```

把此链接填入 Lofi-Radio、VLC、PotPlayer 等任意播放器即可。

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `ROOM_ID` | `27519423` | Bilibili 直播间 ID |
| `DEFAULT_QN` | `80` | 画质 (80=流畅 150=高清 250=超清 400=蓝光 10000=原画) |
| `PORT` | `7860` | 监听端口 |

## 部署

### Docker

```bash
# 构建镜像
docker build -t bili-live-flv-proxy .

# 运行容器
docker run -d --name bili-live-proxy -p 7860:7860 -e ROOM_ID=27519423 bili-live-flv-proxy

# 查看日志
docker logs -f bili-live-proxy
```

定制直播间和画质：

```bash
docker run -d --name bili-live-proxy -p 7860:7860 \
  -e ROOM_ID=27519423 \
  -e DEFAULT_QN=10000 \
  bili-live-flv-proxy
```

### Docker Compose

```yaml
services:
  bili-proxy:
    build: .
    ports:
      - "7860:7860"
    environment:
      - ROOM_ID=27519423
      - DEFAULT_QN=80
    restart: unless-stopped
```

### Hugging Face

1. Fork 本仓库
2. 在 HF 创建 Space，选 Docker SDK，关联仓库
3. 构建完成后即可使用

### 本地运行

```bash
pip install -r requirements.txt
python app.py
```

## 原理

```
播放器 → 固定 URL (本代理) → Bilibili API (getRoomPlayInfo) → CDN FLV 流
                                ↑ 缓存 50 分钟，自动刷新签名
```

`getRoomPlayInfo` 接口无需登录和 Wbi 签名，`sign`/`upsig` 由 Bilibili CDN 服务端签发，本代理不涉及签名计算。

## License

MIT