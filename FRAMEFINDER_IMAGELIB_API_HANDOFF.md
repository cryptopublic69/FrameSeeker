# FrameFinder × ImageLib 4.x 前端联调确认文档

API 版本：1.0.0  
文档修订：1.0.1  
日期：2026-07-15  
状态：服务端已实现，待 ImageLib 前端/客户端确认  
适用服务：FrameFinder Giant/H+ 本地向量检索服务

## 1. 文档用途

本文是 FrameFinder 服务端与 ImageLib 4.x 的正式联调契约，供前端、Tauri 后端和服务端共同确认。

本次服务端已完成：

- 全部 P0 接入阻塞项；
- P1 中的元数据更新、批量删除、远端资产清单和元数据过滤；
- 正式 `/api/v1` OpenAPI、API Key 鉴权、统一错误、稳定分页和请求追踪。

接口字段和类型最终以同目录的 [openapi.json](./openapi.json) 为准。运行中的服务也可通过以下地址读取：

```text
GET http://<framefinder-host>:8000/openapi.json
GET http://<framefinder-host>:8000/docs
```

旧接口 `POST /api/search` 继续供 FrameFinder 网页测试使用，不属于 ImageLib 正式契约。

## 2. 架构边界

ImageLib SQLite 仍是资产信息的唯一事实来源。FrameFinder 仅保存：

- SigLIP 2 语义向量；
- DINOv3 视觉向量；
- pHash；
- 稳定资产身份映射；
- 内容哈希、模型版本和必要的过滤元数据。

FrameFinder 不保存或返回：

- 原始图片；
- 上传的 512 像素缩略图；
- `thumbnail_url`；
- ImageLib 本地文件路径；
- ImageLib SQLite 中的完整资产记录。

搜索结果只返回稳定 `asset_id` 和相似度数据。ImageLib 根据 `asset_id` 从本地 SQLite 加载并展示资产。

## 3. 基础连接约定

### 3.1 Base URL

```text
http://<framefinder-host>:8000/api/v1
```

局域网正式使用建议通过可信反向代理启用 HTTPS。Qdrant 的 `6333/6334` 端口只绑定 `127.0.0.1`，客户端不应直接访问 Qdrant。

### 3.2 鉴权

所有 `/api/v1` 请求必须携带：

```http
X-FrameFinder-Key: <API_KEY>
```

OpenAPI 使用标准 `components.securitySchemes.FrameFinderApiKey` 声明：

```yaml
type: apiKey
in: header
name: X-FrameFinder-Key
```

所有 `/api/v1` operation 均通过 `security: [{FrameFinderApiKey: []}]` 标记为必须鉴权，不再把 API Key 表示为可选 Header 参数。OpenAPI 客户端生成器应通过鉴权配置注入 Key。

约定：

- API Key 由用户配置或通过安全渠道提供，客户端不得硬编码；
- FrameFinder 双击启动脚本首次运行时会生成 `.framefinder-api-key` 私有文件；
- 联调 Key 使用 32 字节密码学安全随机数生成，并限制为当前 Windows 用户和 SYSTEM 可读取；
- API Key 不得写入普通日志、错误提示、遥测或崩溃报告；
- Key 无效返回 `401 INVALID_API_KEY`；
- 服务端未配置 Key 返回 `503 API_KEY_NOT_CONFIGURED`。

### 3.3 请求追踪与幂等

所有响应均包含：

```http
X-Request-Id: req-...
```

该响应头已在 OpenAPI 的所有响应中正式声明。`429` 响应还会在运行时和 OpenAPI 中提供：

```http
Retry-After: <seconds>
```

响应 JSON 中也会返回 `request_id`。问题反馈应记录该 ID，但不要记录 API Key 或图片二进制。

写操作建议携带：

```http
Idempotency-Key: <operation_uuid>
```

资产 Point ID 是确定性的，revision 检查和幂等删除可防止重复 Point 或重复副作用。客户端仍应为一次逻辑写操作复用同一个 `Idempotency-Key`。

### 3.4 时间、ID 与哈希

- 所有 ID 使用 JSON String；
- 时间使用带时区 ISO 8601；
- 文件大小使用字节整数；
- 视频时间点使用整数 `timestamp_ms`；
- SHA-256 格式统一为 `sha256:<64位小写十六进制>`；
- `score` 是相似度排序分数，不是概率。

