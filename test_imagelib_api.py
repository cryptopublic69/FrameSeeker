from __future__ import annotations

import io
import os
import unittest

import torch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from qdrant_client import QdrantClient, models as qmodels

from imagelib_api import HealthResponse, asset_point_id, install_imagelib_api


class FakeCache:
    def __init__(self) -> None:
        self.client = QdrantClient(":memory:")
        self.ready = False

    def ensure_collection(self) -> None:
        if self.ready:
            return
        self.client.create_collection(
            collection_name="test_assets",
            vectors_config={
                "semantic": qmodels.VectorParams(size=4, distance=qmodels.Distance.COSINE),
                "visual": qmodels.VectorParams(size=3, distance=qmodels.Distance.COSINE),
            },
        )
        self.ready = True

    def status(self) -> dict:
        try:
            self.ensure_collection()
            info = self.client.get_collection("test_assets")
            return {"online": True, "stored_points": info.points_count or 0}
        except Exception as error:  # pragma: no cover - defensive parity with production cache
            return {"online": False, "error": error.__class__.__name__}


class FakeModels:
    loaded = False

    def load(self) -> None:
        self.loaded = True

    def semantic_images(self, images: list[Image.Image]) -> torch.Tensor:
        values = []
        for image in images:
            r, g, b = image.resize((1, 1)).getpixel((0, 0))
            vector = torch.tensor([r, g, b, 1.0], dtype=torch.float32)
            values.append(torch.nn.functional.normalize(vector, dim=0))
        return torch.stack(values)

    def visual_images(self, images: list[Image.Image]) -> torch.Tensor:
        values = []
        for image in images:
            r, g, b = image.resize((1, 1)).getpixel((0, 0))
            vector = torch.tensor([r + 1.0, g + 1.0, b + 1.0], dtype=torch.float32)
            values.append(torch.nn.functional.normalize(vector, dim=0))
        return torch.stack(values)

    def semantic_text(self, text: str) -> torch.Tensor:
        if "blue" in text.lower():
            vector = torch.tensor([0.0, 0.0, 1.0, 0.0])
        else:
            vector = torch.tensor([1.0, 0.0, 0.0, 0.0])
        return torch.nn.functional.normalize(vector, dim=0).unsqueeze(0)


