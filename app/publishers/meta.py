"""인스타그램 릴스·페이스북 릴스 올리기 (Meta 그래프 API, Facebook 로그인 방식).

연결: Meta 개발자 앱의 '그래프 API 탐색기'에서 받은 사용자 토큰을 붙여 넣으면, 앱 ID·시크릿으로 장기 토큰으로 바꾼 뒤
그 계정이 관리하는 페이스북 페이지의 페이지 토큰(만료 없음)과 페이지에 연결된 인스타그램 프로페셔널 계정을 찾아 저장합니다.

- 인스타그램: 릴스 컨테이너(upload_type=resumable) → rupload.facebook.com에 영상 전송 → 처리 완료 확인 → 게시
- 페이스북: /{page}/video_reels start → 영상 전송 → finish(PUBLISHED)
"""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from ..config import Settings

RUPLOAD = "https://rupload.facebook.com"
# 그래프 API 탐색기에서 토큰을 만들 때 고를 권한
PERMISSIONS = (
    "pages_show_list", "pages_read_engagement", "pages_manage_posts", "business_management",
    "instagram_basic", "instagram_content_publish",
)


class MetaError(RuntimeError):
    pass


def _error_from(response: httpx.Response, what: str) -> MetaError:
    message = ""
    try:
        err = response.json().get("error") or {}
        message = err.get("error_user_msg") or err.get("message") or ""
        code = err.get("code")
    except (ValueError, AttributeError):
        code = None
    if code == 190:
        return MetaError(f"{what} 실패: 토큰이 만료됐거나 잘못됐습니다. 보관함에서 다시 연결해 주세요.")
    if code in (10, 200) or (code and 200 <= code < 300):
        return MetaError(f"{what} 실패: 권한이 없습니다. 토큰을 만들 때 {', '.join(PERMISSIONS)} 권한을 모두 골랐는지 확인하세요."
                         + (f" ({message})" if message else ""))
    return MetaError(f"{what} 실패 (HTTP {response.status_code})" + (f": {message}" if message else ""))


class MetaClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, poll_interval: float = 5.0,
                 poll_timeout: float = 600.0) -> None:
        self._s = settings
        self._transport = transport
        self._sleep = sleep
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout

    @property
    def configured(self) -> bool:
        return bool(self._s.meta_app_id and self._s.meta_app_secret)

    @property
    def graph(self) -> str:
        return f"https://graph.facebook.com/{self._s.meta_graph_version}"

    def _client(self, timeout: float = 30) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=httpx.Timeout(timeout))

    # ------------------------------------------------------------------ 연결
    async def connect(self, user_token: str) -> tuple[str, dict]:
        """(표시 이름, 저장할 비밀 정보). 페이지가 여러 개면 인스타그램이 연결된 첫 페이지를 씁니다."""
        async with self._client() as c:
            r = await c.get(f"{self.graph}/oauth/access_token", params={
                "grant_type": "fb_exchange_token", "client_id": self._s.meta_app_id,
                "client_secret": self._s.meta_app_secret, "fb_exchange_token": user_token.strip(),
            })
            if r.status_code != 200:
                raise _error_from(r, "장기 토큰 받기")
            long_token = r.json()["access_token"]
            r = await c.get(f"{self.graph}/me/accounts", params={
                "fields": "id,name,access_token,instagram_business_account{id,username}",
                "access_token": long_token, "limit": 100,
            })
            if r.status_code != 200:
                raise _error_from(r, "페이지 목록 조회")
        pages = r.json().get("data") or []
        if not pages:
            raise MetaError("관리하는 페이스북 페이지가 없습니다. 페이스북 페이지를 만들고, 토큰을 만들 때 그 페이지를 선택하세요.")
        page = next((p for p in pages if p.get("instagram_business_account")), pages[0])
        ig = page.get("instagram_business_account") or {}
        secret = {
            "page_id": page["id"], "page_name": page.get("name", ""), "page_token": page["access_token"],
            "ig_user_id": ig.get("id", ""), "ig_username": ig.get("username", ""),
        }
        name = f"페이지 '{secret['page_name']}'" + (f" · 인스타 @{secret['ig_username']}" if secret["ig_user_id"] else "")
        return name, secret

    async def check(self, secret: dict) -> None:
        async with self._client() as c:
            r = await c.get(f"{self.graph}/{secret['page_id']}",
                            params={"fields": "id", "access_token": secret["page_token"]})
        if r.status_code != 200:
            raise _error_from(r, "연결 확인")

    # ------------------------------------------------------------------ 인스타그램 릴스
    async def publish_instagram(self, secret: dict, video_path: Path, caption: str) -> tuple[str, str]:
        ig, token = secret.get("ig_user_id"), secret["page_token"]
        if not ig:
            raise MetaError("페이스북 페이지에 인스타그램 프로페셔널 계정이 연결돼 있지 않습니다. "
                            "인스타그램 앱 → 설정 → 계정 유형에서 프로페셔널 계정으로 바꾸고 페이지와 연결하세요.")
        size = video_path.stat().st_size
        async with self._client(timeout=600) as c:
            r = await c.post(f"{self.graph}/{ig}/media", data={
                "media_type": "REELS", "upload_type": "resumable", "caption": caption[:2200],
                "share_to_feed": "true", "access_token": token,
            })
            if r.status_code != 200:
                raise _error_from(r, "인스타그램 업로드 준비")
            container = r.json()["id"]
            with video_path.open("rb") as f:
                r = await c.post(f"{RUPLOAD}/ig-api-upload/{self._s.meta_graph_version}/{container}", content=f.read(),
                                 headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)})
            if r.status_code != 200:
                raise _error_from(r, "인스타그램 영상 전송")
            await self._wait_container(c, container, token)
            r = await c.post(f"{self.graph}/{ig}/media_publish", data={"creation_id": container, "access_token": token})
            if r.status_code != 200:
                raise _error_from(r, "인스타그램 게시")
            media_id = r.json()["id"]
            r = await c.get(f"{self.graph}/{media_id}", params={"fields": "permalink", "access_token": token})
            url = r.json().get("permalink", "") if r.status_code == 200 else ""
        return media_id, url or "https://www.instagram.com/"

    async def _wait_container(self, c: httpx.AsyncClient, container: str, token: str) -> None:
        waited = 0.0
        while True:
            r = await c.get(f"{self.graph}/{container}", params={"fields": "status_code,status", "access_token": token})
            if r.status_code != 200:
                raise _error_from(r, "인스타그램 처리 확인")
            body = r.json()
            code = body.get("status_code")
            if code in ("FINISHED", "PUBLISHED"):
                return
            if code in ("ERROR", "EXPIRED"):
                raise MetaError(f"인스타그램이 영상을 처리하지 못했습니다: {body.get('status') or code}")
            if waited >= self._poll_timeout:
                raise MetaError("인스타그램 영상 처리가 너무 오래 걸립니다. 잠시 뒤 인스타그램 앱에서 확인하세요.")
            await self._sleep(self._poll_interval)
            waited += self._poll_interval

    # ------------------------------------------------------------------ 페이스북 릴스
    async def publish_facebook(self, secret: dict, video_path: Path, description: str) -> tuple[str, str]:
        page, token = secret["page_id"], secret["page_token"]
        size = video_path.stat().st_size
        async with self._client(timeout=600) as c:
            r = await c.post(f"{self.graph}/{page}/video_reels", data={"upload_phase": "start", "access_token": token})
            if r.status_code != 200:
                raise _error_from(r, "페이스북 업로드 준비")
            body = r.json()
            video_id = body["video_id"]
            upload_url = body.get("upload_url") or f"{RUPLOAD}/video-upload/{self._s.meta_graph_version}/{video_id}"
            with video_path.open("rb") as f:
                r = await c.post(upload_url, content=f.read(),
                                 headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)})
            if r.status_code != 200:
                raise _error_from(r, "페이스북 영상 전송")
            r = await c.post(f"{self.graph}/{page}/video_reels", data={
                "upload_phase": "finish", "video_id": video_id, "video_state": "PUBLISHED",
                "description": description[:2200], "access_token": token,
            })
            if r.status_code != 200 or not r.json().get("success", True):
                raise _error_from(r, "페이스북 게시")
        return video_id, f"https://www.facebook.com/reel/{video_id}"
