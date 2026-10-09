"""장면 ↔ 소재 AI 배치: 사용자가 올린 영상·사진 중 장면 내레이션에 가장 어울리는 것을 Gemini가 고릅니다.

소재마다 미리보기 이미지(사진은 줄인 사본, 영상은 중간 프레임)를 만들어 장면 목록과 함께 보내고,
돌아온 배치는 그대로 믿지 않고 normalize_assignment로 검사·보정합니다. 실패하면 호출하는 쪽이
올린 순서대로 배치합니다.
"""

import asyncio
from io import BytesIO
from pathlib import Path

from google import genai
from google.genai import types
from PIL import Image
from pydantic import BaseModel

from ..config import Settings
from ..media import MediaItem
from ..schemas import ScenePlan
from . import video
from .llm import generate_structured

PREVIEW_SIZE = 512

SYSTEM_INSTRUCTION = """\
당신은 쇼핑 쇼츠 편집자입니다. 장면마다 내레이션이 있고, 사용자가 직접 찍은 소재(사진·영상 미리보기)가 있습니다.
각 장면의 내레이션에 가장 잘 어울리는 소재 번호를 고르세요.

규칙:
- 모든 장면에 소재를 하나씩 정합니다 (scene은 1부터, media도 1부터).
- 소재가 장면 수보다 적지 않으면 모든 소재를 최소 한 번씩 쓰세요.
- 같은 소재를 연달아 쓰는 것은 피하세요 (소재가 하나뿐이면 예외).
- 첫 장면(후킹)에는 제품이 가장 잘 보이거나 눈길을 끄는 소재를 쓰세요.
- 마지막 장면이 광고 표시 문장이면 아무 소재나 괜찮습니다.
"""


class SceneMedia(BaseModel):
    scene: int
    media: int


class MediaAssignment(BaseModel):
    """LLM 응답 스키마. Gemini의 response_schema로도 쓰이므로 단순하게 유지합니다."""

    assignments: list[SceneMedia]


def round_robin(scene_count: int, media_count: int) -> list[int]:
    return [i % media_count for i in range(scene_count)]


def normalize_assignment(raw: MediaAssignment, scene_count: int, media_count: int) -> list[int]:
    """LLM 배치를 0부터 시작하는 소재 번호 목록(장면 순서)으로 바꿉니다.

    범위를 벗어나거나 빠진 장면은 아직 안 쓴 소재 → 직전 장면과 다른 소재 순서로 채웁니다.
    """
    chosen: dict[int, int] = {}
    for a in raw.assignments:
        scene, media = a.scene - 1, a.media - 1
        if 0 <= scene < scene_count and 0 <= media < media_count and scene not in chosen:
            chosen[scene] = media
    result: list[int] = []
    for i in range(scene_count):
        if i in chosen:
            result.append(chosen[i])
            continue
        used = set(result) | set(chosen.values())
        unused = [m for m in range(media_count) if m not in used]
        prev = result[-1] if result else None
        candidates = unused or [m for m in range(media_count) if m != prev] or [0]
        result.append(candidates[0])
    return result


def _image_preview(path: Path) -> bytes:
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((PREVIEW_SIZE, PREVIEW_SIZE))
        buf = BytesIO()
        im.save(buf, format="JPEG", quality=80)
        return buf.getvalue()


async def make_preview(item: MediaItem, work_dir: Path, timeout: float) -> bytes:
    if item.kind == "image":
        return await asyncio.to_thread(_image_preview, item.path)
    frame = work_dir / f"preview_{item.media_id}.jpg"
    at = (item.duration or 0) * 0.4
    await video.run_command(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{at:.3f}", "-i", str(item.path),
         "-frames:v", "1", "-vf", f"scale={PREVIEW_SIZE}:-2", "-q:v", "4", str(frame)],
        timeout=timeout,
    )
    try:
        return frame.read_bytes()
    finally:
        frame.unlink(missing_ok=True)


class GeminiMediaMatcher:
    def __init__(self, client: genai.Client, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def match(self, scenes: list[ScenePlan], media: list[MediaItem], work_dir: Path) -> list[int]:
        previews = await asyncio.gather(
            *(make_preview(m, work_dir, self._settings.ffmpeg_timeout_sec) for m in media)
        )
        scene_lines = "\n".join(f"장면 {i + 1}: {s.narration}" for i, s in enumerate(scenes))
        contents: list = [f"[장면 {len(scenes)}개]\n{scene_lines}\n\n[소재 {len(media)}개 — 아래 이미지 순서대로]"]
        for i, (item, data) in enumerate(zip(media, previews, strict=True), start=1):
            label = "영상 미리보기" if item.kind == "video" else "사진"
            contents += [f"소재 {i} ({label})", types.Part.from_bytes(data=data, mime_type="image/jpeg")]
        raw = await generate_structured(
            self._client,
            self._settings,
            prompt=contents,
            system_instruction=SYSTEM_INSTRUCTION,
            schema=MediaAssignment,
            what="소재 배치",
            temperature=0.2,
        )
        return normalize_assignment(raw, len(scenes), len(media))
