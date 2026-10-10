"""유튜브 업로드 (YouTube Data API v3, OAuth 2.0).

본인 Google Cloud 프로젝트에서 만든 OAuth 클라이언트(웹 애플리케이션)로 연결합니다. 구글의 앱 심사 전에는
API로 올린 영상이 비공개로만 올라가므로 기본 공개 범위는 private입니다. 업로드 1건에 일일 기본 할당량
10,000 중 1,600을 씁니다 (하루 약 6건).
"""

import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx

from ..config import Settings

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
PRIVACY = ("private", "unlisted", "public")
CATEGORY_HOWTO_STYLE = "26"


class YouTubeError(RuntimeError):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


_REASONS = {
    "quotaExceeded": "유튜브 업로드 일일 할당량을 다 썼습니다 (기본 하루 약 6건). 한국 시간 오후 4~5시에 초기화됩니다.",
    "uploadLimitExceeded": "이 채널의 업로드 한도를 넘었습니다. 잠시 뒤 다시 시도하세요.",
    "invalid_grant": "유튜브 연결이 만료됐거나 해제됐습니다. 다시 연결해 주세요.",
    "forbidden": "이 계정으로 업로드할 권한이 없습니다.",
}


def _error_from(response: httpx.Response, what: str) -> YouTubeError:
    reason = ""
    try:
        body = response.json()
        err = body.get("error")
        if isinstance(err, dict):
            reason = (err.get("errors") or [{}])[0].get("reason", "") or err.get("status", "")
        elif isinstance(err, str):  # 토큰 엔드포인트 형식
            reason = err
    except ValueError:
        pass
    return YouTubeError(_REASONS.get(reason, f"{what} 실패 (HTTP {response.status_code} {reason})".strip()),
                        response.status_code)


@dataclass
class UploadRequest:
    video_path: Path
    title: str
    description: str
    tags: list[str]
    privacy: str = "private"


class YouTubeClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._s = settings
        self._transport = transport  # 테스트에서 가짜 응답을 넣을 때 사용
        self._states: dict[str, float] = {}  # OAuth state → 만료 시각

    @property
    def configured(self) -> bool:
        return bool(self._s.youtube_oauth_client_id and self._s.youtube_oauth_client_secret)

    def _client(self, timeout: float = 30) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=httpx.Timeout(timeout))

    # ------------------------------------------------------------------ 연결
    def auth_url(self) -> str:
        now = time.time()
        self._states = {k: v for k, v in self._states.items() if v > now}
        state = secrets.token_urlsafe(24)
        self._states[state] = now + 600
        return AUTH_URL + "?" + urlencode({
            "client_id": self._s.youtube_oauth_client_id,
            "redirect_uri": self._s.youtube_redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "access_type": "offline",
            "prompt": "consent",  # 매번 refresh_token을 받기 위해
            "include_granted_scopes": "true",
            "state": state,
        })

    def consume_state(self, state: str) -> bool:
        expires = self._states.pop(state, 0)
        return expires > time.time()

    async def exchange_code(self, code: str) -> dict:
        async with self._client() as c:
            r = await c.post(TOKEN_URL, data={
                "code": code,
                "client_id": self._s.youtube_oauth_client_id,
                "client_secret": self._s.youtube_oauth_client_secret,
                "redirect_uri": self._s.youtube_redirect_uri,
                "grant_type": "authorization_code",
            })
        if r.status_code != 200:
            raise _error_from(r, "유튜브 연결")
        tokens = r.json()
        if not tokens.get("refresh_token"):
            raise YouTubeError(
                "구글이 장기 토큰을 주지 않았습니다. 구글 계정의 '타사 앱 연결'에서 이 앱을 지운 뒤 다시 연결하세요."
            )
        return tokens

    async def access_token(self, refresh_token: str) -> str:
        async with self._client() as c:
            r = await c.post(TOKEN_URL, data={
                "refresh_token": refresh_token,
                "client_id": self._s.youtube_oauth_client_id,
                "client_secret": self._s.youtube_oauth_client_secret,
                "grant_type": "refresh_token",
            })
        if r.status_code != 200:
            raise _error_from(r, "유튜브 인증")
        return r.json()["access_token"]

    async def channel_title(self, access_token: str) -> str:
        async with self._client() as c:
            r = await c.get(f"{API}/channels", params={"part": "snippet", "mine": "true"},
                            headers={"Authorization": f"Bearer {access_token}"})
        if r.status_code != 200:
            raise _error_from(r, "채널 정보 조회")
        items = r.json().get("items") or []
        if not items:
            raise YouTubeError("이 구글 계정에는 유튜브 채널이 없습니다. 유튜브에서 채널을 먼저 만드세요.")
        return items[0]["snippet"]["title"]

    async def video_stats(self, refresh_token: str, video_ids: list[str]) -> dict[str, dict]:
        """영상 ID → {"views", "likes", "comments"}. 비공개 영상도 본인 채널이면 받을 수 있습니다.
        지워진 영상은 결과에서 빠집니다. 50개씩 묶어 부르며 1회에 할당량 1을 씁니다."""
        if not video_ids:
            return {}
        token = await self.access_token(refresh_token)
        result: dict[str, dict] = {}
        async with self._client() as c:
            for start in range(0, len(video_ids), 50):
                r = await c.get(
                    f"{API}/videos",
                    params={"part": "statistics", "id": ",".join(video_ids[start:start + 50]), "maxResults": 50},
                    headers={"Authorization": f"Bearer {token}"},
                )
                if r.status_code != 200:
                    raise _error_from(r, "성과 조회")
                for item in r.json().get("items") or []:
                    s = item.get("statistics") or {}
                    result[item["id"]] = {
                        key: int(s[src]) if s.get(src) is not None else None
                        for key, src in (("views", "viewCount"), ("likes", "likeCount"), ("comments", "commentCount"))
                    }
        return result

    # ------------------------------------------------------------------ 업로드
    def metadata(self, req: UploadRequest) -> dict:
        title = " ".join(req.title.split())[:100] or "Shorts"
        tags = [t.lstrip("#") for t in req.tags if t.strip("#")][:15]
        description = req.description.strip()
        if "#shorts" not in description.lower():
            description = f"{description}\n#Shorts".strip()
        status = {
            "privacyStatus": req.privacy if req.privacy in PRIVACY else "private",
            "selfDeclaredMadeForKids": False,
        }
        if self._s.youtube_declare_synthetic:
            status["containsSyntheticMedia"] = True  # AI 음성·이미지 사용 표시
        return {
            "snippet": {"title": title, "description": description[:5000], "tags": tags,
                        "categoryId": CATEGORY_HOWTO_STYLE},
            "status": status,
        }

    async def upload(self, refresh_token: str, req: UploadRequest) -> str:
        """영상을 올리고 영상 ID를 돌려줍니다 (재개 가능 업로드, 한 번에 전송)."""
        token = await self.access_token(refresh_token)
        size = req.video_path.stat().st_size
        async with self._client(timeout=600) as c:
            start = await c.post(
                UPLOAD_URL,
                params={"uploadType": "resumable", "part": "snippet,status"},
                json=self.metadata(req),
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Upload-Content-Type": "video/mp4",
                    "X-Upload-Content-Length": str(size),
                },
            )
            if start.status_code != 200 or "location" not in start.headers:
                raise _error_from(start, "업로드 준비")
            with req.video_path.open("rb") as f:
                done = await c.put(start.headers["location"], content=f.read(),
                                   headers={"Content-Type": "video/mp4"})
            if done.status_code not in (200, 201):
                raise _error_from(done, "영상 전송")
        return done.json()["id"]
