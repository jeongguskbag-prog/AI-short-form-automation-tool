import asyncio
import json
import stat
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.accounts import AccountStore
from app.main import create_app
from app.pipeline import Providers
from app.projects import Project
from app.publishers.youtube import UploadRequest, YouTubeClient

from .conftest import FakeImages, FakePlanner, FakeTTS, requires_ffmpeg


def test_account_store_encrypts_and_survives_restart(tmp_path):
    path = tmp_path / "acc.json"
    store = AccountStore(path)
    store.save("youtube", display_name="내 채널", secret={"refresh_token": "r-123"})
    assert "r-123" not in path.read_text()  # 평문으로 남지 않음
    assert stat.S_IMODE(path.with_suffix(".key").stat().st_mode) == 0o600
    assert AccountStore(path).get("youtube") == ("내 채널", {"refresh_token": "r-123"})  # 같은 키 파일로 다시 읽힘
    assert AccountStore(path, key="a" * 43 + "=").get("youtube") is None  # 다른 키로는 못 읽음
    assert store.delete("youtube") and store.get("youtube") is None and not store.delete("youtube")


def test_metadata(settings):
    yt = YouTubeClient(settings)
    m = yt.metadata(UploadRequest(video_path=None, title="  제목 " * 30, description="설명\n#광고",
                                  tags=["#광고", "#주방템", "#"], privacy="weird"))
    assert len(m["snippet"]["title"]) <= 100
    assert m["snippet"]["description"].endswith("#Shorts")
    assert m["snippet"]["tags"] == ["광고", "주방템"]
    assert m["status"] == {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
                           "containsSyntheticMedia": True}


class FakeGoogle:
    def __init__(self):
        self.uploaded = None
        self.metadata = None
        self.fail_upload = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://oauth2.googleapis.com/token"):
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["authorization_code"]:
                assert form["code"] == ["good-code"]
                return httpx.Response(200, json={"access_token": "a1", "refresh_token": "r1"})
            assert form["refresh_token"] == ["r1"]
            return httpx.Response(200, json={"access_token": "a2"})
        if "/youtube/v3/videos" in url and "upload" not in url:
            assert request.headers["Authorization"] == "Bearer a2"
            ids = request.url.params["id"].split(",")
            return httpx.Response(200, json={"items": [
                {"id": vid, "statistics": {"viewCount": "1234", "likeCount": "56", "commentCount": "7"}}
                for vid in ids if vid != "deleted"
            ]})
        if "/youtube/v3/channels" in url:
            return httpx.Response(200, json={"items": [{"snippet": {"title": "살림채널"}}]})
        if url.startswith("https://www.googleapis.com/upload/youtube/v3/videos"):
            assert request.headers["Authorization"] == "Bearer a2"
            if self.fail_upload:
                return httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}]}})
            self.metadata = json.loads(request.content)
            return httpx.Response(200, headers={"Location": "https://upload.example/session1"})
        if url == "https://upload.example/session1":
            self.uploaded = request.content
            return httpx.Response(200, json={"id": "abc123"})
        return httpx.Response(404)


async def _wait_publication(client, pid):
    async with asyncio.timeout(10):
        while True:
            pubs = (await client.get(f"/api/v1/projects/{pid}")).json()["publications"]
            if pubs[0]["status"] != "uploading":
                return pubs
            await asyncio.sleep(0.05)


