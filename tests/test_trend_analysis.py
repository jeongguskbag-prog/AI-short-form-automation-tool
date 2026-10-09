from datetime import UTC, datetime

import httpx
import pytest

from app.main import create_app
from app.pipeline import Providers
from app.services.trend_analysis import TrendAnalysis, clean
from app.services.trends import TrendStore, TrendVideo

from .conftest import FakeImages, FakePlanner, FakeTTS, requires_ffmpeg


def _video(vid="v1"):
    return TrendVideo(vid, "kitchen", "설거지통 이거 뭐예요?", "c", "채널", "", "2026-10-01T00:00:00Z",
                      views=50_000, comments=320, subscribers=100, tags="주방 꿀템", comments_text="어디서 사요?")


def test_clean_trims_and_limits():
    a = clean(TrendAnalysis(product_name="  접이식\n설거지통 ", hook_type="질문", why="x" * 500,
                            buyer_questions=["어디서 사요?", " ", "식세기 돼요?", "가격?", "색상?"], script_tip="팁"))
    assert a.product_name == "접이식 설거지통"
    assert len(a.why) == 120
    assert a.buyer_questions == ["어디서 사요?", "식세기 돼요?", "가격?"]


def test_store_caches_and_drops_analyses_of_removed_videos(tmp_path):
    store = TrendStore(tmp_path / "t.db")
    now = datetime.now(UTC)
    store.replace([_video("a"), _video("b")], now, now)
    store.set_analysis("a", {"product_name": "A"})
    store.set_analysis("b", {"product_name": "B"})
    assert store.get_analysis("a") == {"product_name": "A"}

    store.replace([_video("a")], now, now)  # b는 목록에서 빠짐
    assert store.get_analysis("a") == {"product_name": "A"}  # 남은 영상 분석은 유지
    assert store.get_analysis("b") is None


class FakeAnalyzer:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def analyze(self, video):
        self.calls.append(video)
        if self.fail:
            raise RuntimeError("모델 오류")
        return TrendAnalysis(product_name="접이식 설거지통", hook_type="질문", why="구독자 100명인데 5만 회",
                             buyer_questions=["어디서 사요?"], script_tip="공간 절약을 앞세우세요")


async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@requires_ffmpeg
@pytest.mark.asyncio
async def test_analysis_endpoint_caches(settings):
    analyzer = FakeAnalyzer()
    app = create_app(settings, Providers(FakePlanner(), FakeTTS(), FakeImages(), trend_analyzer=analyzer))
    async with app.router.lifespan_context(app), await _client(app) as client:
        now = datetime.now(UTC)
        app.state.trends.store.replace([_video("v1")], now, now)

        r = await client.post("/api/v1/trends/v1/analysis")
        assert r.status_code == 200, r.text
        assert r.json()["product_name"] == "접이식 설거지통" and r.json()["cached"] is False
        assert "어디서 사요?" in analyzer.calls[0].comments_text  # 댓글까지 넘김

        r = await client.post("/api/v1/trends/v1/analysis")
        assert r.json()["cached"] is True and len(analyzer.calls) == 1  # 다시 부르지 않음
        await client.post("/api/v1/trends/v1/analysis?refresh=true")
        assert len(analyzer.calls) == 2

        assert (await client.post("/api/v1/trends/nope/analysis")).status_code == 404
        analyzer.fail = True
        r = await client.post("/api/v1/trends/v1/analysis?refresh=true")
        assert r.status_code == 502 and "모델 오류" in r.json()["detail"]


@requires_ffmpeg
@pytest.mark.asyncio
async def test_analysis_endpoint_without_analyzer(settings, providers):
    app = create_app(settings, providers)
    async with app.router.lifespan_context(app), await _client(app) as client:
        now = datetime.now(UTC)
        app.state.trends.store.replace([_video("v1")], now, now)
        assert (await client.post("/api/v1/trends/v1/analysis")).status_code == 503
