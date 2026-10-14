"""나레이션 음성 합성.

타입캐스트(설정했을 때) → Edge TTS → Gemini 음성 → Google 번역 음성(gTTS) 순서로 시도합니다.
Edge TTS는 무료지만 Microsoft가 접속을 막으면(403) 쓸 수 없어서 예비 수단을 둡니다.
Edge는 단어마다 읽는 시각을 주고, 나머지는 글자 수 비율로 추정해 자막 타이밍에 씁니다.
"""

import asyncio
import io
import re
import wave
from pathlib import Path

import edge_tts

from ..config import Settings
from ..fallback import FallbackChain
from ..retry import with_retry
from .subtitles import WordTiming

_TICKS_PER_SEC = 10_000_000  # Edge TTS 시각 단위: 100ns


class EdgeTTS:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def synthesize(self, text: str, output_path: Path) -> list[WordTiming]:
        async def call() -> list[WordTiming]:
            communicate = edge_tts.Communicate(
                text, voice=self._settings.tts_voice, rate=self._settings.tts_rate, boundary="WordBoundary"
            )
            words: list[WordTiming] = []
            with output_path.open("wb") as f:
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        f.write(chunk["data"])
                    elif chunk["type"] == "WordBoundary":
                        start = chunk["offset"] / _TICKS_PER_SEC
                        words.append(WordTiming(start, start + chunk["duration"] / _TICKS_PER_SEC, chunk["text"]))
            if output_path.stat().st_size == 0:
                raise RuntimeError("TTS 결과 파일이 비어 있습니다.")
            return words

        return await with_retry(call, attempts=self._settings.max_retries, what="TTS")


# --------------------------------------------------------------------------- 타입캐스트
TYPECAST_API = "https://api.typecast.ai"


class TypecastError(RuntimeError):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code  # 401·402·403·404는 다시 해도 같으므로 재시도하지 않음


def estimate_word_timings(text: str, duration: float, lead: float = 0.05) -> list[WordTiming]:
    """단어마다 읽는 시각을 주지 않는 TTS용: 글자 수에 비례해 시각을 나눕니다 (단어 강조 자막용 근사치)."""
    words = text.split()
    if not words or duration <= 0:
        return []
    usable = max(duration - lead * 2, duration * 0.8)
    weights = [len(w) + 1 for w in words]  # 띄어쓰기 쉼도 조금 반영
    total = sum(weights)
    timings, t = [], lead
    for word, w in zip(words, weights):
        span = usable * w / total
        timings.append(WordTiming(t, t + span, word))
        t += span
    return timings


