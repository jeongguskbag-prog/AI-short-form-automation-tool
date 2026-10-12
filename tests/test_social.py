import asyncio
import hashlib
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.main import create_app
from app.pipeline import Providers
from app.projects import Project
from app.publishers.meta import MetaClient, MetaError
from app.publishers.threads import ThreadsClient
from app.publishers.tiktok import CHUNK, SINGLE_CHUNK_MAX, TikTokClient

from .conftest import FakeImages, FakePlanner, FakeTTS, requires_ffmpeg


async def _no_sleep(_):
    return None


def _form(request: httpx.Request) -> dict:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


# --------------------------------------------------------------------------- Meta
class FakeMeta:
    def __init__(self, with_instagram=True):
        self.with_instagram = with_instagram
        self.uploads = {}
        self.polls = 0
        self.published = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        path = url.path
        if path.endswith("/oauth/access_token"):
            assert url.params["fb_exchange_token"] == "short-user-token-123456"
            return httpx.Response(200, json={"access_token": "long-user"})
        if path.endswith("/me/accounts"):
            page = {"id": "p1", "name": "살림가게", "access_token": "page-tok"}
            if self.with_instagram:
                page["instagram_business_account"] = {"id": "ig1", "username": "salim"}
            return httpx.Response(200, json={"data": [{"id": "p0", "name": "다른 페이지", "access_token": "x"}, page]
                                             if self.with_instagram else [page]})
        if url.host == "rupload.facebook.com":
            assert request.headers["Authorization"] == "OAuth page-tok"
            assert request.headers["file_size"] == str(len(request.content))
            self.uploads[path] = request.content
            return httpx.Response(200, json={"success": True})
        if path.endswith("/ig1/media"):
            form = _form(request)
            assert form["media_type"] == "REELS" and form["upload_type"] == "resumable"
            return httpx.Response(200, json={"id": "c1"})
        if path.endswith("/c1"):
            self.polls += 1
            return httpx.Response(200, json={"status_code": "IN_PROGRESS" if self.polls < 2 else "FINISHED"})
        if path.endswith("/ig1/media_publish"):
            self.published.append(("ig", _form(request)["creation_id"]))
            return httpx.Response(200, json={"id": "m1"})
        if path.endswith("/m1"):
            return httpx.Response(200, json={"permalink": "https://www.instagram.com/reel/abc/"})
        if path.endswith("/p1/video_reels"):
            form = _form(request)
            if form["upload_phase"] == "start":
                return httpx.Response(200, json={"video_id": "v9",
                                                 "upload_url": "https://rupload.facebook.com/video-upload/v24.0/v9"})
            assert form["video_state"] == "PUBLISHED"
            self.published.append(("fb", form["description"]))
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404, json={"error": {"message": f"unexpected {path}"}})


@pytest.mark.asyncio
async def test_meta_connect_picks_page_with_instagram(settings, tmp_path):
    settings.meta_app_id, settings.meta_app_secret = "app", "sec"
    fake = FakeMeta()
    meta = MetaClient(settings, httpx.MockTransport(fake.handler), sleep=_no_sleep)
    name, secret = await meta.connect(" short-user-token-123456 ")
    assert secret == {"page_id": "p1", "page_name": "살림가게", "page_token": "page-tok",
                      "ig_user_id": "ig1", "ig_username": "salim"}
    assert "@salim" in name

    video = tmp_path / "v.mp4"
    video.write_bytes(b"movie")
    assert await meta.publish_instagram(secret, video, "캡션") == ("m1", "https://www.instagram.com/reel/abc/")
    assert fake.polls == 2 and fake.uploads["/ig-api-upload/v24.0/c1"] == b"movie"
    assert await meta.publish_facebook(secret, video, "설명") == ("v9", "https://www.facebook.com/reel/v9")
    assert fake.published == [("ig", "c1"), ("fb", "설명")]


@pytest.mark.asyncio
async def test_meta_errors(settings, tmp_path):
    settings.meta_app_id, settings.meta_app_secret = "app", "sec"
    meta = MetaClient(settings, httpx.MockTransport(FakeMeta(with_instagram=False).handler))
    _, secret = await meta.connect("short-user-token-123456")
    with pytest.raises(MetaError, match="프로페셔널"):
        await meta.publish_instagram(secret, tmp_path / "x.mp4", "c")

    expired = MetaClient(settings, httpx.MockTransport(
        lambda r: httpx.Response(400, json={"error": {"code": 190, "message": "expired"}})))
    with pytest.raises(MetaError, match="만료"):
        await expired.connect("short-user-token-123456")


