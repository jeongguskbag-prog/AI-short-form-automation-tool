"""사용자가 올린 영상·사진·배경음악 (직접 찍은 제품 영상 등).

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
AUDIO_EXTS = {"mp3", "m4a", "aac", "wav", "ogg"}
ALL_EXTS = IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS
MEDIA_ID_RE = re.compile(r"^[0-9a-f]{32}\.(?:" + "|".join(sorted(ALL_EXTS)) + r")$")


class MediaError(ValueError):
    pass


class MediaTooLarge(MediaError):
    pass


@dataclass(frozen=True)
class MediaItem:
    media_id: str
    path: Path
    kind: str  # "image" | "video" | "audio"
    duration: float | None = None  # 영상·음악 길이(초)

    def to_dict(self) -> dict:
        return {"media_id": self.media_id, "kind": self.kind, "duration": self.duration}


def kind_of(ext: str) -> str | None:
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return "audio" if ext in AUDIO_EXTS else None


async def _probe_video(path: Path, timeout: float, kind: str = "video") -> float:
    """영상(또는 kind="audio"면 음악) 트랙이 있는지 확인하고 길이를 돌려줍니다."""
    label = "영상" if kind == "video" else "음악"
    out = await video.run_command(
        ["ffprobe", "-v", "error", "-select_streams", "v:0" if kind == "video" else "a:0",
         "-show_entries", "stream=codec_type:format=duration", "-of", "json", str(path)],
        timeout=timeout,
    )
    data = json.loads(out)
    if not data.get("streams"):
        raise MediaError(f"{label} 트랙이 없는 파일입니다.")
    try:
        duration = float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        raise MediaError(f"{label} 길이를 읽을 수 없습니다.") from None
    if duration < 0.3:
        raise MediaError(f"{label}이 너무 짧습니다 (0.3초 이상).")
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
            allowed = ", ".join(sorted(ALL_EXTS))
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
                duration = await _probe_video(path, self._probe_timeout, kind)
            except video.FFmpegError:
                what = "영상을" if kind == "video" else "음악 파일을"
                raise MediaError(f"{what} 읽을 수 없습니다.") from None
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
            duration = await _probe_video(path, self._probe_timeout, kind)
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


# --------------------------------------------------------------------------- 인터넷 주소에서 가져오기
_CONTENT_TYPES = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/webp": "webp",
    "video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm", "video/x-m4v": "m4v",
}


def _ext_from(url: str, content_type: str) -> str | None:
    ext = _CONTENT_TYPES.get(content_type.split(";")[0].strip().lower())
    if ext:
        return ext
    path = url.split("?", 1)[0].lower()
    for candidate in sorted(IMAGE_EXTS | VIDEO_EXTS, key=len, reverse=True):
        if path.endswith("." + candidate):
            return candidate
    return None


def _assert_public_host(host: str) -> None:
    """내부망 주소(공유기·사내 서버 등)를 대신 열어 주는 데 쓰이지 않게, 공인 IP로만 연결합니다."""
    import ipaddress
    import socket

    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise MediaError(f"주소를 찾을 수 없습니다: {host}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise MediaError("인터넷 공개 주소의 파일만 가져올 수 있습니다.")


async def import_from_url(store: MediaStore, url: str, max_bytes: int,
                          transport=None, check_host=_assert_public_host) -> MediaItem:
    """상품 공식 사진·영상처럼 공개 주소에 있는 파일을 내려받아 올린 파일로 저장합니다 (https만)."""
    import httpx

    if not url.startswith("https://"):
        raise MediaError("https 주소만 가져올 수 있습니다.")

    async def guard(request: httpx.Request) -> None:
        await asyncio.to_thread(check_host, request.url.host)
        if request.url.scheme != "https":
            raise MediaError("https 주소만 가져올 수 있습니다.")

    limit = min(max_bytes, store.max_bytes)
    async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(60), follow_redirects=True,
                                 event_hooks={"request": [guard]},
                                 headers={"User-Agent": "Mozilla/5.0 (AI-Shorts)"}) as client:
        try:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise MediaError(f"파일을 받지 못했습니다 (HTTP {response.status_code}).")
                length = response.headers.get("content-length")
                if length and length.isdigit() and int(length) > limit:
                    raise MediaTooLarge(f"파일이 너무 큽니다 (최대 {limit // 1024 // 1024}MB).")
                ext = _ext_from(str(response.url), response.headers.get("content-type", ""))
                if ext is None:
                    raise MediaError("사진(jpg·png·webp)이나 영상(mp4·mov·webm)이 아닙니다.")
                return await store.save(f"import.{ext}", _limited(response.aiter_bytes(), limit))
        except httpx.HTTPError as exc:
            raise MediaError(f"파일을 받지 못했습니다 ({type(exc).__name__}).") from None


async def _limited(chunks: AsyncIterator[bytes], limit: int) -> AsyncIterator[bytes]:
    size = 0
    async for chunk in chunks:
        size += len(chunk)
        if size > limit:
            raise MediaTooLarge(f"파일이 너무 큽니다 (최대 {limit // 1024 // 1024}MB).")
        yield chunk
