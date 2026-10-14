import math
import struct

import pytest

from app.config import Settings
from app.services import tts, video

from .conftest import requires_ffmpeg

SETTINGS = Settings(max_retries=1, _env_file=None)


class EdgeBlocked(Exception):
    """aiohttp가 Edge TTS 접속 거부 때 내는 WSServerHandshakeError 모양."""

    __module__ = "aiohttp.client_exceptions"
    status = 403


class Recorder:
    def __init__(self, error=None):
        self.error, self.calls = error, 0

    async def synthesize(self, text, output_path):
        self.calls += 1
        if self.error:
            raise self.error
        output_path.write_bytes(b"audio")
        return []


@pytest.mark.parametrize(("rate", "tempo"), [("+15%", 1.15), ("-10%", 0.9), ("0%", 1.0), ("", 1.0), ("x", 1.0)])
def test_rate_to_tempo(rate, tempo):
    assert tts.rate_to_tempo(rate) == pytest.approx(tempo)


@pytest.mark.asyncio
async def test_chain_falls_back_when_edge_is_blocked_and_then_skips_it(tmp_path):
    edge, backup = Recorder(EdgeBlocked("Invalid response status")), Recorder()
    chain = tts.TTSChain([("Edge", edge), ("Google 번역", backup)], cooldown_sec=3600)
    for i in range(3):
        assert await chain.synthesize("안녕", tmp_path / f"{i}.mp3") == []
    assert edge.calls == 1  # 403 뒤로는 Edge를 건너뜀
    assert backup.calls == 3


@pytest.mark.asyncio
async def test_chain_reports_every_source_in_korean(tmp_path):
    chain = tts.TTSChain(
        [("Edge", Recorder(EdgeBlocked("x"))), ("Google 번역", Recorder(RuntimeError("network down")))],
        cooldown_sec=3600,
    )
    with pytest.raises(tts.TTSError) as info:
        await chain.synthesize("안녕", tmp_path / "a.mp3")
    msg = str(info.value)
    assert msg.startswith("나레이션 음성을 만들지 못했어요 — Edge: 서버가 접속을 거부함 (HTTP 403)")
    assert "Google 번역: network down" in msg
    assert "API 키" not in msg  # Edge는 키를 쓰지 않음


def test_build_chain_order():
    assert tts.build_tts_chain(SETTINGS, client=None).names == ["Edge", "Google 번역"]
    assert tts.build_tts_chain(SETTINGS, client=object()).names == ["Edge", "Gemini", "Google 번역"]
    typecast = Settings(typecast_api_key="k", typecast_voice_id="tc_1", max_retries=1, _env_file=None)
    assert tts.build_tts_chain(typecast, client=object()).names == ["타입캐스트", "Edge", "Gemini", "Google 번역"]


@requires_ffmpeg
@pytest.mark.asyncio
async def test_google_translate_tts_writes_sped_up_mp3_with_word_timings(tmp_path, monkeypatch):
    class FakeGTTS:
        def __init__(self, text, lang):
            assert lang == "ko"

        def save(self, path):
            import subprocess

            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=2.3", path],
                check=True,
            )

    import gtts

    monkeypatch.setattr(gtts, "gTTS", FakeGTTS)
    out = tmp_path / "audio_01.mp3"
    words = await tts.GoogleTranslateTTS(SETTINGS).synthesize("안녕 하세요", out)
    assert await video.probe_duration(out) == pytest.approx(2.3 / 1.15, abs=0.1)
    assert [w.text for w in words] == ["안녕", "하세요"]  # 자막 강조용 추정 시각
    assert sorted(p.name for p in tmp_path.iterdir()) == ["audio_01.mp3"]  # 임시 파일 정리


@requires_ffmpeg
@pytest.mark.asyncio
async def test_gemini_tts_turns_pcm_into_mp3(tmp_path):
    pcm = b"".join(struct.pack("<h", int(8000 * math.sin(i / 10))) for i in range(24000))  # 1초

    class Part:
        class inline_data:
            data = pcm

    class Response:
        class candidate:
            class content:
                parts = (Part,)

        candidates = (candidate,)

    class Models:
        async def generate_content(self, *, model, contents, config):
            assert model == SETTINGS.gemini_tts_model
            assert config.response_modalities == ["AUDIO"]
            return Response

    class Client:
        class aio:
            models = Models()

    out = tmp_path / "audio_01.mp3"
    words = await tts.GeminiTTS(Client, SETTINGS).synthesize("안녕하세요", out)
    assert await video.probe_duration(out) == pytest.approx(1.0, abs=0.1)
    assert len(words) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["audio_01.mp3"]
