"""틱톡 올리기 (Login Kit for Desktop + Content Posting API 'Direct Post').

틱톡은 웹 앱의 리디렉션 주소로 https만 받지만, 데스크톱 앱은 http://localhost:포트 주소를 허용하는 대신 PKCE가 필요합니다.
그래서 틱톡 개발자 앱의 플랫폼을 'Desktop'으로 만들고 리디렉션 주소에 TIKTOK_REDIRECT_URI를 넣습니다.
틱톡의 앱 심사(audit)를 통과하기 전에는 '나만 보기(SELF_ONLY)'로만 올릴 수 있습니다.
"""

import asyncio
import hashlib
import secrets
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlencode

import httpx

from ..config import Settings

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
API = "https://open.tiktokapis.com/v2"
SCOPES = "user.info.basic,video.publish"
PRIVACY_LABELS = {
    "SELF_ONLY": "나만 보기", "MUTUAL_FOLLOW_FRIENDS": "맞팔 친구", "FOLLOWER_OF_CREATOR": "팔로워",
    "PUBLIC_TO_EVERYONE": "전체 공개",
}
# 화면의 공개 범위(유튜브 기준)를 틱톡 공개 범위로
PRIVACY_MAP = {"private": "SELF_ONLY", "unlisted": "MUTUAL_FOLLOW_FRIENDS", "public": "PUBLIC_TO_EVERYONE"}
SINGLE_CHUNK_MAX = 64 * 1024 * 1024
CHUNK = 10 * 1024 * 1024


class TikTokError(RuntimeError):
    pass


def _error_from(response: httpx.Response, what: str) -> TikTokError:
    code, message = "", ""
    try:
        body = response.json()
        err = body.get("error")
        if isinstance(err, dict):
            code, message = err.get("code", ""), err.get("message", "")
        else:
            code, message = err or "", body.get("error_description", "")
    except (ValueError, AttributeError):
        pass
    hints = {
        "access_token_invalid": "틱톡 연결이 만료됐습니다. 보관함에서 다시 연결해 주세요.",
        "invalid_grant": "틱톡 연결이 만료됐습니다. 보관함에서 다시 연결해 주세요.",
        "scope_not_authorized": "video.publish 권한이 없습니다. 틱톡 앱에 Content Posting API(Direct Post)를 추가했는지 확인하세요.",
        "unaudited_client_can_only_post_to_private_accounts":
            "심사 전 틱톡 앱은 비공개 계정에만 올릴 수 있습니다. 틱톡 계정을 비공개로 바꾸거나 앱 심사를 받으세요.",
        "spam_risk_too_many_posts": "오늘 올릴 수 있는 횟수를 넘었습니다. 내일 다시 하세요.",
    }
    if code in hints:
        return TikTokError(hints[code])
    return TikTokError(f"{what} 실패 (HTTP {response.status_code})" + (f": {code} {message}".rstrip() if code else ""))


def _ok(response: httpx.Response) -> bool:
    if response.status_code != 200:
        return False
    err = (response.json() or {}).get("error")
    return not isinstance(err, dict) or err.get("code") in ("ok", "", None)


class TikTokClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, clock: Callable[[], float] = time.time,
                 poll_interval: float = 5.0, poll_timeout: float = 600.0) -> None:
        self._s = settings
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout
        self._states: dict[str, tuple[float, str]] = {}  # state → (만료 시각, code_verifier)

    @property
    def configured(self) -> bool:
        return bool(self._s.tiktok_client_key and self._s.tiktok_client_secret)

    def _client(self, timeout: float = 30) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=httpx.Timeout(timeout))

    # ------------------------------------------------------------------ 연결
    def auth_url(self) -> str:
        now = self._clock()
        self._states = {k: v for k, v in self._states.items() if v[0] > now}
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)[:64]
        self._states[state] = (now + 600, verifier)
        # 틱톡 데스크톱 PKCE는 code_challenge를 SHA256의 16진수 문자열로 받습니다.
        challenge = hashlib.sha256(verifier.encode()).hexdigest()
        return AUTH_URL + "?" + urlencode({
            "client_key": self._s.tiktok_client_key, "response_type": "code", "scope": SCOPES,
            "redirect_uri": self._s.tiktok_redirect_uri, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })

    def consume_state(self, state: str) -> str | None:
        """state가 맞으면 code_verifier를 돌려줍니다."""
        expires, verifier = self._states.pop(state, (0.0, ""))
        return verifier if expires > self._clock() else None

    def _secret_from(self, body: dict) -> dict:
        if "access_token" not in body:
            raise TikTokError(f"틱톡 인증 실패: {body.get('error_description') or body.get('error') or '토큰 없음'}")
        now = self._clock()
        return {
            "access_token": body["access_token"], "refresh_token": body.get("refresh_token", ""),
            "open_id": body.get("open_id", ""),
            "expires_at": now + float(body.get("expires_in") or 86400),
            "refresh_expires_at": now + float(body.get("refresh_expires_in") or 365 * 86400),
        }

    async def exchange_code(self, code: str, verifier: str) -> dict:
        async with self._client() as c:
            r = await c.post(TOKEN_URL, data={
                "client_key": self._s.tiktok_client_key, "client_secret": self._s.tiktok_client_secret,
                "code": code, "grant_type": "authorization_code", "redirect_uri": self._s.tiktok_redirect_uri,
                "code_verifier": verifier,
            })
        if r.status_code != 200:
            raise _error_from(r, "틱톡 연결")
        return self._secret_from(r.json())

    async def fresh_secret(self, secret: dict) -> dict | None:
        """접근 토큰이 5분 안에 만료되면 새로 받은 비밀 정보를, 아니면 None."""
        if secret.get("expires_at", 0) - self._clock() > 300:
            return None
        async with self._client() as c:
            r = await c.post(TOKEN_URL, data={
                "client_key": self._s.tiktok_client_key, "client_secret": self._s.tiktok_client_secret,
                "grant_type": "refresh_token", "refresh_token": secret["refresh_token"],
            })
        if r.status_code != 200:
            raise _error_from(r, "틱톡 인증")
        return {**secret, **self._secret_from(r.json())}

    async def display_name(self, secret: dict) -> str:
        async with self._client() as c:
            r = await c.get(f"{API}/user/info/", params={"fields": "display_name"},
                            headers={"Authorization": f"Bearer {secret['access_token']}"})
        if not _ok(r):
            raise _error_from(r, "틱톡 계정 정보")
        return ((r.json().get("data") or {}).get("user") or {}).get("display_name", "") or "틱톡 계정"

    # ------------------------------------------------------------------ 올리기
    @staticmethod
    def chunks(size: int) -> tuple[int, int]:
        """(chunk_size, total_chunk_count). 64MB 이하는 한 번에, 넘으면 10MB씩 (마지막 조각이 나머지를 포함)."""
        if size <= SINGLE_CHUNK_MAX:
            return size, 1
        return CHUNK, size // CHUNK

    async def publish(self, secret: dict, video_path: Path, title: str, privacy: str = "private") -> tuple[str, str]:
        headers = {"Authorization": f"Bearer {secret['access_token']}", "Content-Type": "application/json; charset=UTF-8"}
        size = video_path.stat().st_size
        chunk_size, total = self.chunks(size)
        async with self._client(timeout=600) as c:
            r = await c.post(f"{API}/post/publish/creator_info/query/", headers=headers)
            if not _ok(r):
                raise _error_from(r, "틱톡 계정 확인")
            info = r.json().get("data") or {}
            options = info.get("privacy_level_options") or ["SELF_ONLY"]
            wanted = PRIVACY_MAP.get(privacy, "SELF_ONLY")
            level = wanted if wanted in options else ("SELF_ONLY" if "SELF_ONLY" in options else options[0])
            r = await c.post(f"{API}/post/publish/video/init/", headers=headers, json={
                "post_info": {
                    "title": title[:2200], "privacy_level": level, "disable_comment": False,
                    "disable_duet": False, "disable_stitch": False, "is_aigc": self._s.youtube_declare_synthetic,
                },
                "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk_size,
                                "total_chunk_count": total},
            })
            if not _ok(r):
                raise _error_from(r, "틱톡 업로드 준비")
            data = r.json()["data"]
            publish_id, upload_url = data["publish_id"], data["upload_url"]
            with video_path.open("rb") as f:
                for i in range(total):
                    start = i * chunk_size
                    end = size - 1 if i == total - 1 else start + chunk_size - 1
                    f.seek(start)
                    body = f.read(end - start + 1)
                    r = await c.put(upload_url, content=body, headers={
                        "Content-Type": "video/mp4", "Content-Length": str(len(body)),
                        "Content-Range": f"bytes {start}-{end}/{size}",
                    })
                    if r.status_code not in (200, 201, 206):
                        raise TikTokError(f"틱톡 영상 전송 실패 (HTTP {r.status_code})")
            waited = 0.0
            while True:
                r = await c.post(f"{API}/post/publish/status/fetch/", headers=headers, json={"publish_id": publish_id})
                if not _ok(r):
                    raise _error_from(r, "틱톡 처리 확인")
                status = r.json().get("data") or {}
                if status.get("status") == "PUBLISH_COMPLETE":
                    ids = status.get("publicaly_available_post_id") or status.get("publicly_available_post_id") or []
                    post_id = str(ids[0]) if ids else publish_id
                    url = f"https://www.tiktok.com/video/{ids[0]}" if ids else "https://www.tiktok.com/"
                    return post_id, url
                if status.get("status") == "FAILED":
                    raise TikTokError(f"틱톡이 영상을 올리지 못했습니다: {status.get('fail_reason') or '알 수 없는 이유'}")
                if waited >= self._poll_timeout:
                    raise TikTokError("틱톡 처리가 너무 오래 걸립니다. 잠시 뒤 틱톡 앱에서 확인하세요.")
                await self._sleep(self._poll_interval)
                waited += self._poll_interval
