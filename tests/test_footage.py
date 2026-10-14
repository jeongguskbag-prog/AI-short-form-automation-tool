import httpx
import pytest

from app.config import Settings
from app.jobs import JobStore
from app.pipeline import Providers, ShortsPipeline
from app.services import video
from app.services.footage import PexelsVideoSource, PixabayVideoSource, _pick

from .conftest import FakeImages, FakePlanner, FakeTTS, requires_ffmpeg

SETTINGS = Settings(max_retries=1, max_stock_video_mb=1, _env_file=None)


def test_pick_prefers_portrait_close_to_screen_height():
    files = [
        ("land-hd", 1920, 1080),
        ("port-sd", 540, 960),
        ("port-hd", 720, 1280),
        ("port-4k", 2160, 3840),  # 너무 큼
    ]
    assert _pick(files, 1280) == "port-hd"
    assert _pick([("land", 1280, 720), ("land-small", 640, 360)], 1280) == "land"
    assert _pick([("huge", 2160, 3840)], 1280) == "huge"  # 그것밖에 없으면 씀
    assert _pick([], 1280) is None


def _transport(seen, big=False):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.pexels.com":
            assert request.url.path == "/videos/search"
            return httpx.Response(200, json={"videos": [
                {"id": 1, "user": {"name": "Kim"}, "video_files": [
                    {"link": "https://v.pexels.com/1-hd.mp4", "width": 720, "height": 1280, "file_type": "video/mp4"},
                    {"link": "https://v.pexels.com/1-4k.mp4", "width": 2160, "height": 3840, "file_type": "video/mp4"},
                ]},
            ]})
        if request.url.host == "pixabay.com":
            assert request.url.path == "/api/videos/"
            return httpx.Response(200, json={"hits": [
                {"id": 9, "user": "Lee", "videos": {"medium": {"url": "https://cdn.pixabay.com/9.mp4",
                                                               "width": 1280, "height": 720}}},
            ]})
        return httpx.Response(200, content=b"\0" * (2 * 1024 * 1024 if big else 1000))

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_pexels_video_downloads_best_file(tmp_path):
    seen = []
    source = PexelsVideoSource("key", SETTINGS, transport=_transport(seen))
    out = tmp_path / "c.mp4"
    assert await source.generate("x", out, query="kitchen sink") == "Kim / Pexels"
    assert out.stat().st_size == 1000
    assert str(seen[-1].url) == "https://v.pexels.com/1-hd.mp4"
    assert seen[0].url.params["orientation"] == "portrait"


@pytest.mark.asyncio
async def test_pixabay_video_and_size_limit(tmp_path):
    seen = []
    source = PixabayVideoSource("key", SETTINGS, transport=_transport(seen))
    assert await source.generate("x", tmp_path / "a.mp4", query="dog toy") == "Lee / Pixabay"

    big = PixabayVideoSource("key", SETTINGS, transport=_transport([], big=True))
    with pytest.raises(Exception, match="찾지 못함"):  # 한도(1MB)를 넘는 영상은 건너뜀
        await big.generate("x", tmp_path / "b.mp4", query="dog toy")
    assert not (tmp_path / "b.mp4").exists()


class FakeFootage:
    def __init__(self, clip: bytes | None):
        self.clip = clip
        self.calls = 0

    async def generate(self, prompt, output_path, query="", exclude=()):
        self.calls += 1
        if self.clip is None:
            raise RuntimeError("no clip")
        output_path.write_bytes(self.clip)
        return "Kim / Pexels"


@requires_ffmpeg
@pytest.mark.asyncio
async def test_pipeline_prefers_stock_video_and_falls_back(tmp_path, settings, spec):
    clip = tmp_path / "src.mp4"
    await video.run_command(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=24:duration=0.5",
         "-pix_fmt", "yuv420p", str(clip)],
        timeout=30,
    )
    footage = FakeFootage(clip.read_bytes())
    pipeline = ShortsPipeline(Providers(FakePlanner(2), FakeTTS(), FakeImages(fail=True), footage=footage),
                              settings, spec)
    job = JobStore().create("대본입니다 충분히 길게", "calm")
    await pipeline.run(job)
    assert job.status == "COMPLETED", job.error
    assert footage.calls == 2 and job.image_credits == ["Kim / Pexels"]
    assert job.warnings == []  # 사진 생성(실패하는 공급자)을 부르지 않음

    # 깨진 영상 → 사진 체인으로 (여기선 사진도 실패해 기본 배경 + 경고)
    broken = FakeFootage(b"not a video")
    pipeline = ShortsPipeline(Providers(FakePlanner(1), FakeTTS(), FakeImages(fail=True), footage=broken),
                              settings, spec)
    job = JobStore().create("대본입니다 충분히 길게", "calm")
    await pipeline.run(job)
    assert job.status == "COMPLETED", job.error
    assert len(job.warnings) == 1 and "기본 배경" in job.warnings[0]
    assert not list((settings.output_dir / job.job_id).glob("clip_*.mp4"))
