import asyncio
import time

import httpx
import pytest

from app.main import create_app
from app.pipeline import Providers
from app.projects import Project, ProjectStore

from .conftest import FakeImages, FakePlanner, FakeTTS, requires_ffmpeg


def _project(**kw) -> Project:
    base = dict(project_id="", created_at="2026-10-10T00:00:00+00:00", title="제목", script="대본", tone="calm",
                video_file="final.mp4", job_id="j", thumbnail_files=["thumb_1.jpg"], titles=["제목1", "제목2"])
    return Project(**(base | kw))


def test_store_copies_files_lists_and_deletes(tmp_path):
    src = tmp_path / "job"
    src.mkdir()
    (src / "final.mp4").write_bytes(b"video")
    (src / "thumb_1.jpg").write_bytes(b"jpg")
    store = ProjectStore(tmp_path / "p.db", tmp_path / "lib")

    a = store.save(src, _project(title="첫 번째"))
    b = store.save(src, _project(title="두 번째", product_name="설거지통"))
    assert (tmp_path / "lib" / a.project_id / "final.mp4").read_bytes() == b"video"
    assert [p.title for p in store.list()] == ["두 번째", "첫 번째"]  # 최신순
    got = store.get(b.project_id)
    assert got.titles == ["제목1", "제목2"] and got.product_name == "설거지통"

    assert store.get("../../etc") is None and not store.delete("nope")
    assert store.delete(a.project_id)
    assert store.get(a.project_id) is None and not (tmp_path / "lib" / a.project_id).exists()


def test_store_rolls_back_when_file_missing(tmp_path):
    store = ProjectStore(tmp_path / "p.db", tmp_path / "lib")
    with pytest.raises(FileNotFoundError):
        store.save(tmp_path, _project())  # 작업 폴더에 파일이 없음
    assert store.list() == [] and list((tmp_path / "lib").iterdir()) == []


def test_store_expires_old_projects(tmp_path):
    src = tmp_path / "job"
    src.mkdir()
    (src / "final.mp4").write_bytes(b"v")
    (src / "thumb_1.jpg").write_bytes(b"t")
    store = ProjectStore(tmp_path / "p.db", tmp_path / "lib")
    store.save(src, _project())
    with store._connect() as db:
        db.execute("UPDATE projects SET created_ts = ?", (time.time() - 7200,))
    assert store.remove_older_than(3600) == 1
    assert store.list() == []


@requires_ffmpeg
@pytest.mark.asyncio
async def test_completed_job_is_saved_to_library(settings):
    app = create_app(settings, Providers(FakePlanner(2), FakeTTS(), FakeImages()))
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post("/api/v1/shorts/generate", json={
            "script": "보관함 저장 테스트 대본입니다.",
            "project": {"product_name": "접이식 설거지통", "description": "설명란\n#광고", "titles": ["제목 A"]},
        })
        job_id = r.json()["job_id"]
        async with asyncio.timeout(60):
            while (body := (await client.get(f"/api/v1/shorts/status/{job_id}")).json())["project_id"] is None:
                assert body["status"] != "FAILED", body["error"]
                await asyncio.sleep(0.1)
        project_id = body["project_id"]

        items = (await client.get("/api/v1/projects")).json()["items"]
        assert [i["project_id"] for i in items] == [project_id]
        assert items[0]["product_name"] == "접이식 설거지통" and items[0]["thumbnail_url"]

        detail = (await client.get(f"/api/v1/projects/{project_id}")).json()
        assert detail["description"] == "설명란\n#광고" and detail["titles"] == ["제목 A"]
        assert len(detail["thumbnail_urls"]) == 3

        # 작업 폴더가 지워져도 보관함 파일로 받을 수 있음
        import shutil
        shutil.rmtree(settings.output_dir / job_id)
        v = await client.get(detail["video_url"])
        assert v.status_code == 200 and v.content[4:8] == b"ftyp"
        assert (await client.get(detail["video_url"].replace("final_", "x"))).status_code == 403
        assert (await client.get(detail["video_url"].split("?")[0])).status_code == 403

        assert (await client.delete(f"/api/v1/projects/{project_id}")).status_code == 200
        assert (await client.get(f"/api/v1/projects/{project_id}")).status_code == 404
        assert (await client.get(detail["video_url"])).status_code == 404
        assert (await client.get("/library")).status_code == 200
