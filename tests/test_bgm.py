import asyncio
import re

import httpx
import pytest

from app.main import create_app
from app.media import MediaStore
from app.pipeline import Providers
from app.services import video

from .conftest import FakeImages, FakePlanner, FakeTTS, requires_ffmpeg


async def _sine(path, freq, seconds, volume=1.0):
    await video.run_command(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
         "-af", f"volume={volume}", str(path)],
        timeout=30,
    )


async def _video_with_voice(path, seconds=3.0):
    # 앞 1.5초만 "내레이션"(440Hz), 뒤는 무음
    await video.run_command(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c=black:s=320x568:d={seconds}:r=24",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration=1.5,apad=whole_dur={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        timeout=60,
    )


async def _mean_volume(path, start=None, dur=None) -> float:
    args = ["ffmpeg", "-hide_banner"]
    if start is not None:
        args += ["-ss", str(start), "-t", str(dur)]
    args += ["-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"]
    proc = await asyncio.create_subprocess_exec(*args, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    return float(re.search(r"mean_volume: (-?[\d.]+) dB", err.decode()).group(1))


@requires_ffmpeg
@pytest.mark.asyncio
async def test_mix_adds_music_ducks_under_voice_and_keeps_length(tmp_path):
    src, music = tmp_path / "v.mp4", tmp_path / "m.mp3"
    await _video_with_voice(src)
    await _sine(music, 220, 1.0, volume=0.8)  # 영상보다 짧아도 반복됨

    ducked, plain = tmp_path / "d.mp4", tmp_path / "p.mp4"
    await video.mix_background_music(src, music, ducked, volume=0.5, duck=True, timeout=60)
    await video.mix_background_music(src, music, plain, volume=0.5, duck=False, timeout=60)

    assert await video.probe_duration(ducked) == pytest.approx(await video.probe_duration(src), abs=0.1)
    # 무음 구간(1.8~2.4초)에도 음악이 들림 — 반복 재생 확인
    assert await _mean_volume(ducked, 1.8, 0.6) > -40
    # 내레이션 구간(0.3~1.2초)은 덕킹 쪽이 더 조용함 (음악이 줄어듦)
    assert await _mean_volume(ducked, 0.3, 0.9) < await _mean_volume(plain, 0.3, 0.9) - 0.5


@requires_ffmpeg
@pytest.mark.asyncio
async def test_store_accepts_audio(tmp_path):
    music = tmp_path / "m.mp3"
    await _sine(music, 220, 1.0)

    async def chunks():
        yield music.read_bytes()

    store = MediaStore(tmp_path / "m", max_bytes=10_000_000)
    item = await store.save("노래.MP3", chunks())
    assert item.kind == "audio" and 0.9 < item.duration < 1.2
    assert (await store.get(item.media_id)).kind == "audio"


@requires_ffmpeg
@pytest.mark.asyncio
async def test_job_with_background_music(settings, tmp_path):
    music = tmp_path / "m.mp3"
    await _sine(music, 220, 0.7)
    app = create_app(settings, Providers(FakePlanner(2), FakeTTS(), FakeImages()))
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        bgm = (await client.put("/api/v1/media", params={"filename": "bgm.mp3"}, content=music.read_bytes())).json()
        assert bgm["kind"] == "audio"

        # 음악은 장면 배경으로 못 씀 / 사진은 배경음악으로 못 씀
        script = "대본 대본 대본 대본"
        r = await client.post("/api/v1/shorts/generate", json={"script": script, "media_ids": [bgm["media_id"]]})
        assert r.status_code == 422
        r = await client.post("/api/v1/shorts/generate", json={"script": script, "bgm_media_id": "nope.mp3"})
        assert r.status_code == 422

        r = await client.post("/api/v1/shorts/generate", json={
            "script": "배경음악 테스트 대본입니다.", "bgm_media_id": bgm["media_id"], "bgm_volume": 0.3,
        })
        job_id = r.json()["job_id"]
        async with asyncio.timeout(60):
            while (body := (await client.get(f"/api/v1/shorts/status/{job_id}")).json())["status"] not in (
                "COMPLETED", "FAILED"):
                await asyncio.sleep(0.1)
        assert body["status"] == "COMPLETED", body["error"]
        assert body["warnings"] == []
        assert (await client.get(body["video_url"])).status_code == 200
        assert not list((settings.output_dir / job_id).glob("bgm_*"))  # 임시 파일 정리