# --------------------------------------------------------------------------- Threads
@pytest.mark.asyncio
async def test_threads_connect_refresh_and_publish(settings, tmp_path):
    settings.threads_app_secret = "tsec"
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        url, path = request.url, request.url.path
        calls.append(path)
        if path == "/access_token":
            return httpx.Response(200, json={"access_token": "long-t", "expires_in": 5_184_000})
        if path == "/refresh_access_token":
            return httpx.Response(200, json={"access_token": "renewed", "expires_in": 5_184_000})
        if path == "/v1.0/me":
            return httpx.Response(200, json={"id": "u1", "username": "salim"})
        if url.host == "tmpfiles.org":
            assert b"movie" in request.content
            return httpx.Response(200, json={"status": "success", "data": {"url": "http://tmpfiles.org/77/v.mp4"}})
        if path == "/v1.0/u1/threads":
            form = _form(request)
            assert form["video_url"] == "https://tmpfiles.org/dl/77/v.mp4" and len(form["text"]) <= 500
            return httpx.Response(200, json={"id": "c1"})
        if path == "/v1.0/c1":
            return httpx.Response(200, json={"status": "FINISHED"})
        if path == "/v1.0/u1/threads_publish":
            return httpx.Response(200, json={"id": "t1"})
        if path == "/v1.0/t1":
            return httpx.Response(200, json={"permalink": "https://www.threads.net/@salim/post/x"})
        return httpx.Response(404)

    now = [1000.0]
    client = ThreadsClient(settings, httpx.MockTransport(handler), sleep=_no_sleep, clock=lambda: now[0])
    name, secret = await client.connect("short-threads-token-000")
    assert name == "@salim" and secret["token"] == "long-t" and secret["expires_at"] == 1000 + 5_184_000
    assert await client.fresh_secret(secret) is None
    now[0] += 5_184_000 - 86400  # 만료 하루 전
    assert (await client.fresh_secret(secret))["token"] == "renewed"

    video = tmp_path / "v.mp4"
    video.write_bytes(b"movie")
    assert await client.publish(secret, video, "글" * 900) == ("t1", "https://www.threads.net/@salim/post/x")


# --------------------------------------------------------------------------- TikTok
def test_tiktok_chunks():
    assert TikTokClient.chunks(5) == (5, 1)
    assert TikTokClient.chunks(SINGLE_CHUNK_MAX) == (SINGLE_CHUNK_MAX, 1)
    size = SINGLE_CHUNK_MAX + 5 * CHUNK + 123
    assert TikTokClient.chunks(size) == (CHUNK, size // CHUNK)


@pytest.mark.asyncio
async def test_tiktok_pkce_auth_and_publish(settings, tmp_path):
    settings.tiktok_client_key, settings.tiktok_client_secret = "ck", "cs"
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/oauth/token/":
            form = _form(request)
            seen.setdefault("token_forms", []).append(form)
            return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "open_id": "o1",
                                             "expires_in": 86400, "refresh_expires_in": 31536000})
        if request.url.host == "open-upload.tiktokapis.com":
            seen["range"] = request.headers["Content-Range"]
            return httpx.Response(201)
        assert request.headers["Authorization"] == "Bearer at"
        if path == "/v2/user/info/":
            return httpx.Response(200, json={"data": {"user": {"display_name": "살림왕"}}, "error": {"code": "ok"}})
        if path == "/v2/post/publish/creator_info/query/":
            return httpx.Response(200, json={"data": {"privacy_level_options": ["SELF_ONLY"]}, "error": {"code": "ok"}})
        if path == "/v2/post/publish/video/init/":
            seen["init"] = json.loads(request.content)
            return httpx.Response(200, json={"data": {"publish_id": "pub1", "upload_url": "https://open-upload.tiktokapis.com/u/1"},
                                             "error": {"code": "ok"}})
        if path == "/v2/post/publish/status/fetch/":
            seen["polls"] = seen.get("polls", 0) + 1
            status = "PROCESSING_UPLOAD" if seen["polls"] < 2 else "PUBLISH_COMPLETE"
            return httpx.Response(200, json={"data": {"status": status, "publicaly_available_post_id": [7311]},
                                             "error": {"code": "ok"}})
        return httpx.Response(404)

    client = TikTokClient(settings, httpx.MockTransport(handler), sleep=_no_sleep, clock=lambda: 100.0)
    q = parse_qs(urlparse(client.auth_url()).query)
    assert q["redirect_uri"] == ["http://localhost:8000/oauth/tiktok/callback"]
    assert q["code_challenge_method"] == ["S256"] and q["scope"] == ["user.info.basic,video.publish"]
    verifier = client.consume_state(q["state"][0])
    assert hashlib.sha256(verifier.encode()).hexdigest() == q["code_challenge"][0]
    assert client.consume_state(q["state"][0]) is None  # 한 번만

    secret = await client.exchange_code("code1", verifier)
    assert seen["token_forms"][0]["code_verifier"] == verifier
    assert secret["expires_at"] == 100 + 86400
    assert await client.display_name(secret) == "살림왕"

    video = tmp_path / "v.mp4"
    video.write_bytes(b"0123456789")
    # 공개를 골라도 심사 전 앱은 나만 보기만 허용 → SELF_ONLY
    assert await client.publish(secret, video, "제목 #광고", "public") == ("7311", "https://www.tiktok.com/video/7311")
    assert seen["init"]["post_info"]["privacy_level"] == "SELF_ONLY"
    assert seen["init"]["source_info"] == {"source": "FILE_UPLOAD", "video_size": 10, "chunk_size": 10,
                                           "total_chunk_count": 1}
    assert seen["range"] == "bytes 0-9/10"


