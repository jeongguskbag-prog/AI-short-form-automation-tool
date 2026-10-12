import json

import httpx
import pytest

from app.services.setup_check import SetupChecker
from app.services.tts import TypecastError, TypecastTTS, estimate_word_timings

from .conftest import requires_ffmpeg


def test_estimate_word_timings_cover_duration_in_order():
    words = estimate_word_timings("이 랜턴 하나면 캠핑 끝", 3.0)
    assert [w.text for w in words] == ["이", "랜턴", "하나면", "캠핑", "끝"]
    assert words[0].start > 0 and words[-1].end <= 3.0
    assert all(a.end == pytest.approx(b.start) for a, b in zip(words, words[1:]))
    assert words[2].end - words[2].start > words[0].end - words[0].start  # 긴 단어가 더 오래
    assert estimate_word_timings("", 2.0) == [] and estimate_word_timings("a", 0) == []


def test_request_body(settings):
    settings.typecast_voice_id = "tc_abc"
    settings.typecast_emotion = "happy"
    body = TypecastTTS(settings).request_body("안녕" * 1500)
    assert body["voice_id"] == "tc_abc" and body["model"] == "ssfm-v30" and len(body["text"]) == 2000
    assert body["output"]["audio_format"] == "mp3" and body["output"]["audio_tempo"] == 1.1
    assert body["prompt"] == {"emotion_preset": "happy", "emotion_intensity": 1.0}
    settings.typecast_emotion = ""
    assert "prompt" not in TypecastTTS(settings).request_body("x")


@requires_ffmpeg
@pytest.mark.asyncio
async def test_synthesize_writes_audio_and_estimates_words(settings, tmp_path):
    import subprocess

    mp3 = tmp_path / "src.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=2", str(mp3)], check=True)
    settings.typecast_api_key, settings.typecast_voice_id = "key", "tc_abc"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-KEY"] == "key"
        assert json.loads(request.content)["text"] == "캠핑 랜턴 추천"
        return httpx.Response(200, content=mp3.read_bytes(), headers={"content-type": "audio/mpeg"})

    out = tmp_path / "out.mp3"
    words = await TypecastTTS(settings, httpx.MockTransport(handler)).synthesize("캠핑 랜턴 추천", out)
    assert out.read_bytes() == mp3.read_bytes()
    assert [w.text for w in words] == ["캠핑", "랜턴", "추천"] and 1.5 < words[-1].end <= 2.1


@pytest.mark.asyncio
async def test_errors_are_explained_and_not_retried(settings, tmp_path):
    settings.typecast_api_key, settings.typecast_voice_id = "key", "tc_abc"
    settings.max_retries = 3
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(402, json={"message": "insufficient credits"})

    with pytest.raises(TypecastError, match="크레딧"):
        await TypecastTTS(settings, httpx.MockTransport(handler)).synthesize("x", tmp_path / "o.mp3")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_voices_and_setup_check(settings):
    settings.typecast_api_key = "key"
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v3/voices":
            return httpx.Response(404)
        return httpx.Response(200, json=[
            {"voice_id": "tc_1", "voice_name": {"kor": "지윤", "eng": "Jiyun"}, "gender": "female", "age": "young_adult",
             "models": [{"version": "ssfm-v30", "emotions": ["normal", "happy"]}]},
            {"voice_id": "tc_2", "voice_name": "민준", "models": []},
        ])

    transport = httpx.MockTransport(handler)
    voices = await TypecastTTS(settings, transport).voices()
    assert paths[:2] == ["/v3/voices", "/v2/voices"]
    assert voices[0] == {"voice_id": "tc_1", "name": "지윤", "gender": "female", "age": "young_adult",
                         "emotions": ["normal", "happy"]}

    checker = SetupChecker(settings, transport=transport)
    results = {r["key"]: r for r in await checker.run()}
    assert results["typecast"]["status"] == "warn"
    settings.typecast_voice_id = "tc_1"
    results = {r["key"]: r for r in await checker.run()}
    assert results["typecast"]["status"] == "ok" and "지윤" in results["typecast"]["message"]
    settings.typecast_voice_id = "tc_gone"
    results = {r["key"]: r for r in await checker.run()}
    assert results["typecast"]["status"] == "error"
