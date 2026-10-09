"""완성 영상용 서명된 링크.

`<video>` 태그는 요청에 API 키 헤더를 붙일 수 없으므로, 영상 주소에 만료 시각과
HMAC-SHA256 서명을 넣어 일정 시간 동안만 열리게 합니다.
"""

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode

VIDEO_ROUTE_PREFIX = "/videos"
LIBRARY_ROUTE_PREFIX = "/library-files"


@dataclass(frozen=True)
class SignedURL:
    url: str
    expires_at: datetime


class URLSigner:
    def __init__(self, secret: bytes, ttl_sec: int) -> None:
        if len(secret) < 16:
            raise ValueError("서명 비밀키는 16바이트 이상이어야 합니다.")
        self._secret = secret
        self._ttl = ttl_sec

    def _signature(self, path: str, expires: int) -> str:
        mac = hmac.new(self._secret, f"{path}\n{expires}".encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()

    def sign(self, path: str, *, now: float | None = None) -> SignedURL:
        expires = int(now if now is not None else time.time()) + self._ttl
        query = urlencode({"expires": expires, "sig": self._signature(path, expires)})
        return SignedURL(url=f"{path}?{query}", expires_at=datetime.fromtimestamp(expires, UTC))

    def verify(self, path: str, expires: int, sig: str, *, now: float | None = None) -> bool:
        if expires < (now if now is not None else time.time()):
            return False
        return hmac.compare_digest(self._signature(path, expires), sig)
