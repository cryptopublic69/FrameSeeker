from __future__ import annotations

import hashlib
import io
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

os.environ.setdefault("HF_HOME", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".hf-cache"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import imagehash
import timm
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image, UnidentifiedImageError
from qdrant_client import QdrantClient, models as qmodels
from transformers import AutoModel, AutoProcessor, AutoTokenizer

from imagelib_api import HealthResponse, install_imagelib_api


SIGLIP_MODEL = "google/siglip2-giant-opt-patch16-384"
DINO_MODEL = "hf_hub:timm/vit_huge_plus_patch16_dinov3.lvd1689m"
COLLECTION = "framefinder_assets_v3"
SEMANTIC_VECTOR_SIZE = 1536
VISUAL_VECTOR_SIZE = 1280
BATCH_SIZE = 4
API_HOST = os.environ.get("FRAMEFINDER_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("FRAMEFINDER_PORT", "8000"))
LAN_ORIGIN_REGEX = (
    r"^https?://(localhost|127\.0\.0\.1|10(?:\.\d{1,3}){3}|"
    r"192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})"
    r"(?::\d+)?$"
)


def normalize(tensor: torch.Tensor) -> torch.Tensor:
    return torch.nn.functional.normalize(tensor.float(), dim=-1)


def move_inputs(inputs: dict[str, torch.Tensor], device: str, dtype: torch.dtype):
    return {
        key: value.to(device=device, dtype=dtype) if value.is_floating_point() else value.to(device)
        for key, value in inputs.items()
    }


class Models:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.loaded = False
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.dtype = (
            torch.bfloat16
            if self.device == "cuda" and torch.cuda.is_bf16_supported()
            else torch.float16 if self.device == "cuda" else torch.float32
        )

    def load(self) -> None:
        if self.loaded:
            return
        with self.lock:
            if self.loaded:
                return
            self.siglip_processor = AutoProcessor.from_pretrained(SIGLIP_MODEL)
            self.siglip_tokenizer = AutoTokenizer.from_pretrained(SIGLIP_MODEL)
            self.siglip = AutoModel.from_pretrained(SIGLIP_MODEL, dtype=self.dtype)
            self.siglip = self.siglip.to(self.device).eval()

            self.dino = timm.create_model(DINO_MODEL, pretrained=True, num_classes=0)
            self.dino = self.dino.to(device=self.device, dtype=self.dtype).eval()
            config = timm.data.resolve_model_data_config(self.dino)
            self.dino_transform = timm.data.create_transform(**config, is_training=False)
            self.loaded = True

    def semantic_images(self, images: list[Image.Image]) -> torch.Tensor:
        vectors = []
        for start in range(0, len(images), BATCH_SIZE):
            inputs = self.siglip_processor(
                images=images[start : start + BATCH_SIZE], return_tensors="pt"
            )
            inputs = move_inputs(dict(inputs), self.device, self.dtype)
            with torch.inference_mode():
                vectors.append(normalize(self.siglip.get_image_features(**inputs)).cpu())
        return torch.cat(vectors)

    def visual_images(self, images: list[Image.Image]) -> torch.Tensor:
        vectors = []
        for start in range(0, len(images), BATCH_SIZE):
            batch = torch.stack(
                [self.dino_transform(image) for image in images[start : start + BATCH_SIZE]]
            ).to(device=self.device, dtype=self.dtype)
            with torch.inference_mode():
                vectors.append(normalize(self.dino(batch)).cpu())
        return torch.cat(vectors)

    def semantic_text(self, text: str) -> torch.Tensor:
        inputs = self.siglip_tokenizer(
            [text], padding="max_length", max_length=64, truncation=True, return_tensors="pt"
        )
        inputs = move_inputs(dict(inputs), self.device, self.dtype)
        with torch.inference_mode():
            return normalize(self.siglip.get_text_features(**inputs)).cpu()


@dataclass
class CachedAsset:
    semantic: list[float]
    visual: list[float]
    phash: str


class VectorCache:
    def __init__(self) -> None:
        self.client = QdrantClient(url="http://127.0.0.1:6333", timeout=10)
        self.lock = threading.Lock()
        self.ready = False

    def ensure_collection(self) -> None:
        if self.ready:
            return
        with self.lock:
            if self.ready:
                return
            if not self.client.collection_exists(COLLECTION):
                self.client.create_collection(
                    collection_name=COLLECTION,
                    vectors_config={
                        "semantic": qmodels.VectorParams(
                            size=SEMANTIC_VECTOR_SIZE,
                            distance=qmodels.Distance.COSINE,
                            on_disk=True,
                        ),
                        "visual": qmodels.VectorParams(
                            size=VISUAL_VECTOR_SIZE,
                            distance=qmodels.Distance.COSINE,
                            on_disk=True,
                        ),
                    },
                    on_disk_payload=True,
                )
            info = self.client.get_collection(COLLECTION)
            payload_schema = info.payload_schema or {}
            for field_name, field_schema in {
                "library_id": qmodels.PayloadSchemaType.KEYWORD,
                "asset_id": qmodels.PayloadSchemaType.KEYWORD,
                "media_type": qmodels.PayloadSchemaType.KEYWORD,
                "storage_root_id": qmodels.PayloadSchemaType.KEYWORD,
                "virtual_folder_ids": qmodels.PayloadSchemaType.KEYWORD,
                "tag_ids": qmodels.PayloadSchemaType.KEYWORD,
                "source_video_id": qmodels.PayloadSchemaType.KEYWORD,
                "indexed_file_hash": qmodels.PayloadSchemaType.KEYWORD,
                "model_version": qmodels.PayloadSchemaType.KEYWORD,
            }.items():
                if field_name not in payload_schema:
                    self.client.create_payload_index(
                        collection_name=COLLECTION,
                        field_name=field_name,
                        field_schema=field_schema,
                        wait=True,
                    )
            self.ready = True

    def status(self) -> dict:
        try:
            self.ensure_collection()
            info = self.client.get_collection(COLLECTION)
            return {
                "online": True,
                "collection": COLLECTION,
                "stored_points": info.points_count or 0,
            }
        except Exception as error:
            self.ready = False
            return {
                "online": False,
                "collection": COLLECTION,
                "stored_points": 0,
                "error": error.__class__.__name__,
            }

    def get_many(self, point_ids: list[str]) -> dict[str, CachedAsset]:
        self.ensure_collection()
        unique_ids = list(dict.fromkeys(point_ids))
        records = self.client.retrieve(
            collection_name=COLLECTION,
            ids=unique_ids,
            with_payload=True,
            with_vectors=True,
        )
        found: dict[str, CachedAsset] = {}
        for record in records:
            vectors = record.vector if isinstance(record.vector, dict) else {}
            payload = record.payload or {}
            semantic = vectors.get("semantic")
            visual = vectors.get("visual")
            phash_value = payload.get("phash")
            if semantic is not None and visual is not None and phash_value:
                found[str(record.id)] = CachedAsset(semantic, visual, str(phash_value))
        return found

    def put_many(
        self,
        point_ids: list[str],
        content_hashes: list[str],
        filenames: list[str],
        images: list[Image.Image],
        semantics: torch.Tensor,
        visuals: torch.Tensor,
        phashes: list[str],
    ) -> None:
        points = []
        for index, point_id in enumerate(point_ids):
            points.append(
                qmodels.PointStruct(
                    id=point_id,
                    vector={
                        "semantic": semantics[index].tolist(),
                        "visual": visuals[index].tolist(),
                    },
                    payload={
                        "content_hash": content_hashes[index],
                        "filename": filenames[index],
                        "width": images[index].width,
                        "height": images[index].height,
                        "phash": phashes[index],
                        "semantic_model": SIGLIP_MODEL,
                        "visual_model": DINO_MODEL,
                    },
                )
            )
        if points:
            self.client.upsert(collection_name=COLLECTION, points=points, wait=True)


models = Models()
cache = VectorCache()


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        cache.ensure_collection()
    except Exception:
        # The health endpoint must remain available while Qdrant is recovering.
        cache.ready = False
    yield


app = FastAPI(
    title="FrameFinder local inference",
    version="1.0.0",
    description="FrameFinder 本地向量检索服务；/api/v1 为 ImageLib 正式契约。",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "http://127.0.0.1:3417",
        "http://localhost:3417",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ],
    allow_origin_regex=LAN_ORIGIN_REGEX,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)

install_imagelib_api(
    app,
    cache,
    models,
    collection=COLLECTION,
    semantic_model=SIGLIP_MODEL,
    visual_model=DINO_MODEL,
    semantic_vector_size=SEMANTIC_VECTOR_SIZE,
    visual_vector_size=VISUAL_VECTOR_SIZE,
)


@app.get("/api/health", response_model=HealthResponse, summary="轻量健康检查")
def health():
    gpu = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU"
    database = cache.status()
    return {
        "ok": True,
        "loaded": models.loaded,
        "models_loaded": models.loaded,
        "accepting_requests": bool(database.get("online")),
        "status": "ready" if models.loaded else "warming",
        "device": gpu,
        "semantic_model": "SigLIP 2 Giant 1B Patch16 384",
        "visual_model": "DINOv3 ViT-H+/16",
        "database": database,
    }


def decode_image(data: bytes, filename: str) -> Image.Image:
    try:
        return Image.open(io.BytesIO(data)).convert("RGB")
    except (UnidentifiedImageError, OSError) as error:
        raise HTTPException(400, f"无法读取图片：{filename}") from error


def point_id(content_hash: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"framefinder:{content_hash}"))


