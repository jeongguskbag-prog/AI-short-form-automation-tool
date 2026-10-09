"""FFmpeg 기반 영상 합성.

자막은 FFmpeg drawtext 대신 Pillow로 투명 PNG를 만들어 overlay 합니다.
- 따옴표·콜론·백슬래시 등 특수문자 이스케이프 문제가 원천적으로 사라지고
- 한글 폰트, 여러 줄 가운데 정렬, 외곽선, 둥근 배경 박스를 정확히 제어할 수 있습니다.
"""

import asyncio
import json
import logging
import math
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# 자주 쓰이는 한글 지원 폰트 경로 (Debian/Ubuntu, macOS, Windows)
FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "C:/Windows/Fonts/malgunbd.ttf",
    "C:/Windows/Fonts/malgun.ttf",
]

# 안드로이드(Termux)는 fontconfig가 없으므로 시스템 폰트 폴더에서 한글 지원 폰트를 찾습니다.
ANDROID_FONT_DIR = Path("/system/fonts")
ANDROID_FONT_PATTERNS = ["NotoSansCJK*.ttc", "NotoSansKR*.[ot]tf", "*CJK*.tt[cf]", "*KR*.[ot]tf"]


def _find_android_font(font_dir: Path = ANDROID_FONT_DIR) -> Path | None:
    if not font_dir.is_dir():
        return None
    for pattern in ANDROID_FONT_PATTERNS:
        matches = sorted(font_dir.glob(pattern))
        if matches:
            return matches[0]
    return None


class FFmpegError(RuntimeError):
    pass


@dataclass(frozen=True)
class VideoSpec:
    width: int
    height: int
    fps: int
    max_zoom: float
    padding_sec: float
    font_path: Path
    font_size: int
    timeout_sec: float

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height


def ensure_ffmpeg() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    if missing:
        raise RuntimeError(f"{', '.join(missing)}을(를) 찾을 수 없습니다. FFmpeg를 설치하세요.")


@lru_cache
def find_korean_font(configured: Path | None = None) -> Path:
    if configured is not None:
        if not configured.is_file():
            raise FileNotFoundError(f"FONT_PATH에 지정한 폰트가 없습니다: {configured}")
        return configured
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return Path(candidate)
    android_font = _find_android_font()
    if android_font is not None:
        return android_font
    if shutil.which("fc-match"):
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", ":lang=ko"], capture_output=True, text=True, check=False
        )
        if result.returncode == 0 and Path(result.stdout.strip()).is_file():
            return Path(result.stdout.strip())
    raise FileNotFoundError(
        "한글 폰트를 찾지 못했습니다. fonts-noto-cjk 등을 설치하거나 FONT_PATH를 지정하세요."
    )


def _run_blocking(args: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise TimeoutError(f"{Path(args[0]).name}이(가) {timeout:.0f}초 안에 끝나지 않았습니다.") from None
    return proc.returncode, stdout, stderr


async def _run_async(args: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, stdout, stderr


async def run_command(args: list[str], *, timeout: float) -> str:
    """외부 프로세스를 이벤트 루프를 막지 않고 실행합니다."""
    try:
        returncode, stdout, stderr = await _run_async(args, timeout)
    except NotImplementedError:
        # Windows의 SelectorEventLoop(예: uvicorn --reload)는 비동기 subprocess를 지원하지 않으므로
        # 별도 스레드에서 실행합니다.
        returncode, stdout, stderr = await asyncio.to_thread(_run_blocking, args, timeout)
    if returncode != 0:
        tail = stderr.decode(errors="replace").strip().splitlines()[-15:]
        raise FFmpegError(f"{Path(args[0]).name} 실패 (exit {returncode}):\n" + "\n".join(tail))
    return stdout.decode(errors="replace")


async def probe_duration(path: Path, *, timeout: float = 30) -> float:
    out = await run_command(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        timeout=timeout,
    )
    try:
        duration = float(json.loads(out)["format"]["duration"])
    except (KeyError, ValueError, TypeError) as exc:
        raise FFmpegError(f"길이를 읽을 수 없습니다: {path}") from exc
    if duration <= 0:
        raise FFmpegError(f"길이가 0인 미디어입니다: {path}")
    return duration


# ---------------------------------------------------------------- 자막 렌더링


def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: float) -> list[str]:
    """단어 단위로 줄바꿈하고, 한 단어가 너무 길면 글자 단위로 자릅니다."""
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}".strip()
            if font.getlength(candidate) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
            # 단어 자체가 한 줄보다 긴 경우 글자 단위로 분할
            current = ""
            for char in word:
                if font.getlength(current + char) > max_width and current:
                    lines.append(current)
                    current = char
                else:
                    current += char
        if current:
            lines.append(current)
    return lines


