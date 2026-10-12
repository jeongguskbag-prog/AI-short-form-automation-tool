import asyncio
import json
from datetime import date

import httpx
import pytest

from app.main import create_app
from app.services import products
from app.services.products import AliExpress, Ebay, NaverShopping, Product, ProductCollector, aliexpress_sign

from .conftest import requires_ffmpeg


def _transport(handler):
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_naver_collects_and_strips_tags(settings):
    settings.naver_client_id, settings.naver_client_secret = "id", "secret"
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["X-Naver-Client-Id"] == "id"
        return httpx.Response(200, json={"items": [{
            "title": "<b>접이식</b> 설거지통 &amp; 건조대", "link": "https://smartstore.naver.com/x",
            "image": "https://shopping-phinf.pstatic.net/a.jpg", "lprice": "12900", "mallName": "가나다몰",
            "productId": "123",
        }]})

    found = await NaverShopping(settings, _transport(handler)).collect(["kitchen"])
    assert len(seen) == len(products.NAVER_QUERIES["kitchen"])
    assert seen[0].url.params["sort"] == "sim"
    p = found[0]
    assert (p.title, p.price, p.currency, p.mall, p.category) == ("접이식 설거지통 & 건조대", 12900, "KRW", "가나다몰", "kitchen")


@pytest.mark.asyncio
async def test_naver_category_trends(settings):
    settings.naver_client_id, settings.naver_client_secret = "id", "secret"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["timeUnit"] == "week" and len(body["category"]) <= 3
        return httpx.Response(200, json={"results": [
            {"title": c["name"], "data": [{"ratio": r} for r in (10, 10, 10, 10, 12, 12, 12, 12)]}
            for c in body["category"]
        ]})

    trends = await NaverShopping(settings, _transport(handler)).category_trends(
        ["kitchen", "camping"], today=date(2026, 10, 12))
    assert trends == {"kitchen": 20.0, "camping": 20.0}

    denied = await NaverShopping(settings, _transport(lambda r: httpx.Response(403))).category_trends(["kitchen"])
    assert denied == {}


def test_aliexpress_sign_is_sorted_hmac():
    params = {"method": "m", "app_key": "k", "timestamp": "1"}
    import hashlib
    import hmac
    expected = hmac.new(b"s", b"app_keykmethodmtimestamp1", hashlib.sha256).hexdigest().upper()
    assert aliexpress_sign(params, "s") == expected


@pytest.mark.asyncio
async def test_aliexpress_parses_products_with_video(settings):
    settings.aliexpress_app_key, settings.aliexpress_app_secret = "key", "secret"
    settings.aliexpress_tracking_id = "track"

    def handler(request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        assert form["method"] == "aliexpress.affiliate.product.query"
        assert form["sort"] == "LAST_VOLUME_DESC" and form["tracking_id"] == "track"
        sign = form.pop("sign")
        assert sign == aliexpress_sign(form, "secret")
        return httpx.Response(200, json={"aliexpress_affiliate_product_query_response": {"resp_result": {
            "resp_code": 200, "result": {"products": {"product": [{
                "product_id": 1005, "product_title": "Camping Lantern", "promotion_link": "https://s.click.aliexpress.com/e/x",
                "product_main_image_url": "https://ae01.alicdn.com/main.jpg",
                "product_small_image_urls": {"string": ["https://ae01.alicdn.com/1.jpg", "//ae01.alicdn.com/2.jpg"]},
                "product_video_url": "https://video.aliexpress-media.com/v.mp4",
                "target_sale_price": "15,300", "target_sale_price_currency": "KRW",
                "lastest_volume": 812, "evaluate_rate": "96.5%", "shop_name": "Lamp Store",
            }]}}}}})

    found = await AliExpress(settings, _transport(handler), clock=lambda: 1.0).collect(["camping"])
    p = found[0]
    assert (p.product_id, p.sales, p.rating, p.price, p.currency) == ("1005", 812, 96.5, 15300, "KRW")
    assert p.video_url.endswith("v.mp4")
    assert p.images == ["https://ae01.alicdn.com/1.jpg", "https://ae01.alicdn.com/2.jpg"]


@pytest.mark.asyncio
async def test_aliexpress_error_response(settings):
    settings.aliexpress_app_key, settings.aliexpress_app_secret = "key", "secret"
    handler = lambda r: httpx.Response(200, json={"error_response": {"code": "IncompleteSignature", "msg": "sign error"}})
    with pytest.raises(products.ProductSourceError, match="sign error"):
        await AliExpress(settings, _transport(handler)).collect(["pet"])


@pytest.mark.asyncio
async def test_ebay_token_and_markets(settings):
    settings.ebay_client_id, settings.ebay_client_secret = "app", "cert"
    markets = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            assert request.headers["Authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": "tok"})
        assert request.headers["Authorization"] == "Bearer tok"
        markets.append(request.headers["X-EBAY-C-MARKETPLACE-ID"])
        return httpx.Response(200, json={"itemSummaries": [{
            "itemId": f"v1|{markets[-1]}", "title": "Pet Brush", "price": {"value": "9.99", "currency": "EUR"},
            "image": {"imageUrl": "https://i.ebayimg.com/a.jpg"}, "itemWebUrl": "https://www.ebay.de/itm/1",
        }]})

    found = await Ebay(settings, _transport(handler)).collect(["pet"])
    assert markets == list(products.EBAY_MARKETS)
    assert {p.market for p in found} == {"독일", "영국"}
    assert found[0].price == 9.99


