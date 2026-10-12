import httpx
import pytest

from app.main import create_app
from app.services.setup_check import SetupChecker

from .conftest import requires_ffmpeg


def _transport(status_by_host: dict[str, int]):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_by_host.get(request.url.host, 200), json={})
    return httpx.MockTransport(handler)


async def _gemini_ok():
    return "키가 동작합니다"


async def _tts_ok(path):
    path.write_bytes(b"ID3")


def _by_key(results):
    return {r["key"]: r for r in results}


@requires_ffmpeg
@pytest.mark.asyncio
async def test_all_configured_and_working(settings):
    settings.gemini_api_key = "g"
    settings.youtube_api_key = "y"
    settings.pexels_api_key = "p"
    settings.pixabay_api_key = "x"

    async def yt_status():
        return True, True, "살림채널"

    checker = SetupChecker(settings, transport=_transport({}), gemini_check=_gemini_ok, tts_check=_tts_ok,
                           youtube_status=yt_status)
    r = _by_key(await checker.run())
    assert {k: v["status"] for k, v in r.items()} == {
        "ffmpeg": "ok", "storage": "ok", "gemini": "ok", "tts": "ok", "youtube_data": "ok",
        "pexels": "ok", "pixabay": "ok", "openverse": "ok", "youtube_upload": "ok",
        "naver": "skipped", "aliexpress": "skipped", "ebay": "skipped",
        "instagram": "skipped", "facebook": "skipped", "threads": "skipped", "tiktok": "skipped",
    }
    assert "살림채널" in r["youtube_upload"]["message"]


@pytest.mark.asyncio
async def test_missing_keys_and_failures_explain_how_to_fix(settings):
    settings.pexels_api_key = "bad"

    async def tts_broken(path):
        raise RuntimeError("network down")

    async def yt_status():
        return True, False, None

    checker = SetupChecker(settings, transport=_transport({"api.pexels.com": 401, "api.openverse.org": 429}),
                           tts_check=tts_broken, youtube_status=yt_status)
    r = _by_key(await checker.run())
    assert r["gemini"]["status"] == "error" and "aistudio" in r["gemini"]["hint"]
    assert r["youtube_data"]["status"] == "warn"
    assert r["pexels"]["status"] == "error" and "키가 올바르지 않거나" in r["pexels"]["message"]
    assert r["pixabay"]["status"] == "skipped"
    assert r["openverse"]["status"] == "warn" and "429" in r["openverse"]["message"]
    assert r["tts"]["status"] == "error" and "network down" in r["tts"]["message"]
    assert r["youtube_upload"]["status"] == "warn" and "연결" in r["youtube_upload"]["hint"]


@pytest.mark.asyncio
async def test_unreachable_service_is_reported(settings):
    settings.youtube_api_key = "y"

    def handler(request):
        raise httpx.ConnectError("blocked")

    checker = SetupChecker(settings, transport=httpx.MockTransport(handler))
    r = _by_key(await checker.run())
    assert r["youtube_data"]["status"] == "error" and "연결할 수 없습니다" in r["youtube_data"]["message"]


@requires_ffmpeg
@pytest.mark.asyncio
async def test_setup_endpoint_and_page(settings, providers):
    checker = SetupChecker(settings, transport=_transport({}))
    app = create_app(settings, providers, setup_checker=checker)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        checks = (await client.get("/api/v1/setup/check")).json()["checks"]
        assert len(checks) == 16 and {c["key"] for c in checks} >= {"gemini", "ffmpeg"}
        page = await client.get("/setup")
        assert page.status_code == 200 and "/api/v1/setup/check" in page.text


@pytest.mark.asyncio
async def test_shops_and_social_checks(settings):
    settings.naver_client_id, settings.naver_client_secret = "n", "s"
    settings.ebay_client_id, settings.ebay_client_secret = "e", "s"

    async def social(platform):
        return {"instagram": (True, True, "@salim"), "facebook": (True, False, None),
                "threads": (False, False, None), "tiktok": (True, True, "살림왕")}[platform]

    checker = SetupChecker(settings, transport=_transport({"api.ebay.com": 401}), social_status=social)
    r = _by_key(await checker.run())
    assert r["naver"]["status"] == "ok"
    assert r["ebay"]["status"] == "error" and "Production" in r["ebay"]["hint"]
    assert r["aliexpress"]["status"] == "skipped"
    assert r["instagram"]["status"] == "ok" and "@salim" in r["instagram"]["message"]
    assert r["facebook"]["status"] == "warn"
    assert r["threads"]["status"] == "skipped"
    assert r["tiktok"]["status"] == "ok"
