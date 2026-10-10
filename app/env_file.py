""".env 파일의 키 값을 화면에서 넣고 고칠 수 있게 읽고 씁니다.

키 값 전체는 밖으로 내보내지 않고, 넣었는지와 끝 4자리만 알려 줍니다.
고친 줄만 바꾸고 나머지 줄(설명, 다른 설정)은 그대로 둡니다.
"""

import os
import re
from pathlib import Path

# 화면에서 고칠 수 있는 키와 설명. 순서대로 화면에 나옵니다.
EDITABLE_KEYS: dict[str, str] = {
    "GEMINI_API_KEY": "Gemini (필수 · 대본·장면 기획)",
    "YOUTUBE_API_KEY": "YouTube Data API 키 (아이템 찾기)",
    "PEXELS_API_KEY": "Pexels (무료 스톡 영상·사진)",
    "PIXABAY_API_KEY": "Pixabay (무료 스톡 영상·사진)",
    "YOUTUBE_OAUTH_CLIENT_ID": "유튜브 올리기 · OAuth 클라이언트 ID",
    "YOUTUBE_OAUTH_CLIENT_SECRET": "유튜브 올리기 · OAuth 클라이언트 보안 비밀번호",
}
PLACEHOLDERS = {"your_gemini_api_key_here"}
# 키·클라이언트 ID에 쓰이는 글자만 받아, 줄바꿈이나 주석 문자로 다른 설정을 덮어쓰지 못하게 합니다.
VALUE_RE = re.compile(r"^[A-Za-z0-9._\-]{1,300}$")
_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Z0-9_]+)\s*=(.*)$")


class EnvValueError(ValueError):
    pass


def _parse_value(raw: str) -> str:
    value = raw.strip()
    if value[:1] in ("'", '"'):
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:end] if end > 0 else value[1:]
    return re.split(r"(?:^|\s+)#", value, maxsplit=1)[0].strip()


def read_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        m = _LINE_RE.match(line)
        if m:
            values[m.group(1)] = _parse_value(m.group(2))
    return values


def _mask(value: str) -> str:
    return "…" + value[-4:] if len(value) > 8 else "설정됨"


def key_status(path: Path) -> list[dict]:
    """화면에 보여 줄 키 목록. 환경변수로 넣은 값은 .env보다 우선하므로 따로 표시합니다."""
    values = read_values(path)
    result = []
    for key, label in EDITABLE_KEYS.items():
        value = values.get(key, "")
        if value in PLACEHOLDERS:
            value = ""
        from_env = bool(os.environ.get(key))
        result.append({
            "key": key,
            "label": label,
            "set": bool(value) or from_env,
            "masked": _mask(os.environ[key] if from_env else value) if (value or from_env) else "",
            "from_environment": from_env,
        })
    return result


def update_values(path: Path, updates: dict[str, str]) -> list[str]:
    """빈 값은 건너뛰고, 나머지는 해당 줄을 바꾸거나 끝에 붙입니다. 바꾼 키 목록을 돌려줍니다."""
    clean: dict[str, str] = {}
    for key, value in updates.items():
        if key not in EDITABLE_KEYS:
            raise EnvValueError(f"고칠 수 없는 항목입니다: {key}")
        value = (value or "").strip()
        if not value:
            continue
        if not VALUE_RE.match(value):
            raise EnvValueError(f"{EDITABLE_KEYS[key]}: 키에 쓸 수 없는 글자가 있어요. 복사할 때 공백이나 따옴표가 들어갔는지 확인하세요.")
        clean[key] = value
    if not clean:
        return []

    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else []
    pending = dict(clean)
    for i, line in enumerate(lines):
        m = _LINE_RE.match(line)
        if m and m.group(1) in pending:
            lines[i] = f"{m.group(1)}={pending.pop(m.group(1))}"
    lines.extend(f"{key}={value}" for key, value in pending.items())

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    return list(clean)
