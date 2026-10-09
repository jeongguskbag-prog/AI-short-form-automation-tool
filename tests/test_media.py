import asyncio
import os
import time
from io import BytesIO

import httpx
import pytest
from PIL import Image

from app.main import create_app
from app.media import MediaError, MediaStore, MediaTooLarge
from app.pipeline import Providers
from app.services import video

from .conftest import FakeImages, FakePlanner, FakeTTS, requires_ffmpeg


async def _chunks(data: bytes, size: int = 1000):
    for i in range(0, len(data), size):
        yield data[i:i + size]


def _png() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (400, 300), (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


async def _mp4(tmp_path, seconds=1.0) -> bytes:
    path = tmp_path / "clip.mp4"
    await video.run_command(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size=640x360:rate=24:duration={seconds}",
         "-pix_fmt", "yuv420p", str(path)],
        timeout=30,
    )
    return path.read_bytes()


@pytest.mark.asyncio
async def test_store_validates_type_size_and_content(tmp_path):
    store = MediaStore(tmp_path / "m", max_bytes=50_000)
    item = await store.save("상품.PNG", _chunks(_png()))
    assert item.kind == "image" and item.media_id.endswith(".png")
    assert (await store.get(item.media_id)).path == item.path

    with pytest.raises(MediaError, match="지원하지 않는"):
        await store.save("a.exe", _chunks(b"x"))
    with pytest.raises(MediaTooLarge):
        await store.save("big.jpg", _chunks(b"x" * 60_000))
    with pytest.raises(MediaError, match="이미지를 열 수 없"):
        await store.save("fake.jpg", _chunks(b"not an image"))
    with pytest.raises(MediaError, match="빈 파일"):
        await store.save("empty.png", _chunks(b""))
    assert sorted(p.name for p in store.directory.iterdir()) == [item.media_id]  # 실패한 파일은 남지 않음

    for bad in ("../secret.png", item.media_id.replace(".png", ".exe"), "x" * 32 + ".png"):
        assert await store.get(bad) is None

    old = time.time() - 7200
    os.utime(item.path, (old, old))
    assert store.remove_older_than(3600) == 1
    assert await store.get(item.media_id) is None


@requires_ffmpeg
@pytest.mark.asyncio
async def test_store_probes_video(tmp_path):
    store = MediaStore(tmp_path / "m", max_bytes=10_000_000)
    item = await store.save("clip.mov", _chunks(await _mp4(tmp_path)))
    assert item.kind == "video" and 0.9 < item.duration < 1.2
    with pytest.raises(MediaError, match="영상을 읽을 수 없"):
        await store.save("broken.mp4", _chunks(b"\x00" * 2000))


async def _wait_done(client, job_id, timeout=60):
    async with asyncio.timeout(timeout):
        while True:
            body = (await client.get(f"/api/v1/shorts/status/{job_id}")).json()
            if body["status"] in ("COMPLETED", "FAILED"):
                return body
            await asyncio.sleep(0.1)


@requires_ffmpeg
@pytest.mark.asyncio
async def test_job_uses_uploaded_media_in_order(settings, tmp_path):
    # 이미지 생성이 실패하는 공급자: 사용자 파일을 쓰면 호출되지 않아 경고가 없어야 함
    app = create_app(settings, Providers(FakePlanner(3), FakeTTS(), FakeImages(fail=True)))
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.put("/api/v1/media", params={"filename": "clip.mp4"}, content=await _mp4(tmp_path, 1.0))
        assert r.status_code == 201, r.text
        clip = r.json()
        assert clip["kind"] == "video"
        photo = (await client.put("/api/v1/media", params={"filename": "p.png"}, content=_png())).json()

        assert (await client.put("/api/v1/media", params={"filename": "x.txt"}, content=b"hi")).status_code == 422
        r = await client.post(
            "/api/v1/shorts/generate", json={"script": "대본 대본 대본 대본", "media_ids": ["nope.png"]}
        )
        assert r.status_code == 422

        # 씬 3개, 파일 2개 → 영상, 사진, 영상(반복: 1초짜리라 처음부터)
        r = await client.post("/api/v1/shorts/generate", json={
            "script": "제품 소개 대본입니다. 충분히 길어요.", "media_ids": [clip["media_id"], photo["media_id"]],
        })
        body = await _wait_done(client, r.json()["job_id"])
        assert body["status"] == "COMPLETED", body["error"]
        assert body["warnings"] == []
        assert body["scene_count"] == 3
        assert (await client.get(body["video_url"])).status_code == 200


@requires_ffmpeg
@pytest.mark.asyncio
async def test_upload_size_limit(settings):
    settings.max_upload_mb = 1
    app = create_app(settings, Providers(FakePlanner(), FakeTTS(), FakeImages()))
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.put("/api/v1/media", params={"filename": "big.mp4"}, content=b"x" * (1024 * 1024 + 1))
        assert r.status_code == 413