def _render_subtitle_png(text: str, spec: VideoSpec, output_path: Path, max_lines: int = 3) -> None:
    canvas = Image.new("RGBA", spec.size, (0, 0, 0, 0))
    text = " ".join(text.split())
    if text:
        max_text_width = spec.width * 0.84
        font_size = spec.font_size
        while True:
            font = ImageFont.truetype(str(spec.font_path), font_size)
            lines = wrap_text(text, font, max_text_width)
            if len(lines) <= max_lines or font_size <= 28:
                break
            font_size -= 4
        lines = lines[:max_lines]

        draw = ImageDraw.Draw(canvas)
        stroke = max(2, font_size // 14)
        ascent, descent = font.getmetrics()
        line_height = ascent + descent
        spacing = int(font_size * 0.25)
        block_h = line_height * len(lines) + spacing * (len(lines) - 1)
        block_w = max(font.getlength(line) for line in lines)

        pad_x, pad_y = int(font_size * 0.6), int(font_size * 0.4)
        bottom = int(spec.height * 0.80)  # 하단 UI(제목·버튼)에 가리지 않는 높이
        top = bottom - block_h
        box = (
            (spec.width - block_w) / 2 - pad_x,
            top - pad_y,
            (spec.width + block_w) / 2 + pad_x,
            bottom + pad_y,
        )
        draw.rounded_rectangle(box, radius=int(font_size * 0.4), fill=(0, 0, 0, 150))

        y = top
        for line in lines:
            x = (spec.width - font.getlength(line)) / 2
            draw.text(
                (x, y), line, font=font, fill=(255, 255, 255, 255),
                stroke_width=stroke, stroke_fill=(0, 0, 0, 255),
            )
            y += line_height + spacing
    canvas.save(output_path, format="PNG")


async def render_subtitle_png(text: str, spec: VideoSpec, output_path: Path) -> None:
    await asyncio.to_thread(_render_subtitle_png, text, spec, output_path)


# ---------------------------------------------------------------- 씬 / 최종 렌더링


def _ken_burns_filter(spec: VideoSpec, frames: int, zoom_in: bool) -> str:
    # 원본을 2배 해상도로 키운 뒤 zoompan 하면 정수 좌표 반올림으로 생기는 떨림이 줄어듭니다.
    w2, h2 = spec.width * 2, spec.height * 2
    delta = spec.max_zoom - 1
    progress = f"on/{max(1, frames - 1)}"
    zoom = f"1+{delta:.4f}*{progress}" if zoom_in else f"{spec.max_zoom:.4f}-{delta:.4f}*{progress}"
    return (
        f"scale={w2}:{h2}:force_original_aspect_ratio=increase,crop={w2}:{h2},"
        f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d={frames}:s={spec.width}x{spec.height}:fps={spec.fps},setsar=1"
    )


def _video_background_filter(spec: VideoSpec) -> str:
    # 가로·세로 어떤 영상이든 화면을 꽉 채우도록 키운 뒤 가운데를 9:16으로 자릅니다.
    return (
        f"scale={spec.width}:{spec.height}:force_original_aspect_ratio=increase,"
        f"crop={spec.width}:{spec.height},setsar=1,fps={spec.fps}"
    )


async def render_scene(
    *,
    image_path: Path,
    audio_path: Path,
    subtitle_png: Path,
    output_path: Path,
    spec: VideoSpec,
    zoom_in: bool = True,
    video_start: float | None = None,
) -> float:
    """배경 + 음성 + 자막으로 씬 영상을 만들고, 씬 길이(초)를 반환합니다.

    video_start가 None이면 image_path를 사진으로 보고 켄 번스 효과를 줍니다. 값이 있으면 영상으로 보고
    그 시점부터 재생하며, 씬보다 짧으면 반복합니다. 원본 영상의 소리는 쓰지 않습니다.
    """
    audio_duration = await probe_duration(audio_path)
    frames = math.ceil((audio_duration + spec.padding_sec) * spec.fps)
    duration = frames / spec.fps

    if video_start is None:
        background_input = ["-i", str(image_path)]
        background = _ken_burns_filter(spec, frames, zoom_in)
    else:
        background_input = ["-stream_loop", "-1", "-ss", f"{video_start:.3f}", "-i", str(image_path)]
        background = _video_background_filter(spec)

    filter_complex = (
        f"[0:v]{background}[bg];"
        f"[bg][1:v]overlay=0:0:format=auto,format=yuv420p[v];"
        f"[2:a]aresample=44100,apad[a]"
    )
    await run_command(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            *background_input,
            "-i", str(subtitle_png),
            "-i", str(audio_path),
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "[a]",
            "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-r", str(spec.fps),
            "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "44100",
            str(output_path),
        ],
        timeout=spec.timeout_sec,
    )
    return duration


async def concat_scenes(scene_paths: list[Path], output_path: Path, *, timeout: float) -> float:
    """동일 인코딩 설정의 씬들을 재인코딩 없이 이어 붙이고 전체 길이를 반환합니다."""
    if not scene_paths:
        raise ValueError("이어 붙일 씬이 없습니다.")
    list_file = output_path.with_suffix(".txt")
    lines = []
    for path in scene_paths:
        escaped = str(path.resolve()).replace("'", r"'\''")
        lines.append(f"file '{escaped}'\n")
    list_file.write_text("".join(lines), encoding="utf-8")
    try:
        await run_command(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(list_file),
                "-c", "copy", "-movflags", "+faststart",
                str(output_path),
            ],
            timeout=timeout,
        )
    finally:
        list_file.unlink(missing_ok=True)
    return await probe_duration(output_path)
