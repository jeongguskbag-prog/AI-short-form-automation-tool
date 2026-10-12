"""설정 점검: 키와 외부 서비스가 실제로 동작하는지 하나씩 가볍게 시험해 보고, 안 되면 이유와 해결 방법을 알려 줍니다.

각 점검은 비용이 거의 없는 호출만 씁니다 (모델 정보 조회, 검색 1건, 짧은 음성 1개 등).
"""

import asyncio
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from ..config import Settings
from . import video

OK, WARN, ERROR, SKIPPED = "ok", "warn", "error", "skipped"


@dataclass
class CheckResult:
    key: str
    name: str
    status: str
    message: str
    hint: str = ""


class CheckFailed(Exception):
    """점검 실패. 메시지와 해결 방법(hint)을 그대로 화면에 보여 줍니다."""

    def __init__(self, message: str, hint: str = "", status: str = ERROR) -> None:
        super().__init__(message)
        self.hint = hint
        self.status = status


def _http_reason(r: httpx.Response) -> str:
    try:
        err = r.json().get("error") or {}
        reason = (err.get("errors") or [{}])[0].get("reason", "") if isinstance(err, dict) else ""
    except (ValueError, AttributeError):
        reason = ""
    if reason in ("keyInvalid", "badRequest") and r.status_code == 400:
        return "키가 올바르지 않습니다 (HTTP 400)"
    if r.status_code in (401, 403):
        return f"키가 올바르지 않거나 권한이 없습니다 (HTTP {r.status_code})"
    if r.status_code == 429:
        return "사용량 한도를 넘었습니다 (HTTP 429)"
    return f"HTTP {r.status_code}"


