from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal

import imagehash
import torch
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    Query,
    Request,
    Security,
    UploadFile,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from qdrant_client import models as qmodels
from starlette.exceptions import HTTPException as StarletteHTTPException


API_VERSION = "1.0.0"
MODEL_VERSION = os.environ.get("FRAMEFINDER_MODEL_VERSION", "giant-hplus-v3")
COLLECTION_GENERATION = os.environ.get(
    "FRAMEFINDER_COLLECTION_GENERATION", "2026-07-15-01"
)
SERVER_INSTANCE_ID = os.environ.get("FRAMEFINDER_SERVER_INSTANCE_ID", "ff-server-01")
MAX_UPLOAD_BYTES = int(os.environ.get("FRAMEFINDER_MAX_UPLOAD_BYTES", 20 * 1024 * 1024))
MAX_BATCH_STATUS_ITEMS = int(os.environ.get("FRAMEFINDER_MAX_BATCH_STATUS_ITEMS", 1000))
DEFAULT_SEARCH_LIMIT = int(os.environ.get("FRAMEFINDER_DEFAULT_SEARCH_LIMIT", 120))
MAX_SEARCH_LIMIT = int(os.environ.get("FRAMEFINDER_MAX_SEARCH_LIMIT", 500))
SEARCH_SESSION_MAX_RESULTS = int(
    os.environ.get("FRAMEFINDER_SEARCH_SESSION_MAX_RESULTS", 5000)
)
SEARCH_SESSION_TTL_SECONDS = int(
    os.environ.get("FRAMEFINDER_SEARCH_SESSION_TTL_SECONDS", 600)
)
MAX_CONCURRENT_INFERENCE = int(os.environ.get("FRAMEFINDER_MAX_CONCURRENT_INFERENCE", 2))
MAX_QUERY_LENGTH = int(os.environ.get("FRAMEFINDER_MAX_QUERY_LENGTH", 2000))
ACCEPTED_MIME_TYPES = {"image/jpeg", "image/png"}
SUPPORTED_MEDIA_TYPES = {"image", "video_frame"}
SUPPORTED_METADATA_FIELDS = {
    "asset_kind",
    "width",
    "height",
    "storage_root_id",
    "virtual_folder_ids",
    "tag_ids",
    "source_video_id",
    "timestamp_ms",
    "metadata_updated_at",
}
SUPPORTED_FILTER_FIELDS = {
    "media_types",
    "storage_root_ids",
    "virtual_folder_ids_any",
    "virtual_folder_ids_all",
    "tag_ids_any",
    "tag_ids_all",
    "source_video_id",
    "exclude_asset_ids",
}
FRAMEFINDER_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "framefinder:image-lib:v1")
HASH_PATTERN = "sha256:"
API_KEY_SECURITY_SCHEME = APIKeyHeader(
    name="X-FrameFinder-Key",
    scheme_name="FrameFinderApiKey",
    description="FrameFinder /api/v1 API Key。必须通过安全配置提供，不得硬编码或记录到日志。",
    auto_error=False,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_value(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def metadata_hash(metadata: dict[str, Any]) -> str:
    return sha256_value(canonical_json(metadata).encode("utf-8"))


def asset_point_id(library_id: str, asset_id: str) -> str:
    return str(uuid.uuid5(FRAMEFINDER_NAMESPACE, f"{library_id}:{asset_id}"))


def request_id(request: Request) -> str:
    return getattr(request.state, "request_id", f"req-{uuid.uuid4()}")


class FrameFinderError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}
        self.headers = headers or {}


def error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    rid = request_id(request)
    response = JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
                "request_id": rid,
                "details": details or {},
            }
        },
        headers=headers,
    )
    response.headers["X-Request-Id"] = rid
    return response


