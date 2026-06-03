# Emby 115 Gateway

一个面向个人媒体库的 Emby + OpenList + 115 STRM 网关和后台管理工具。

它可以从 OpenList 扫描网盘媒体目录，生成 Emby 可识别的 STRM 文件，并在 Emby Web 中注入外部播放器按钮。点击播放时才通过 OpenList 获取直链，客户端再用 PotPlayer、VLC、Infuse 等播放器打开。

> 本项目只提供个人媒体库管理和播放器辅助能力。请确保你的使用方式符合所在地法律法规、媒体版权要求，以及相关服务的用户协议。

## 功能

- 后台管理 Emby、OpenList、网关地址、媒体源、安全模式和同步策略。
- 按媒体源生成 STRM 文件。
- 支持电视剧目录归一化，将发布组目录整理为 Emby 更容易识别的 `Season xx` 结构。
- 通过 nginx 网关注入外部播放器按钮，不需要修改 Emby 容器文件。
- 支持 PotPlayer、VLC、MPV、IINA、NPlayer、MX Player、Infuse 和复制链接。
- 支持手动同步、定时同步、目录指纹缓存和缺失文件延迟删除。

## 架构

```text
Browser / Emby Client
        |
        v
nginx gateway :8097  ----->  Emby Web / API
        |
        +---- injected external-player.js
        |
        v
FastAPI app :8000  ----->  OpenList API  ----->  115 direct link
        |
        v
STRM output directory
```

## 端口

```text
8097  Emby 网关入口
8098  后台管理入口
```

后台管理端口默认面向本地、内网或 VPN 使用。项目只提供基础登录、密码哈希和签名 Cookie，不提供公网管理后台所需的完整安全体系。

建议公网只暴露 Emby 网关入口，后台管理入口尽量只允许内网、VPN、反向代理认证或 IP 白名单访问。

## 快速开始

复制配置：

```bash
cp .env.example .env
```

编辑 `.env`：

```env
COMPOSE_PROJECT_NAME=emby115-gateway
GATEWAY_PORT=8097
ADMIN_PORT=8098
ADMIN_INIT_USER=admin
ADMIN_INIT_PASSWORD=change-me-please
APP_SECRET_KEY=
SESSION_COOKIE_SECURE=false
STRM_OUTPUT_HOST=./strm-output
EMBY_UPSTREAM=http://host.docker.internal:8096
```

启动：

```bash
docker compose up -d --build
```

访问后台：

```text
http://服务器IP:8098/admin
```

默认账号：

```text
admin / change-me-please
```

首次登录后请立即修改密码。

## 必填配置

后台进入 `连接配置`，填写：

```text
网关访问地址：http://服务器IP:8097
STRM 容器输出根目录：/strm-output
Emby 内部地址：http://host.docker.internal:8096
Emby API Key：在 Emby 后台创建
OpenList 内部地址：http://host.docker.internal:5244
OpenList Token：在 OpenList 中获取
```

点击页面上的测试按钮，确认 Emby 和 OpenList 都可访问。

## 媒体源

后台进入 `媒体源` 添加映射。

示例：

```text
名称：电影
OpenList 完整目录：/Cloud/115/Media/Movies
STRM 输出子目录：Movies
Emby 内 STRM 目录：/media/Strm115/Movies
媒体类型：电影

名称：电视剧
OpenList 完整目录：/Cloud/115/Media/TV
STRM 输出子目录：TV
Emby 内 STRM 目录：/media/Strm115/TV
媒体类型：电视剧
```

说明：

- `STRM_OUTPUT_HOST` 是宿主机目录。
- `/strm-output` 是容器内目录。
- `Emby 内 STRM 目录` 是 Emby 容器看到的路径，需要与你的 Emby 媒体库挂载一致。
- OpenList 完整目录不能过于宽泛，建议至少定位到媒体分类目录。

## 电视剧目录归一化

电视剧源可开启目录归一化。

OpenList 原目录可以是：

```text
Show Name/
  Release.Group.S01.2160p.WEB-DL/
    Show.Name.S01E01.2160p.WEB-DL.mkv
    Show.Name.S01E02.2160p.WEB-DL.mkv
```

STRM 输出会变成：

