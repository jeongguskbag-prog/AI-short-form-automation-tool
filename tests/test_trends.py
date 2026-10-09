import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.main import create_app
from app.services import trends
from app.services.trends import TrendStore, TrendVideo, YouTubeTrendSource, query_trends

from .conftest import requires_ffmpeg

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def _video(video_id, *, days_ago=3, views=1000, subs=100, likes=10, comments=5, category="kitchen", **kw):
    return TrendVideo(
        video_id=video_id, category=category, title=kw.pop("title", f"영상 {video_id}"),
        channel_id=f"ch-{video_id}", channel_title=kw.pop("channel_title", "채널"),
        thumbnail_url="https://i.ytimg.com/x.jpg",
        published_at=(NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z"),
        views=views, likes=likes, comments=comments, subscribers=subs, **kw,
    )


def _rows(*videos, favorites=(), collection_id=1, stale=()):
    return [(v, v.video_id in favorites, 0 if v.video_id in stale else collection_id) for v in videos]


COLLECTION = trends.Collection(1, "2026-10-01T00:00:00+00:00", "2026-10-01T00:05:00+00:00", 3)


def test_performance_sort_puts_unknown_last_and_excludes_old():
    rows = _rows(
        _video("a", views=1000, subs=100),  # x10
        _video("b", views=50_000, subs=100),  # x500
        _video("c", subs=None),  # 구독자 비공개 → 뒤로
        _video("d", days_ago=60, views=10**9, subs=1),  # 1개월 밖
    )
    r = query_trends(rows, COLLECTION, period="1m", sort="performance", now=NOW)
    assert [i["video_id"] for i in r.items] == ["b", "a", "c"]
    assert r.items[0]["performance"] == 500
    assert r.items[2]["performance"] is None
    assert (r.with_value, r.missing_value, r.out_of_period, r.total) == (2, 1, 1, 3)

    r = query_trends(rows, COLLECTION, period="3m", sort="views", now=NOW)
    assert [i["video_id"] for i in r.items][:2] == ["d", "b"]
    assert r.out_of_period == 0


def test_comment_sort_and_latest():
    rows = _rows(
        _video("a", comments=3, days_ago=1),
        _video("b", comments=900, days_ago=5),
        _video("c", comments=None, days_ago=2),  # 댓글 수 숨김
    )
    assert [i["video_id"] for i in query_trends(rows, COLLECTION, sort="comments", now=NOW).items] == ["b", "a", "c"]
    latest = query_trends(rows, COLLECTION, sort="latest", now=NOW)
    assert [i["video_id"] for i in latest.items] == ["a", "c", "b"]
    assert latest.missing_value == 0


def test_favorites_first_regardless_of_filters():
    rows = _rows(
        _video("a", views=10**6, subs=10),
        _video("old-fav", days_ago=300, category="pet", views=10, subs=100),
        favorites={"old-fav"},
        stale={"old-fav"},  # 이번 수집에는 없었지만 즐겨찾기라 남은 영상
    )
    r = query_trends(rows, COLLECTION, category="kitchen", period="1m", q="영상 a", now=NOW)
    assert [i["video_id"] for i in r.items] == ["old-fav", "a"]
    assert r.items[0]["favorite"] is True
    assert (r.favorites, r.favorites_kept, r.out_of_period) == (1, 1, 0)


def test_search_covers_title_tags_channel_and_comments():
    rows = _rows(
        _video("t", title="실리콘 주걱 끝판왕"),
        _video("g", tags="수납 정리"),
        _video("ch", channel_title="살림하는 루시"),
        _video("cm", comments_text="이거 어디서 사요?\n링크 주세요"),
    )

    def ids(q):
        return [i["video_id"] for i in query_trends(rows, COLLECTION, q=q, now=NOW).items]

    assert ids("주걱") == ["t"]
    assert ids("수납") == ["g"]
    assert ids("루시") == ["ch"]
    assert ids("어디서 사요") == ["cm"]
    assert ids("없는말") == []


def test_store_keeps_favorites_across_collections(tmp_path):
    store = TrendStore(tmp_path / "t.db")
    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    store.replace([_video("a"), _video("b")], t0, t0)
    assert store.set_favorite("a", True)
    assert not store.set_favorite("nope", True)

    second = store.replace([_video("c", views=7)], t0, t0 + timedelta(minutes=5))
    rows = {v.video_id: (fav, cid) for v, fav, cid in store.all_rows()}
    assert rows == {"a": (True, second.id - 1), "c": (False, second.id)}
    assert store.last_collection() == second

    store.set_favorite("a", False)
    store.replace([], t0, t0)
    assert store.all_rows() == []


# --------------------------------------------------------------------------- YouTube 수집


def _youtube_transport(calls: list[str], *, quota_error=False):
    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        params = request.url.params
        calls.append(endpoint)
        assert params["key"] == "yt-key"
        if quota_error:
            return httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}]}})
        if endpoint == "search":
            assert params["videoDuration"] == "short"
            n = 2 if "주방" in params["q"] else 0
            return httpx.Response(200, json={"items": [{"id": {"videoId": f"v{i}"}} for i in range(n)]})
        if endpoint == "videos":
            return httpx.Response(200, json={"items": [
                {"id": vid, "snippet": {
                    "title": f"제목 {vid}", "channelId": f"c{vid}", "channelTitle": "채널",
                    "publishedAt": "2026-09-30T00:00:00Z", "tags": ["주방", "꿀템"],
                    "thumbnails": {"high": {"url": f"https://i.ytimg.com/{vid}.jpg"}},
                 }, "statistics": {"viewCount": "5000", "likeCount": "40"}
                    | ({"commentCount": "7"} if vid == "v0" else {})}
                for vid in params["id"].split(",")
            ]})
        if endpoint == "channels":
            return httpx.Response(200, json={"items": [
                {"id": "cv0", "statistics": {"subscriberCount": "10", "hiddenSubscriberCount": False}},
                {"id": "cv1", "statistics": {"hiddenSubscriberCount": True}},
            ]})
        if endpoint == "commentThreads":
            if params["videoId"] == "v1":
                return httpx.Response(403, json={"error": {"errors": [{"reason": "commentsDisabled"}]}})
            return httpx.Response(200, json={"items": [
                {"snippet": {"topLevelComment": {"snippet": {"textDisplay": "구매 링크 있나요?"}}}}
            ]})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_youtube_source_collects_metrics(settings):
    settings.youtube_api_key = "yt-key"
    calls: list[str] = []
    source = YouTubeTrendSource(settings, transport=_youtube_transport(calls))
    videos = {v.video_id: v for v in await source.collect()}

    assert set(videos) == {"v0", "v1"}
    v0, v1 = videos["v0"], videos["v1"]
    assert (v0.category, v0.views, v0.likes, v0.comments, v0.subscribers) == ("kitchen", 5000, 40, 7, 10)
    assert v0.performance == 500
    assert v0.tags == "주방 꿀템"
    assert v0.comments_text == "구매 링크 있나요?"
    assert (v1.comments, v1.subscribers, v1.performance, v1.comments_text) == (None, None, None, "")
    assert calls.count("search") == 2 * len(trends.CATEGORIES)