# --------------------------------------------------------------------------- API (연결 → 여러 플랫폼에 올리기)
@requires_ffmpeg
@pytest.mark.asyncio
async def test_connect_meta_and_publish_to_instagram_and_facebook(settings, tmp_path):
    settings.meta_app_id, settings.meta_app_secret = "app", "sec"
    fake = FakeMeta()
    meta = MetaClient(settings, httpx.MockTransport(fake.handler), sleep=_no_sleep)
    app = create_app(settings, Providers(FakePlanner(), FakeTTS(), FakeImages()), meta=meta)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        accounts = (await client.get("/api/v1/accounts")).json()
        assert accounts["instagram"] == {"configured": True, "connected": False, "name": None}
        assert accounts["tiktok"]["configured"] is False
        assert (await client.post("/api/v1/accounts/tiktok/connect")).status_code == 409
        assert (await client.post("/api/v1/accounts/meta/connect", json={"token": "short"})).status_code == 422

        r = await client.post("/api/v1/accounts/meta/connect", json={"token": "short-user-token-123456"})
        assert r.status_code == 200 and r.json()["instagram"] is True
        accounts = (await client.get("/api/v1/accounts")).json()
        assert accounts["instagram"]["name"] == "@salim" and accounts["facebook"]["name"] == "살림가게"

        src = tmp_path / "job"
        src.mkdir()
        (src / "final.mp4").write_bytes(b"movie")
        pid = app.state.projects.save(src, Project(
            project_id="", created_at="2026-10-10T00:00:00+00:00", title="제목", script="대본", tone="calm",
            video_file="final.mp4", job_id="j", description="광고 표시\n\n#광고 #캠핑")).project_id
        r = await client.post(f"/api/v1/projects/{pid}/publish", json={"title": "캠핑 랜턴", "platform": "instagram"})
        assert r.status_code == 202
        r = await client.post(f"/api/v1/projects/{pid}/publish", json={"title": "캠핑 랜턴", "platform": "facebook"})
        assert r.status_code == 202
        r = await client.post(f"/api/v1/projects/{pid}/publish", json={"title": "x", "platform": "threads"})
        assert r.status_code == 409 and "스레드" in r.json()["detail"]

        async with asyncio.timeout(10):
            while True:
                pubs = (await client.get(f"/api/v1/projects/{pid}")).json()["publications"]
                if all(p["status"] != "uploading" for p in pubs):
                    break
                await asyncio.sleep(0.05)
        by_platform = {p["platform"]: p for p in pubs}
        assert by_platform["instagram"]["url"] == "https://www.instagram.com/reel/abc/"
        assert by_platform["facebook"]["url"] == "https://www.facebook.com/reel/v9"
        assert ("fb", "캠핑 랜턴\n\n광고 표시\n\n#광고 #캠핑") in fake.published

        assert (await client.delete("/api/v1/accounts/meta")).status_code == 200
        assert (await client.delete("/api/v1/accounts/nope")).status_code == 404
        assert (await client.get("/api/v1/accounts")).json()["facebook"]["connected"] is False
