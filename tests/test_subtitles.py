import subprocess

import pytest
from PIL import Image

from app.services import video
from app.services.subtitles import WordTiming, chunk_words, karaoke_frames

from .conftest import FakeTTS, requires_ffmpeg


def _w(*items):
    return [WordTiming(s, s + 0.3, t) for s, t in items]


def test_chunks_break_on_length_count_and_punctuation():
    words = _w((0.1, "싱크대"), (0.4, "좁으면"), (0.8, "설거지통"), (1.2, "둘"), (1.4, "데"), (1.6, "없죠"),
               (1.9, "?"), (2.2, "이거"), (2.5, "보세요"))
    chunks = [[w.text for w in c] for c in chunk_words(words)]
    assert chunks == [["싱크대", "좁으면", "설거지통"], ["둘", "데", "없죠?"], ["이거", "보세요"]]
    assert all(sum(len(t) for t in c) <= 10 and len(c) <= 3 for c in chunks)


def test_karaoke_frames_cover_whole_scene():
    frames = karaoke_frames(_w((0.2, "접으면"), (0.6, "3cm"), (1.0, "끝!"), (1.5, "진짜예요")))
    assert [(f.text, f.highlight) for f in frames] == [
        ("접으면 3cm 끝!", 0), ("접으면 3cm 끝!", 1), ("접으면 3cm 끝!", 2), ("진짜예요", 0),
    ]
    assert frames[0].start == 0.0  # 첫 단어 전에도 자막이 보임
    assert [f.end for f in frames] == [0.6, 1.0, 1.5, None]  # 다음 단어까지, 마지막은 씬 끝까지
    assert karaoke_frames([]) == []


def test_highlighted_word_is_yellow(spec, tmp_path):
    out = tmp_path / "s.png"
    video._render_subtitle_png("하나 둘 셋", spec, out, highlight=1)
    with Image.open(out) as im:
        colors = {c for _, c in im.getcolors(maxcolors=1_000_000)}
    assert video.HIGHLIGHT_COLOR in colors and (255, 255, 255, 255) in colors

    plain = tmp_path / "p.png"
    video._render_subtitle_png("하나 둘 셋", spec, plain)
    with Image.open(plain) as im:
        assert video.HIGHLIGHT_COLOR not in {c for _, c in im.getcolors(maxcolors=1_000_000)}


def _frame_color(path, t, xy):
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1", "-f", "rawvideo",
         "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout
    w = 360
    x, y = xy
    i = (y * w + x) * 3
    return tuple(out[i:i + 3])


@requires_ffmpeg
@pytest.mark.asyncio
async def test_timed_subtitles_switch_at_given_times(tmp_path, spec):
    bg = tmp_path / "bg.png"
    Image.new("RGB", spec.size, (0, 0, 0)).save(bg)
    audio = tmp_path / "a.mp3"
    await FakeTTS().synthesize("x", audio)  # 1초
    red, blue = tmp_path / "r.png", tmp_path / "b.png"
    for path, color in ((red, (255, 0, 0, 255)), (blue, (0, 0, 255, 255))):
        im = Image.new("RGBA", spec.size, (0, 0, 0, 0))  # 투명 배경에 사각형만
        im.paste(Image.new("RGBA", (100, 100), color), (130, 270))
        im.save(path)

    out = tmp_path / "scene.mp4"
    await video.render_scene(
        image_path=bg, audio_path=audio, output_path=out, spec=spec,
        timed_subtitles=[(red, 0.2, 0.6), (blue, 0.6, None)],
    )
    early, late = _frame_color(out, 0.1, (180, 320)), _frame_color(out, 0.9, (180, 320))
    assert early[0] > 200 and early[2] < 60  # 첫 구절은 0초부터 (빈 구간 없음)
    assert late[2] > 200 and late[0] < 60
    corner = _frame_color(out, 0.9, (10, 10))
    assert max(corner) < 40  # PNG 투명 부분은 배경이 보임
    assert not list(tmp_path.glob("*.subs.txt"))  # 목록 파일 정리


@pytest.mark.asyncio
async def test_edge_tts_collects_word_boundaries(monkeypatch, tmp_path, settings):
    import edge_tts

    from app.services.tts import EdgeTTS

    seen = {}

    class FakeCommunicate:
        def __init__(self, text, voice, rate, boundary):
            seen.update(text=text, boundary=boundary)

        async def stream(self):
            yield {"type": "WordBoundary", "offset": 1_000_000, "duration": 3_000_000, "text": "안녕"}
            yield {"type": "audio", "data": b"ID3abc"}
            yield {"type": "WordBoundary", "offset": 5_000_000, "duration": 2_000_000, "text": "하세요"}
            yield {"type": "audio", "data": b"def"}

    monkeypatch.setattr(edge_tts, "Communicate", FakeCommunicate)
    out = tmp_path / "a.mp3"
    words = await EdgeTTS(settings).synthesize("안녕 하세요", out)
    assert seen["boundary"] == "WordBoundary"
    assert out.read_bytes() == b"ID3abcdef"
    assert words == [WordTiming(0.1, 0.4, "안녕"), WordTiming(0.5, 0.7, "하세요")]


class WordTTS(FakeTTS):
    async def synthesize(self, text, output_path):
        await super().synthesize(text, output_path)
        return [WordTiming(0.1 + 0.2 * i, 0.3 + 0.2 * i, w) for i, w in enumerate(text.split())]


@requires_ffmpeg
@pytest.mark.asyncio
async def test_pipeline_uses_word_subtitles(settings, spec):
    from app.jobs import JobStore
    from app.pipeline import Providers, ShortsPipeline

    from .conftest import FakeImages, FakePlanner

    pipeline = ShortsPipeline(Providers(FakePlanner(2), WordTTS(), FakeImages()), settings, spec)
    job = JobStore().create("대본입니다 충분히 길게", "calm")
    await pipeline.run(job)
    assert job.status == "COMPLETED", job.error
    job_dir = settings.output_dir / job.job_id
    # FakePlanner 나레이션 "나레이션 0" → 단어 2개 → 씬마다 구절 PNG 2장
    assert len(list(job_dir.glob("subtitle_01_*.png"))) == 2
    assert not (job_dir / "subtitle_01.png").exists()

    settings.subtitle_mode = "scene"
    job = JobStore().create("대본입니다 충분히 길게", "calm")
    await pipeline.run(job)
    assert (settings.output_dir / job.job_id / "subtitle_01.png").exists()
