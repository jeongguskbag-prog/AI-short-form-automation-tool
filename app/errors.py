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
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None


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
    if code is not None:
        return _SHORT_HINTS.get(code, f"오류 (HTTP {code})")
    return str(exc) or type(exc).__name__


def describe_error(exc: BaseException, limit: int = 200) -> str:
    code = api_error_code(exc)
    if code is not None:
        detail = (getattr(exc, "message", None) or str(exc)).strip()
        if len(detail) > limit:
            detail = detail[:limit].rstrip() + "…"
        hint = _HINTS.get(code, "Gemini API 오류입니다")
        return f"{hint} [HTTP {code}] {detail}"
    return f"{type(exc).__name__}: {exc}"
