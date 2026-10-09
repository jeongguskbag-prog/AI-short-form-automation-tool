from datetime import UTC, datetime

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.pipeline import Providers
from app.services import llm
from app.services.scriptwriter import (
    ProductBrief,
    ShoppingScriptDraft,
    build_prompt,
    finalize,
    find_risky_expressions,
)
from app.services.trends import TrendVideo

from .conftest import FakeImages, FakePlanner, FakeTTS, requires_ffmpeg


def _draft(**kw):
    base = dict(
        hooks=["설거지통 아직도 자리 차지해요?", " 설거지통 아직도 자리 차지해요? ", "접으면 3cm!", "물때 걱정 끝"],
        body="싱크대가 좁으면 설거지통이 늘 거슬리죠.\n\n"
        "이건 실리콘이라 쓰고 나면 납작하게 접혀요.\n뜨거운 물도 괜찮아요.",
        cta="고정 댓글 링크에서 확인해 보세요.",
        titles=["좁은 싱크대 구원템", "접는 설거지통 써 봤어요"],
        hashtags=["#주방템", "살림 꿀템", "#광고", "##"],
    )
    return ShoppingScriptDraft(**(base | kw))


def test_finalize_cleans_and_adds_disclosure(settings):
    s = finalize(_draft(), settings)
    assert s.hooks == ["설거지통 아직도 자리 차지해요?", "접으면 3cm!", "물때 걱정 끝"]
    assert s.body.count("\n") == 2  # 빈 줄 제거
    assert s.hashtags == ["#주방템", "#살림꿀템"]
    assert s.description.splitlines()[0] == settings.ad_disclosure
    assert s.description.splitlines()[-1] == "#광고 #주방템 #살림꿀템"
    assert "좁은 싱크대 구원템" in s.description
    assert s.warnings == []

    full = s.full_script(1)
    assert full.startswith("접으면 3cm!\n싱크대가")
    assert full.endswith(settings.ad_disclosure_spoken)


def test_finalize_warns_about_risky_expressions(settings):
    s = finalize(_draft(cta="무조건 사세요, 효과 보장합니다", titles=["주방템 1위"]), settings)
    assert any(w.startswith("단정 표현: '무조건'") for w in s.warnings)
    assert any("효과 보장" in w for w in s.warnings)
    assert any("'1위'" in w for w in s.warnings)


def test_finalize_rejects_empty(settings):
    with pytest.raises(ValueError):
        finalize(_draft(hooks=["  "]), settings)


def test_risky_expressions():
    assert find_risky_expressions("깔끔하게 정리돼요") == []
    assert find_risky_expressions("100 % 만족, 부작용 없어요, 살 빠져요") == [
        "단정 표현: '100 %'",
        "의약품으로 오인될 수 있는 효능 표현: '부작용 없'",
        "체중 감량 효능 표현: '살 빠'",
    ]


def test_prompt_includes_reference_only_when_given():
    assert "참고 영상" not in build_prompt(ProductBrief("설거지통"))
    prompt = build_prompt(ProductBrief("설거지통", "접힘", "calm", "제목: 이거 뭐예요"))
    assert "제품 정보:\n접힘" in prompt and "제목: 이거 뭐예요" in prompt


# --------------------------------------------------------------------------- 모델 전환


class _ApiError(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.code = code


class _FakeResponse:
    def __init__(self, text):
        self.parsed = None
        self.text = text


class _FakeClient:
    def __init__(self, errors):
        self.errors = errors  # 모델 이름 → 오류 코드
        self.calls = []
        self.aio = self
        self.models = self

    async def generate_content(self, model, contents, config):
        self.calls.append(model)
        if model in self.errors:
            raise _ApiError(self.errors[model])
        return _FakeResponse(_draft().model_dump_json())


@pytest.mark.asyncio
async def test_generate_structured_falls_back_only_for_transient_errors(settings):
    settings.llm_model, settings.llm_fallback_model = "main", "backup"
    kw = dict(prompt="p", system_instruction="s", schema=ShoppingScriptDraft, what="테스트")

    client = _FakeClient({"main": 503})
    draft = await llm.generate_structured(client, settings, **kw)
    assert draft.cta and client.calls == ["main", "backup"]

    client = _FakeClient({"main": 401})
    with pytest.raises(_ApiError):
        await llm.generate_structured(client, settings, **kw)
    assert client.calls == ["main"]


# --------------------------------------------------------------------------- API


class FakeWriter:
    def __init__(self, fail=False):
        self.fail = fail
        self.briefs = []

    async def write(self, brief):
        self.briefs.append(brief)
        if self.fail:
            raise RuntimeError("모델 오류")
        return finalize(_draft(), Settings(_env_file=None))


async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@requires_ffmpeg
@pytest.mark.asyncio
async def test_shopping_script_api(settings):
    writer = FakeWriter()
    app = create_app(settings, Providers(FakePlanner(), FakeTTS(), FakeImages(), scriptwriter=writer))
    async with app.router.lifespan_context(app), await _client(app) as client:
        app.state.trends.store.replace(
            [TrendVideo("ref123", "kitchen", "이거 뭐예요?", "c", "채널", "", "2026-10-01T00:00:00Z",
                        views=5000, subscribers=10, comments_text="어디서 사요")],
            datetime.now(UTC), datetime.now(UTC),
        )
        r = await client.post("/api/v1/scripts/shopping", json={
            "product_name": " 접이식 설거지통 ", "features": "실리콘", "reference_video_id": "ref123",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["hooks"]) == len(body["scripts"]) == 3
        assert body["scripts"][2].startswith("물때 걱정 끝")
        assert body["reference_used"] is True
        brief = writer.briefs[-1]
        assert brief.product_name == "접이식 설거지통"
        assert "어디서 사요" in brief.reference and "구독자 대비 500배" in brief.reference

        r = await client.post("/api/v1/scripts/shopping", json={"product_name": "x", "reference_video_id": "nope"})
        assert r.json()["reference_used"] is False
        assert (await client.post("/api/v1/scripts/shopping", json={"product_name": "  "})).status_code == 422

        writer.fail = True
        r = await client.post("/api/v1/scripts/shopping", json={"product_name": "x"})
        assert r.status_code == 502 and "모델 오류" in r.json()["detail"]


@requires_ffmpeg
@pytest.mark.asyncio
async def test_shopping_script_api_without_writer(settings, providers):
    app = create_app(settings, providers)
    async with app.router.lifespan_context(app), await _client(app) as client:
        assert (await client.post("/api/v1/scripts/shopping", json={"product_name": "x"})).status_code == 503
