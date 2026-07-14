# FrameFinder 本地检索服务

当前组合：

- SigLIP 2 Giant 1B Patch16 384：文字与图片语义向量（1536 维）
- DINOv3 ViT-H+/16：视觉结构向量（1280 维）
- pHash：重复图片指纹
- Qdrant 1.18.1：持久化保存两组命名向量和图片元数据

当前使用 `framefinder_assets_v3` 集合。升级前的向量仍保留在
`framefinder_assets_v1` 和 `framefinder_assets_v2`，不会被覆盖；不同版本的
向量维度不同，不能混用。

ImageLib 4.x 使用 `/api/v1` 正式接口。旧 `/api/search` 继续供网页模型效果测试，
不会作为 ImageLib 的资产索引契约。

## 启动网页

双击 `start_ui.cmd`。它会依次启动 Qdrant、模型服务和网页，然后打开：

```text
本机：http://localhost:3417
局域网：http://192.168.5.108:3417
```

当前局域网接口：

```text
API：http://192.168.5.108:8000
健康检查：http://192.168.5.108:8000/api/health
接口文档：http://192.168.5.108:8000/docs
OpenAPI：http://192.168.5.108:8000/openapi.json
```

启动脚本会自动检测当前局域网 IPv4 并打开对应地址。如果路由器重新分配了 IP，
请以启动时显示的新地址为准；长期对接建议在路由器中为本机设置 DHCP 地址保留。

使用完毕后双击 `stop_ui.cmd`。Qdrant 容器会停止，但数据库卷会保留，下次启动仍可复用向量。

Qdrant 管理页面：

```text
http://localhost:6333/dashboard
```

## ImageLib API Key

所有 `/api/v1` 请求必须携带 `X-FrameFinder-Key`。双击启动脚本时，服务会在首次
启动生成随机密钥并保存在项目根目录的 `.framefinder-api-key` 私有文件中；该文件
已被 Git 忽略，并限制为当前 Windows 用户和 SYSTEM 可读取；启动日志和错误响应都
不会输出密钥。通过安全渠道把此文件中的值配置到 ImageLib。

也可以在启动前通过环境变量指定固定密钥：

```powershell
$env:FRAMEFINDER_API_KEY = "<your-private-key>"
.\start_ui.ps1
```

更换密钥后需要重启 FrameFinder。正式局域网部署建议由密码管理工具注入环境变量，
并通过可信反向代理启用 HTTPS。

## ImageLib 正式接口

P0 接口：

- `GET /api/v1/capabilities`
- `POST /api/v1/assets/upsert`
- `GET /api/v1/assets/{asset_id}`
- `POST /api/v1/assets/status:batch`
- `DELETE /api/v1/assets/{asset_id}`
- `POST /api/v1/search/text`
- `POST /api/v1/search/by-asset`

同时提供的 P1 接口：

- `PATCH /api/v1/assets/{asset_id}/metadata`
- `POST /api/v1/assets/delete:batch`
- `GET /api/v1/libraries/{library_id}/assets/manifest`
- 标签、虚拟目录、存储根目录、媒体类型等 Qdrant payload 过滤

异步索引任务和临时上传搜全库暂未实现，`/api/v1/capabilities` 会明确返回
`async_indexing=false` 和 `search_by_upload=false`。单资产 upsert 当前是同步契约，
成功返回时资产已可立即查询和搜索。

所有正式资产使用 `(library_id, asset_id)` 作为唯一身份，并映射到稳定 UUIDv5
Qdrant Point ID。服务端不保存或返回 ImageLib 原图、缩略图 URL 或本地文件路径。

## 服务端限制

默认值均会由 `/api/v1/capabilities` 返回，并可通过环境变量调整：

- 单文件最大 20 MiB，仅接受 JPEG、PNG
- 批量状态查询最多 1000 项
- 搜索默认 120 项，单页最多 500 项
- 同时推理最多 2 个请求，超限返回 `429` 和 `Retry-After`
- 搜索会话默认保留 10 分钟，游标过期返回 `409 SEARCH_CURSOR_EXPIRED`

主要环境变量包括 `FRAMEFINDER_API_KEY`、`FRAMEFINDER_HOST`、
`FRAMEFINDER_PORT`、`FRAMEFINDER_MODEL_VERSION`、
`FRAMEFINDER_COLLECTION_GENERATION`、`FRAMEFINDER_MAX_UPLOAD_BYTES`、
`FRAMEFINDER_MAX_CONCURRENT_INFERENCE` 和 `FRAMEFINDER_SEARCH_SESSION_TTL_SECONDS`。

## 契约测试

契约测试使用内存 Qdrant 和轻量假模型，不会下载或加载 Giant/H+ 权重：

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_imagelib_api.py
```

## 旧网页接口缓存规则

图片按文件内容的 SHA-256 哈希识别：

- 首次出现：计算 SigLIP 2、DINOv3、pHash 并写入 Qdrant。
- 再次出现：直接读取已有向量，不重新计算图片向量。
- 修改文件名：内容不变时仍然命中。
- 修改、重新压缩或裁剪图片：会作为新素材计算；pHash 仍可辅助判断近似重复。

Qdrant 只保存向量、指纹和尺寸等元数据，不保存原始图片文件。当前网页仍由你选择本机图片作为候选集，再使用数据库缓存加速检索。

## 命令行测试

运行内置合成图片测试：

```powershell
.\.venv\Scripts\python.exe smoke_test.py --text "a red circle"
```

测试自己的图片目录：

```powershell
.\.venv\Scripts\python.exe smoke_test.py .\test_images --text "女性站在健身房窗边"
```
