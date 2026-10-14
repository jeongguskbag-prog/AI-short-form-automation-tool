"""Gemini 구조화(JSON 스키마) 응답 호출. 기본 모델이 계속 실패하면 예비 모델로 한 번 더 시도합니다."""

import logging
from typing import TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel

from ..config import Settings
from ..errors import api_error_code, is_retryable
from ..retry import with_retry

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class EmptyResponseError(RuntimeError):
    pass


async def generate_structured(
    client: genai.Client,
    settings: Settings,
    *,
    prompt: str | list,
    system_instruction: str,
    schema: type[T],
    what: str,
    temperature: float = 0.7,
) -> T:
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        response_mime_type="application/json",
        response_schema=schema,
        temperature=temperature,
    )

    async def call(model: str) -> T:
        response = await client.aio.models.generate_content(model=model, contents=prompt, config=config)
        if isinstance(response.parsed, schema):
            return response.parsed
        if not response.text:
            raise EmptyResponseError("LLM이 빈 응답을 반환했습니다.")
        return schema.model_validate_json(response.text)

    models = [settings.llm_model]
    if settings.llm_fallback_model and settings.llm_fallback_model not in models:
        models.append(settings.llm_fallback_model)

    for i, model in enumerate(models):
        try:
            return await with_retry(lambda m=model: call(m), attempts=settings.max_retries, what=f"{what}({model})")
        except Exception as exc:
            # 과부하·일시 오류, 모델이 내려간 경우(404), 사용량 한도 초과(429)에만 예비 모델로 넘어갑니다.
            # 무료 등급 한도는 모델마다 따로라 예비 모델은 아직 쓸 수 있는 경우가 많습니다.
            # 키 오류 같은 문제는 예비 모델로도 해결되지 않으므로 바로 알립니다.
            if i == len(models) - 1 or not (is_retryable(exc) or api_error_code(exc) in (404, 429)):
                raise
            logger.warning("%s 모델 %s 실패, 예비 모델 %s로 전환: %s", what, model, models[i + 1], exc)
    raise AssertionError("unreachable")