def install_error_handling(app: Any) -> None:
    @app.middleware("http")
    async def add_request_id(request: Request, call_next: Callable):
        incoming = request.headers.get("X-Request-Id", "").strip()
        request.state.request_id = incoming[:128] if incoming else f"req-{uuid.uuid4()}"
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    @app.exception_handler(FrameFinderError)
    async def framefinder_error_handler(request: Request, exc: FrameFinderError):
        return error_response(
            request,
            exc.status_code,
            exc.code,
            exc.message,
            retryable=exc.retryable,
            details=exc.details,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        problems = []
        unsupported_metadata_fields = []
        for item in exc.errors():
            location = tuple(item.get("loc", ()))
            if item.get("type") == "extra_forbidden" and "metadata" in location:
                unsupported_metadata_fields.append(str(location[-1]))
            problems.append(
                {
                    "field": ".".join(str(part) for part in location),
                    "message": item.get("msg", "invalid value"),
                    "type": item.get("type", "validation_error"),
                }
            )
        if unsupported_metadata_fields:
            return error_response(
                request,
                422,
                "UNSUPPORTED_METADATA_FIELD",
                "metadata 包含服务端不支持的字段",
                details={"fields": sorted(set(unsupported_metadata_fields))},
            )
        return error_response(
            request,
            422,
            "INVALID_REQUEST",
            "请求字段校验失败",
            details={"problems": problems},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        message = str(exc.detail) if exc.detail else "请求失败"
        code = {
            400: "INVALID_REQUEST",
            401: "INVALID_API_KEY",
            403: "ACCESS_DENIED",
            404: "NOT_FOUND",
            413: "UPLOAD_TOO_LARGE",
            429: "RATE_LIMITED",
            503: "SERVICE_UNAVAILABLE",
        }.get(exc.status_code, "HTTP_ERROR")
        return error_response(request, exc.status_code, code, message)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        return error_response(
            request,
            500,
            "INTERNAL_ERROR",
            "服务端发生未预期错误",
            retryable=False,
            details={"reason": exc.__class__.__name__},
        )


def install_openapi_response_headers(app: Any) -> None:
    original_openapi = app.openapi

    def openapi_with_response_headers() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = original_openapi()
        methods = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
        request_id_header = {
            "description": "服务端请求追踪 ID；反馈问题时请提供该值。",
            "schema": {"type": "string", "example": "req-..."},
        }
        retry_after_header = {
            "description": "客户端再次尝试前应等待的秒数。",
            "schema": {"type": "integer", "minimum": 0, "example": 1},
        }
        for path_item in schema.get("paths", {}).values():
            for method, operation in path_item.items():
                if method not in methods or not isinstance(operation, dict):
                    continue
                for response_code, response in operation.get("responses", {}).items():
                    headers = response.setdefault("headers", {})
                    headers.setdefault("X-Request-Id", request_id_header)
                    if response_code == "429":
                        headers.setdefault("Retry-After", retry_after_header)
        app.openapi_schema = schema
        return schema

    app.openapi = openapi_with_response_headers


def require_api_key(
    api_key: str | None = Security(API_KEY_SECURITY_SCHEME),
) -> None:
    expected = os.environ.get("FRAMEFINDER_API_KEY", "")
    if not expected:
        raise FrameFinderError(
            503,
            "API_KEY_NOT_CONFIGURED",
            "服务端尚未配置 FrameFinder API Key",
            retryable=False,
        )
    if api_key is None or not hmac.compare_digest(api_key, expected):
        raise FrameFinderError(401, "INVALID_API_KEY", "API Key 无效")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


ErrorCode = Literal[
    "INVALID_REQUEST",
    "INVALID_API_KEY",
    "ACCESS_DENIED",
    "API_KEY_NOT_CONFIGURED",
    "ASSET_NOT_FOUND",
    "ASSET_NOT_INDEXED",
    "STALE_CLIENT_REVISION",
    "CLIENT_REVISION_CONFLICT",
    "MODEL_VERSION_MISMATCH",
    "SEARCH_CURSOR_EXPIRED",
    "MANIFEST_CURSOR_EXPIRED",
    "UPLOAD_TOO_LARGE",
    "BATCH_TOO_LARGE",
    "UNSUPPORTED_MEDIA_TYPE",
    "UNSUPPORTED_FILTER",
    "UNSUPPORTED_METADATA_FIELD",
    "INVALID_METADATA",
    "INVALID_HASH",
    "RATE_LIMITED",
    "MODEL_UNAVAILABLE",
    "VECTOR_DATABASE_UNAVAILABLE",
    "INFERENCE_TIMEOUT",
    "INTERNAL_ERROR",
    "NOT_FOUND",
    "HTTP_ERROR",
    "SERVICE_UNAVAILABLE",
]


class ErrorBody(StrictModel):
    code: ErrorCode
    message: str
    retryable: bool
    request_id: str
    details: dict[str, Any]


class ErrorEnvelope(StrictModel):
    error: ErrorBody


COMMON_ERROR_RESPONSES = {
    400: {"model": ErrorEnvelope, "description": "请求格式错误"},
    401: {"model": ErrorEnvelope, "description": "API Key 无效"},
    403: {"model": ErrorEnvelope, "description": "拒绝访问"},
    404: {"model": ErrorEnvelope, "description": "资产或路径不存在"},
    409: {"model": ErrorEnvelope, "description": "资产版本或游标状态冲突"},
    413: {"model": ErrorEnvelope, "description": "上传或批次超过限制"},
    422: {"model": ErrorEnvelope, "description": "字段或媒体类型不受支持"},
    429: {"model": ErrorEnvelope, "description": "推理并发已满"},
    500: {"model": ErrorEnvelope, "description": "未预期服务端错误"},
    503: {"model": ErrorEnvelope, "description": "模型或向量数据库暂不可用"},
    504: {"model": ErrorEnvelope, "description": "推理超时"},
}


class VectorDimensions(StrictModel):
    semantic: int
    visual: int


class AssetMetadata(StrictModel):
    asset_kind: Literal["image", "video_frame"] | None = None
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    storage_root_id: str | None = None
    virtual_folder_ids: list[str] | None = None
    tag_ids: list[str] | None = None
    source_video_id: str | None = None
    timestamp_ms: int | None = Field(default=None, ge=0)
    metadata_updated_at: datetime | None = None


class AssetLastError(StrictModel):
    code: str
    message: str
    retryable: bool
    details: dict[str, Any] = Field(default_factory=dict)


class UpsertResponse(StrictModel):
    library_id: str
    asset_id: str
    status: Literal["created", "updated", "reused", "metadata_only"]
    client_revision: int
    source_content_hash: str
    indexed_file_hash: str
    model_version: str
    collection_generation: str
    vectors: VectorDimensions
    phash: str
    indexed_at: str
    request_id: str


class AssetStatusResponse(StrictModel):
    library_id: str
    asset_id: str
    status: Literal["indexed", "model_outdated", "indexing", "failed"]
    client_revision: int
    source_content_hash: str
    indexed_file_hash: str
    model_version: str
    collection_generation: str
    indexed_at: str
    last_error: AssetLastError | None = None
    request_id: str


class BatchStatusResult(StrictModel):
    asset_id: str
    status: Literal[
        "indexed",
        "missing",
        "stale_content",
        "stale_metadata",
        "model_outdated",
        "indexing",
        "failed",
    ]
    needs_upsert: bool
    needs_metadata_patch: bool
    model_version: str | None
    last_error: AssetLastError | None = None


class BatchStatusResponse(StrictModel):
    library_id: str
    count: int
    results: list[BatchStatusResult]
    request_id: str


class DeleteResponse(StrictModel):
    library_id: str
    asset_id: str
    deleted: bool
    existed: bool
    request_id: str


class ItemError(StrictModel):
    code: str
    message: str
    retryable: bool


class BatchDeleteResult(StrictModel):
    asset_id: str
    deleted: bool
    existed: bool
    error: ItemError | None


class BatchDeleteResponse(StrictModel):
    library_id: str
    count: int
    results: list[BatchDeleteResult]
    request_id: str


class MetadataPatchResponse(StrictModel):
    library_id: str
    asset_id: str
    status: Literal["metadata_only"]
    client_revision: int
    metadata_hash: str
    request_id: str


class SearchHit(StrictModel):
    asset_id: str
    rank: int
    score: float = Field(description="相似度排序分数，不是概率")
    semantic_score: float | None
    visual_score: float | None
    phash_distance: int | None
    indexed_source_hash: str


class SearchResponse(StrictModel):
    query_type: Literal["text", "image_asset"]
    library_id: str
    count: int = Field(description="当前页 hits 数量")
    total_count: int = Field(description="当前稳定搜索会话中捕获的结果总数")
    results_truncated: bool = Field(
        description="结果是否达到服务端 search_session_max_results 上限而被截断"
    )
    limit: int
    has_more: bool
    next_cursor: str | None
    search_session_id: str
    cursor_expires_at: str
    elapsed_ms: int
    model_version: str
    collection_generation: str
    hits: list[SearchHit]
    request_id: str


class ManifestAsset(StrictModel):
    asset_id: str
    source_content_hash: str
    client_revision: int
    model_version: str
    status: Literal["indexed", "model_outdated"]


class ManifestResponse(StrictModel):
    library_id: str
    has_more: bool
    next_cursor: str | None
    assets: list[ManifestAsset]
    request_id: str


class CapabilityModels(StrictModel):
    semantic: str
    visual: str
    model_version: str


class CapabilityCollection(StrictModel):
    name: str
    generation: str
    status: Literal["active", "unavailable"]
    stored_assets: int


class CapabilityLimits(StrictModel):
    max_upload_bytes: int
    max_batch_status_items: int
    default_search_limit: int
    max_search_limit: int
    max_query_length: int
    max_concurrent_inference: int
    search_session_max_results: int


class CapabilityFeatures(StrictModel):
    text_search: bool
    search_by_asset: bool
    search_by_upload: bool
    metadata_patch: bool
    metadata_filters: bool
    async_indexing: bool
    batch_delete: bool
    asset_manifest: bool


class CapabilitiesResponse(StrictModel):
    ok: bool
    api_version: str
    server_instance_id: str
    accepting_requests: bool
    models_loaded: bool
    device: str
    models: CapabilityModels
    collection: CapabilityCollection
    supported_media_types: list[str]
    accepted_upload_mime_types: list[str]
    supported_filters: list[str]
    limits: CapabilityLimits
    features: CapabilityFeatures
    server_time: str
    request_id: str


class HealthDatabaseStatus(StrictModel):
    online: bool
    collection: str
    stored_points: int
    error: str | None = None


class HealthResponse(StrictModel):
    ok: bool
    loaded: bool = Field(description="兼容旧网页客户端；等同于 models_loaded")
    models_loaded: bool
    accepting_requests: bool
    status: Literal["warming", "ready"]
    device: str
    semantic_model: str
    visual_model: str
    database: HealthDatabaseStatus


class SearchFilters(StrictModel):
    media_types: list[Literal["image", "video_frame"]] | None = None
    storage_root_ids: list[str] | None = None
    virtual_folder_ids_any: list[str] | None = None
    virtual_folder_ids_all: list[str] | None = None
    tag_ids_any: list[str] | None = None
    tag_ids_all: list[str] | None = None
    source_video_id: str | None = None
    exclude_asset_ids: list[str] | None = None


class TextSearchRequest(StrictModel):
    library_id: str = Field(min_length=1, max_length=256)
    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    limit: int = Field(default=DEFAULT_SEARCH_LIMIT, ge=1, le=MAX_SEARCH_LIMIT)
    cursor: str | None = None
    min_score: float | None = None
    filters: SearchFilters | None = None

    @field_validator("query")
    @classmethod
    def query_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query cannot be blank")
        return value


class AssetSearchRequest(StrictModel):
    library_id: str = Field(min_length=1, max_length=256)
    asset_id: str = Field(min_length=1, max_length=512)
    limit: int = Field(default=DEFAULT_SEARCH_LIMIT, ge=1, le=MAX_SEARCH_LIMIT)
    cursor: str | None = None
    min_score: float | None = None
    semantic_weight: float = Field(default=0.55, ge=0)
    visual_weight: float = Field(default=0.45, ge=0)
    exclude_self: bool = True
    filters: SearchFilters | None = None

    @field_validator("visual_weight")
    @classmethod
    def weights_not_both_zero(cls, value: float, info: Any) -> float:
        if value == 0 and info.data.get("semantic_weight", 0) == 0:
            raise ValueError("at least one search weight must be greater than zero")
        return value


class StatusAsset(StrictModel):
    asset_id: str = Field(min_length=1, max_length=512)
    source_content_hash: str
    client_revision: int = Field(ge=0)

    @field_validator("source_content_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return validate_sha256(value, "source_content_hash")


class BatchStatusRequest(StrictModel):
    library_id: str = Field(min_length=1, max_length=256)
    assets: list[StatusAsset]


class DeleteAsset(StrictModel):
    asset_id: str = Field(min_length=1, max_length=512)
    client_revision: int = Field(ge=0)


class BatchDeleteRequest(StrictModel):
    library_id: str = Field(min_length=1, max_length=256)
    assets: list[DeleteAsset]


class MetadataPatchRequest(StrictModel):
    client_revision: int = Field(ge=0)
    metadata: AssetMetadata


def validate_sha256(value: str, field_name: str) -> str:
    value = value.strip().lower()
    if not value.startswith(HASH_PATTERN) or len(value) != 71:
        raise ValueError(f"{field_name} must use sha256:<64 lowercase hex> format")
    try:
        int(value[7:], 16)
    except ValueError as error:
        raise ValueError(f"{field_name} contains invalid hexadecimal digits") from error
    return value


def parse_metadata(value: str | None) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise FrameFinderError(
            422,
            "INVALID_METADATA",
            "metadata 必须是有效的 JSON 对象字符串",
            details={"offset": error.pos},
        ) from error
    if not isinstance(parsed, dict):
        raise FrameFinderError(422, "INVALID_METADATA", "metadata 必须是 JSON 对象")
    validate_metadata(parsed)
    try:
        return AssetMetadata.model_validate(parsed).model_dump(mode="json", exclude_unset=True)
    except ValidationError as error:
        raise FrameFinderError(
            422,
            "INVALID_METADATA",
            "metadata 字段类型或取值无效",
            details={
                "problems": [
                    {
                        "field": ".".join(str(part) for part in item.get("loc", ())),
                        "message": item.get("msg", "invalid value"),
                        "type": item.get("type", "validation_error"),
                    }
                    for item in error.errors()
                ]
            },
        ) from error


def validate_metadata(metadata: dict[str, Any]) -> None:
    unsupported = sorted(set(metadata) - SUPPORTED_METADATA_FIELDS)
    if unsupported:
        raise FrameFinderError(
            422,
            "UNSUPPORTED_METADATA_FIELD",
            "metadata 包含服务端不支持的字段",
            details={"fields": unsupported},
        )
    for key in ("virtual_folder_ids", "tag_ids"):
        if key in metadata and not isinstance(metadata[key], list):
            raise FrameFinderError(
                422,
                "INVALID_METADATA",
                f"metadata.{key} 必须是字符串数组",
                details={"field": key},
            )


def decode_uploaded_image(data: bytes, filename: str, mime_type: str | None) -> Image.Image:
    if len(data) > MAX_UPLOAD_BYTES:
        raise FrameFinderError(
            413,
            "UPLOAD_TOO_LARGE",
            "上传图片超过服务端大小上限",
            details={"max_upload_bytes": MAX_UPLOAD_BYTES},
        )
    if mime_type not in ACCEPTED_MIME_TYPES:
        raise FrameFinderError(
            422,
            "UNSUPPORTED_MEDIA_TYPE",
            "仅接受 JPEG 或 PNG 图片",
            details={"content_type": mime_type},
        )
    try:
        image = Image.open(io.BytesIO(data))
        detected = image.format
        if detected not in {"JPEG", "PNG"}:
            raise FrameFinderError(422, "UNSUPPORTED_MEDIA_TYPE", "图片内容不是 JPEG 或 PNG")
        return image.convert("RGB")
    except FrameFinderError:
        raise
    except (UnidentifiedImageError, OSError) as error:
        raise FrameFinderError(422, "UNSUPPORTED_MEDIA_TYPE", f"无法读取图片：{filename}") from error


def qdrant_error(error: Exception) -> FrameFinderError:
    return FrameFinderError(
        503,
        "VECTOR_DATABASE_UNAVAILABLE",
        "向量数据库暂不可用",
        retryable=True,
        details={"reason": error.__class__.__name__},
    )


@dataclass
class PageSession:
    session_id: str
    fingerprint: str
    query_type: str
    library_id: str
    hits: list[dict[str, Any]]
    expires_at: datetime
    generation: str
    results_truncated: bool


class SessionStore:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sessions: dict[str, PageSession] = {}
        self.cursors: dict[str, tuple[str, int]] = {}

    def create(
        self,
        fingerprint: str,
        query_type: str,
        library_id: str,
        hits: list[dict[str, Any]],
        results_truncated: bool,
    ) -> PageSession:
        session = PageSession(
            session_id=f"search-{uuid.uuid4()}",
            fingerprint=fingerprint,
            query_type=query_type,
            library_id=library_id,
            hits=hits,
            expires_at=utc_now() + timedelta(seconds=SEARCH_SESSION_TTL_SECONDS),
            generation=COLLECTION_GENERATION,
            results_truncated=results_truncated,
        )
        with self.lock:
            self._cleanup_locked()
            self.sessions[session.session_id] = session
        return session

    def cursor_for(self, session: PageSession, offset: int) -> str:
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.cursors[token] = (session.session_id, offset)
        return token

    def resolve(self, cursor: str, fingerprint: str) -> tuple[PageSession, int]:
        with self.lock:
            self._cleanup_locked()
            location = self.cursors.get(cursor)
            session = self.sessions.get(location[0]) if location else None
            if (
                location is None
                or session is None
                or session.fingerprint != fingerprint
                or session.generation != COLLECTION_GENERATION
            ):
                raise FrameFinderError(
                    409,
                    "SEARCH_CURSOR_EXPIRED",
                    "搜索游标已过期或与当前查询不匹配",
                    retryable=True,
                )
            return session, location[1]

    def _cleanup_locked(self) -> None:
        now = utc_now()
        expired = {key for key, value in self.sessions.items() if value.expires_at <= now}
        for key in expired:
            self.sessions.pop(key, None)
        for key, value in list(self.cursors.items()):
            if value[0] in expired:
                self.cursors.pop(key, None)


class ImageLibService:
    def __init__(
        self,
        cache: Any,
        model_store: Any,
        *,
        collection: str,
        semantic_model: str,
        visual_model: str,
        semantic_vector_size: int,
        visual_vector_size: int,
    ) -> None:
        self.cache = cache
        self.models = model_store
        self.collection = collection
        self.semantic_model = semantic_model
        self.visual_model = visual_model
        self.semantic_vector_size = semantic_vector_size
        self.visual_vector_size = visual_vector_size
        self.inference_slots = threading.BoundedSemaphore(MAX_CONCURRENT_INFERENCE)
        self.sessions = SessionStore()

    @property
    def client(self) -> Any:
        return self.cache.client

    def ensure_collection(self) -> None:
        try:
            self.cache.ensure_collection()
        except Exception as error:
            raise qdrant_error(error) from error

    def get_record(self, library_id: str, asset_id: str, *, vectors: bool = False) -> Any | None:
        self.ensure_collection()
        try:
            records = self.client.retrieve(
                collection_name=self.collection,
                ids=[asset_point_id(library_id, asset_id)],
                with_payload=True,
                with_vectors=vectors,
            )
        except Exception as error:
            raise qdrant_error(error) from error
        return records[0] if records else None

    def formal_asset_count(self) -> int:
        self.ensure_collection()
        try:
            result = self.client.count(
                collection_name=self.collection,
                count_filter=qmodels.Filter(
                    must=[qmodels.IsNullCondition(is_null=qmodels.PayloadField(key="library_id"))]
                ),
                exact=True,
            )
            # IsNull means missing or null, so subtract legacy cache points from total.
            total = self.client.get_collection(self.collection).points_count or 0
            return max(0, total - result.count)
        except Exception:
            # Count is informational; do not make capabilities fail on old Qdrant versions.
            return 0

    def infer_image(self, image: Image.Image) -> tuple[list[float], list[float], str]:
        if not self.inference_slots.acquire(blocking=False):
            raise FrameFinderError(
                429,
                "RATE_LIMITED",
                "当前推理并发已满，请稍后重试",
                retryable=True,
                headers={"Retry-After": "1"},
            )
        try:
            try:
                self.models.load()
                semantic = self.models.semantic_images([image])[0].tolist()
                visual = self.models.visual_images([image])[0].tolist()
                return semantic, visual, str(imagehash.phash(image))
            except FrameFinderError:
                raise
            except Exception as error:
                raise FrameFinderError(
                    503,
                    "MODEL_UNAVAILABLE",
                    "图片模型暂不可用",
                    retryable=True,
                    details={"reason": error.__class__.__name__},
                ) from error
        finally:
            self.inference_slots.release()

    def infer_text(self, query: str) -> list[float]:
        if not self.inference_slots.acquire(blocking=False):
            raise FrameFinderError(
                429,
                "RATE_LIMITED",
                "当前推理并发已满，请稍后重试",
                retryable=True,
                headers={"Retry-After": "1"},
            )
        try:
            try:
                self.models.load()
                return self.models.semantic_text(query)[0].tolist()
            except Exception as error:
                raise FrameFinderError(
                    503,
                    "MODEL_UNAVAILABLE",
                    "文本模型暂不可用",
                    retryable=True,
                    details={"reason": error.__class__.__name__},
                ) from error
        finally:
            self.inference_slots.release()

    def reusable_vectors(self, indexed_hash: str) -> tuple[list[float], list[float], str] | None:
        self.ensure_collection()
        query_filter = qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="indexed_file_hash", match=qmodels.MatchValue(value=indexed_hash)
                ),
                qmodels.FieldCondition(
                    key="model_version", match=qmodels.MatchValue(value=MODEL_VERSION)
                ),
            ]
        )
        try:
            records, _ = self.client.scroll(
                collection_name=self.collection,
                scroll_filter=query_filter,
                limit=1,
                with_payload=True,
                with_vectors=True,
            )
            if not records:
                # Reuse vectors created by the legacy web endpoint when possible.
                raw_hash = indexed_hash.removeprefix(HASH_PATTERN)
                legacy_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"framefinder:{raw_hash}"))
                records = self.client.retrieve(
                    collection_name=self.collection,
                    ids=[legacy_id],
                    with_payload=True,
                    with_vectors=True,
                )
            if not records:
                return None
            vectors = records[0].vector if isinstance(records[0].vector, dict) else {}
            payload = records[0].payload or {}
            semantic = vectors.get("semantic")
            visual = vectors.get("visual")
            phash = payload.get("phash")
            if semantic is None or visual is None or not phash:
                return None
            return list(semantic), list(visual), str(phash)
        except Exception as error:
            raise qdrant_error(error) from error

    def upsert_point(
        self,
        point_id: str,
        semantic: list[float],
        visual: list[float],
        payload: dict[str, Any],
    ) -> None:
        self.ensure_collection()
        try:
            self.client.upsert(
                collection_name=self.collection,
                points=[
                    qmodels.PointStruct(
                        id=point_id,
                        vector={"semantic": semantic, "visual": visual},
                        payload=payload,
                    )
                ],
                wait=True,
            )
        except Exception as error:
            raise qdrant_error(error) from error

    def set_metadata(self, record: Any, metadata: dict[str, Any], revision: int) -> None:
        payload = dict(record.payload or {})
        for field in SUPPORTED_METADATA_FIELDS:
            payload.pop(field, None)
        payload.update(metadata)
        payload["metadata"] = metadata
        payload["metadata_hash"] = metadata_hash(metadata)
        payload["client_revision"] = revision
        payload["updated_at"] = iso_now()
        try:
            self.client.overwrite_payload(
                collection_name=self.collection,
                payload=payload,
                points=[record.id],
                wait=True,
            )
        except Exception as error:
            raise qdrant_error(error) from error

    def delete_asset(self, library_id: str, asset_id: str, revision: int) -> tuple[bool, bool]:
        record = self.get_record(library_id, asset_id)
        if not record:
            return True, False
        current_revision = int((record.payload or {}).get("client_revision", 0))
        if revision < current_revision:
            raise FrameFinderError(
                409,
                "STALE_CLIENT_REVISION",
                "删除请求的 client_revision 早于服务端状态",
                details={
                    "library_id": library_id,
                    "asset_id": asset_id,
                    "current_revision": current_revision,
                },
            )
        try:
            self.client.delete(
                collection_name=self.collection,
                points_selector=[record.id],
                wait=True,
            )
        except Exception as error:
            raise qdrant_error(error) from error
        return True, True

    def search_filter(self, library_id: str, filters: SearchFilters | None) -> qmodels.Filter:
        must: list[Any] = [
            qmodels.FieldCondition(key="library_id", match=qmodels.MatchValue(value=library_id))
        ]
        must_not: list[Any] = []
        if filters:
            values = filters.model_dump(exclude_none=True)
            media_types = values.get("media_types")
            if media_types:
                must.append(
                    qmodels.FieldCondition(
                        key="media_type", match=qmodels.MatchAny(any=media_types)
                    )
                )
            for request_key, payload_key in (
                ("storage_root_ids", "storage_root_id"),
                ("virtual_folder_ids_any", "virtual_folder_ids"),
                ("tag_ids_any", "tag_ids"),
            ):
                if values.get(request_key):
                    must.append(
                        qmodels.FieldCondition(
                            key=payload_key,
                            match=qmodels.MatchAny(any=values[request_key]),
                        )
                    )
            for request_key, payload_key in (
                ("virtual_folder_ids_all", "virtual_folder_ids"),
                ("tag_ids_all", "tag_ids"),
            ):
                for value in values.get(request_key, []):
                    must.append(
                        qmodels.FieldCondition(
                            key=payload_key, match=qmodels.MatchValue(value=value)
                        )
                    )
            if values.get("source_video_id"):
                must.append(
                    qmodels.FieldCondition(
                        key="source_video_id",
                        match=qmodels.MatchValue(value=values["source_video_id"]),
                    )
                )
            excluded = values.get("exclude_asset_ids", [])
            if excluded:
                must_not.append(
                    qmodels.HasIdCondition(
                        has_id=[asset_point_id(library_id, item) for item in excluded]
                    )
                )
        return qmodels.Filter(must=must, must_not=must_not or None)

    def query_vector(
        self,
        vector: list[float],
        using: str,
        library_id: str,
        filters: SearchFilters | None,
    ) -> list[Any]:
        self.ensure_collection()
        try:
            response = self.client.query_points(
                collection_name=self.collection,
                query=vector,
                using=using,
                query_filter=self.search_filter(library_id, filters),
                limit=SEARCH_SESSION_MAX_RESULTS + 1,
                with_payload=True,
                with_vectors=False,
            )
            return list(response.points)
        except Exception as error:
            raise qdrant_error(error) from error

    def response_page(
        self,
        request: Request,
        body: TextSearchRequest | AssetSearchRequest,
        query_type: str,
        hits: list[dict[str, Any]] | None = None,
        elapsed_ms: int = 0,
    ) -> dict[str, Any]:
        fingerprint_body = body.model_dump(exclude={"cursor", "limit"})
        fingerprint = sha256_value(canonical_json(fingerprint_body).encode("utf-8"))
        if body.cursor:
            session, offset = self.sessions.resolve(body.cursor, fingerprint)
        else:
            all_hits = hits or []
            results_truncated = len(all_hits) > SEARCH_SESSION_MAX_RESULTS
            captured_hits = all_hits[:SEARCH_SESSION_MAX_RESULTS]
            session = self.sessions.create(
                fingerprint,
                query_type,
                body.library_id,
                captured_hits,
                results_truncated,
            )
            offset = 0
        page = session.hits[offset : offset + body.limit]
        next_offset = offset + len(page)
        has_more = next_offset < len(session.hits)
        next_cursor = self.sessions.cursor_for(session, next_offset) if has_more else None
        ranked = []
        for index, hit in enumerate(page, start=offset + 1):
            ranked.append({**hit, "rank": index})
        return {
            "query_type": query_type,
            "library_id": body.library_id,
            "count": len(ranked),
            "total_count": len(session.hits),
            "results_truncated": session.results_truncated,
            "limit": body.limit,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "search_session_id": session.session_id,
            "cursor_expires_at": session.expires_at.isoformat(timespec="seconds"),
            "elapsed_ms": elapsed_ms,
            "model_version": MODEL_VERSION,
            "collection_generation": COLLECTION_GENERATION,
            "hits": ranked,
            "request_id": request_id(request),
        }