@pytest.mark.asyncio
async def test_youtube_quota_error_is_explained(settings):
    settings.youtube_api_key = "yt-key"
    calls: list[str] = []
    source = YouTubeTrendSource(settings, transport=_youtube_transport(calls, quota_error=True))
    with pytest.raises(trends.TrendCollectionError, match="할당량"):
        await source.collect()
    assert calls == ["search"]  # 할당량 초과는 다시 시도하지 않음


# --------------------------------------------------------------------------- API


class FakeTrendSource:
    def __init__(self, videos=None, fail=False):
        self.videos = videos or []
        self.fail = fail
        self.calls = 0

    async def collect(self):
        self.calls += 1
        if self.fail:
            raise trends.TrendCollectionError(403, "quotaExceeded", "할당량 초과")
        return self.videos


async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _wait_collected(client, timeout=10):
    async with asyncio.timeout(timeout):
        while True:
            body = (await client.get("/api/v1/trends?period=1y")).json()
            if not body["collecting"] and (body["collection"] or body["last_error"]):
                return body
            await asyncio.sleep(0.05)


def _recent(video_id, **kw):
    published = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    return TrendVideo(
        video_id=video_id, category=kw.get("category", "kitchen"), title=f"제목 {video_id}", channel_id="c",
        channel_title="채널", thumbnail_url="", published_at=published,
        views=kw.get("views", 100), subscribers=kw.get("subs", 10),
    )


