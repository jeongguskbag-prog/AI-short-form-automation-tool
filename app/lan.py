"""폰 접속 모드: 같은 와이파이의 폰·태블릿에서 이 PC의 화면을 열 수 있게 합니다.

`python main.py --phone`(또는 .env의 PHONE_ACCESS=true로 실행 파일)로 켜면 모든 네트워크 주소에서 요청을 받고,
다른 기기는 접속 키(API_KEYS)가 있어야 쓸 수 있습니다. 키가 없으면 하나 만들어 .env에 저장합니다.
"""

import secrets
import socket
from pathlib import Path


def lan_ip() -> str | None:
    """이 PC의 와이파이·랜 주소 (예: 192.168.0.12). 인터넷에 실제로 보내지는 않고 경로만 확인합니다."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("127.") else ip


# 폰에서 손으로 치기 쉽게 헷갈리는 글자(0/O, 1/l/I)를 뺀 소문자·숫자로 만듭니다.
_KEY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def _new_key(length: int = 10) -> str:
    return "".join(secrets.choice(_KEY_ALPHABET) for _ in range(length))


def _read(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else []


def ensure_access_key(env_path: Path) -> str:
    """API_KEYS의 첫 키를 돌려줍니다. 없으면 새로 만들어 .env에 저장합니다."""
    lines = _read(env_path)
    for i, line in enumerate(lines):
        key, sep, value = line.partition("=")
        if sep and key.strip() == "API_KEYS":
            value = value.split(" #", 1)[0].strip().strip('"').strip("'")
            first = next((k.strip() for k in value.split(",") if k.strip()), "")
            if first:
                return first
            new = _new_key()
            lines[i] = f"API_KEYS={new}"
            break
    else:
        new = _new_key()
        lines.append(f"API_KEYS={new}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return new


def enable_phone_mode(env_path: Path) -> tuple[str, str | None]:
    """폰 접속 모드에 필요한 키를 준비하고 (접속 키, 폰에서 열 주소의 IP)를 돌려줍니다."""
    import os

    key = ensure_access_key(env_path)
    os.environ["PHONE_ACCESS"] = "true"
    return key, lan_ip()


def phone_banner(key: str, ip: str | None, port: int) -> str:
    where = f"http://{ip}:{port}" if ip else f"http://(이 PC의 IP 주소):{port}"
    return "\n".join([
        "",
        " ─────────────── 폰에서 열기 ───────────────",
        "  1) 폰을 이 PC와 같은 와이파이에 연결",
        f"  2) 폰 브라우저 주소창에: {where}",
        f"  3) 접속 키를 물으면: {key}",
        "  Windows가 '네트워크 접근 허용'을 물으면 '개인 네트워크'에 체크하고 허용하세요.",
        " ─────────────────────────────────────────",
        "",
    ])
