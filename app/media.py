"""사용자가 올린 영상·사진 (직접 찍은 제품 영상 등).

파일은 `media_dir/<32자 hex>.<확장자>`로 저장하고, 그 파일 이름이 곧 media_id입니다.
id 형식을 정규식으로 검사해 경로 조작을 막습니다.
"""

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .services import video

IMAGE_EXTS = {"jpg", "jpeg", "png", "webp"}
VIDEO_EXTS = {"mp4", "mov", "m4v", "webm"}
MEDIA_ID_RE = re.compile(r"^[0-9a-f]{32}\.(?:" + "|".join(sorted(IMAGE_EXTS | VIDEO_EXTS)) + r")$")


class MediaError(ValueError):
    pass


class MediaTooLarge(MediaError):
    pass


@dataclass(frozen=True)
class MediaItem:
    media_id: str
    path: Path
    kind: str  # "image" | "video"
    duration: float | None = None  # 영상 길이(초)

    def to_dict(self) -> dict:
        return {"media_id": self.media_id, "kind": self.kind, "duration": self.duration}


def kind_of(ext: str) -> str | None:
    return "image" if ext in IMAGE_EXTS else "video" if ext in VIDEO_EXTS else None


async def _probe_video(path: Path, timeout: float) -> float:
    out = await video.run_command(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_type:format=duration", "-of", "json", str(path)],
        timeout=timeout,
    )
    data = json.loads(out)
    if not data.get("streams"):
        raise MediaError("영상 트랙이 없는 파일입니다.")
    try:
        duration = float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        raise MediaError("영상 길이를 읽을 수 없습니다.") from None
    if duration < 0.3:
        raise MediaError("영상이 너무 짧습니다 (0.3초 이상).")
    return duration


def _check_image(path: Path) -> None:
    try:
        with Image.open(path) as im:
            im.verify()
    except Exception:
        raise MediaError("이미지를 열 수 없습니다.") from None


class MediaStore:
    def __init__(self, directory: Path, max_bytes: int, probe_timeout: float = 60) -> None:
        self.directory = directory
        self.max_bytes = max_bytes
        self._probe_timeout = probe_timeout
        directory.mkdir(parents=True, exist_ok=True)

    async def save(self, filename: str, chunks: AsyncIterator[bytes]) -> MediaItem:
        ext = Path(filename).suffix.lower().lstrip(".")
        kind = kind_of(ext)
        if kind is None:
            allowed = ", ".join(sorted(IMAGE_EXTS | VIDEO_EXTS))
            raise MediaError(f"지원하지 않는 파일 형식입니다 ({allowed}).")
        media_id = f"{uuid.uuid4().hex}.{ext}"
        path = self.directory / media_id
        size = 0
        try:
            with path.open("wb") as f:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise MediaTooLarge(f"파일이 너무 큽니다 (최대 {self.max_bytes // 1024 // 1024}MB).")
                    f.write(chunk)
            if size == 0:
                raise MediaError("빈 파일입니다.")
            if kind == "image":
                await asyncio.to_thread(_check_image, path)
                return MediaItem(media_id, path, kind)
            try:
                duration = await _probe_video(path, self._probe_timeout)
            except video.FFmpegError:
                raise MediaError("영상을 읽을 수 없습니다.") from None
            return MediaItem(media_id, path, kind, round(duration, 2))
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    async def get(self, media_id: str) -> MediaItem | None:
        if not MEDIA_ID_RE.match(media_id):
            return None
        path = self.directory / media_id
        if not path.is_file():
            return None
        kind = kind_of(media_id.rsplit(".", 1)[1])
        assert kind is not None
        if kind == "image":
            return MediaItem(media_id, path, kind)
        try:
            duration = await _probe_video(path, self._probe_timeout)
        except (MediaError, video.FFmpegError):
            return None
        return MediaItem(media_id, path, kind, round(duration, 2))

    def remove_older_than(self, seconds: float) -> int:
        cutoff = time.time() - seconds
        removed = 0
        for path in self.directory.iterdir():
            if path.is_file() and MEDIA_ID_RE.match(path.name) and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        return removed
