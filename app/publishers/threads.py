"""스레드 올리기 (Threads API).

연결: Meta 개발자 앱의 'Threads API' 사용 사례 → 설정의 '사용자 토큰 생성기'에서 받은 토큰을 붙여 넣습니다.
단기 토큰이면 스레드 앱 시크릿으로 장기 토큰(60일)으로 바꾸고, 올릴 때마다 만료가 가까우면 자동으로 연장합니다.

스레드는 공개 인터넷 주소에 있는 영상만 가져가므로, 올릴 때 영상을 임시 파일 저장소(기본 tmpfiles.org, 약 1시간 뒤 삭제)에
잠깐 올리고 그 주소를 넘깁니다.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from ..config import Settings

GRAPH = "https://graph.threads.net/v1.0"
TOKEN_BASE = "https://graph.threads.net"
REFRESH_BEFORE_SEC = 7 * 86400
TEXT_LIMIT = 500


class ThreadsError(RuntimeError):
    pass


def _error_from(response: httpx.Response, what: str) -> ThreadsError:
    message = ""
    try:
        err = response.json().get("error") or {}
        message = err.get("error_user_msg") or err.get("message") or ""
        if err.get("code") == 190:
            return ThreadsError(f"{what} 실패: 토큰이 만료됐거나 잘못됐습니다. 보관함에서 스레드를 다시 연결해 주세요.")
    except (ValueError, AttributeError):
        pass
    return ThreadsError(f"{what} 실패 (HTTP {response.status_code})" + (f": {message}" if message else ""))


class ThreadsClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, clock: Callable[[], float] = time.time,
                 poll_interval: float = 10.0, poll_timeout: float = 600.0) -> None:
        self._s = settings
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout

    @property
    def configured(self) -> bool:
        return True  # 토큰만 있으면 됩니다 (시크릿은 단기 토큰을 장기로 바꿀 때만 필요)

    def _client(self, timeout: float = 30) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=httpx.Timeout(timeout))

    # ------------------------------------------------------------------ 연결·토큰
    async def connect(self, token: str) -> tuple[str, dict]:
        token = token.strip()
        expires_in = None
        async with self._client() as c:
            if self._s.threads_app_secret:
                r = await c.get(f"{TOKEN_BASE}/access_token", params={
                    "grant_type": "th_exchange_token", "client_secret": self._s.threads_app_secret,
                    "access_token": token,
                })
                if r.status_code == 200:  # 이미 장기 토큰이면 실패하므로 그대로 씁니다
                    body = r.json()
                    token, expires_in = body["access_token"], body.get("expires_in")
            r = await c.get(f"{GRAPH}/me", params={"fields": "id,username", "access_token": token})
            if r.status_code != 200:
                raise _error_from(r, "스레드 연결")
        me = r.json()
        secret = {"user_id": me["id"], "username": me.get("username", ""), "token": token,
                  "expires_at": self._clock() + float(expires_in or 60 * 86400)}
        return f"@{secret['username']}", secret

    async def fresh_secret(self, secret: dict) -> dict | None:
        """만료 7일 전이면 연장한 새 비밀 정보를, 아니면 None을 돌려줍니다."""
        if secret.get("expires_at", 0) - self._clock() > REFRESH_BEFORE_SEC:
            return None
        async with self._client() as c:
            r = await c.get(f"{TOKEN_BASE}/refresh_access_token",
                            params={"grant_type": "th_refresh_token", "access_token": secret["token"]})
        if r.status_code != 200:
            raise _error_from(r, "스레드 토큰 연장")
        body = r.json()
        return {**secret, "token": body["access_token"],
                "expires_at": self._clock() + float(body.get("expires_in") or 60 * 86400)}

    async def check(self, secret: dict) -> None:
        async with self._client() as c:
            r = await c.get(f"{GRAPH}/me", params={"fields": "id", "access_token": secret["token"]})
        if r.status_code != 200:
            raise _error_from(r, "연결 확인")

    # ------------------------------------------------------------------ 올리기
    async def _public_url(self, c: httpx.AsyncClient, video_path: Path) -> str:
        with video_path.open("rb") as f:
            r = await c.post(self._s.threads_temp_upload_url, files={"file": (video_path.name, f.read(), "video/mp4")})
        try:
            url = r.json()["data"]["url"]
        except (ValueError, KeyError, TypeError):
            raise ThreadsError(f"영상을 임시 저장소에 올리지 못했습니다 (HTTP {r.status_code}). 잠시 뒤 다시 하세요.") from None
        # tmpfiles.org는 보기 페이지 주소를 주므로 파일을 바로 받는 /dl/ 주소로 바꿉니다.
        url = url.replace("http://", "https://", 1)
        if "tmpfiles.org/" in url and "/dl/" not in url:
            url = url.replace("tmpfiles.org/", "tmpfiles.org/dl/", 1)
        return url

    async def publish(self, secret: dict, video_path: Path, text: str) -> tuple[str, str]:
        uid, token = secret["user_id"], secret["token"]
        async with self._client(timeout=600) as c:
            video_url = await self._public_url(c, video_path)
            r = await c.post(f"{GRAPH}/{uid}/threads", data={
                "media_type": "VIDEO", "video_url": video_url, "text": text[:TEXT_LIMIT], "access_token": token,
            })
            if r.status_code != 200:
                raise _error_from(r, "스레드 업로드 준비")
            container = r.json()["id"]
            waited = 0.0
            while True:
                r = await c.get(f"{GRAPH}/{container}", params={"fields": "status,error_message", "access_token": token})
                if r.status_code != 200:
                    raise _error_from(r, "스레드 처리 확인")
                body = r.json()
                if body.get("status") in ("FINISHED", "PUBLISHED"):
                    break
                if body.get("status") in ("ERROR", "EXPIRED"):
                    raise ThreadsError(f"스레드가 영상을 처리하지 못했습니다: {body.get('error_message') or body.get('status')}")
                if waited >= self._poll_timeout:
                    raise ThreadsError("스레드 영상 처리가 너무 오래 걸립니다. 잠시 뒤 스레드 앱에서 확인하세요.")
                await self._sleep(self._poll_interval)
                waited += self._poll_interval
            r = await c.post(f"{GRAPH}/{uid}/threads_publish", data={"creation_id": container, "access_token": token})
            if r.status_code != 200:
                raise _error_from(r, "스레드 게시")
            post_id = r.json()["id"]
            r = await c.get(f"{GRAPH}/{post_id}", params={"fields": "permalink", "access_token": token})
            url = r.json().get("permalink", "") if r.status_code == 200 else ""
        return post_id, url or f"https://www.threads.net/@{secret.get('username', '')}"