def png_bytes(color: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(output, format="PNG")
    return output.getvalue()


class ImageLibApiTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["FRAMEFINDER_API_KEY"] = "test-key"
        app = FastAPI()
        self.cache = FakeCache()
        self.models = FakeModels()

        @app.get("/api/health", response_model=HealthResponse)
        def health():
            return {
                "ok": True,
                "loaded": self.models.loaded,
                "models_loaded": self.models.loaded,
                "accepting_requests": True,
                "status": "ready" if self.models.loaded else "warming",
                "device": "CPU",
                "semantic_model": "test-semantic",
                "visual_model": "test-visual",
                "database": {
                    "online": True,
                    "collection": "test_assets",
                    "stored_points": 0,
                },
            }

        install_imagelib_api(
            app,
            self.cache,
            self.models,
            collection="test_assets",
            semantic_model="test-semantic",
            visual_model="test-visual",
            semantic_vector_size=4,
            visual_vector_size=3,
        )
        self.client = TestClient(app)
        self.headers = {"X-FrameFinder-Key": "test-key"}

    def upsert(
        self,
        library_id: str,
        asset_id: str,
        color: str,
        revision: int = 1,
        metadata: str = "{}",
    ):
        data = png_bytes(color)
        return self.client.post(
            "/api/v1/assets/upsert",
            headers=self.headers,
            data={
                "library_id": library_id,
                "asset_id": asset_id,
                "source_content_hash": "sha256:" + color.encode().hex().ljust(64, "0")[:64],
                "client_revision": str(revision),
                "media_type": "image",
                "metadata": metadata,
            },
            files={"file": (f"{color}.png", data, "image/png")},
        )

    def test_auth_and_capabilities(self) -> None:
        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200, health.text)
        self.assertEqual(health.json()["status"], "warming")
        self.assertIn("X-Request-Id", health.headers)

        unauthorized = self.client.get("/api/v1/capabilities")
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(unauthorized.json()["error"]["code"], "INVALID_API_KEY")
        self.assertIn("X-Request-Id", unauthorized.headers)

        response = self.client.get("/api/v1/capabilities", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["api_version"], "1.0.0")
        self.assertTrue(body["features"]["text_search"])
        self.assertFalse(body["features"]["async_indexing"])

    def test_openapi_contract_is_client_generation_ready(self) -> None:
        schema = self.client.app.openapi()
        security_scheme = schema["components"]["securitySchemes"]["FrameFinderApiKey"]
        self.assertEqual(security_scheme["type"], "apiKey")
        self.assertEqual(security_scheme["in"], "header")
        self.assertEqual(security_scheme["name"], "X-FrameFinder-Key")

        operation = schema["paths"]["/api/v1/search/text"]["post"]
        self.assertEqual(operation["security"], [{"FrameFinderApiKey": []}])
        self.assertFalse(
            any(
                parameter.get("name") == "X-FrameFinder-Key"
                for parameter in operation.get("parameters", [])
            )
        )
        self.assertEqual(
            schema["paths"]["/api/health"]["get"]["responses"]["200"]["content"]
            ["application/json"]["schema"]["$ref"],
            "#/components/schemas/HealthResponse",
        )
        metadata_schema = schema["components"]["schemas"]["AssetMetadata"]
        self.assertFalse(metadata_schema["additionalProperties"])
        self.assertIn("tag_ids", metadata_schema["properties"])
        batch_result = schema["components"]["schemas"]["BatchStatusResult"]
        self.assertIn("last_error", batch_result["properties"])
        search_response = schema["components"]["schemas"]["SearchResponse"]
        self.assertIn("total_count", search_response["properties"])
        self.assertIn("results_truncated", search_response["properties"])
        self.assertIn("X-Request-Id", operation["responses"]["200"]["headers"])
        self.assertIn("Retry-After", operation["responses"]["429"]["headers"])

    def test_upsert_revision_status_and_library_isolation(self) -> None:
        created = self.upsert("library-a", "same-id", "red")
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["status"], "created")

        reused = self.upsert("library-a", "same-id", "red")
        self.assertEqual(reused.status_code, 200, reused.text)
        self.assertEqual(reused.json()["status"], "reused")

        other_library = self.upsert("library-b", "same-id", "red")
        self.assertEqual(other_library.status_code, 201, other_library.text)
        self.assertIn(other_library.json()["status"], {"created", "reused"})

        metadata_only = self.upsert(
            "library-a", "same-id", "red", 2, '{"tag_ids":["selected"]}'
        )
        self.assertEqual(metadata_only.status_code, 200, metadata_only.text)
        self.assertEqual(metadata_only.json()["status"], "metadata_only")

        stale = self.upsert("library-a", "same-id", "red", 1)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["error"]["code"], "STALE_CLIENT_REVISION")

        status = self.client.post(
            "/api/v1/assets/status:batch",
            headers=self.headers,
            json={
                "library_id": "library-a",
                "assets": [
                    {
                        "asset_id": "same-id",
                        "source_content_hash": "sha256:" + "red".encode().hex().ljust(64, "0")[:64],
                        "client_revision": 3,
                    },
                    {
                        "asset_id": "missing",
                        "source_content_hash": "sha256:" + "0" * 64,
                        "client_revision": 1,
                    },
                ],
            },
        )
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["results"][0]["status"], "stale_metadata")
        self.assertEqual(status.json()["results"][1]["status"], "missing")

        self.cache.client.set_payload(
            collection_name="test_assets",
            points=[asset_point_id("library-a", "same-id")],
            payload={
                "last_error": {
                    "code": "INFERENCE_TIMEOUT",
                    "message": "inference timed out",
                    "retryable": True,
                    "details": {},
                }
            },
            wait=True,
        )
        failed = self.client.post(
            "/api/v1/assets/status:batch",
            headers=self.headers,
            json={
                "library_id": "library-a",
                "assets": [
                    {
                        "asset_id": "same-id",
                        "source_content_hash": "sha256:" + "red".encode().hex().ljust(64, "0")[:64],
                        "client_revision": 2,
                    }
                ],
            },
        )
        self.assertEqual(failed.status_code, 200, failed.text)
        self.assertEqual(failed.json()["results"][0]["status"], "failed")
        self.assertEqual(
            failed.json()["results"][0]["last_error"]["code"], "INFERENCE_TIMEOUT"
        )

    def test_search_pagination_filters_and_by_asset(self) -> None:
        self.upsert("library-a", "red", "red")
        self.upsert("library-a", "blue", "blue")
        self.upsert("library-a", "green", "green")
        self.upsert("library-b", "private-red", "red")

        first = self.client.post(
            "/api/v1/search/text",
            headers=self.headers,
            json={"library_id": "library-a", "query": "red object", "limit": 1},
        )
        self.assertEqual(first.status_code, 200, first.text)
        body = first.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["total_count"], 3)
        self.assertFalse(body["results_truncated"])
        self.assertTrue(body["has_more"])
        self.assertNotEqual(body["hits"][0]["asset_id"], "private-red")

        second = self.client.post(
            "/api/v1/search/text",
            headers=self.headers,
            json={
                "library_id": "library-a",
                "query": "red object",
                "limit": 1,
                "cursor": body["next_cursor"],
            },
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["total_count"], 3)
        self.assertNotEqual(second.json()["hits"][0]["asset_id"], body["hits"][0]["asset_id"])

        mismatch = self.client.post(
            "/api/v1/search/text",
            headers=self.headers,
            json={
                "library_id": "library-a",
                "query": "blue object",
                "limit": 1,
                "cursor": body["next_cursor"],
            },
        )
        self.assertEqual(mismatch.status_code, 409)
        self.assertEqual(mismatch.json()["error"]["code"], "SEARCH_CURSOR_EXPIRED")

        by_asset = self.client.post(
            "/api/v1/search/by-asset",
            headers=self.headers,
            json={"library_id": "library-a", "asset_id": "red", "limit": 10},
        )
        self.assertEqual(by_asset.status_code, 200, by_asset.text)
        ids = [item["asset_id"] for item in by_asset.json()["hits"]]
        self.assertNotIn("red", ids)
        self.assertNotIn("private-red", ids)

    def test_metadata_manifest_and_idempotent_delete(self) -> None:
        self.upsert("library-a", "asset-1", "blue")
        patch = self.client.patch(
            "/api/v1/assets/asset-1/metadata?library_id=library-a",
            headers=self.headers,
            json={"client_revision": 2, "metadata": {"tag_ids": ["blue"]}},
        )
        self.assertEqual(patch.status_code, 200, patch.text)

        unsupported = self.client.patch(
            "/api/v1/assets/asset-1/metadata?library_id=library-a",
            headers=self.headers,
            json={"client_revision": 3, "metadata": {"unknown_field": "value"}},
        )
        self.assertEqual(unsupported.status_code, 422, unsupported.text)
        self.assertEqual(
            unsupported.json()["error"]["code"], "UNSUPPORTED_METADATA_FIELD"
        )

        manifest = self.client.get(
            "/api/v1/libraries/library-a/assets/manifest",
            headers=self.headers,
        )
        self.assertEqual(manifest.status_code, 200, manifest.text)
        self.assertEqual(manifest.json()["assets"][0]["asset_id"], "asset-1")

        deleted = self.client.delete(
            "/api/v1/assets/asset-1?library_id=library-a&client_revision=3",
            headers=self.headers,
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertTrue(deleted.json()["existed"])

        repeated = self.client.delete(
            "/api/v1/assets/asset-1?library_id=library-a&client_revision=3",
            headers=self.headers,
        )
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertFalse(repeated.json()["existed"])


if __name__ == "__main__":
    unittest.main()
