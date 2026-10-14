import pytest

from app.config import Settings
from app.errors import describe_error, is_retryable
from app.jobs import JobStore
from app.pipeline import Providers, ShortsPipeline
from app.services.planner import GeminiPlanner

from .conftest import FakePlanner, FakeTTS, requires_ffmpeg


class FakeAPIError(Exception):
    """google.genai.errors.APIError처럼 code와 message를 가진 오류."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{code} {message}")
        self.code = code
        self.message = message


QUOTA = FakeAPIError(429, "You exceeded your current quota, please check your plan and billing details.")


@pytest.mark.parametrize(
    ("exc", "retryable"),
    [
        (FakeAPIError(503, "high demand"), True),
        (FakeAPIError(429, "Resource has been exhausted (rate)"), True),
        (QUOTA, False),
        (FakeAPIError(401, "API key not valid"), False),
        (FakeAPIError(404, "model not found"), False),
        (RuntimeError("network"), True),
    ],
)
def test_is_retryable(exc, retryable):
    assert is_retryable(exc) is retryable


def test_describe_error_is_friendly_and_short():
    msg = describe_error(FakeAPIError(429, "x" * 1000))
    assert msg.startswith("사용량 한도를 초과했습니다")
    assert "[HTTP 429]" in msg
    assert len(msg) < 300
    assert describe_error(ValueError("boom")) == "ValueError: boom"


def test_edge_tts_and_own_errors_are_not_blamed_on_gemini():
    import aiohttp

    from app.services.tts import TypecastError

    edge = aiohttp.WSServerHandshakeError(None, (), status=403, message="Invalid response status")
    msg = describe_error(edge)
    assert "Edge TTS" in msg and "Gemini" not in msg and "[HTTP 403]" in msg
    assert not is_retryable(edge)
    assert describe_error(TypecastError("타입캐스트 크레딧이 부족합니다.", 402)) == "타입캐스트 크레딧이 부족합니다."


class _FakeModels:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    async def generate_content(self, *, model, contents, config):
        self.calls.append(model)
        result = self.behaviour[model]
        if isinstance(result, Exception):
            raise result

        class R:
            parsed = None
            text = result
        return R()


class _FakeClient:
    def __init__(self, behaviour):
        self.models = _FakeModels(behaviour)
        self.aio = self


PLAN_JSON = '{"title": "t", "scenes": [{"scene_id": 1, "narration": "n", "visual_prompt": "v", "subtitle_text": "s"}]}'


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_error", [FakeAPIError(503, "high demand"), FakeAPIError(404, "gone")])
async def test_planner_falls_back_to_second_model(primary_error):
    client = _FakeClient({"main": primary_error, "backup": PLAN_JSON})
    settings = Settings(llm_model="main", llm_fallback_model="backup", max_retries=1, _env_file=None)
    plan = await GeminiPlanner(client, settings).plan("대본", "tone")
    assert plan.title == "t"
    assert client.models.calls == ["main", "backup"]


@pytest.mark.asyncio
async def test_planner_does_not_fall_back_on_bad_key():
    client = _FakeClient({"main": FakeAPIError(401, "bad key"), "backup": PLAN_JSON})
    settings = Settings(llm_model="main", llm_fallback_model="backup", max_retries=3, _env_file=None)
    with pytest.raises(FakeAPIError):
        await GeminiPlanner(client, settings).plan("대본", "tone")
    assert client.models.calls == ["main"]  # 재시도도, 예비 모델도 쓰지 않음


@requires_ffmpeg
@pytest.mark.asyncio
async def test_permanent_image_error_stops_further_image_calls(settings, spec):
    class QuotaImages:
        calls = 0

        async def generate(self, prompt, output_path, query="", exclude=()):
            QuotaImages.calls += 1
            raise QUOTA

    one_at_a_time = settings.model_copy(update={"max_concurrent_scenes": 1})
    pipeline = ShortsPipeline(Providers(FakePlanner(3), FakeTTS(), QuotaImages()), one_at_a_time, spec)
    job = JobStore().create("대본", "tone")
    await pipeline.run(job)
    assert job.status == "COMPLETED", job.error
    assert QuotaImages.calls == 1
    assert len(job.warnings) == 1
    assert job.warnings[0].startswith("씬 1, 2, 3: ")
    assert "사용량 한도" in job.warnings[0]