## 4. 资产身份

正式资产唯一键为：

```text
(library_id, asset_id)
```

- `library_id`：ImageLib 资料库稳定 ID，资料库移动路径后不得改变；
- `asset_id`：ImageLib 资产稳定 ID；
- 不同 `library_id` 可以拥有相同 `asset_id`，彼此完全隔离；
- 服务端 Qdrant Point ID 使用稳定 UUIDv5 生成；
- 相同图片内容的不同资产仍保留独立 Point；删除其中一个不会影响其他资产。

所有新增、查询、搜索、更新和删除请求都必须明确指定 `library_id`。

## 5. 能力与健康状态

### 5.1 正式能力查询

```http
GET /api/v1/capabilities
X-FrameFinder-Key: <API_KEY>
```

客户端启动后应先调用该接口，不要在代码中固化服务端限制或功能开关。

关键响应示例：

```json
{
  "ok": true,
  "api_version": "1.0.0",
  "server_instance_id": "ff-server-01",
  "accepting_requests": true,
  "models_loaded": false,
  "models": {
    "semantic": "google/siglip2-giant-opt-patch16-384",
    "visual": "hf_hub:timm/vit_huge_plus_patch16_dinov3.lvd1689m",
    "model_version": "giant-hplus-v3"
  },
  "collection": {
    "name": "framefinder_assets_v3",
    "generation": "2026-07-15-01",
    "status": "active",
    "stored_assets": 0
  },
  "supported_media_types": ["image", "video_frame"],
  "accepted_upload_mime_types": ["image/jpeg", "image/png"],
  "supported_filters": [
    "exclude_asset_ids",
    "media_types",
    "source_video_id",
    "storage_root_ids",
    "tag_ids_all",
    "tag_ids_any",
    "virtual_folder_ids_all",
    "virtual_folder_ids_any"
  ],
  "limits": {
    "max_upload_bytes": 20971520,
    "max_batch_status_items": 1000,
    "default_search_limit": 120,
    "max_search_limit": 500,
    "max_query_length": 2000,
    "max_concurrent_inference": 2,
    "search_session_max_results": 5000
  },
  "features": {
    "text_search": true,
    "search_by_asset": true,
    "search_by_upload": false,
    "metadata_patch": true,
    "metadata_filters": true,
    "async_indexing": false,
    "batch_delete": true,
    "asset_manifest": true
  },
  "server_time": "2026-07-15T00:00:00+00:00",
  "request_id": "req-..."
}
```

客户端处理要求：

- `accepting_requests=false` 时不要提交索引或搜索；
- `models_loaded=false` 只表示模型尚未预热，不表示服务离线；
- 以 `features` 决定是否展示相应功能；
- 以 `limits` 决定批次大小、上传大小和搜索页大小；
- 记录 `model_version` 和 `collection.generation`，用于显示重建或升级状态。

### 5.2 轻量健康检查

```http
GET /api/health
```

该接口不需要 API Key，适合连接测试。关键字段包括：

- `ok`：服务进程可响应；
- `database.online`：Qdrant 是否可用；
- `models_loaded`：大型模型是否已加载；
- `accepting_requests`：是否可以提交正式请求；
- `status`：`warming` 或 `ready`。

OpenAPI 中该接口成功响应使用正式 `HealthResponse` Schema，数据库字段使用 `HealthDatabaseStatus` Schema。

## 6. 资产新增或更新

### 6.1 请求

