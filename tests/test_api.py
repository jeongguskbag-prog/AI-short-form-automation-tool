import asyncio

import httpx
import pytest

from app.main import create_app
from app.pipeline import Providers

from .conftest import FakeImages, FakePlanner, FakeTTS, requires_ffmpeg


async def _wait_done(client, job_id, timeout=60):
    async with asyncio.timeout(timeout):
        while True:
            body = (await client.get(f"/api/v1/shorts/status/{job_id}")).json()
            if body["status"] in ("COMPLETED", "FAILED"):
                return body
            await asyncio.sleep(0.1)


async def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@requires_ffmpeg
@pytest.mark.asyncio
async def test_end_to_end_with_image_fallback(settings):
    providers = Providers(planner=FakePlanner(3), tts=FakeTTS(), images=FakeImages(fail=True))
    app = create_app(settings, providers)
    async with app.router.lifespan_context(app), await _client(app) as client:
        r = await client.post("/api/v1/shorts/generate", json={"script": "테스트 대본입니다. 충분히 길어요."})
        assert r.status_code == 202
        job_id = r.json()["job_id"]
        assert r.json()["status_url"] == f"/api/v1/shorts/status/{job_id}"

        body = await _wait_done(client, job_id)
        assert body["status"] == "COMPLETED", body["error"]
        assert body["progress"] == 100
        assert body["scene_count"] == 3
        # 같은 이유의 이미지 실패는 경고 하나로 묶임
        assert len(body["warnings"]) == 1
        assert body["warnings"][0].startswith("씬 1, 2, 3: ")
        assert body["duration_sec"] > 3

        video_resp = await client.get(body["video_url"])
        assert video_resp.status_code == 200
        assert video_resp.content[4:8] == b"ftyp"


@requires_ffmpeg
@pytest.mark.asyncio
async def test_failure_is_reported(settings):
    class BrokenTTS:
        async def synthesize(self, text, output_path):
            raise RuntimeError("tts down")

    app = create_app(settings, Providers(FakePlanner(), BrokenTTS(), FakeImages()))
    async with app.router.lifespan_context(app), await _client(app) as client:
        job_id = (await client.post("/api/v1/shorts/generate", json={"script": "대본 대본 대본 대본"})).json()["job_id"]
        body = await _wait_done(client, job_id)
        assert body["status"] == "FAILED"
        assert body["error"] == "RuntimeError: tts down"


@requires_ffmpeg
@pytest.mark.asyncio
async def test_validation_and_404(settings, providers):
    app = create_app(settings, providers)
    async with app.router.lifespan_context(app), await _client(app) as client:
        assert (await client.post("/api/v1/shorts/generate", json={"script": "짧음"})).status_code == 422
        assert (await client.post("/api/v1/shorts/generate", json={"script": " " * 20})).status_code == 422
        assert (await client.get("/api/v1/shorts/status/nope")).status_code == 404
        assert (await client.get("/health")).json()["status"] == "ok"


@requires_ffmpeg
@pytest.mark.asyncio
async def test_web_index_served(settings, providers):
    app = create_app(settings, providers)
    async with app.router.lifespan_context(app), await _client(app) as client:
        r = await client.get("/")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert "/api/v1/shorts" in r.text


@requires_ffmpeg
@pytest.mark.asyncio
async def test_stock_photo_credit_is_reported(settings):
    from io import BytesIO

    from PIL import Image

    class StockPhotos:
        async def generate(self, prompt, output_path, query="", exclude=()):
            buf = BytesIO()
            Image.new("RGB", (800, 1200), (30, 120, 60)).save(buf, format="JPEG")
            output_path.write_bytes(buf.getvalue())  # Pexels처럼 .png 경로에 JPEG 저장
            return "Kim / Pexels"

    app = create_app(settings, Providers(FakePlanner(2), FakeTTS(), StockPhotos()))
    async with app.router.lifespan_context(app), await _client(app) as client:
        job_id = (await client.post("/api/v1/shorts/generate", json={"script": "사진 출처 테스트 대본"})).json()["job_id"]
        body = await _wait_done(client, job_id)
        assert body["status"] == "COMPLETED", body["error"]
        assert body["image_credits"] == ["Kim / Pexels"]  # 씬 2개가 같은 촬영자면 한 번만
        assert body["warnings"] == []
