"""썸네일 3안: 장면 배경(자막 없는 원본) 위에 큰 글씨 문구를 세 가지 디자인으로 얹습니다."""

import asyncio
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import video

STYLES = ("top", "center", "box")


@dataclass(frozen=True)
class Background:
    path: Path
    video_start: float | None = None  # None이면 사진, 값이 있으면 그 시점의 영상 프레임


async def load_background(bg: Background, work_dir: Path, name: str, timeout: float) -> Image.Image:
    if bg.video_start is None:
        source = bg.path
    else:
        source = work_dir / f"{name}_frame.jpg"
        await video.run_command(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{bg.video_start + 0.5:.3f}",
             "-i", str(bg.path), "-frames:v", "1", "-q:v", "2", str(source)],
            timeout=timeout,
        )
        if not source.is_file():  # 시작 시점이 끝에 가까우면 프레임이 안 나올 수 있음
            await video.run_command(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(bg.path),
                 "-frames:v", "1", "-q:v", "2", str(source)],
                timeout=timeout,
            )
    with Image.open(source) as im:
        return im.convert("RGB")


def _fit_text(text: str, font_path: Path, width: int, max_lines: int = 3):
    size = int(width * 0.16)  # 짧은 문구일수록 크게, 3줄을 넘으면 줄여 나감
    while True:
        font = ImageFont.truetype(str(font_path), size)
        lines = video.wrap_text(text, font, width * 0.86)
        if len(lines) <= max_lines or size <= int(width * 0.065):
            return font, lines[:max_lines]
        size -= 4


def compose(background: Image.Image, text: str, style: str, size: tuple[int, int], font_path: Path) -> Image.Image:
    w, h = size
    canvas = ImageOps.fit(background, size, method=Image.Resampling.LANCZOS).convert("RGBA")
    text = " ".join(text.split())
    font, lines = _fit_text(text, font_path, w)
    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    gap = int(font.size * 0.15)
    block_h = line_h * len(lines) + gap * (len(lines) - 1)
    stroke = max(3, font.size // 10)

    shade = Image.new("RGBA", size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    if style == "top":
        # 위쪽을 어둡게 그라디언트 → 노란 글씨
        band = int(h * 0.45)
        for y in range(band):
            sd.line([(0, y), (w, y)], fill=(0, 0, 0, int(190 * (1 - y / band))))
        top, color, box = int(h * 0.08), (255, 225, 77, 255), None
    elif style == "center":
        sd.rectangle([0, 0, w, h], fill=(0, 0, 0, 90))
        top, color, box = (h - block_h) // 2, (255, 255, 255, 255), None
    else:
        top, color, box = int(h * 0.62), (255, 255, 255, 255), (232, 50, 43, 235)
    canvas = Image.alpha_composite(canvas, shade)

    draw = ImageDraw.Draw(canvas)
    if box is not None:
        block_w = max(font.getlength(line) for line in lines)
        pad_x, pad_y = int(font.size * 0.45), int(font.size * 0.3)
        draw.rounded_rectangle(
            ((w - block_w) / 2 - pad_x, top - pad_y, (w + block_w) / 2 + pad_x, top + block_h + pad_y),
            radius=int(font.size * 0.25), fill=box,
        )
    y = top
    for line in lines:
        x = (w - font.getlength(line)) / 2
        draw.text((x, y), line, font=font, fill=color,
                  stroke_width=0 if box else stroke, stroke_fill=(0, 0, 0, 255))
        y += line_h + gap
    return canvas.convert("RGB")


async def make_thumbnails(
    backgrounds: list[Background],
    text: str,
    output_dir: Path,
    spec: video.VideoSpec,
) -> list[str]:
    """배경 후보에서 서로 다른 장면 3개를 골라 디자인 3가지로 만들고, 파일 이름 목록을 돌려줍니다."""
    if not backgrounds or not text.strip():
        return []
    n = len(backgrounds)
    picks = list(dict.fromkeys([0, n // 3, (2 * n) // 3]))
    while len(picks) < len(STYLES):
        picks.append(picks[len(picks) % len(picks)])
    names: list[str] = []
    for i, (style, idx) in enumerate(zip(STYLES, picks, strict=False), start=1):
        name = f"thumb_{i}.jpg"
        bg = await load_background(backgrounds[idx], output_dir, f"thumb_{i}", spec.timeout_sec)
        image = await asyncio.to_thread(compose, bg, text, style, spec.size, spec.font_path)
        await asyncio.to_thread(image.save, output_dir / name, "JPEG", quality=90)
        names.append(name)
    return names