class FakeSource:
    def __init__(self, name, items=(), fail=False, configured=True):
        self.name, self.items, self.fail, self.configured = name, list(items), fail, configured
        self.calls = 0

    async def collect(self, categories):
        self.calls += 1
        if self.fail:
            raise products.ProductSourceError(f"{name_label(self.name)}: 키가 틀렸습니다")
        return self.items


def name_label(name):
    return products.SOURCES[name]


def _p(source, pid, **kw):
    return Product(source=source, product_id=pid, title=kw.pop("title", f"상품 {pid}"),
                   category=kw.pop("category", "kitchen"), url="https://x", **kw)


@pytest.mark.asyncio
async def test_collector_keeps_previous_items_of_failed_source(settings):
    ali = FakeSource("aliexpress", [_p("aliexpress", "1", sales=5)])
    ebay = FakeSource("ebay", [_p("ebay", "2")])
    collector = ProductCollector(settings, settings.products_path, [ali, ebay])
    await collector.refresh()
    assert len(collector.load()["items"]) == 2

    ebay.fail = True
    ali.items = [_p("aliexpress", "3", sales=9)]
    await collector.refresh()
    data = collector.load()
    assert {i["product_id"] for i in data["items"]} == {"3", "2"}  # 이베이는 지난번 것 유지
    assert data["errors"] == {"ebay": "이베이 (유럽): 키가 틀렸습니다"}


def test_query_products_sort_and_filter():
    data = {"items": [
        {"source": "aliexpress", "product_id": "a", "title": "캠핑 랜턴", "category": "camping", "sales": 10, "price": 5},
        {"source": "aliexpress", "product_id": "b", "title": "캠핑 의자", "category": "camping", "sales": 500, "price": 30},
        {"source": "naver", "product_id": "c", "title": "주방 집게", "category": "kitchen", "sales": None, "price": 1},
    ]}
    assert [p["product_id"] for p in products.query_products(data)] == ["b", "a", "c"]
    assert [p["product_id"] for p in products.query_products(data, sort="price_asc")] == ["c", "a", "b"]
    assert [p["product_id"] for p in products.query_products(data, q="의자")] == ["b"]
    only = products.query_products(data, source="naver")
    assert [p["product_id"] for p in only] == ["c"]
    assert only[0]["source_label"] == "네이버 쇼핑" and only[0]["category_label"] == "주방"


@requires_ffmpeg
@pytest.mark.asyncio
async def test_products_api(settings, providers):
    source = FakeSource("aliexpress", [_p("aliexpress", "1", category="camping", sales=3)])
    app = create_app(settings, providers, product_sources=[source, FakeSource("naver", configured=False)])
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        async with asyncio.timeout(10):  # 시작할 때 처음 한 번 모읍니다
            while not (body := (await client.get("/api/v1/products")).json())["items"]:
                await asyncio.sleep(0.05)
        assert body["configured"] is True
        assert [s["configured"] for s in body["sources"]] == [True, False]
        assert body["items"][0]["category_label"] == "캠핑"
        assert (await client.get("/api/v1/products?source=nope")).status_code == 422
        assert (await client.post("/api/v1/products/refresh")).status_code == 202

    app = create_app(settings, providers, product_sources=[FakeSource("naver", configured=False)])
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post("/api/v1/products/refresh")).status_code == 409


@pytest.mark.asyncio
async def test_import_media_from_url(tmp_path):
    from io import BytesIO

    from PIL import Image

    from app.media import MediaError, MediaStore, MediaTooLarge, import_from_url

    buf = BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, "JPEG")
    jpeg = buf.getvalue()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "https://cdn.example/p.jpg"})
        if request.url.path == "/big.jpg":
            return httpx.Response(200, content=b"x" * 2048, headers={"content-type": "image/jpeg"})
        if request.url.path == "/page.html":
            return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})
        return httpx.Response(200, content=jpeg, headers={"content-type": "image/jpeg"})

    hosts = []
    store = MediaStore(tmp_path / "m", 10_000)
    opts = {"transport": httpx.MockTransport(handler), "check_host": hosts.append}
    item = await import_from_url(store, "https://shop.example/redirect", 10_000, **opts)
    assert item.kind == "image" and item.path.read_bytes() == jpeg
    assert hosts == ["shop.example", "cdn.example"]  # 리디렉션한 곳도 확인
    with pytest.raises(MediaError, match="https"):
        await import_from_url(store, "http://shop.example/a.jpg", 10_000, **opts)
    with pytest.raises(MediaTooLarge):
        await import_from_url(store, "https://shop.example/big.jpg", 1000, **opts)
    with pytest.raises(MediaError, match="사진"):
        await import_from_url(store, "https://shop.example/page.html", 10_000, **opts)


def test_private_hosts_are_blocked():
    from app.media import MediaError, _assert_public_host

    for host in ("127.0.0.1", "localhost", "192.168.0.1", "10.0.0.5"):
        with pytest.raises(MediaError):
            _assert_public_host(host)