```text
Show Name/
  Season 01/
    Show.Name.S01E01.2160p.WEB-DL.strm
    Show.Name.S01E02.2160p.WEB-DL.strm
```

支持识别：

```text
S01E01
S1E1
1x01
EP01
E01
第01集
```

相关配置：

```text
媒体类型：电视剧
电视剧目录归一化：开启
剧名目录层级：默认 1
默认季号：默认 1
无法识别集数时：原样输出 / 放入 _Unmatched / 跳过并记录
```

强制同步时会清理该媒体源输出目录下不再被数据库引用的旧 `.strm` 文件，并删除空目录。

## 安全模式

后台 `安全` 页支持：

```text
strict
compatible
private
```

- `strict`：最保守。STRM 静态入口不可直接播放，只能通过 Emby Web 外部播放器按钮换取短效播放地址。适合公网网关或多用户环境；缺点是 Infuse、VLC 等直接读取 STRM 的客户端兼容性较差。
- `compatible`：兼容性与安全折中。STRM 静态 token 可换短效 `/play` 地址，兼容 Infuse、VLC 等客户端。适合内网/VPN 或可信用户环境；如果 STRM 内容或 token 泄露，攻击者可能在 token 轮换前持续换取播放地址。
- `private`：最宽松。STRM 静态 token 直接跳转 OpenList/115 直链。仅建议纯内网/VPN 自用，不建议公网或多人共享环境使用。

如果需要 Infuse 等客户端直接添加 Emby 使用，通常建议使用 `compatible`。

无论哪种模式，只要攻击者能访问网关并拿到有效 token，都可能消耗直链获取次数或触发网盘风控。安全模式不能替代网络访问控制。

公网 HTTPS 反代时建议在 `.env` 中设置：

```env
SESSION_COOKIE_SECURE=true
```

并设置固定的：

```env
APP_SECRET_KEY=一段随机长字符串
```

## 同步

默认同步模式是 `manual`，容器启动不会主动访问 OpenList 或 115。

后台可手动同步，也可以设置：

```text
manual   只手动同步
smart    定时同步并跳过未变化目录
full     定时全量扫描
webhook  仅接受外部事件触发
```

命令行手动同步全部启用媒体源：

```bash
docker exec emby115-gateway-app python -c 'import main; print(main.sync_once(force=True))'
```

同步指定媒体源：

```bash
docker exec emby115-gateway-app python -c 'import main; print(main.sync_once(source_key="媒体源key", force=True))'
```

同步结果中的常见字段：

```text
videos              扫描到的视频数
tv_normalized       已完成电视剧归一化的视频数
tv_unmatched        未识别集数的视频数
orphan_strm_removed 清理的旧 STRM 数
```

## API

健康检查：

```bash
curl http://127.0.0.1:8098/admin-api/health
```

触发同步：

```bash
curl -X POST "http://127.0.0.1:8098/admin-api/sync?token=你的Token&force=true"
```

Webhook：

```bash
curl -X POST "http://127.0.0.1:8098/admin-api/webhook?token=你的WebhookToken&path=/Cloud/115/Media/TV"
```

## Emby 配置提示

STRM 同步完成后，需要刷新 Emby 媒体库。

电视剧推荐结构：

```text
Show Name/Season 01/Show.Name.S01E01.strm
```

如果电视剧页面没有选集，通常是目录结构未被 Emby 识别。可以尝试：

1. 确认 STRM 已归一化到 `Season xx`。
2. 刷新 Emby 媒体库。
3. 对该剧执行“刷新元数据/替换所有元数据”。
4. 必要时删除旧条目后重新扫描。

## 常用排查

查看容器：

```bash
docker ps --filter name=emby115-gateway --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
```

查看日志：

```bash
docker logs --tail=100 emby115-gateway-app
```

查看 STRM 输出：

```bash
find ./strm-output -maxdepth 4 -name '*.strm' | head -50
```

## 开源与合规

本项目不包含任何媒体资源，不绕过账号权限，不内置任何第三方服务凭据。

使用者需要自行配置 Emby、OpenList 和相关网盘账号，并自行承担账号安全、版权合规、服务条款合规等责任。

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).

Copyright (c) 2026 Amigo and contributors.

Original project: [Amigo-x/emby-115-gateway](https://github.com/Amigo-x/emby-115-gateway)

