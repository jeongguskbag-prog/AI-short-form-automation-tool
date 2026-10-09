import json

import pytest
from PIL import Image

from app.jobs import JobStore
from app.media import MediaItem
from app.pipeline import Providers, ShortsPipeline
from app.schemas import ScenePlan
from app.services import video
from app.services.matcher import GeminiMediaMatcher, MediaAssignment, SceneMedia, make_preview, normalize_assignment

from .conftest import FakeImages, FakePlanner, FakeTTS, requires_ffmpeg


def _a(*pairs):
    return MediaAssignment(assignments=[SceneMedia(scene=s, media=m) for s, m in pairs])


def test_normalize_keeps_valid_and_fills_gaps():
    assert normalize_assignment(_a((1, 2), (2, 1), (3, 2)), 3, 2) == [1, 0, 1]
    # 장면 2 누락, 범위 밖 번호, 중복 장면 → 안 쓴 소재(3번째) 먼저 채움
    assert normalize_assignment(_a((1, 1), (3, 9), (1, 2), (3, 2)), 3, 3) == [0, 2, 1]
    # 전부 엉망이면 직전과 다른 소재로 돌려 씀
    assert normalize_assignment(_a(), 4, 2) == [0, 1, 0, 1]
    assert normalize_assignment(_a(), 2, 1) == [0, 0]


def _photo(tmp_path, name, color):
    path = tmp_path / name
    Image.new("RGB", (900, 1600), color).save(path)
    return MediaItem(name, path, "image")


@requires_ffmpeg
@pytest.mark.asyncio
async def test_preview_for_photo_and_video(tmp_path):
    photo = _photo(tmp_path, "p.png", (200, 0, 0))
    data = await make_preview(photo, tmp_path, 30)
    assert data[:2] == b"\xff\xd8"  # JPEG
    clip = tmp_path / "c.mp4"
    await video.run_command(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=24:duration=1",
         "-pix_fmt", "yuv420p", str(clip)], timeout=30)
    data = await make_preview(MediaItem("c.mp4", clip, "video", 1.0), tmp_path, 30)
    assert data[:2] == b"\xff\xd8"
    assert not list(tmp_path.glob("preview_*"))  # 임시 프레임 정리


@pytest.mark.asyncio
async def test_gemini_matcher_sends_scenes_and_images(tmp_path, settings):
    seen = {}

    class Resp:
        parsed = None
        text = json.dumps({"assignments": [{"scene": 1, "media": 2}, {"scene": 2, "media": 1}]})

    class Client:
        def __init__(self):
            self.aio = self
            self.models = self

        async def generate_content(self, model, contents, config):
            seen["contents"] = contents
            return Resp()

    media = [_photo(tmp_path, "a.png", (255, 0, 0)), _photo(tmp_path, "b.png", (0, 0, 255))]
    scenes = [
        ScenePlan(scene_id=i + 1, narration=f"나레이션 {i}", visual_prompt="", subtitle_text="") for i in range(2)
    ]
    result = await GeminiMediaMatcher(Client(), settings).match(scenes, media, tmp_path)
    assert result == [1, 0]
    assert "장면 2: 나레이션 1" in seen["contents"][0]
    assert sum(1 for c in seen["contents"] if not isinstance(c, str)) == 2  # 이미지 두 장


class FakeMatcher:
    def __init__(self, result=None, fail=False):
        self.result, self.fail, self.calls = result, fail, 0

    async def match(self, scenes, media, work_dir):
        self.calls += 1
        if self.fail:
            raise RuntimeError("vision down")
        return self.result


@requires_ffmpeg
@pytest.mark.asyncio
async def test_pipeline_uses_ai_assignment_or_falls_back(tmp_path, settings, spec):
    media = [_photo(tmp_path, "a.png", (255, 0, 0)), _photo(tmp_path, "b.png", (0, 0, 255))]

    async def run(matcher, order="auto"):
        pipeline = ShortsPipeline(Providers(FakePlanner(3), FakeTTS(), FakeImages(), matcher=matcher), settings, spec)
        job = JobStore().create("대본입니다 충분히 길게", "calm", media)
        job.media_order = order
        await pipeline.run(job)
        assert job.status == "COMPLETED", job.error
        return job

    matcher = FakeMatcher([1, 1, 0])
    job = await run(matcher)
    assert job.scene_media == [1, 1, 0] and job.warnings == []

    job = await run(FakeMatcher([5, 0, 0]))  # 잘못된 번호 → 올린 순서
    assert job.scene_media == [0, 1, 0]

    job = await run(FakeMatcher(fail=True))
    assert job.scene_media == [0, 1, 0] and "올린 순서" in job.warnings[0]

    off = FakeMatcher([1, 1, 1])
    job = await run(off, order="upload")
    assert off.calls == 0 and job.scene_media == [0, 1, 0]
