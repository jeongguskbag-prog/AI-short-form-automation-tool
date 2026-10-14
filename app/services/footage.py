"""무료 스톡 영상 (Pexels·Pixabay 비디오).

사진 소스와 같은 형태(`generate(prompt, output_path, query) -> 출처 표기`)라 `ImageSourceChain`으로 묶어 씁니다.
영상은 커서 끝까지 메모리에 올리지 않고 나눠 받으며, 크기 한도를 넘으면 그 영상은 건너뜁니다.
"""

from pathlib import Path

import httpx

from ..config import Settings
from .images import PexelsImageSource, PixabayImageSource, StockPhoto


class _StreamingDownload:
    """크기 한도를 두고 나눠 받는 다운로드. 받다가 끊기거나 한도를 넘으면 파일을 지웁니다."""

    _settings: Settings

    async def _download(self, client: httpx.AsyncClient, url: str, output_path: Path) -> bool:
        limit = self._settings.max_stock_video_mb * 1024 * 1024
        size = 0
        try:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    return False
                with output_path.open("wb") as f:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise _TooLarge
                        f.write(chunk)
        except (_TooLarge, httpx.HTTPError):
            output_path.unlink(missing_ok=True)
            return False
        if size == 0:
            output_path.unlink(missing_ok=True)
            return False
        return True


class _TooLarge(Exception):
    pass


def _pick(files: list[tuple[str, int, int]], target_h: int) -> str | None:
    """(주소, 너비, 높이) 중 세로 영상을 우선, 화면 높이에 가장 가까우면서 너무 크지 않은 것을 고릅니다."""
    usable = [f for f in files if f[0] and f[2] <= target_h * 1.5]
    if not usable:
        usable = [f for f in files if f[0]]
    if not usable:
        return None
    return min(usable, key=lambda f: (f[2] < f[1], abs(f[2] - target_h)))[0]


class PexelsVideoSource(_StreamingDownload, PexelsImageSource):
    """Pexels 무료 영상 (사진과 같은 API 키). https://www.pexels.com/api/documentation/#videos"""

    name = "Pexels 영상"
    SEARCH_URL = "https://api.pexels.com/videos/search"

    async def _search(self, client: httpx.AsyncClient, query: str) -> list[StockPhoto]:
        data = await self._get_json(
            client, self.SEARCH_URL, {"query": query, "orientation": "portrait", "per_page": 15}
        )
        clips = []
        for item in data.get("videos") or []:
            files = [
                (f.get("link", ""), f.get("width") or 0, f.get("height") or 0)
                for f in item.get("video_files") or []
                if f.get("file_type", "video/mp4") == "video/mp4"
            ]
            url = _pick(files, self._settings.video_height)
            if url:
                who = (item.get("user") or {}).get("name") or "Unknown"
                text = f"{item.get('url') or ''} {' '.join(map(str, item.get('tags') or []))}"
                clips.append(StockPhoto(f"pexels-video:{item.get('id')}", url, f"{who} / Pexels", text))
        return clips


class PixabayVideoSource(_StreamingDownload, PixabayImageSource):
    """Pixabay 무료 영상 (사진과 같은 API 키). https://pixabay.com/api/docs/#api_search_videos"""

    name = "Pixabay 영상"
    SEARCH_URL = "https://pixabay.com/api/videos/"

    async def _search(self, client: httpx.AsyncClient, query: str) -> list[StockPhoto]:
        data = await self._get_json(
            client,
            self.SEARCH_URL,
            {"key": self._api_key, "q": query[:100], "safesearch": "true", "per_page": 20},
        )
        clips = []
        for item in data.get("hits") or []:
            variants = (item.get("videos") or {}).values()
            files = [(v.get("url", ""), v.get("width") or 0, v.get("height") or 0) for v in variants]
            url = _pick(files, self._settings.video_height)
            if url:
                who = item.get("user") or "Unknown"
                text = f"{item.get('tags') or ''} {item.get('pageURL') or ''}"
                clips.append(StockPhoto(f"pixabay-video:{item.get('id')}", url, f"{who} / Pixabay", text))
        return clips
