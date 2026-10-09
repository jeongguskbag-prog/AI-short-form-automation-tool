"""단어 단위 자막: TTS가 알려 준 단어별 시각으로 짧은 구절을 보여 주고, 지금 읽는 단어를 강조합니다."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class WordTiming:
    start: float  # 초
    end: float
    text: str


@dataclass(frozen=True)
class SubtitleFrame:
    text: str  # 화면에 보일 구절
    highlight: int  # 구절 안에서 강조할 단어 번호
    start: float
    end: float | None  # None이면 씬 끝까지


_PUNCT_ONLY = re.compile(r"^[\W_]+$")
_SENTENCE_END = re.compile(r"[.!?…,~]$")


def _merge_punctuation(words: list[WordTiming]) -> list[WordTiming]:
    """문장부호만 있는 토큰은 앞 단어에 붙입니다."""
    merged: list[WordTiming] = []
    for w in words:
        text = w.text.strip()
        if not text:
            continue
        if _PUNCT_ONLY.match(text) and merged:
            prev = merged[-1]
            merged[-1] = WordTiming(prev.start, max(prev.end, w.end), prev.text + text)
        else:
            merged.append(WordTiming(w.start, w.end, text))
    return merged


def chunk_words(words: list[WordTiming], max_chars: int = 10, max_words: int = 3) -> list[list[WordTiming]]:
    """한 화면에 보일 구절로 묶습니다. 기본 자막 크기에서 한 줄에 들어가도록 글자 수·단어 수를 넘거나
    문장부호에서 끊습니다."""
    chunks: list[list[WordTiming]] = []
    current: list[WordTiming] = []
    for w in _merge_punctuation(words):
        chars = sum(len(x.text) for x in current) + len(w.text)
        if current and (chars > max_chars or len(current) >= max_words):
            chunks.append(current)
            current = []
        current.append(w)
        if _SENTENCE_END.search(w.text):
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return chunks


def karaoke_frames(words: list[WordTiming]) -> list[SubtitleFrame]:
    """단어마다 한 장: 그 단어가 시작할 때부터 다음 단어가 시작할 때까지 보입니다.

    첫 장은 0초부터, 마지막 장은 씬 끝까지 보여 자막이 비는 순간이 없게 합니다.
    """
    frames: list[SubtitleFrame] = []
    for chunk in chunk_words(words):
        text = " ".join(w.text for w in chunk)
        for i, w in enumerate(chunk):
            frames.append(SubtitleFrame(text, i, w.start, None))
    for i in range(len(frames) - 1):
        frames[i] = SubtitleFrame(frames[i].text, frames[i].highlight, frames[i].start, frames[i + 1].start)
    if frames:
        frames[0] = SubtitleFrame(frames[0].text, frames[0].highlight, 0.0, frames[0].end)
    return frames
