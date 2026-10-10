import asyncio
from io import BytesIO

import httpx
import pytest
from PIL import Image

from app.main import create_app
from app.pipeline import Providers

from .conftest import FakeImages, FakePlanner, FakeTTS, requires_ffmpeg


class CountingPlanner(FakePlanner):
    calls = 0

    async def plan(self, script, tone):
        CountingPlanner.calls += 1
        return await super().plan(script, tone)


class CountingTTS(FakeTTS):
    texts: list[str] = []

    async def synthesize(self, text, output_path):
        CountingTTS.texts.append(text)
        return await super().synthesize(text, output_path)


async def _wait(client, job_id):
    async with asyncio.timeout(60):
        while (body := (await client.get(f"/api/v1/shorts/status/{job_id}")).json())["status"] not in (
                "COMPLETED", "FAILED"):
            await asyncio.sleep(0.05)
    assert body["status"] == "COMPLETED", body["error"]
    return body


@requires_ffmpeg
@pytest.mark.asyncio
async def test_revise_rebuilds_only_changed_scenes(settings):
    CountingPlanner.calls = 0
    CountingTTS.texts = []
    app = create_app(settings, Providers(CountingPlanner(3), CountingTTS(), FakeImages()))
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post("/api/v1/shorts/generate", json={"script": "장면 고치기 테스트 대본입니다."})
        src = r.json()["job_id"]
        await _wait(client, src)
        assert CountingPlanner.calls == 1 and len(CountingTTS.texts) == 3

        scenes = (await client.get(f"/api/v1/shorts/{src}/scenes")).json()
        assert [s["narration"] for s in scenes["scenes"]] == ["나레이션 0", "나레이션 1", "나레이션 2"]
        preview = await client.get(scenes["scenes"][0]["preview_url"])
        assert preview.status_code == 200 and preview.content[:2] == b"\xff\xd8"

        edits = [{"narration": s["narration"]} for s in scenes["scenes"]]
        assert (await client.post(f"/api/v1/shorts/{src}/revise", json={"scenes": edits})).status_code == 422
        assert (await client.post(f"/api/v1/shorts/{src}/revise", json={"scenes": edits[:2]})).status_code == 422
        assert (await client.post("/api/v1/shorts/nope/revise", json={"scenes": edits})).status_code == 404

        edits[1]["narration"] = "고친 두 번째 문장입니다"
        r = await client.post(f"/api/v1/shorts/{src}/revise", json={"scenes": edits})
        assert r.status_code == 202 and "1개" in r.json()["message"]
        rev = r.json()["job_id"]
        body = await _wait(client, rev)
        assert CountingPlanner.calls == 1  # AI 기획을 다시 하지 않음
        assert CountingTTS.texts[3:] == ["고친 두 번째 문장입니다"]  # 바뀐 장면만 음성 생성
        assert body["project_id"] and len(body["thumbnail_urls"]) == 3

        src_dir, rev_dir = settings.output_dir / src, settings.output_dir / rev
        for n in ("01", "03"):  # 안 바뀐 장면은 그대로 복사
            assert (rev_dir / f"scene_{n}.mp4").read_bytes() == (src_dir / f"scene_{n}.mp4").read_bytes()
        assert (rev_dir / "scene_02.mp4").read_bytes() != (src_dir / "scene_02.mp4").read_bytes()

        # 고친 영상을 또 고칠 수 있음 (배경을 올린 사진으로 바꾸기)
        buf = BytesIO()
        Image.new("RGB", (400, 700), (0, 200, 0)).save(buf, format="PNG")
        photo = (await client.put("/api/v1/media", params={"filename": "p.png"}, content=buf.getvalue())).json()
        scenes2 = (await client.get(f"/api/v1/shorts/{rev}/scenes")).json()["scenes"]
        assert scenes2[1]["narration"] == "고친 두 번째 문장입니다"
        edits2 = [{"narration": s["narration"]} for s in scenes2]
        edits2[2]["media_id"] = photo["media_id"]
        rev2 = (await client.post(f"/api/v1/shorts/{rev}/revise", json={"scenes": edits2})).json()["job_id"]
        await _wait(client, rev2)
        assert len(CountingTTS.texts) == 5  # 배경만 바꿔도 그 장면은 다시 렌더링 (음성 포함)
        scenes3 = (await client.get(f"/api/v1/shorts/{rev2}/scenes")).json()["scenes"]
        assert scenes3[2]["media_id"] == photo["media_id"]

        # 원본 작업 폴더가 지워져도 고친 작업은 다시 고칠 수 있음 (배경을 복사해 둠)
        import shutil
        shutil.rmtree(src_dir)
        edits3 = [{"narration": s["narration"]} for s in scenes3]
        edits3[0]["narration"] = "첫 문장도 고침"
        rev3 = (await client.post(f"/api/v1/shorts/{rev2}/revise", json={"scenes": edits3})).json()["job_id"]
        await _wait(client, rev3)