def install_imagelib_api(
    app: Any,
    cache: Any,
    model_store: Any,
    *,
    collection: str,
    semantic_model: str,
    visual_model: str,
    semantic_vector_size: int,
    visual_vector_size: int,
) -> ImageLibService:
    install_error_handling(app)
    service = ImageLibService(
        cache,
        model_store,
        collection=collection,
        semantic_model=semantic_model,
        visual_model=visual_model,
        semantic_vector_size=semantic_vector_size,
        visual_vector_size=visual_vector_size,
    )
    router = APIRouter(
        prefix="/api/v1",
        dependencies=[Depends(require_api_key)],
        responses=COMMON_ERROR_RESPONSES,
    )

    @router.get(
        "/capabilities",
        summary="查询 FrameFinder 服务能力",
        response_model=CapabilitiesResponse,
    )
    def capabilities(request: Request) -> dict[str, Any]:
        status = cache.status()
        accepting = bool(status.get("online"))
        device = (
            torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU"
        )
        return {
            "ok": True,
            "api_version": API_VERSION,
            "server_instance_id": SERVER_INSTANCE_ID,
            "accepting_requests": accepting,
            "models_loaded": bool(model_store.loaded),
            "device": device,
            "models": {
                "semantic": semantic_model,
                "visual": visual_model,
                "model_version": MODEL_VERSION,
            },
            "collection": {
                "name": collection,
                "generation": COLLECTION_GENERATION,
                "status": "active" if accepting else "unavailable",
                "stored_assets": service.formal_asset_count() if accepting else 0,
            },
            "supported_media_types": sorted(SUPPORTED_MEDIA_TYPES),
            "accepted_upload_mime_types": sorted(ACCEPTED_MIME_TYPES),
            "supported_filters": sorted(SUPPORTED_FILTER_FIELDS),
            "limits": {
                "max_upload_bytes": MAX_UPLOAD_BYTES,
                "max_batch_status_items": MAX_BATCH_STATUS_ITEMS,
                "default_search_limit": DEFAULT_SEARCH_LIMIT,
                "max_search_limit": MAX_SEARCH_LIMIT,
                "max_query_length": MAX_QUERY_LENGTH,
                "max_concurrent_inference": MAX_CONCURRENT_INFERENCE,
                "search_session_max_results": SEARCH_SESSION_MAX_RESULTS,
            },
            "features": {
                "text_search": True,
                "search_by_asset": True,
                "search_by_upload": False,
                "metadata_patch": True,
                "metadata_filters": True,
                "async_indexing": False,
                "batch_delete": True,
                "asset_manifest": True,
            },
            "server_time": iso_now(),
            "request_id": request_id(request),
        }

    @router.post(
        "/assets/upsert",
        summary="新增或更新单个资产",
        response_model=UpsertResponse,
        status_code=201,
        responses={
            200: {
                "model": UpsertResponse,
                "description": "资产已更新、复用或仅更新元数据",
            }
        },
    )
    async def upsert_asset(
        request: Request,
        library_id: str = Form(..., min_length=1, max_length=256),
        asset_id: str = Form(..., min_length=1, max_length=512),
        file: UploadFile = File(...),
        source_content_hash: str = Form(...),
        client_revision: int = Form(..., ge=0),
        media_type: Literal["image", "video_frame"] = Form(...),
        metadata: str | None = Form(
            default=None,
            description="JSON 编码的 AssetMetadata 对象；正式字段见 components.schemas.AssetMetadata。",
        ),
        force: bool = Form(default=False),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        del idempotency_key  # Deterministic point IDs and revision checks provide durable idempotency.
        try:
            source_hash = validate_sha256(source_content_hash, "source_content_hash")
        except ValueError as error:
            raise FrameFinderError(422, "INVALID_HASH", str(error)) from error
        raw = await file.read(MAX_UPLOAD_BYTES + 1)
        image = decode_uploaded_image(raw, file.filename or "upload", file.content_type)
        indexed_hash = sha256_value(raw)
        parsed_metadata = parse_metadata(metadata)
        parsed_metadata.setdefault("asset_kind", media_type)
        parsed_metadata.setdefault("width", image.width)
        parsed_metadata.setdefault("height", image.height)
        current = service.get_record(library_id, asset_id, vectors=True)
        current_payload = dict(current.payload or {}) if current else {}
        current_revision = int(current_payload.get("client_revision", -1))
        current_metadata = current_payload.get("metadata", {})
        exact_content = bool(
            current
            and current_payload.get("source_content_hash") == source_hash
            and current_payload.get("indexed_file_hash") == indexed_hash
            and current_payload.get("media_type") == media_type
        )
        current_model = current_payload.get("model_version") == MODEL_VERSION
        exact_metadata = current_metadata == parsed_metadata

        if current and client_revision < current_revision:
            raise FrameFinderError(
                409,
                "STALE_CLIENT_REVISION",
                "client_revision 早于服务端当前版本",
                details={
                    "library_id": library_id,
                    "asset_id": asset_id,
                    "current_revision": current_revision,
                },
            )
        if current and client_revision == current_revision and not (exact_content and exact_metadata):
            raise FrameFinderError(
                409,
                "CLIENT_REVISION_CONFLICT",
                "相同 client_revision 对应的内容与服务端记录不一致",
                details={"library_id": library_id, "asset_id": asset_id},
            )

        indexed_at = current_payload.get("indexed_at") or iso_now()
        response_status = "created" if current is None else "updated"
        if current and exact_content and exact_metadata and current_model and not force:
            response_status = "reused"
            semantic = (current.vector or {}).get("semantic")
            visual = (current.vector or {}).get("visual")
            phash = current_payload.get("phash")
        elif current and exact_content and current_model and not force:
            service.set_metadata(current, parsed_metadata, client_revision)
            response_status = "metadata_only"
            semantic = (current.vector or {}).get("semantic")
            visual = (current.vector or {}).get("visual")
            phash = current_payload.get("phash")
        else:
            reusable = None
            if current is None and not force:
                reusable = service.reusable_vectors(indexed_hash)
            if reusable:
                semantic, visual, phash = reusable
                response_status = "reused"
            else:
                semantic, visual, phash = service.infer_image(image)
            indexed_at = iso_now()
            payload = {
                "library_id": library_id,
                "asset_id": asset_id,
                "source_content_hash": source_hash,
                "indexed_file_hash": indexed_hash,
                "client_revision": client_revision,
                "media_type": media_type,
                "model_version": MODEL_VERSION,
                "collection_generation": COLLECTION_GENERATION,
                "semantic_model": semantic_model,
                "visual_model": visual_model,
                "phash": phash,
                "indexed_at": indexed_at,
                "updated_at": iso_now(),
                "metadata": parsed_metadata,
                "metadata_hash": metadata_hash(parsed_metadata),
                **parsed_metadata,
            }
            service.upsert_point(
                asset_point_id(library_id, asset_id), semantic, visual, payload
            )

        body = {
            "library_id": library_id,
            "asset_id": asset_id,
            "status": response_status,
            "client_revision": client_revision,
            "source_content_hash": source_hash,
            "indexed_file_hash": indexed_hash,
            "model_version": MODEL_VERSION,
            "collection_generation": COLLECTION_GENERATION,
            "vectors": {
                "semantic": len(semantic or []),
                "visual": len(visual or []),
            },
            "phash": phash,
            "indexed_at": indexed_at,
            "request_id": request_id(request),
        }
        return JSONResponse(status_code=201 if current is None else 200, content=body)

    @router.get(
        "/assets/{asset_id}",
        summary="查询单个资产索引状态",
        response_model=AssetStatusResponse,
    )
    def get_asset(
        request: Request,
        asset_id: str,
        library_id: str = Query(..., min_length=1, max_length=256),
    ) -> dict[str, Any]:
        record = service.get_record(library_id, asset_id)
        if not record:
            raise FrameFinderError(
                404,
                "ASSET_NOT_FOUND",
                "资产尚未建立远端索引",
                details={"library_id": library_id, "asset_id": asset_id},
            )
        payload = record.payload or {}
        state = "indexed" if payload.get("model_version") == MODEL_VERSION else "model_outdated"
        return {
            "library_id": library_id,
            "asset_id": asset_id,
            "status": state,
            "client_revision": payload.get("client_revision"),
            "source_content_hash": payload.get("source_content_hash"),
            "indexed_file_hash": payload.get("indexed_file_hash"),
            "model_version": payload.get("model_version"),
            "collection_generation": payload.get("collection_generation"),
            "indexed_at": payload.get("indexed_at"),
            "last_error": payload.get("last_error"),
            "request_id": request_id(request),
        }

    @router.post(
        "/assets/status:batch",
        summary="批量对账资产索引状态",
        response_model=BatchStatusResponse,
    )
    def batch_status(request: Request, body: BatchStatusRequest) -> dict[str, Any]:
        if len(body.assets) > MAX_BATCH_STATUS_ITEMS:
            raise FrameFinderError(
                413,
                "BATCH_TOO_LARGE",
                "批量状态查询超过服务端上限",
                details={"max_batch_status_items": MAX_BATCH_STATUS_ITEMS},
            )
        point_ids = [asset_point_id(body.library_id, item.asset_id) for item in body.assets]
        service.ensure_collection()
        try:
            records = service.client.retrieve(
                collection_name=collection,
                ids=point_ids,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as error:
            raise qdrant_error(error) from error
        found = {str(record.id): record for record in records}
        results = []
        for item, pid in zip(body.assets, point_ids):
            record = found.get(pid)
            last_error = None
            if not record:
                state = "missing"
                needs_upsert = True
                needs_metadata_patch = False
                stored_model = None
            else:
                payload = record.payload or {}
                stored_model = payload.get("model_version")
                last_error = payload.get("last_error")
                if last_error:
                    state = "failed"
                    needs_upsert = True
                    needs_metadata_patch = False
                elif payload.get("status") == "indexing":
                    state = "indexing"
                    needs_upsert = False
                    needs_metadata_patch = False
                elif stored_model != MODEL_VERSION:
                    state = "model_outdated"
                    needs_upsert = True
                    needs_metadata_patch = False
                elif payload.get("source_content_hash") != item.source_content_hash:
                    state = "stale_content"
                    needs_upsert = True
                    needs_metadata_patch = False
                elif item.client_revision > int(payload.get("client_revision", 0)):
                    state = "stale_metadata"
                    needs_upsert = False
                    needs_metadata_patch = True
                else:
                    state = "indexed"
                    needs_upsert = False
                    needs_metadata_patch = False
            results.append(
                {
                    "asset_id": item.asset_id,
                    "status": state,
                    "needs_upsert": needs_upsert,
                    "needs_metadata_patch": needs_metadata_patch,
                    "model_version": stored_model,
                    "last_error": last_error,
                }
            )
        return {
            "library_id": body.library_id,
            "count": len(results),
            "results": results,
            "request_id": request_id(request),
        }

    @router.delete(
        "/assets/{asset_id}",
        summary="幂等删除单个资产",
        response_model=DeleteResponse,
    )
    def delete_asset(
        request: Request,
        asset_id: str,
        library_id: str = Query(..., min_length=1, max_length=256),
        client_revision: int = Query(..., ge=0),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        del idempotency_key
        deleted, existed = service.delete_asset(library_id, asset_id, client_revision)
        return {
            "library_id": library_id,
            "asset_id": asset_id,
            "deleted": deleted,
            "existed": existed,
            "request_id": request_id(request),
        }

    @router.post(
        "/assets/delete:batch",
        summary="批量删除资产",
        response_model=BatchDeleteResponse,
    )
    def batch_delete(request: Request, body: BatchDeleteRequest) -> dict[str, Any]:
        if len(body.assets) > MAX_BATCH_STATUS_ITEMS:
            raise FrameFinderError(413, "BATCH_TOO_LARGE", "批量删除超过服务端上限")
        results = []
        for item in body.assets:
            try:
                deleted, existed = service.delete_asset(
                    body.library_id, item.asset_id, item.client_revision
                )
                results.append(
                    {
                        "asset_id": item.asset_id,
                        "deleted": deleted,
                        "existed": existed,
                        "error": None,
                    }
                )
            except FrameFinderError as error:
                results.append(
                    {
                        "asset_id": item.asset_id,
                        "deleted": False,
                        "existed": True,
                        "error": {
                            "code": error.code,
                            "message": error.message,
                            "retryable": error.retryable,
                        },
                    }
                )
        return {
            "library_id": body.library_id,
            "count": len(results),
            "results": results,
            "request_id": request_id(request),
        }

    @router.patch(
        "/assets/{asset_id}/metadata",
        summary="不重新编码地更新资产元数据",
        response_model=MetadataPatchResponse,
    )
    def patch_metadata(
        request: Request,
        asset_id: str,
        body: MetadataPatchRequest,
        library_id: str = Query(..., min_length=1, max_length=256),
    ) -> dict[str, Any]:
        new_metadata = body.metadata.model_dump(mode="json", exclude_unset=True)
        record = service.get_record(library_id, asset_id)
        if not record:
            raise FrameFinderError(
                404,
                "ASSET_NOT_FOUND",
                "资产尚未建立远端索引",
                details={"library_id": library_id, "asset_id": asset_id},
            )
        payload = record.payload or {}
        current_revision = int(payload.get("client_revision", 0))
        current_metadata = payload.get("metadata", {})
        if body.client_revision < current_revision:
            raise FrameFinderError(
                409,
                "STALE_CLIENT_REVISION",
                "client_revision 早于服务端当前版本",
                details={"current_revision": current_revision},
            )
        if body.client_revision == current_revision and new_metadata != current_metadata:
            raise FrameFinderError(
                409,
                "CLIENT_REVISION_CONFLICT",
                "相同 client_revision 对应的 metadata 不一致",
            )
        if new_metadata != current_metadata:
            service.set_metadata(record, new_metadata, body.client_revision)
        return {
            "library_id": library_id,
            "asset_id": asset_id,
            "status": "metadata_only",
            "client_revision": body.client_revision,
            "metadata_hash": metadata_hash(new_metadata),
            "request_id": request_id(request),
        }

    @router.post(
        "/search/text",
        summary="在指定资料库中进行全库文字搜索",
        response_model=SearchResponse,
    )
    def search_text(request: Request, body: TextSearchRequest) -> dict[str, Any]:
        started = time.perf_counter()
        hits = None
        if not body.cursor:
            vector = service.infer_text(body.query)
            points = service.query_vector(
                vector, "semantic", body.library_id, body.filters
            )
            hits = []
            for point in points:
                score = float(point.score)
                if body.min_score is not None and score < body.min_score:
                    continue
                payload = point.payload or {}
                hits.append(
                    {
                        "asset_id": payload.get("asset_id"),
                        "score": round(score, 6),
                        "semantic_score": round(score, 6),
                        "visual_score": None,
                        "phash_distance": None,
                        "indexed_source_hash": payload.get("source_content_hash"),
                        "_point_id": str(point.id),
                    }
                )
            hits.sort(key=lambda item: (-item["score"], item["_point_id"]))
            for item in hits:
                item.pop("_point_id", None)
        elapsed = round((time.perf_counter() - started) * 1000)
        return service.response_page(request, body, "text", hits, elapsed)

    @router.post(
        "/search/by-asset",
        summary="使用已入库资产进行以图搜图",
        response_model=SearchResponse,
    )
    def search_by_asset(request: Request, body: AssetSearchRequest) -> dict[str, Any]:
        started = time.perf_counter()
        hits = None
        if not body.cursor:
            reference = service.get_record(body.library_id, body.asset_id, vectors=True)
            if not reference:
                raise FrameFinderError(
                    404,
                    "ASSET_NOT_FOUND",
                    "参考资产不存在",
                    details={"library_id": body.library_id, "asset_id": body.asset_id},
                )
            payload = reference.payload or {}
            if payload.get("model_version") != MODEL_VERSION:
                raise FrameFinderError(
                    409,
                    "MODEL_VERSION_MISMATCH",
                    "参考资产使用的模型版本与当前服务不一致",
                )
            vectors = reference.vector if isinstance(reference.vector, dict) else {}
            semantic_vector = vectors.get("semantic")
            visual_vector = vectors.get("visual")
            if semantic_vector is None or visual_vector is None:
                raise FrameFinderError(
                    409, "ASSET_NOT_INDEXED", "参考资产尚未完成向量索引"
                )
            total_weight = body.semantic_weight + body.visual_weight
            semantic_weight = body.semantic_weight / total_weight
            visual_weight = body.visual_weight / total_weight
            semantic_points = (
                service.query_vector(
                    list(semantic_vector), "semantic", body.library_id, body.filters
                )
                if semantic_weight > 0
                else []
            )
            visual_points = (
                service.query_vector(
                    list(visual_vector), "visual", body.library_id, body.filters
                )
                if visual_weight > 0
                else []
            )
            combined: dict[str, dict[str, Any]] = {}
            for point in semantic_points:
                combined[str(point.id)] = {
                    "record": point,
                    "semantic_score": float(point.score),
                    "visual_score": None,
                }
            for point in visual_points:
                item = combined.setdefault(
                    str(point.id),
                    {"record": point, "semantic_score": None, "visual_score": None},
                )
                item["visual_score"] = float(point.score)
            reference_hash = imagehash.hex_to_hash(str(payload.get("phash")))
            hits = []
            self_id = asset_point_id(body.library_id, body.asset_id)
            for pid, item in combined.items():
                if body.exclude_self and pid == self_id:
                    continue
                semantic_score = item["semantic_score"]
                visual_score = item["visual_score"]
                score = (semantic_score or 0.0) * semantic_weight + (
                    visual_score or 0.0
                ) * visual_weight
                if body.min_score is not None and score < body.min_score:
                    continue
                point_payload = item["record"].payload or {}
                point_phash = point_payload.get("phash")
                distance = (
                    reference_hash - imagehash.hex_to_hash(str(point_phash))
                    if point_phash
                    else None
                )
                hits.append(
                    {
                        "asset_id": point_payload.get("asset_id"),
                        "score": round(score, 6),
                        "semantic_score": (
                            round(semantic_score, 6) if semantic_score is not None else None
                        ),
                        "visual_score": (
                            round(visual_score, 6) if visual_score is not None else None
                        ),
                        "phash_distance": distance,
                        "indexed_source_hash": point_payload.get("source_content_hash"),
                        "_point_id": pid,
                    }
                )
            hits.sort(key=lambda item: (-item["score"], item["_point_id"]))
            for item in hits:
                item.pop("_point_id", None)
        elapsed = round((time.perf_counter() - started) * 1000)
        return service.response_page(request, body, "image_asset", hits, elapsed)

    @router.get(
        "/libraries/{library_id}/assets/manifest",
        summary="分页列出资料库远端资产清单",
        response_model=ManifestResponse,
    )
    def asset_manifest(
        request: Request,
        library_id: str,
        limit: int = Query(default=1000, ge=1, le=5000),
        cursor: str | None = Query(default=None),
    ) -> dict[str, Any]:
        service.ensure_collection()
        offset: int | str | uuid.UUID | None = None
        if cursor:
            try:
                decoded = json.loads(
                    base64.urlsafe_b64decode(cursor.encode("ascii") + b"===").decode("utf-8")
                )
                if decoded.get("library_id") != library_id:
                    raise ValueError("library mismatch")
                offset = decoded.get("offset")
            except Exception as error:
                raise FrameFinderError(
                    409,
                    "MANIFEST_CURSOR_EXPIRED",
                    "远端清单游标无效",
                    retryable=True,
                ) from error
        try:
            records, next_offset = service.client.scroll(
                collection_name=collection,
                scroll_filter=service.search_filter(library_id, None),
                limit=limit,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as error:
            raise qdrant_error(error) from error
        next_cursor = None
        if next_offset is not None:
            token_data = canonical_json(
                {"library_id": library_id, "offset": str(next_offset)}
            ).encode("utf-8")
            next_cursor = base64.urlsafe_b64encode(token_data).decode("ascii").rstrip("=")
        assets = []
        for record in records:
            payload = record.payload or {}
            assets.append(
                {
                    "asset_id": payload.get("asset_id"),
                    "source_content_hash": payload.get("source_content_hash"),
                    "client_revision": payload.get("client_revision"),
                    "model_version": payload.get("model_version"),
                    "status": (
                        "indexed"
                        if payload.get("model_version") == MODEL_VERSION
                        else "model_outdated"
                    ),
                }
            )
        return {
            "library_id": library_id,
            "has_more": next_cursor is not None,
            "next_cursor": next_cursor,
            "assets": assets,
            "request_id": request_id(request),
        }

    app.include_router(router)
    install_openapi_response_headers(app)
    app.state.imagelib_service = service
    return service