class SetupChecker:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        gemini_check: Callable[[], Awaitable[str]] | None = None,
        tts_check: Callable[[Path], Awaitable[object]] | None = None,
        youtube_status: Callable[[], Awaitable[tuple[bool, bool, str | None]]] | None = None,
        social_status: Callable[[str], Awaitable[tuple[bool, bool, str | None]]] | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._s = settings
        self._transport = transport
        self._gemini_check = gemini_check
        self._tts_check = tts_check
        self._youtube_status = youtube_status
        self._social_status = social_status
        self._timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=httpx.Timeout(self._timeout))

    async def run(self) -> list[dict]:
        checks: list[tuple[str, str, Callable[[], Awaitable[str]]]] = [
            ("ffmpeg", "FFmpeg·한글 폰트", self._ffmpeg),
            ("storage", "저장 공간", self._storage),
            ("gemini", "Gemini (대본·장면 기획)", self._gemini),
            ("tts", "Edge TTS (내레이션)", self._tts),
            ("youtube_data", "YouTube Data API 키 (아이템 찾기)", self._youtube_data),
            ("pexels", "Pexels (무료 사진·영상)", self._pexels),
            ("pixabay", "Pixabay (무료 사진·영상)", self._pixabay),
            ("openverse", "Openverse (키 없는 무료 사진)", self._openverse),
            ("youtube_upload", "유튜브 채널 연결 (배포)", self._youtube_upload),
            ("naver", "네이버 쇼핑 (아이템 찾기)", lambda: self._shop("naver")),
            ("aliexpress", "알리익스프레스 (아이템 찾기)", lambda: self._shop("aliexpress")),
            ("ebay", "이베이 유럽 (아이템 찾기)", lambda: self._shop("ebay")),
            ("instagram", "인스타그램 연결 (배포)", lambda: self._social("instagram")),
            ("facebook", "페이스북 페이지 연결 (배포)", lambda: self._social("facebook")),
            ("threads", "스레드 연결 (배포)", lambda: self._social("threads")),
            ("tiktok", "틱톡 연결 (배포)", lambda: self._social("tiktok")),
        ]

        async def one(key: str, name: str, fn: Callable[[], Awaitable[str]]) -> CheckResult:
            try:
                message = await asyncio.wait_for(fn(), timeout=self._timeout + 5)
                return CheckResult(key, name, OK, message)
            except CheckFailed as exc:
                return CheckResult(key, name, exc.status, str(exc), exc.hint)
            except TimeoutError:
                return CheckResult(
                    key, name, ERROR, "응답이 없습니다 (시간 초과)", "인터넷 연결이나 방화벽을 확인하세요."
                )
            except httpx.HTTPError as exc:
                return CheckResult(key, name, ERROR, f"연결할 수 없습니다 ({type(exc).__name__})",
                                   "인터넷 연결이나 방화벽·프록시를 확인하세요.")
            except Exception as exc:
                if isinstance(exc, OSError) or type(exc).__module__.startswith("aiohttp"):
                    return CheckResult(key, name, ERROR, f"연결할 수 없습니다 ({type(exc).__name__})",
                                       "인터넷 연결이나 방화벽·프록시를 확인하세요.")
                return CheckResult(key, name, ERROR, f"{type(exc).__name__}: {exc}"[:300])

        results = await asyncio.gather(*(one(k, n, f) for k, n, f in checks))
        return [asdict(r) for r in results]

    # ------------------------------------------------------------------ 로컬
    async def _ffmpeg(self) -> str:
        try:
            video.ensure_ffmpeg()
        except RuntimeError as exc:
            raise CheckFailed(
                str(exc), "Docker 이미지를 쓰거나, ffmpeg를 설치하세요 (Termux: pkg install ffmpeg)."
            ) from None
        try:
            font = video.find_korean_font(self._s.font_path)
        except FileNotFoundError as exc:
            raise CheckFailed(str(exc), "fonts-noto-cjk를 설치하거나 FONT_PATH를 지정하세요.") from None
        return f"사용 가능 · 자막 폰트 {font.name}"

    async def _storage(self) -> str:
        base = self._s.output_dir
        base.mkdir(parents=True, exist_ok=True)
        free_gb = shutil.disk_usage(base).free / 1024**3
        if free_gb < 1:
            raise CheckFailed(
                f"남은 공간이 {free_gb:.1f}GB입니다", "영상 하나에 수십 MB가 듭니다. 공간을 비우세요.", WARN
            )
        return f"남은 공간 {free_gb:.1f}GB"

    # ------------------------------------------------------------------ 필수 서비스
    async def _gemini(self) -> str:
        if not self._s.gemini_api_key:
            raise CheckFailed(
                "GEMINI_API_KEY가 없습니다", "https://aistudio.google.com/apikey 에서 키를 받아 위 '키 넣기'에 넣으세요."
            )
        if self._gemini_check is None:
            raise CheckFailed("점검할 수 없습니다", status=SKIPPED)
        return await self._gemini_check()

    async def _tts(self) -> str:
        if self._tts_check is None:
            raise CheckFailed("점검할 수 없습니다", status=SKIPPED)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "check.mp3"
            await self._tts_check(out)
            if not out.is_file() or out.stat().st_size == 0:
                raise CheckFailed(
                    "음성 파일이 만들어지지 않았습니다", "인터넷 연결을 확인하세요 (Edge TTS는 온라인 서비스)."
                )
        return f"목소리 {self._s.tts_voice}로 음성을 만들 수 있습니다"

    # ------------------------------------------------------------------ 선택 서비스
    async def _youtube_data(self) -> str:
        key = self._s.youtube_api_key
        if not key:
            raise CheckFailed("YOUTUBE_API_KEY가 없어 아이템 찾기를 쓸 수 없습니다",
                        "Google Cloud에서 YouTube Data API v3를 켜고 API 키를 만들어 위 '키 넣기'에 넣으세요.", WARN)
        async with self._client() as c:
            r = await c.get("https://www.googleapis.com/youtube/v3/videoCategories",
                            params={"part": "snippet", "regionCode": self._s.trends_region, "key": key})
        if r.status_code != 200:
            raise CheckFailed(_http_reason(r), "키가 맞는지, YouTube Data API v3가 사용 설정됐는지 확인하세요.")
        return "키가 동작합니다"

    async def _pexels(self) -> str:
        key = self._s.pexels_api_key
        if not key:
            raise CheckFailed(
                "키가 없어 건너뜁니다 (선택)", "https://www.pexels.com/api/ 에서 무료 키를 받을 수 있어요.", SKIPPED
            )
        async with self._client() as c:
            r = await c.get("https://api.pexels.com/v1/search", params={"query": "kitchen", "per_page": 1},
                            headers={"Authorization": key})
        if r.status_code != 200:
            raise CheckFailed(_http_reason(r), "PEXELS_API_KEY를 확인하세요.")
        return "키가 동작합니다"

    async def _pixabay(self) -> str:
        key = self._s.pixabay_api_key
        if not key:
            raise CheckFailed(
                "키가 없어 건너뜁니다 (선택)",
                "https://pixabay.com/api/docs/ 에서 가입하면 바로 받을 수 있어요.",
                SKIPPED,
            )
        async with self._client() as c:
            r = await c.get("https://pixabay.com/api/", params={"key": key, "q": "kitchen", "per_page": 3})
        if r.status_code != 200:
            raise CheckFailed(_http_reason(r), "PIXABAY_API_KEY를 확인하세요.")
        return "키가 동작합니다"

    async def _openverse(self) -> str:
        if "openverse" not in self._s.image_sources.lower():
            raise CheckFailed("IMAGE_SOURCES에 없어 쓰지 않습니다", status=SKIPPED)
        async with self._client() as c:
            r = await c.get("https://api.openverse.org/v1/images/", params={"q": "kitchen", "page_size": 1},
                            headers={"User-Agent": "ai-shortform-video/1.0"})
        if r.status_code != 200:
            raise CheckFailed(
                _http_reason(r), "잠시 뒤 다시 시도하세요. 키 없이 쓰는 대신 요청 수 제한이 있습니다.", WARN
            )
        return "연결됩니다"

    async def _youtube_upload(self) -> str:
        if self._youtube_status is None:
            raise CheckFailed("점검할 수 없습니다", status=SKIPPED)
        configured, connected, name = await self._youtube_status()
        if not configured:
            raise CheckFailed("OAuth 클라이언트가 없어 유튜브에 올릴 수 없습니다 (선택)",
                        "README의 '배포 (유튜브)'를 따라 OAuth 클라이언트를 만들어 위 '키 넣기'에 넣으세요.", SKIPPED)
        if not connected:
            raise CheckFailed("채널이 연결되지 않았습니다", "보관함에서 '유튜브 채널 연결'을 누르세요.", WARN)
        return f"채널 '{name}'에 올릴 수 있습니다"

    # ------------------------------------------------------------------ 쇼핑몰·SNS (선택)
    async def _shop(self, name: str) -> str:
        from .products import AliExpress, Ebay, NaverShopping, ProductSourceError

        source = {"naver": NaverShopping, "aliexpress": AliExpress, "ebay": Ebay}[name](self._s, self._transport)
        if not source.configured:
            raise CheckFailed("키가 없어 건너뜁니다 (선택)", "아이템 찾기 → 쇼핑몰 상품 화면의 안내를 따라 키를 받으세요.", SKIPPED)
        try:
            await source.probe()
        except ProductSourceError as exc:
            hints = {
                "naver": "네이버 개발자센터 앱의 사용 API에 '검색'이 있는지, Client ID·Secret이 맞는지 확인하세요.",
                "aliexpress": "App Key·Secret과 제휴(Affiliate) API 권한을 확인하세요.",
                "ebay": "Production 키(App ID·Cert ID)인지 확인하세요 (Sandbox 키는 안 됩니다).",
            }
            raise CheckFailed(str(exc), hints[name]) from None
        return "키가 동작합니다"

    async def _social(self, platform: str) -> str:
        if self._social_status is None:
            raise CheckFailed("점검할 수 없습니다", status=SKIPPED)
        configured, connected, name = await self._social_status(platform)
        how = {
            "instagram": "README의 'SNS에 올리기 → 인스타그램·페이스북'을 따라 META_APP_ID·SECRET을 넣으세요.",
            "facebook": "README의 'SNS에 올리기 → 인스타그램·페이스북'을 따라 META_APP_ID·SECRET을 넣으세요.",
            "threads": "README의 'SNS에 올리기 → 스레드'를 따라 토큰을 받으세요.",
            "tiktok": "README의 'SNS에 올리기 → 틱톡'을 따라 TIKTOK_CLIENT_KEY·SECRET을 넣으세요.",
        }[platform]
        if not configured:
            raise CheckFailed("앱 키가 없어 건너뜁니다 (선택)", how, SKIPPED)
        if not connected:
            raise CheckFailed("계정이 연결되지 않았습니다", "보관함에서 영상을 열고 'SNS에 올리기'의 연결 버튼을 누르세요.", WARN)
        return f"{name}에 올릴 수 있습니다"
