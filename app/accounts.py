"""연결한 플랫폼 계정(OAuth 토큰) 저장.

토큰은 Fernet(AES)으로 암호화해 `accounts_path`(JSON)에 둡니다. 키는 TOKEN_ENCRYPTION_KEY를 쓰고,
비어 있으면 처음 한 번 만들어 `<accounts_path>.key` 파일(본인만 읽기)에 저장합니다.
키 파일과 계정 파일이 같은 곳에 있으면 디스크를 통째로 가져간 사람에게는 소용이 없으니,
서버를 여러 사람이 쓰거나 백업을 밖에 둘 때는 TOKEN_ENCRYPTION_KEY를 따로 보관하세요.
"""

import json
import os
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class AccountStore:
    def __init__(self, path: Path, key: str = "") -> None:
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(key.encode() if key else self._load_or_create_key(path.with_suffix(".key")))

    @staticmethod
    def _load_or_create_key(key_path: Path) -> bytes:
        if key_path.is_file():
            return key_path.read_bytes().strip()
        key = Fernet.generate_key()
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        return key

    def _read(self) -> dict:
        if not self._path.is_file():
            return {}
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _write(self, data: dict) -> None:
        tmp = self._path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        tmp.replace(self._path)

    def save(self, platform: str, *, display_name: str, secret: dict) -> None:
        """secret(토큰 등)은 암호화하고, display_name(채널 이름 등)은 화면 표시용으로 그대로 둡니다."""
        token = self._fernet.encrypt(json.dumps(secret).encode()).decode()
        with self._lock:
            data = self._read()
            data[platform] = {"display_name": display_name, "secret": token}
            self._write(data)

    def get(self, platform: str) -> tuple[str, dict] | None:
        """(표시 이름, 비밀 정보). 없거나 키가 바뀌어 풀 수 없으면 None."""
        with self._lock:
            entry = self._read().get(platform)
        if not entry:
            return None
        try:
            secret = json.loads(self._fernet.decrypt(entry["secret"].encode()))
        except (InvalidToken, KeyError, ValueError):
            return None
        return entry.get("display_name", ""), secret

    def delete(self, platform: str) -> bool:
        with self._lock:
            data = self._read()
            existed = data.pop(platform, None) is not None
            if existed:
                self._write(data)
        return existed