@app.post("/api/search")
async def search(
    files: list[UploadFile] = File(...),
    mode: str = Form("text"),
    text: str = Form(""),
    query_index: int = Form(0),
    semantic_weight: float = Form(0.58),
    visual_weight: float = Form(0.42),
):
    if not files or len(files) > 200:
        raise HTTPException(400, "请选择 1～200 张图片")
    if mode not in {"text", "image"}:
        raise HTTPException(400, "不支持的检索模式")
    if mode == "text" and not text.strip():
        raise HTTPException(400, "请输入文字描述")

    raw = [await file.read() for file in files]
    images = [
        decode_image(data, file.filename or f"image-{index}")
        for index, (data, file) in enumerate(zip(raw, files))
    ]
    filenames = [file.filename or f"image-{index}" for index, file in enumerate(files)]
    content_hashes = [hashlib.sha256(data).hexdigest() for data in raw]
    point_ids = [point_id(value) for value in content_hashes]
    query_index = min(max(query_index, 0), len(images) - 1)
    started = time.perf_counter()

    try:
        cached = cache.get_many(point_ids)
    except Exception as error:
        raise HTTPException(503, f"向量数据库暂不可用：{error}") from error

    missing_unique_indices: list[int] = []
    seen_missing: set[str] = set()
    for index, asset_id in enumerate(point_ids):
        if asset_id not in cached and asset_id not in seen_missing:
            missing_unique_indices.append(index)
            seen_missing.add(asset_id)

    models.load()
    if missing_unique_indices:
        missing_images = [images[index] for index in missing_unique_indices]
        missing_semantics = models.semantic_images(missing_images)
        missing_visuals = models.visual_images(missing_images)
        missing_phashes = [str(imagehash.phash(image)) for image in missing_images]
        cache.put_many(
            [point_ids[index] for index in missing_unique_indices],
            [content_hashes[index] for index in missing_unique_indices],
            [filenames[index] for index in missing_unique_indices],
            missing_images,
            missing_semantics,
            missing_visuals,
            missing_phashes,
        )
        for offset, index in enumerate(missing_unique_indices):
            cached[point_ids[index]] = CachedAsset(
                missing_semantics[offset].tolist(),
                missing_visuals[offset].tolist(),
                missing_phashes[offset],
            )

    semantic_vectors = torch.tensor(
        [cached[asset_id].semantic for asset_id in point_ids], dtype=torch.float32
    )
    visual_vectors = torch.tensor(
        [cached[asset_id].visual for asset_id in point_ids], dtype=torch.float32
    )
    hashes = [imagehash.hex_to_hash(cached[asset_id].phash) for asset_id in point_ids]
    was_cached = [asset_id not in seen_missing for asset_id in point_ids]

    visual_scores: torch.Tensor | None = None
    phash_distances: list[int] | None = None
    if mode == "text":
        semantic_scores = (models.semantic_text(text.strip()) @ semantic_vectors.T)[0]
        final_scores = semantic_scores
    else:
        semantic_scores = semantic_vectors[query_index] @ semantic_vectors.T
        visual_scores = visual_vectors[query_index] @ visual_vectors.T
        total = max(semantic_weight + visual_weight, 0.0001)
        final_scores = (
            semantic_scores * (semantic_weight / total)
            + visual_scores * (visual_weight / total)
        )
        phash_distances = [hashes[query_index] - value for value in hashes]

    order = torch.argsort(final_scores, descending=True).tolist()
    results = []
    for rank, index in enumerate(order, start=1):
        image = images[index]
        results.append(
            {
                "index": index,
                "rank": rank,
                "filename": filenames[index],
                "score": round(float(final_scores[index]), 6),
                "semantic_score": round(float(semantic_scores[index]), 6),
                "visual_score": (
                    round(float(visual_scores[index]), 6) if visual_scores is not None else None
                ),
                "phash_distance": (
                    phash_distances[index] if phash_distances is not None else None
                ),
                "width": image.width,
                "height": image.height,
                "cache_hit": was_cached[index],
            }
        )

    return {
        "mode": mode,
        "count": len(results),
        "query_index": query_index if mode == "image" else None,
        "inference_ms": round((time.perf_counter() - started) * 1000),
        "cache_hits": sum(was_cached),
        "cache_misses": len(missing_unique_indices),
        "stored_points": cache.status()["stored_points"],
        "results": results,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=API_HOST, port=API_PORT)