async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@requires_ffmpeg
@pytest.mark.asyncio
async def test_connect_and_publish(settings, tmp_path):
    settings.youtube_oauth_client_id = "cid"
    settings.youtube_oauth_client_secret = "secret"
    google = FakeGoogle()
    yt = YouTubeClient(settings, transport=httpx.MockTransport(google.handler))
    app = create_app(settings, Providers(FakePlanner(), FakeTTS(), FakeImages()), youtube=yt)
    async with app.router.lifespan_context(app), await _client(app) as client:
        acc = (await client.get("/api/v1/accounts")).json()["youtube"]
        assert acc["configured"] and not acc["connected"]
        assert (await client.post("/api/v1/projects/" + "0" * 32 + "/publish", json={"title": "t"})).status_code == 404

        auth_url = (await client.post("/api/v1/accounts/youtube/connect")).json()["auth_url"]
        q = parse_qs(urlparse(auth_url).query)
        assert q["client_id"] == ["cid"] and q["access_type"] == ["offline"]
        def callback(state):
            return client.get("/oauth/youtube/callback", params={"code": "good-code", "state": state})

        assert (await callback("forged")).status_code == 400
        r = await callback(q["state"][0])
        assert r.status_code == 200 and "살림채널" in r.text
        assert (await callback(q["state"][0])).status_code == 400  # state는 한 번만 쓸 수 있음
        assert (await client.get("/api/v1/accounts")).json()["youtube"]["name"] == "살림채널"

        # 보관함 프로젝트를 만들고 올리기
        src = tmp_path / "job"
        src.mkdir()
        (src / "final.mp4").write_bytes(b"\0\0\0\x18ftypmp42")
        project = app.state.projects.save(src, Project(
            project_id="", created_at="2026-10-10T00:00:00+00:00", title="제목", script="대본", tone="calm",
            video_file="final.mp4", job_id="j", description="이 게시물은 ... 수수료를 제공받습니다.\n\n#광고 #주방템"))
        pid = project.project_id
        body = {"title": "접는 설거지통", "privacy": "unlisted"}
        r = await client.post(f"/api/v1/projects/{pid}/publish", json=body)
        assert r.status_code == 202
        pubs = await _wait_publication(client, pid)
        assert pubs[0]["status"] == "done" and pubs[0]["url"] == "https://youtube.com/shorts/abc123"
        assert google.uploaded == b"\0\0\0\x18ftypmp42"
        assert google.metadata["snippet"]["title"] == "접는 설거지통"
        assert google.metadata["snippet"]["tags"] == ["광고", "주방템"]
        assert google.metadata["status"]["privacyStatus"] == "unlisted"

        # 성과 받아 오기
        assert (await client.post("/api/v1/stats/refresh")).status_code == 202
        async with asyncio.timeout(10):
            while (stats := (await client.get("/api/v1/stats")).json())["refreshing"] or not stats["stats_at"]:
                await asyncio.sleep(0.05)
        assert (stats["uploaded_videos"], stats["total_views"], stats["total_likes"]) == (1, 1234, 56)
        assert stats["projects"][0]["project_id"] == pid and stats["last_error"] is None
        pub = (await client.get(f"/api/v1/projects/{pid}")).json()["publications"][0]
        assert (pub["video_id"], pub["views"], pub["comments"]) == ("abc123", 1234, 7)

        google.fail_upload = True
        await client.post(f"/api/v1/projects/{pid}/publish", json={"title": "다시"})
        pubs = await _wait_publication(client, pid)
        assert pubs[0]["status"] == "failed" and "할당량" in pubs[0]["error"]

        assert (await client.delete("/api/v1/accounts/youtube")).status_code == 200
        r = await client.post(f"/api/v1/projects/{pid}/publish", json={"title": "x"})
        assert r.status_code == 409 and "연결" in r.json()["detail"]


@requires_ffmpeg
@pytest.mark.asyncio
async def test_connect_requires_client_settings(settings, providers):
    app = create_app(settings, providers)
    async with app.router.lifespan_context(app), await _client(app) as client:
        assert (await client.get("/api/v1/accounts")).json()["youtube"]["configured"] is False
        assert (await client.post("/api/v1/accounts/youtube/connect")).status_code == 409


def test_old_database_gets_stats_columns(tmp_path):
    import sqlite3

    from app.projects import ProjectStore

    db = tmp_path / "p.db"
    with sqlite3.connect(db) as conn:  # 성과 칼럼이 없던 예전 형식
        conn.execute("CREATE TABLE publications (id INTEGER PRIMARY KEY, project_id TEXT, platform TEXT, "
                     "status TEXT, privacy TEXT, title TEXT, url TEXT, error TEXT, created_at TEXT, updated_at TEXT)")
    store = ProjectStore(db, tmp_path / "lib")
    assert store.publications("x") == []
    assert store.stats_summary()["total_views"] == 0
