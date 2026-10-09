"""씬 이미지 만들기.

Gemini 이미지 모델(또는 Imagen) → 무료 사진(Pexels·Pixabay·Openverse) → 그라디언트 배경 순서로 시도합니다.
이미지 소스는 모두 같은 형태(`generate(prompt, output_path, query) -> 출처 표기 | None`)를 따릅니다.
"""

import asyncio
import colorsys
import hashlib
import logging
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import httpx
from google import genai
from google.genai import types
from PIL import Image, ImageDraw

from ..config import Settings
from ..errors import is_retryable, short_reason
from ..retry import with_retry

logger = logging.getLogger(__name__)

STYLE_SUFFIX = (
    "vertical 9:16 composition, cinematic lighting, highly detailed, sharp focus, "
    "no text, no captions, no watermark"
)


class ImageGenerationError(RuntimeError):
    pass


class GeminiImageGenerator:
    """Gemini 이미지 모델(generate_content)과 Imagen(generate_images)을 모델 이름으로 골라 씁니다."""

    def __init__(self, client: genai.Client, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    @property
    def _is_imagen(self) -> bool:
        return self._settings.image_model.startswith("imagen")

    async def _generate_gemini(self, prompt: str) -> bytes | None:
        response = await self._client.aio.models.generate_content(
            model=self._settings.image_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(aspect_ratio="9:16"),
            ),
        )
        for candidate in response.candidates or []:
            for part in (candidate.content.parts if candidate.content else None) or []:
                if part.inline_data and part.inline_data.data:
                    return part.inline_data.data
        return None

    async def _generate_imagen(self, prompt: str) -> bytes | None:
        result = await self._client.aio.models.generate_images(
            model=self._settings.image_model,
            prompt=prompt,
            config=types.GenerateImagesConfig(
                number_of_images=1,
                aspect_ratio="9:16",
                negative_prompt="text, letters, watermark, logo, blurry, distorted",
            ),
        )
        images = result.generated_images or []
        return images[0].image.image_bytes if images and images[0].image else None

    async def generate(self, prompt: str, output_path: Path, query: str = "") -> str | None:
        full_prompt = f"{prompt}, {STYLE_SUFFIX}"

        async def call() -> None:
            if self._is_imagen:
                image_bytes = await self._generate_imagen(full_prompt)
            else:
                image_bytes = await self._generate_gemini(full_prompt)
            if not image_bytes:
                # 안전 필터에 걸리면 이미지 없이 응답이 옵니다.
                raise ImageGenerationError("이미지가 생성되지 않았습니다 (안전 필터 차단 가능).")
            output_path.write_bytes(image_bytes)

        await with_retry(call, attempts=self._settings.max_retries, what="이미지 생성")
        return None  # AI 생성 이미지는 출처 표기가 필요 없음


class StockPhotoError(RuntimeError):
    def __init__(self, service: str, code: int, message: str) -> None:
        super().__init__(f"{service} {code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class StockPhoto:
    id: str
    url: str
    credit: str


class StockPhotoSource:
    """무료 사진 검색 서비스 공통 동작: 검색 → 안 쓴 사진 고르기 → 다운로드 → 출처 반환."""

    name = "Stock"
    MAX_DOWNLOAD_TRIES = 3  # 원본 서버가 응답하지 않는 사진은 다음 사진으로 넘어감

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport  # 테스트에서 가짜 응답을 넣을 때 사용
        # 같은 영상 안에서 같은 사진이 반복되지 않도록 최근에 쓴 사진을 기억합니다.
        self._recent: deque[str] = deque(maxlen=200)

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": "ai-shortform-video/1.0"}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(20.0),
            headers=self._headers(),
            follow_redirects=True,
        )

    async def _search(self, client: httpx.AsyncClient, query: str) -> list[StockPhoto]:
        raise NotImplementedError

    async def _get_json(self, client: httpx.AsyncClient, url: str, params: dict) -> dict:
        response = await client.get(url, params=params)
        if response.status_code != 200:
            raise StockPhotoError(self.name, response.status_code, response.text[:200])
        return response.json()

    async def _download(self, client: httpx.AsyncClient, url: str, output_path: Path) -> bool:
        response = await client.get(url)
        if response.status_code == 200 and response.content:
            output_path.write_bytes(response.content)
            return True
        return False

    async def generate(self, prompt: str, output_path: Path, query: str = "") -> str | None:
        words = (query or prompt).split()
        # 검색 결과가 없으면 더 짧은 검색어로 한 번 더 찾습니다.
        queries = [q for q in dict.fromkeys([" ".join(words[:4]), " ".join(words[:2])]) if q]

        async def call() -> str | None:
            async with self._client() as client:
                for q in queries:
                    photos = await self._search(client, q)
                    fresh = [ph for ph in photos if ph.id not in self._recent] or photos
                    for photo in fresh[: self.MAX_DOWNLOAD_TRIES]:
                        try:
                            ok = await self._download(client, photo.url, output_path)
                        except httpx.HTTPError:
                            continue
                        if ok:
                            self._recent.append(photo.id)
                            return photo.credit
            return None

        credit = await with_retry(call, attempts=self._settings.max_retries, what=f"{self.name} 사진")
        if credit is None:
            # 검색 결과가 없는 건 다시 시도해도 같으므로 재시도 밖에서 알립니다.
            raise ImageGenerationError(f"'{queries[0] if queries else prompt}'에 맞는 사진을 찾지 못함")
        return credit