class TypecastTTS:
    """타입캐스트(typecast.ai) 음성. 사람 같은 한국어 성우 목소리를 씁니다 (유료 크레딧 사용).
    단어별 시각은 주지 않아 글자 수 비율로 추정해 자막을 맞춥니다."""

    def __init__(self, settings: Settings, transport=None) -> None:
        self._settings = settings
        self._transport = transport

    def _client(self):
        import httpx

        return httpx.AsyncClient(transport=self._transport, timeout=httpx.Timeout(120))

    def request_body(self, text: str) -> dict:
        s = self._settings
        body: dict = {
            "voice_id": s.typecast_voice_id,
            "text": text[:2000],
            "model": s.typecast_model,
            "output": {"audio_format": "mp3", "audio_tempo": s.typecast_tempo, "volume": 100, "audio_pitch": 0},
        }
        if s.typecast_emotion and s.typecast_emotion != "normal":
            body["prompt"] = {"emotion_preset": s.typecast_emotion, "emotion_intensity": 1.0}
        return body

    async def synthesize(self, text: str, output_path: Path) -> list[WordTiming]:
        from . import video

        async def call() -> None:
            async with self._client() as c:
                r = await c.post(f"{TYPECAST_API}/v1/text-to-speech", json=self.request_body(text),
                                 headers={"X-API-KEY": self._settings.typecast_api_key})
            if r.status_code != 200:
                raise typecast_error(r)
            if r.headers.get("content-type", "").startswith("application/json"):
                raise TypecastError("타입캐스트가 음성 대신 다른 응답을 보냈습니다: " + r.text[:200])
            output_path.write_bytes(r.content)
            if output_path.stat().st_size == 0:
                raise TypecastError("타입캐스트 음성 파일이 비어 있습니다.")

        await with_retry(call, attempts=self._settings.max_retries, what="타입캐스트 TTS")
        duration = await video.probe_duration(output_path)
        return estimate_word_timings(text, duration)

    async def voices(self) -> list[dict]:
        """목소리 목록 [{voice_id, name, gender, age, emotions}] (크레딧을 쓰지 않음)."""
        headers = {"X-API-KEY": self._settings.typecast_api_key}
        async with self._client() as c:
            for version in ("v3", "v2", "v1"):  # 최신 경로부터, 없으면(404) 예전 경로
                r = await c.get(f"{TYPECAST_API}/{version}/voices", params={"model": self._settings.typecast_model},
                                headers=headers)
                if r.status_code != 404:
                    break
        if r.status_code != 200:
            raise typecast_error(r)
        body = r.json()
        items = body if isinstance(body, list) else body.get("voices") or body.get("result") or []
        result = []
        for v in items:
            name = v.get("voice_name") or v.get("name") or ""
            if isinstance(name, dict):
                name = name.get("kor") or name.get("ko") or name.get("eng") or next(iter(name.values()), "")
            emotions = v.get("emotions") or []
            for m in v.get("models") or []:
                if isinstance(m, dict) and m.get("version") == self._settings.typecast_model:
                    emotions = m.get("emotions") or emotions
            result.append({"voice_id": v.get("voice_id", ""), "name": name, "gender": v.get("gender") or "",
                           "age": v.get("age") or "", "emotions": emotions})
        return [v for v in result if v["voice_id"]]


def typecast_error(r) -> TypecastError:
    detail = ""
    try:
        body = r.json()
        detail = body.get("message") or body.get("detail") or ""
        if isinstance(detail, (dict, list)):
            detail = str(detail)
    except ValueError:
        pass
    hints = {
        401: "타입캐스트 API 키가 틀렸습니다.",
        402: "타입캐스트 크레딧이 부족합니다. 요금제를 확인하세요.",
        403: "타입캐스트 API를 쓸 권한이 없습니다 (요금제·키 확인).",
        404: "목소리(voice_id)를 찾을 수 없습니다. 설정 점검에서 목소리를 다시 고르세요.",
        429: "타입캐스트 요청이 너무 많습니다. 잠시 뒤 다시 하세요.",
    }
    message = hints.get(r.status_code, f"타입캐스트 오류 (HTTP {r.status_code})")
    return TypecastError(message + (f" — {detail}" if detail else ""), r.status_code)


# --------------------------------------------------------------------------- 예비 음성
class TTSError(RuntimeError):
    pass


def rate_to_tempo(rate: str) -> float:
    """Edge TTS 형식의 속도("+15%")를 재생 배율(1.15)로 바꿉니다."""
    match = re.fullmatch(r"\s*([+-]?\d+(?:\.\d+)?)\s*%\s*", rate or "")
    if not match:
        return 1.0
    return min(2.0, max(0.5, 1 + float(match.group(1)) / 100))


async def _to_mp3(source: Path, output_path: Path, tempo: float, timeout: float) -> None:
    """임시 오디오를 mp3로 바꾸면서 말하기 속도를 맞춥니다."""
    from . import video

    args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source)]
    if abs(tempo - 1.0) > 0.01:
        args += ["-filter:a", f"atempo={tempo:.3f}"]
    args += ["-c:a", "libmp3lame", "-q:a", "3", str(output_path)]
    await video.run_command(args, timeout=timeout)


async def _timings_from_file(text: str, output_path: Path) -> list[WordTiming]:
    from . import video

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise TTSError("음성 파일이 비어 있습니다.")
    return estimate_word_timings(text, await video.probe_duration(output_path))


