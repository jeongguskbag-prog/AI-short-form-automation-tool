"""나레이션 음성 합성 (Edge TTS). 단어마다 읽는 시각도 함께 받아 자막 타이밍에 씁니다."""

from pathlib import Path

import edge_tts

from ..config import Settings
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
