import httpx
import pytest

from app.main import create_app
from app.pipeline import Providers
from app.services.media_search import MediaSearch
from app.services.product_brief import ProductBrief, clean

from .conftest import FakeImages, FakePlanner, FakeTTS, requires_ffmpeg


def _handler(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    if host == "api-sg.aliexpress.com":
        return httpx.Response(200, json={"aliexpress_affiliate_product_query_response": {"resp_result": {
            "resp_code": 200, "result": {"products": {"product": [{
                "product_id": 1, "product_title": "캠핑 랜턴", "promotion_link": "https://s.click.aliexpress.com/x",
                "product_main_image_url": "https://ae01.alicdn.com/main.jpg",
                "product_video_url": "https://video.aliexpress-media.com/v.mp4", "shop_name": "Lamp",
            }]}}}}})
    if host == "api.ebay.com" and path.endswith("/token"):
        return httpx.Response(200, json={"access_token": "t"})
    if host == "api.ebay.com":
        assert request.headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_GB"
        return httpx.Response(200, json={"itemSummaries": [{
            "itemId": "e1", "title": "Lantern", "image": {"imageUrl": "https://i.ebayimg.com/a.jpg"},
            "itemWebUrl": "https://ebay.co.uk/itm/1"}]})
    if host == "pixabay.com":
        return httpx.Response(200, json={"hits": [{"id": 9, "user": "kim", "videos": {
            "small": {"url": "https://cdn.pixabay.com/v.mp4", "width": 720, "height": 1280}}}]})
    if host == "api.pexels.com":
        return httpx.Response(500, text="down")
    return httpx.Response(404)


@pytest.mark.asyncio
async def test_search_merges_sources_official_first(settings):
    settings.aliexpress_app_key, settings.aliexpress_app_secret = "k", "s"
    settings.ebay_client_id, settings.ebay_client_secret = "e", "s"
    settings.pixabay_api_key, settings.pexels_api_key = "p", "x"
    search = MediaSearch(settings, httpx.MockTransport(_handler))
    found, notes = await search.search("camping lantern", "캠핑 랜턴")
    assert [(c["source"], c["kind"]) for c in found] == [
        ("알리익스프레스", "video"), ("알리익스프레스", "image"), ("이베이", "image"), ("Pixabay 영상", "video")]
    assert found[0]["official"] and not found[-1]["official"]
    assert len(notes) == 1 and notes[0].startswith("Pexels 영상")


@pytest.mark.asyncio
async def test_search_without_keys_is_empty(settings):
    found, notes = await MediaSearch(settings).search("x")
    assert found == [] and notes == []
    assert all(not configured for _, configured in MediaSearch(settings).sources())


def test_brief_clean():
    b = clean(ProductBrief(product_name="  캠핑\nLED 랜턴 ", features=["충전식", " ", "방수", "a", "b", "c"],
                           search_keyword_en=" camping  led lantern "))
    assert (b.product_name, b.features, b.search_keyword_en) == (
        "캠핑 LED 랜턴", ["충전식", "방수", "a", "b"], "camping led lantern")


class FakeBriefer:
    def __init__(self):
        self.calls = []

    async def brief(self, title, details=""):
        self.calls.append(title)
        return ProductBrief(product_name="캠핑 랜턴", features=["충전식"], search_keyword_en="camping lantern")


@requires_ffmpeg
@pytest.mark.asyncio
async def test_brief_and_search_endpoints(settings):
    settings.pixabay_api_key = "p"
    briefer = FakeBriefer()
    providers = Providers(FakePlanner(), FakeTTS(), FakeImages(), briefer=briefer)
    app = create_app(settings, providers, media_search=MediaSearch(settings, httpx.MockTransport(_handler)))
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post("/api/v1/products/brief", json={"title": "2026 신형 캠핑 랜턴 충전식 무료배송"})
        assert r.json() == {"product_name": "캠핑 랜턴", "features": ["충전식"], "search_keyword_en": "camping lantern"}

        body = (await client.get("/api/v1/media/search", params={"q": "캠핑 랜턴"})).json()
        assert body["keyword_en"] == "camping lantern"  # 한국어면 영어 검색어를 만들어 씀
        assert [c["source"] for c in body["candidates"]] == ["Pixabay 영상"]
        assert {s["name"]: s["configured"] for s in body["sources"]}["알리익스프레스"] is False

        before = len(briefer.calls)
        body = (await client.get("/api/v1/media/search", params={"q": "캠핑 랜턴", "q_en": "lantern"})).json()
        assert body["keyword_en"] == "lantern" and len(briefer.calls) == before

    app = create_app(settings, Providers(FakePlanner(), FakeTTS(), FakeImages()))
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post("/api/v1/products/brief", json={"title": "x"})).status_code == 503
