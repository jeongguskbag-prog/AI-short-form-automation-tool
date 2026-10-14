"""외부 API 오류를 사용자가 이해할 수 있는 한국어 메시지로 바꿉니다."""

_HINTS = {
    400: "요청이 거부되었습니다",
    401: "Gemini API 키가 올바르지 않습니다 (GEMINI_API_KEY 확인)",
    403: "Gemini API 키에 권한이 없습니다 (GEMINI_API_KEY 확인)",
    404: "모델을 찾을 수 없습니다 (LLM_MODEL / IMAGE_MODEL 확인)",
    429: "사용량 한도를 초과했습니다 (무료 등급 한도이거나 결제 설정이 필요합니다)",
    500: "Gemini 서버 오류입니다. 잠시 후 다시 시도하세요",
    503: "모델 사용량이 많아 일시적으로 응답하지 않습니다. 잠시 후 다시 시도하세요",
}

# 다시 시도해도 결과가 같은 오류. 429 중 요금제 한도 초과도 몇 초 안에 풀리지 않습니다.
_PERMANENT_CODES = {400, 401, 402, 403, 404}
_QUOTA_MARKERS = ("exceeded your current quota", "check your plan and billing")


def api_error_code(exc: BaseException) -> int | None:
    if _is_edge_tts(exc):  # aiohttp의 .code는 사라질 예정인 별칭이라 status를 씁니다
        status = getattr(exc, "status", None)
        return status if isinstance(status, int) else None
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None


def _is_edge_tts(exc: BaseException) -> bool:
    """무료 나레이션(Edge TTS)은 aiohttp로 마이크로소프트 서버에 연결합니다."""
    return type(exc).__module__.startswith(("aiohttp", "edge_tts"))


def _is_ours(exc: BaseException) -> bool:
    """타입캐스트·유튜브 등 이 앱의 오류 (메시지에 이미 한국어 설명이 들어 있음)."""
    return type(exc).__module__.startswith("app.")


def is_retryable(exc: BaseException) -> bool:
    code = api_error_code(exc)
    if code in _PERMANENT_CODES:
        return False
    if code == 429 and any(m in str(exc) for m in _QUOTA_MARKERS):
        return False
    return True


_SHORT_HINTS = {
    400: "요청 거부",
    401: "API 키가 올바르지 않음",
    403: "API 키 권한 없음",
    404: "모델·주소를 찾을 수 없음",
    429: "사용량 한도 초과 (무료 등급이거나 결제 필요)",
    500: "서버 오류",
    503: "사용량이 많아 일시적으로 응답 없음",
}


def short_reason(exc: BaseException) -> str:
    """화면 경고용 짧은 한국어 이유. 영어 원문은 서버 로그에만 남깁니다."""
    code = api_error_code(exc)
    if _is_edge_tts(exc):  # Edge는 키를 쓰지 않으므로 'API 키' 안내는 틀린 말
        return f"서버가 접속을 거부함 (HTTP {code})" if code else "서버에 연결하지 못함"
    if code is not None:
        return _SHORT_HINTS.get(code, f"오류 (HTTP {code})")
    return str(exc) or type(exc).__name__


def describe_error(exc: BaseException, limit: int = 200) -> str:
    code = api_error_code(exc)
    if _is_edge_tts(exc):
        hint = ("무료 나레이션 목소리(Edge TTS) 서버가 요청을 거부했습니다. PC의 날짜·시간이 정확한지(자동으로 시간 설정 켜기) "
                "확인하고 잠시 뒤 다시 해 보세요. 계속되면 설정 점검에서 타입캐스트 목소리를 쓰세요")
        return f"{hint} [HTTP {code}]" if code else f"{hint} ({type(exc).__name__})"
    if _is_ours(exc):
        return str(exc)[:limit]
    if code is not None:
        detail = (getattr(exc, "message", None) or str(exc)).strip()
        if len(detail) > limit:
            detail = detail[:limit].rstrip() + "…"
        hint = _HINTS.get(code, "Gemini API 오류입니다")
        return f"{hint} [HTTP {code}] {detail}"
    return f"{type(exc).__name__}: {exc}"
