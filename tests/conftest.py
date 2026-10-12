import shutil
from pathlib import Path

import pytest
from PIL import Image

from app.config import Settings
from app.pipeline import Providers
from app.schemas import ScenePlan, ShortsPlan
from app.services import video

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg 필요"
)


class FakePlanner:
    def __init__(self, scenes: int = 2) -> None:
        self.scenes = scenes

    async def plan(self, script: str, tone: str) -> ShortsPlan:
        return ShortsPlan(
            title="테스트 쇼츠",
            scenes=[
                ScenePlan(
                    scene_id=1,  # 일부러 중복 id — 파이프라인이 인덱스로 파일명을 정해야 함
                    narration=f"나레이션 {i}",
                    visual_prompt=f"scene {i}",
                    subtitle_text=f"자막 '{i}': 특수문자 \\ 테스트입니다 아주 긴 자막이 줄바꿈되는지 확인",
                )
                for i in range(self.scenes)
            ],
        )


class FakeTTS:
    async def synthesize(self, text: str, output_path: Path) -> None:
        await video.run_command(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=1.0", "-ar", "24000", str(output_path)],
            timeout=30,
        )


class FakeImages:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def generate(self, prompt: str, output_path: Path, query: str = "") -> str | None:
        if self.fail:
            raise RuntimeError("imagen down")
        Image.new("RGB", (1024, 1792), (200, 40, 40)).save(output_path)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        output_dir=tmp_path / "output",
        trends_db_path=tmp_path / "trends.db",
        media_dir=tmp_path / "uploads",
        projects_db_path=tmp_path / "projects.db",
        library_dir=tmp_path / "library",
        accounts_path=tmp_path / "accounts.json",
        products_path=tmp_path / "products.json",
        video_width=360, video_height=640, video_fps=24,  # 테스트 속도를 위해 작게
        max_retries=1,
        _env_file=None,
    )


@pytest.fixture
def spec(settings: Settings) -> video.VideoSpec:
    return video.VideoSpec(
        width=settings.video_width, height=settings.video_height, fps=settings.video_fps,
        max_zoom=settings.max_zoom, padding_sec=settings.scene_padding_sec,
        font_path=video.find_korean_font(None), font_size=30,
        timeout_sec=settings.ffmpeg_timeout_sec,
    )


@pytest.fixture
def providers() -> Providers:
    return Providers(planner=FakePlanner(), tts=FakeTTS(), images=FakeImages())