```http
POST /api/v1/assets/upsert
Content-Type: multipart/form-data
X-FrameFinder-Key: <API_KEY>
Idempotency-Key: <operation_uuid>
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `library_id` | String | 是 | ImageLib 资料库稳定 ID |
| `asset_id` | String | 是 | ImageLib 资产稳定 ID |
| `file` | File | 是 | JPEG 或 PNG 缩略图，最大值由 capabilities 返回 |
| `source_content_hash` | String | 是 | 原始资产文件 SHA-256，不是缩略图哈希 |
| `client_revision` | Integer | 是 | 该资产单调递增版本，必须 `>= 0` |
| `media_type` | Enum | 是 | `image` 或 `video_frame` |
| `metadata` | JSON String | 否 | multipart 中的 JSON 对象字符串 |
| `force` | Boolean | 否 | 默认 `false`；为 `true` 时强制重新编码 |

示例：

```text
library_id=lib-01J...
asset_id=A001
source_content_hash=sha256:0000000000000000000000000000000000000000000000000000000000000000
client_revision=1
media_type=image
metadata={"tag_ids":["tag-person"],"virtual_folder_ids":["folder-001"]}
force=false
file=<thumbnail.png>
```

服务端计算：

- `indexed_file_hash`：实际收到的 JPEG/PNG 文件 SHA-256；
- `metadata_hash`：规范化 metadata JSON 的 SHA-256；
- pHash；
- 当前模型版本的语义和视觉向量。

### 6.2 正式 AssetMetadata Schema

OpenAPI 提供 `components.schemas.AssetMetadata`。metadata 不再表示为任意对象，禁止额外字段。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `asset_kind` | `image \| video_frame \| null` | 可选 |
| `width` | Integer/null | `>= 1` |
| `height` | Integer/null | `>= 1` |
| `storage_root_id` | String/null | 稳定存储根 ID |
| `virtual_folder_ids` | String[]/null | 稳定目录 ID |
| `tag_ids` | String[]/null | 稳定标签 ID |
| `source_video_id` | String/null | 来源视频资产 ID |
| `timestamp_ms` | Integer/null | `>= 0` |
| `metadata_updated_at` | ISO 8601 Date-Time/null | 建议带时区 |

multipart upsert 中，`metadata` 仍以 JSON String 传输，但解码后的对象必须符合 `AssetMetadata`。metadata PATCH 直接使用 `AssetMetadata` Schema。

示例：

```json
{
  "asset_kind": "image",
  "width": 1920,
  "height": 1080,
  "storage_root_id": "root-01",
  "virtual_folder_ids": ["folder-2026-001"],
  "tag_ids": ["tag-person", "tag-selected"],
  "source_video_id": null,
  "timestamp_ms": null,
  "metadata_updated_at": "2026-07-15T10:20:00+08:00"
}
```

未知字段会返回 `422 UNSUPPORTED_METADATA_FIELD`；类型或取值错误返回 `422 INVALID_METADATA` 或 `422 INVALID_REQUEST`。服务端不会静默忽略非法字段。

### 6.3 响应

新建资产映射通常返回 `201`；更新、复用或仅更新元数据返回 `200`。客户端应将所有 2xx 视为成功，并读取 `status`：

- `created`：新建资产和向量；
- `updated`：内容或模型变化，已更新向量；
- `reused`：请求幂等复用，或复用了相同文件的已有向量；
- `metadata_only`：只更新元数据，未执行图片编码。

```json
{
  "library_id": "lib-01J...",
  "asset_id": "A001",
  "status": "created",
  "client_revision": 1,
  "source_content_hash": "sha256:...",
  "indexed_file_hash": "sha256:...",
  "model_version": "giant-hplus-v3",
  "collection_generation": "2026-07-15-01",
  "vectors": {
    "semantic": 1536,
    "visual": 1280
  },
  "phash": "f0c0...",
  "indexed_at": "2026-07-15T02:30:06+00:00",
  "request_id": "req-..."
}
```

同步 upsert 返回成功时，资产已经可以被后续状态查询和搜索看到。

### 6.4 revision 规则

设服务端当前 revision 为 `N`：

- 请求 `< N`：返回 `409 STALE_CLIENT_REVISION`；
- 请求 `= N` 且内容和 metadata 相同：幂等成功；
- 请求 `= N` 但内容或 metadata 不同：返回 `409 CLIENT_REVISION_CONFLICT`；
- 请求 `> N`：按内容变化执行向量更新或 metadata-only 更新；
- 当前模型版本变化时，即使客户端内容不变，也会重新建立当前模型向量。

建议 ImageLib 对图片内容更新、metadata 更新和删除事件统一使用单调递增 revision。

## 7. 资产状态与首次对账

### 7.1 单资产状态

```http
GET /api/v1/assets/{asset_id}?library_id=<library_id>
```

资产不存在返回 `404 ASSET_NOT_FOUND`。存在时返回：

```json
{
  "library_id": "lib-01J...",
  "asset_id": "A001",
  "status": "indexed",
  "client_revision": 4,
  "source_content_hash": "sha256:...",
  "indexed_file_hash": "sha256:...",
  "model_version": "giant-hplus-v3",
  "collection_generation": "2026-07-15-01",
  "indexed_at": "2026-07-15T02:30:06+00:00",
  "last_error": null,
  "request_id": "req-..."
}
```

### 7.2 批量状态对账

```http
POST /api/v1/assets/status:batch
Content-Type: application/json
```

```json
{
  "library_id": "lib-01J...",
  "assets": [
    {
      "asset_id": "A001",
      "source_content_hash": "sha256:...",
      "client_revision": 3
    },
    {
      "asset_id": "A002",
      "source_content_hash": "sha256:...",
      "client_revision": 7
    }
  ]
}
```

批次默认最多 1000 项，以 capabilities 返回值为准。超过上限返回 `413 BATCH_TOO_LARGE`，不会丢弃部分项目。

```json
{
  "library_id": "lib-01J...",
  "count": 2,
  "results": [
    {
      "asset_id": "A001",
      "status": "indexed",
      "needs_upsert": false,
      "needs_metadata_patch": false,
      "model_version": "giant-hplus-v3",
      "last_error": null
    },
    {
      "asset_id": "A002",
      "status": "model_outdated",
      "needs_upsert": true,
      "needs_metadata_patch": false,
      "model_version": "giant-hplus-v2",
      "last_error": null
    }
  ],
  "request_id": "req-..."
}
```

客户端处理矩阵：

| `status` | 客户端动作 |
| --- | --- |
| `indexed` | 无需操作 |
| `missing` | 提交 upsert |
| `stale_content` | 提交 upsert |
| `stale_metadata` | 优先提交 metadata PATCH |
| `model_outdated` | 提交 upsert，服务端重建当前模型向量 |
| `indexing` | 当前同步版本通常不会出现；保留用于未来异步模式 |
| `failed` | 展示错误并按 `last_error`/错误码决定重试 |

当前批量状态请求不携带 `metadata_hash`。同一内容哈希下，客户端 revision 高于服务端时会判定为 `stale_metadata`。

每个 `BatchStatusResult` 都包含可空 `last_error`。`status=failed` 时服务端返回正式 `AssetLastError`：

```json
{
  "code": "INFERENCE_TIMEOUT",
  "message": "inference timed out",
  "retryable": true,
  "details": {}
}
```

## 8. 元数据更新

```http
PATCH /api/v1/assets/{asset_id}/metadata?library_id=<library_id>
Content-Type: application/json
```

```json
{
  "client_revision": 6,
  "metadata": {
    "storage_root_id": "root-01",
    "virtual_folder_ids": ["folder-archive"],
    "tag_ids": ["tag-person", "tag-selected"],
    "metadata_updated_at": "2026-07-15T11:00:00+08:00"
  }
}
```

该接口不会运行图片编码器，只更新 Qdrant payload。`metadata` 表示该资产新的完整过滤 metadata；客户端不应把它当作单字段 JSON Merge Patch。

相同 revision 和相同 metadata 幂等成功；旧 revision 返回 `409 STALE_CLIENT_REVISION`；相同 revision 但 metadata 不同返回 `409 CLIENT_REVISION_CONFLICT`。

## 9. 删除资产

### 9.1 单资产删除

```http
DELETE /api/v1/assets/{asset_id}?library_id=<library_id>&client_revision=<revision>
```

```json
{
  "library_id": "lib-01J...",
  "asset_id": "A001",
  "deleted": true,
  "existed": true,
  "request_id": "req-..."
}
```

删除是幂等的。资产已不存在时仍返回 `200`、`deleted=true`、`existed=false`。客户端不要因 `existed=false` 重试。

删除 revision 小于服务端当前 revision 时返回 `409 STALE_CLIENT_REVISION`。

### 9.2 批量删除

```http
POST /api/v1/assets/delete:batch
Content-Type: application/json
```

```json
{
  "library_id": "lib-01J...",
  "assets": [
    {"asset_id": "A001", "client_revision": 5},
    {"asset_id": "A002", "client_revision": 8}
  ]
}
```

响应逐项包含 `deleted`、`existed` 和可选 `error`。批量接口可能部分成功，客户端必须逐项检查，不要只检查 HTTP 状态和总数。

## 10. 全库文字搜索

```http
POST /api/v1/search/text
Content-Type: application/json
```

```json
{
  "library_id": "lib-01J...",
  "query": "紫色灯光下的人物特写",
  "limit": 120,
  "cursor": null,
  "min_score": null,
  "filters": {
    "media_types": ["image", "video_frame"],
    "storage_root_ids": ["root-01"],
    "virtual_folder_ids_any": ["folder-2026-001"],
    "tag_ids_any": ["tag-person"],
    "tag_ids_all": ["tag-selected"]
  }
}
```

规则：

- 只搜索指定 `library_id`；
- `query.trim()` 不得为空，最大长度以 capabilities 为准；
- 文本只编码一次，然后直接搜索已入库资产；
- 不需要重新上传候选图片；
- `min_score` 为可选相似度下限；
- 未提供的 filters 不参与过滤。

## 11. 使用已入库资产以图搜图

```http
POST /api/v1/search/by-asset
Content-Type: application/json
```

```json
{
  "library_id": "lib-01J...",
  "asset_id": "A001",
  "limit": 120,
  "cursor": null,
  "min_score": null,
  "semantic_weight": 0.55,
  "visual_weight": 0.45,
  "exclude_self": true,
  "filters": {
    "media_types": ["image"],
    "virtual_folder_ids_any": ["folder-2026-001"]
  }
}
```

规则：

- 参考资产已入库时，客户端只传 `asset_id`，不再上传图片；
- 两个权重必须 `>= 0`，且至少一个大于 0；
- 服务端会归一化权重；
- `exclude_self=true` 排除完全相同的 `(library_id, asset_id)`；
- 参考资产不存在返回 `404 ASSET_NOT_FOUND`；
- 参考资产无向量返回 `409 ASSET_NOT_INDEXED`；
- 参考资产模型版本过旧返回 `409 MODEL_VERSION_MISMATCH`。

## 12. 统一搜索响应与分页

文字搜索和以图搜图使用同一 Hit 结构：

```json
{
  "query_type": "image_asset",
  "library_id": "lib-01J...",
  "count": 2,
  "total_count": 238,
  "results_truncated": false,
  "limit": 120,
  "has_more": true,
  "next_cursor": "opaque-cursor-token",
  "search_session_id": "search-...",
  "cursor_expires_at": "2026-07-15T02:40:00+00:00",
  "elapsed_ms": 18,
  "model_version": "giant-hplus-v3",
  "collection_generation": "2026-07-15-01",
  "hits": [
    {
      "asset_id": "A108",
      "rank": 1,
      "score": 0.8731,
      "semantic_score": 0.8214,
      "visual_score": 0.9363,
      "phash_distance": 7,
      "indexed_source_hash": "sha256:..."
    }
  ],
  "request_id": "req-..."
}
```

说明：

- `count` 是当前页实际返回的 `hits` 数量；
- `total_count` 是当前稳定搜索会话中已捕获的结果总数；
- `results_truncated=false` 时，`total_count` 是本次匹配结果的精确总数；
- `results_truncated=true` 表示结果达到 `search_session_max_results` 上限，此时 `total_count` 是已保留的前 N 个结果数量，不代表完整匹配总数；
- 文字搜索中 `visual_score` 和 `phash_distance` 为 `null`；
- 结果按最终 `score` 降序，再按稳定 Point ID 升序打破同分；
- `rank` 是该搜索会话中的全局排名，不是单页内排名；
- Hit 不返回原图、缩略图、URL 或路径；
- `score` 不是概率，前端不要显示为“置信率 xx%”。

分页流程：

1. 首次请求传 `cursor=null`；
2. `has_more=true` 时，用 `next_cursor` 请求下一页；
3. 下一页必须保持相同的 `library_id`、query/asset、权重、阈值和 filters；
4. 游标是不可解析令牌，客户端不得读取或构造内部结构；
5. 默认有效期 10 分钟，以 `cursor_expires_at` 为准；
6. 服务重启、会话过期、查询条件改变或 collection generation 切换后返回 `409 SEARCH_CURSOR_EXPIRED`；
7. 收到该错误后清空现有结果并从第一页重新搜索。

搜索会话保存稳定结果快照，因此会话期间新写入、删除或分数变化不会导致跨页重复和遗漏。当前单次搜索会话最多保存前 5000 个结果，以 capabilities 为准。

## 13. 支持的过滤器

| 字段 | 语义 |
| --- | --- |
| `media_types` | `image` / `video_frame` 任一匹配 |
| `storage_root_ids` | 任一存储根匹配 |
| `virtual_folder_ids_any` | 至少一个虚拟目录匹配 |
| `virtual_folder_ids_all` | 必须包含全部虚拟目录 |
| `tag_ids_any` | 至少一个标签匹配 |
| `tag_ids_all` | 必须包含全部标签 |
| `source_video_id` | 精确匹配来源视频 ID |
| `exclude_asset_ids` | 排除指定资产 ID |

过滤字段使用稳定 ID，不使用可修改的显示名称。

所有过滤器都在 Qdrant payload 上执行并建立相应索引。未知过滤字段会由请求模型返回 `422 INVALID_REQUEST`，不会静默忽略后进行错误范围的搜索。

## 14. 远端资产清单

```http
GET /api/v1/libraries/{library_id}/assets/manifest?limit=1000&cursor=<cursor>
```

```json
{
  "library_id": "lib-01J...",
  "has_more": false,
  "next_cursor": null,
  "assets": [
    {
      "asset_id": "A001",
      "source_content_hash": "sha256:...",
      "client_revision": 5,
      "model_version": "giant-hplus-v3",
      "status": "indexed"
    }
  ],
  "request_id": "req-..."
}
```

该接口用于完整对账、备份恢复和孤儿 Point 清理。客户端不要解析 manifest cursor。无效游标返回 `409 MANIFEST_CURSOR_EXPIRED`。

## 15. 统一错误结构

所有非 2xx 响应使用：

```json
{
  "error": {
    "code": "ASSET_NOT_INDEXED",
    "message": "参考资产尚未完成向量索引",
    "retryable": false,
    "request_id": "req-...",
    "details": {
      "library_id": "lib-...",
      "asset_id": "A001"
    }
  }
}
```

前端逻辑必须基于稳定 `error.code` 和 `retryable`，不要解析 `message` 文本。

所有响应（包括错误响应）在 OpenAPI 中声明 `X-Request-Id` Header；`429` 另外声明 `Retry-After` Header。

| HTTP | `error.code` | 建议处理 |
| --- | --- | --- |
| 400/422 | `INVALID_REQUEST` | 修正客户端字段，不重试 |
| 401 | `INVALID_API_KEY` | 停止正式请求，提示重新配置密钥 |
| 503 | `API_KEY_NOT_CONFIGURED` | 提示服务端配置密钥，不自动重试 |
| 404 | `ASSET_NOT_FOUND` | 标记远端缺失，必要时 upsert |
| 409 | `STALE_CLIENT_REVISION` | 重新读取状态并对账 |
| 409 | `CLIENT_REVISION_CONFLICT` | 重新读取状态，检查本地 revision 生成逻辑 |
| 409 | `ASSET_NOT_INDEXED` | 先建立索引 |
| 409 | `MODEL_VERSION_MISMATCH` | 重新 upsert 参考资产 |
| 409 | `SEARCH_CURSOR_EXPIRED` | 清空分页状态，从第一页重新搜索 |
| 409 | `MANIFEST_CURSOR_EXPIRED` | 从 manifest 第一页重新读取 |
| 413 | `UPLOAD_TOO_LARGE` | 生成更小缩略图，不重试原文件 |
| 413 | `BATCH_TOO_LARGE` | 按 capabilities 降低批次大小 |
| 422 | `UNSUPPORTED_MEDIA_TYPE` | 转为 JPEG/PNG |
| 422 | `UNSUPPORTED_METADATA_FIELD` | 修正客户端契约 |
| 429 | `RATE_LIMITED` | 遵守 `Retry-After` 后重试 |
| 503 | `MODEL_UNAVAILABLE` | 可重试，显示模型不可用/加载失败 |
| 503 | `VECTOR_DATABASE_UNAVAILABLE` | 可重试，显示向量库不可用 |
| 500 | `INTERNAL_ERROR` | 不无限重试，记录 request ID |

对于 `retryable=true` 且没有 `Retry-After` 的错误，建议指数退避：1、2、4、8 秒，最多 4 次。应用退出、切换资料库或用户取消时应终止重试。

## 16. 推荐客户端同步流程

### 16.1 启动与首次接入

1. 调用 `/api/health` 检查主机可达；
2. 调用 `/api/v1/capabilities` 验证 Key、版本和能力；
3. 按 `max_batch_status_items` 分批提交本地资产状态；
4. 对 `missing`、`stale_content`、`model_outdated` 逐项 upsert；
5. 对 `stale_metadata` 调用 metadata PATCH；
6. 限制客户端索引并发，建议默认 1～2；
7. 同步结束后可使用 manifest 做完整性复核。

### 16.2 增量事件

| ImageLib 事件 | FrameFinder 操作 |
| --- | --- |
| 新增图片 | upsert |
| 原始文件内容变化 | revision +1，upsert |
| 缩略图生成结果变化 | revision +1，upsert |
| 标签、虚拟目录、存储根变化 | revision +1，metadata PATCH |
| 删除资产 | revision +1，DELETE |
| 资料库路径移动 | 保持同一 `library_id`，无需重建 |
| 模型升级后状态为 `model_outdated` | 重新 upsert |

### 16.3 搜索

1. 前端发起文字搜索或选择已入库参考资产；
2. Tauri 后端携带 API Key 请求 FrameFinder；
3. 前端按返回顺序使用 `asset_id` 查询本地 SQLite；
4. 本地资产已删除但搜索仍返回时，可忽略该 Hit，并在后续 manifest 对账中清理孤儿 Point；
5. 用户继续滚动时使用 `next_cursor`；
6. 游标过期则从第一页重新执行。

## 17. 当前明确未实现的能力

以下功能不阻塞第一版 ImageLib 全库检索，但客户端不得假设可用：

- 异步索引 Job、进度和取消：`async_indexing=false`；
- 临时上传一张图片搜索全库：`search_by_upload=false`；
- pHash 独立重复图片查询接口；
- 视频文件直接上传、自动抽帧和时间轴回跳；
- mDNS 或自动服务发现。

当前 upsert 是同步接口。返回 2xx 时资产已经可查可搜。大型模型首次请求可能需要冷启动时间，客户端应展示“模型正在加载”，不要误报为服务离线。

搜索会话保存在服务端内存中，服务重启后旧游标会失效，这是预期行为。

## 18. 前端确认清单

请 ImageLib 开发侧逐项确认：

- [ ] 每个资料库已有稳定 `library_id`，移动数据库路径后保持不变；
- [ ] 每个资产已有稳定 String `asset_id`；
- [ ] `client_revision` 对内容、metadata 和删除事件单调递增；
- [ ] `source_content_hash` 来自原始资产，不是 512 缩略图；
- [ ] 可稳定生成 JPEG/PNG 缩略图，并遵守 `max_upload_bytes`；
- [ ] API Key 存入安全配置，不写入源码和普通日志；
- [ ] OpenAPI 生成客户端通过 `FrameFinderApiKey` security scheme 注入 Key；
- [ ] 正式网络请求由 Tauri 后端发出，而不是在浏览器前端暴露 Key；
- [ ] 客户端先读取 capabilities，不固化限制和 features；
- [ ] 能处理 upsert 的四种 `status`；
- [ ] 能处理批量状态的七种 `status`；
- [ ] 能逐项处理批量删除结果；
- [ ] metadata PATCH 发送完整的新 metadata，而不是单字段增量；
- [ ] 搜索分页保存并原样回传 cursor，不解析 cursor；
- [ ] 搜索 UI 将 `count` 视为当前页数量，并使用 `total_count`/`results_truncated` 展示总数；
- [ ] `SEARCH_CURSOR_EXPIRED` 会从第一页重新搜索；
- [ ] 搜索 UI 不把 `score` 显示为概率；
- [ ] 搜索 Hit 使用 `asset_id` 回查本地 SQLite；
- [ ] 可按 `retryable`、`Retry-After` 和错误码执行有限重试；
- [ ] 已确认第一版是否需要启用 `video_frame`；
- [ ] 已准备联调用的测试 `library_id`、测试资产和安全分发的 API Key。

## 19. 请前端回复的确认结果

请回复以下内容，作为正式联调开始条件：

```text
ImageLib 版本：
负责模块/联系人：
library_id 生成方式：
asset_id 类型与示例：
client_revision 生成规则：
缩略图格式与最大尺寸：
第一版是否包含 video_frame：是 / 否
需要启用的 filters：
API Key 保存方式：
预计首次同步资产数量：
预计索引并发：
对本文字段或状态的异议：
```

完成上述确认后，双方以 [openapi.json](./openapi.json) 固化客户端类型和联调测试。
