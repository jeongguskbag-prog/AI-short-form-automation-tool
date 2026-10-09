import pytest
from PIL import Image

from app.services import video
from app.services.thumbnail import STYLES, Background, compose, make_thumbnails

from .conftest import requires_ffmpeg


def test_compose_styles_keep_size_and_change_pixels(spec):
    bg = Image.new("RGB", (1600, 900), (40, 160, 90))  # 가로 사진 → 9:16으로 잘림
    for style in STYLES:
        out = compose(bg, "좁은 싱크대 구원템 이거 하나면 끝", style, spec.size, spec.font_path)
        assert out.size == spec.size and out.mode == "RGB"
        assert len(out.getcolors(maxcolors=1_000_000)) > 10  # 글씨·그림자가 들어감


def test_long_text_is_limited_to_three_lines(spec):
    out = compose(Image.new("RGB", spec.size), "아주 " * 60, "center", spec.size, spec.font_path)
    assert out.size == spec.size


@requires_ffmpeg
@pytest.mark.asyncio
async def test_make_thumbnails_from_photos_and_video(tmp_path, spec):
    photo = tmp_path / "p.png"
    Image.new("RGB", (500, 900), (200, 80, 30)).save(photo)
    clip = tmp_path / "c.mp4"
    await video.run_command(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=24:duration=1",
         "-pix_fmt", "yuv420p", str(clip)],
        timeout=30,
    )
    # 영상 시작 시점이 끝을 넘어도 첫 프레임으로 대체
    names = await make_thumbnails([Background(clip, 0.0), Background(photo), Background(clip, 5.0)],
                                  "테스트 문구", tmp_path, spec)
    assert names == ["thumb_1.jpg", "thumb_2.jpg", "thumb_3.jpg"]
    for name in names:
        with Image.open(tmp_path / name) as im:
            assert im.size == spec.size

    assert await make_thumbnails([Background(photo)], "  ", tmp_path, spec) == []
    (tmp_path / "one").mkdir()
    one = await make_thumbnails([Background(photo)], "하나", tmp_path / "one", spec)
    assert len(one) == 3  # 장면이 하나뿐이어도 디자인 3가지