class PexelsImageSource(StockPhotoSource):
    """Pexels 무료 사진 (API 키 필요). https://www.pexels.com/api/"""

    name = "Pexels"
    SEARCH_URL = "https://api.pexels.com/v1/search"

    def __init__(self, api_key: str, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(settings, transport)
        self._api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {**super()._headers(), "Authorization": self._api_key}

    async def _search(self, client: httpx.AsyncClient, query: str) -> list[StockPhoto]:
        data = await self._get_json(
            client, self.SEARCH_URL, {"query": query, "orientation": "portrait", "per_page": 15}
        )
        photos = []
        for item in data.get("photos") or []:
            src = item.get("src") or {}
            url = src.get("portrait") or src.get("large2x") or src.get("original")
            if url:
                who = item.get("photographer") or "Unknown"
                photos.append(StockPhoto(f"pexels:{item.get('id')}", url, f"{who} / Pexels"))
        return photos


class PixabayImageSource(StockPhotoSource):
    """Pixabay 무료 사진 (가입 후 바로 받는 무료 API 키 필요). https://pixabay.com/api/docs/"""

    name = "Pixabay"
    SEARCH_URL = "https://pixabay.com/api/"

    def __init__(self, api_key: str, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(settings, transport)
        self._api_key = api_key

    async def _search(self, client: httpx.AsyncClient, query: str) -> list[StockPhoto]:
        data = await self._get_json(
            client,
            self.SEARCH_URL,
            {
                "key": self._api_key,
                "q": query[:100],
                "image_type": "photo",
                "orientation": "vertical",
                "safesearch": "true",
                "per_page": 20,
            },
        )
        photos = []
        for item in data.get("hits") or []:
            url = item.get("largeImageURL") or item.get("webformatURL")
            if url:
                who = item.get("user") or "Unknown"
                photos.append(StockPhoto(f"pixabay:{item.get('id')}", url, f"{who} / Pixabay"))
        return photos


class OpenverseImageSource(StockPhotoSource):
    """Openverse 공개 라이선스 이미지 (키 불필요). https://api.openverse.org/

    영상에 출처만 밝히면 쓸 수 있는 라이선스(CC0, 퍼블릭 도메인, CC BY)만 검색합니다.
    키 없이 쓰는 대신 요청 횟수 제한이 있습니다.
    """

    name = "Openverse"
    SEARCH_URL = "https://api.openverse.org/v1/images/"
    LICENSES = "cc0,pdm,by"

    async def _search(self, client: httpx.AsyncClient, query: str) -> list[StockPhoto]:
        data = await self._get_json(
            client,
            self.SEARCH_URL,
            {
                "q": query,
                "license": self.LICENSES,
                "aspect_ratio": "tall",
                "mature": "false",
                "page_size": 20,
            },
        )
        photos = []
        for item in data.get("results") or []:
            url = item.get("url")
            if not url:
                continue
            license_name = f"{item.get('license', '')} {item.get('license_version') or ''}".strip().upper()
            who = item.get("creator") or "Unknown"
            credit = f"{who} / Openverse ({license_name})" if license_name else f"{who} / Openverse"
            photos.append(StockPhoto(f"openverse:{item.get('id')}", url, credit))
        return photos


PexelsError = StockPhotoError  # 이전 이름 호환


class NotConfiguredSource:
    """설정이 빠진 소스. 경고에 무엇을 설정하면 되는지 알려 줍니다."""

    def __init__(self, hint: str) -> None:
        self._hint = hint

    async def generate(self, prompt: str, output_path: Path, query: str = "") -> str | None:
        raise ImageGenerationError(self._hint)


class ImageSourceChain:
    """이미지 소스를 순서대로 시도합니다.

    한도 0·키 오류처럼 다시 해도 안 되는 오류가 난 소스는 cooldown 동안 건너뛰어,
    결제를 켜지 않은 Gemini에 씬마다 헛된 요청을 보내지 않습니다.
    """

    def __init__(self, sources: list[tuple[str, object]], cooldown_sec: float) -> None:
        self._sources = sources
        self._cooldown = cooldown_sec
        self._disabled_until: dict[str, tuple[float, str]] = {}

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self._sources]

    async def generate(self, prompt: str, output_path: Path, query: str = "") -> str | None:
        reasons: list[str] = []
        for name, source in self._sources:
            skipped = self._disabled_until.get(name)
            if skipped and skipped[0] > time.monotonic():
                reasons.append(f"{name}: {skipped[1]}")
                continue
            try:
                return await source.generate(prompt, output_path, query)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = short_reason(exc)
                logger.warning("이미지 소스 %s 실패: %s", name, exc)
                reasons.append(f"{name}: {reason}")
                if not is_retryable(exc) and self._cooldown > 0:
                    self._disabled_until[name] = (time.monotonic() + self._cooldown, reason)
        if not self._sources:
            reasons.append("사용할 이미지 소스가 없습니다")
        raise ImageGenerationError(" / ".join(reasons))


def _make_placeholder(seed_text: str, size: tuple[int, int], output_path: Path) -> None:
    """프롬프트별로 색이 다른 세로 그라디언트 이미지를 만듭니다."""
    width, height = size
    hue = int(hashlib.sha256(seed_text.encode()).hexdigest()[:4], 16) / 0xFFFF
    top = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.55, 0.45))
    bottom = tuple(int(c * 255) for c in colorsys.hsv_to_rgb((hue + 0.12) % 1, 0.7, 0.12))

    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t = y / max(1, height - 1)
        color = tuple(int(a + (b - a) * t) for a, b in zip(top, bottom, strict=True))
        draw.line([(0, y), (width, y)], fill=color)
    img.save(output_path, format="PNG")


async def make_placeholder(seed_text: str, size: tuple[int, int], output_path: Path) -> None:
    await asyncio.to_thread(_make_placeholder, seed_text, size, output_path)
