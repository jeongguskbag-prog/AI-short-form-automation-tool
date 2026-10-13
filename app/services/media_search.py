"""상품 영상·사진 찾기: 한 제품을 홍보용으로 쓸 수 있는 곳에서 한 번에 찾아 후보 목록을 돌려줍니다.

- 알리익스프레스 (제휴 API): 판매자가 올린 공식 상품 영상·사진 — 제휴 홍보용으로 제공되는 소재
- 이베이: 상품 사진
- Pixabay·Pexels: 제품과 어울리는 무료 스톡 영상 (같은 제품은 아님)

후보를 고르면 화면이 `/api/v1/media/import`로 하나씩 가져옵니다. 남의 영상 플랫폼(유튜브·틱톡)이나
공식 API가 없는 쇼핑몰의 영상은 찾지 않습니다 (저작권·약관).
"""

import asyncio
import logging
from dataclasses import asdict, dataclass

import httpx

from ..config import Settings
from .footage import PexelsVideoSource, PixabayVideoSource
from .products import AliExpress, Ebay

logger = logging.getLogger(__name__)

MAX_PER_SOURCE = 12


@dataclass
class MediaCandidate:
    source: str  # 화면 표시용 이름
    kind: str  # "video" | "image"
    url: str
    title: str = ""
    credit: str = ""
    product_url: str = ""
    official: bool = False  # 그 제품의 판매자 소재인지 (스톡은 False)


class MediaSearch:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._s = settings
        self._transport = transport

    def sources(self) -> list[tuple[str, bool]]:
        s = self._s
        return [
            ("알리익스프레스", bool(s.aliexpress_app_key and s.aliexpress_app_secret)),
            ("이베이", bool(s.ebay_client_id and s.ebay_client_secret)),
            ("Pixabay 영상", bool(s.pixabay_api_key)),
            ("Pexels 영상", bool(s.pexels_api_key)),
        ]

    async def search(self, keyword_en: str, keyword_ko: str = "") -> tuple[list[dict], list[str]]:
        """(후보 목록, 실패한 곳 안내). 키가 없는 곳은 건너뜁니다."""
        jobs: list[tuple[str, object]] = []
        s = self._s
        if s.aliexpress_app_key and s.aliexpress_app_secret:
            # 알리는 한국어 상품명으로도 찾지만 영어가 더 잘 맞아 영어를 먼저 씁니다.
            jobs.append(("알리익스프레스", self._aliexpress(keyword_en or keyword_ko)))
        if s.ebay_client_id and s.ebay_client_secret:
            jobs.append(("이베이", self._ebay(keyword_en or keyword_ko)))
        if s.pixabay_api_key:
            jobs.append(("Pixabay 영상", self._stock(PixabayVideoSource(s.pixabay_api_key, s, self._transport),
                                                     keyword_en or keyword_ko)))
        if s.pexels_api_key:
            jobs.append(("Pexels 영상", self._stock(PexelsVideoSource(s.pexels_api_key, s, self._transport),
                                                    keyword_en or keyword_ko)))
        results = await asyncio.gather(*(job for _, job in jobs), return_exceptions=True)
        found: list[MediaCandidate] = []
        notes: list[str] = []
        for (name, _), result in zip(jobs, results):
            if isinstance(result, BaseException):
                logger.warning("상품 영상 찾기 실패 (%s): %s", name, result)
                notes.append(f"{name}: 찾지 못했어요 ({str(result)[:120] or type(result).__name__})")
            else:
                found.extend(result)
        # 공식 영상 → 공식 사진 → 스톡 영상 순서
        found.sort(key=lambda c: (not c.official, c.kind != "video"))
        unique, seen = [], set()
        for c in found:
            if c.url not in seen:
                seen.add(c.url)
                unique.append(asdict(c))
        return unique, notes

    async def _aliexpress(self, keyword: str) -> list[MediaCandidate]:
        products = await AliExpress(self._s, self._transport).search(keyword, limit=10)
        out: list[MediaCandidate] = []
        for p in products:
            if p.video_url:
                out.append(MediaCandidate("알리익스프레스", "video", p.video_url, p.title, p.mall, p.url, True))
            for url in [p.image_url, *p.images][:3]:
                if url:
                    out.append(MediaCandidate("알리익스프레스", "image", url, p.title, p.mall, p.url, True))
        return out[: MAX_PER_SOURCE * 2]

    async def _ebay(self, keyword: str) -> list[MediaCandidate]:
        products = await Ebay(self._s, self._transport).search(keyword, limit=8)
        out = [MediaCandidate("이베이", "image", url, p.title, p.mall, p.url, True)
               for p in products for url in [p.image_url, *p.images][:2] if url]
        return out[:MAX_PER_SOURCE]

    async def _stock(self, source, keyword: str) -> list[MediaCandidate]:
        async with httpx.AsyncClient(transport=self._transport, timeout=30, headers=source._headers()) as c:
            clips = await source._search(c, keyword)
        return [MediaCandidate(source.name, "video", clip.url, keyword, clip.credit) for clip in clips[:MAX_PER_SOURCE]]
