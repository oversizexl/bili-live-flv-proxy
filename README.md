<img width="1284" height="548" alt="image" src="https://github.com/user-attachments/assets/9458a61c-d0a0-4307-a8dd-93608f2b0aa8" />


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
| `ROOM_IDS` | `27519423` | 直播间 ID，多个用逗号分隔，如 `27519423,12345678` |
| `DEFAULT_QN` | `80` | 画质 (80=流畅 150=高清 250=超清 400=蓝光 10000=原画) |
| `PORT` | `7860` | 监听端口 |

## 部署

### Docker

```bash
# 构建镜像
docker build -t bili-live-flv-proxy .

# 运行容器
docker run -d --name bili-live-proxy -p 7860:7860 -e ROOM_IDS=27519423 bili-live-flv-proxy

# 查看日志
docker logs -f bili-live-proxy
```

定制直播间和画质：

```bash
docker run -d --name bili-live-proxy -p 7860:7860 \
  -e ROOM_IDS=27519423 \
  -e DEFAULT_QN=80 \
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
      - ROOM_IDS=27519423
      - DEFAULT_QN=80
    restart: unless-stopped
```

### Hugging Face

推送到 GitHub `main` 后，GitHub Actions 会自动把部署文件同步到 Space：

- 工作流：`.github/workflows/sync-to-hub.yml`
- 只同步 `app.py`、`Dockerfile`、`requirements.txt`
- 需要在 GitHub 仓库的 **Settings → Secrets and variables → Actions** 配置：
  - `HF_TOKEN`：Hugging Face 写权限 token
  - `HF_SPACE_REPO`：目标 Space 的仓库 ID，格式为 `<用户名>/<Space 名>`
- 也可以在 GitHub Actions 页面手动触发 `Sync to Hugging Face Space`

Space 的 `README.md` 是构建配置来源（包含 `sdk: docker`），因此工作流使用 `include` 白名单并设置 `delete_removed: false`，不会用本仓库的 README 覆盖它。

同步会触发 Space 重新构建，正在播放的连接会短暂中断，播放器会自动重连。

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