@requires_ffmpeg
@pytest.mark.asyncio
async def test_trends_api_collects_on_startup_and_filters(settings, providers):
    source = FakeTrendSource(
        [_recent("a", views=100), _recent("b", views=9000), _recent("p", category="pet", views=50)]
    )
    app = create_app(settings, providers, trend_source=source)
    async with app.router.lifespan_context(app), await _client(app) as client:
        body = await _wait_collected(client)
        assert body["configured"] is True
        assert body["categories"][0] == {"key": "all", "label": "전체 분야"}
        assert [i["video_id"] for i in body["items"]] == ["b", "a", "p"]
        assert body["collection"]["video_count"] == 3
        assert body["summary"]["sort_label"] == "성과순"

        kitchen = (await client.get("/api/v1/trends?category=kitchen&sort=latest")).json()
        assert {i["video_id"] for i in kitchen["items"]} == {"a", "b"}

        assert (await client.put("/api/v1/trends/a/favorite")).json() == {"video_id": "a", "favorite": True}
        assert (await client.put("/api/v1/trends/zzz/favorite")).status_code == 404
        pet = (await client.get("/api/v1/trends?category=pet")).json()
        assert [i["video_id"] for i in pet["items"]] == ["a", "p"]
        assert (await client.delete("/api/v1/trends/a/favorite")).status_code == 200

        for bad in ("category=nope", "period=2y", "sort=random"):
            assert (await client.get(f"/api/v1/trends?{bad}")).status_code == 422

        assert (await client.post("/api/v1/trends/refresh")).status_code == 202
        await _wait_collected(client)
        assert source.calls == 2

        page = await client.get("/trends")
        assert page.status_code == 200 and "/api/v1/trends" in page.text


@requires_ffmpeg
@pytest.mark.asyncio
async def test_trends_api_without_key_and_failure(settings, providers):
    app = create_app(settings, providers)  # YOUTUBE_API_KEY 없음
    async with app.router.lifespan_context(app), await _client(app) as client:
        body = (await client.get("/api/v1/trends")).json()
        assert (body["configured"], body["collection"], body["items"]) == (False, None, [])
        assert (await client.post("/api/v1/trends/refresh")).status_code == 409

    app = create_app(settings, providers, trend_source=FakeTrendSource(fail=True))
    async with app.router.lifespan_context(app), await _client(app) as client:
        body = await _wait_collected(client)
        assert body["last_error"] == "할당량 초과"


@requires_ffmpeg
@pytest.mark.asyncio
async def test_trends_api_requires_key_when_configured(settings, providers):
    settings.api_keys = "secret"
    app = create_app(settings, providers, trend_source=FakeTrendSource())
    async with app.router.lifespan_context(app), await _client(app) as client:
        assert (await client.get("/api/v1/trends")).status_code == 401
        assert (await client.get("/api/v1/trends", headers={"X-API-Key": "secret"})).status_code == 200
        assert (await client.get("/trends")).status_code == 200  # 화면 자체는 공개