class GeminiTTS:
    """Gemini 음성 모델. 24kHz 16비트 PCM을 받아 mp3로 바꿉니다."""

    SAMPLE_RATE = 24000

    def __init__(self, client, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def synthesize(self, text: str, output_path: Path) -> list[WordTiming]:
        from google.genai import types

        config = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self._settings.gemini_tts_voice)
                )
            ),
        )

        async def call() -> bytes:
            response = await self._client.aio.models.generate_content(
                model=self._settings.gemini_tts_model,
                contents=f"Read this Korean narration naturally and energetically: {text}",
                config=config,
            )
            for candidate in response.candidates or []:
                for part in (candidate.content.parts if candidate.content else None) or []:
                    if part.inline_data and part.inline_data.data:
                        return part.inline_data.data
            raise TTSError("Gemini가 음성을 돌려주지 않았습니다.")

        pcm = await with_retry(call, attempts=self._settings.max_retries, what="Gemini 음성")
        wav_path = output_path.with_suffix(".gemini.wav")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(self.SAMPLE_RATE)
            wav.writeframes(pcm)
        wav_path.write_bytes(buf.getvalue())
        try:
            await _to_mp3(wav_path, output_path, 1.0, self._settings.ffmpeg_timeout_sec)
        finally:
            wav_path.unlink(missing_ok=True)
        return await _timings_from_file(text, output_path)


class GoogleTranslateTTS:
    """Google 번역 음성 (gTTS, 무료, 키 불필요). 기계음에 가깝지만 가장 잘 막히지 않습니다."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def synthesize(self, text: str, output_path: Path) -> list[WordTiming]:
        from gtts import gTTS

        raw_path = output_path.with_suffix(".gtts.mp3")

        async def call() -> None:
            await asyncio.to_thread(gTTS(text=text, lang="ko").save, str(raw_path))
            if not raw_path.exists() or raw_path.stat().st_size == 0:
                raise TTSError("음성 파일이 비어 있습니다.")

        await with_retry(call, attempts=self._settings.max_retries, what="Google 번역 음성")
        try:
            # gTTS는 말이 느린 편이라 Edge와 같은 속도로 맞춥니다.
            tempo = rate_to_tempo(self._settings.tts_rate)
            await _to_mp3(raw_path, output_path, tempo, self._settings.ffmpeg_timeout_sec)
        finally:
            raw_path.unlink(missing_ok=True)
        return await _timings_from_file(text, output_path)


class TTSChain(FallbackChain):
    """앞의 목소리가 실패하면 다음 목소리로 넘어갑니다. 다시 해도 안 되는 오류(403 등)가 난 목소리는 잠시 건너뜁니다."""

    def __init__(self, sources: list[tuple[str, object]], cooldown_sec: float) -> None:
        super().__init__(sources, cooldown_sec, what="나레이션 음성", error_cls=TTSError)

    async def synthesize(self, text: str, output_path: Path) -> list[WordTiming]:
        try:
            return await self.run(lambda source: source.synthesize(text, output_path))
        except TTSError as exc:
            raise TTSError(f"나레이션 음성을 만들지 못했어요 — {exc}") from exc


def build_tts_chain(settings: Settings, client=None) -> TTSChain:
    sources: list[tuple[str, object]] = []
    if settings.typecast_api_key and settings.typecast_voice_id:
        sources.append(("타입캐스트", TypecastTTS(settings)))
    for name in (n.strip().lower() for n in settings.tts_sources.split(",")):
        if name == "edge":
            sources.append(("Edge", EdgeTTS(settings)))
        elif name == "gemini" and client is not None:
            sources.append(("Gemini", GeminiTTS(client, settings)))
        elif name == "gtts":
            sources.append(("Google 번역", GoogleTranslateTTS(settings)))
    return TTSChain(sources, settings.image_source_cooldown_sec)
