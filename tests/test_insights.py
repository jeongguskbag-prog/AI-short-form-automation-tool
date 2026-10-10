import httpx
import pytest

from app.main import create_app
from app.pipeline import Providers
from app.projects import Project, ProjectStore
from app.services.scriptwriter import ProductBrief, build_prompt, format_insights

from .conftest import FakeImages, FakePlanner, FakeTTS, requires_ffmpeg
from .test_scriptwriter import FakeWriter


def _store_with(tmp_path, views_by_hook: dict[str, int | None]) -> ProjectStore:
    src = tmp_path / "job"
    src.mkdir(exist_ok=True)
    (src / "final.mp4").write_bytes(b"v")
    store = ProjectStore(tmp_path / "p.db", tmp_path / "lib")
    for i, (hook, views) in enumerate(views_by_hook.items()):
        p = store.save(src, Project(project_id="", created_at="2026-10-10T00:00:00+00:00", title=f"제목{i}",
                                    script=f"\n{hook}\n본문입니다.", tone="calm", video_file="final.mp4", job_id="j"))
        pub = store.add_publication(p.project_id, "youtube", title="t", privacy="private")
        store.finish_publication(pub, url="u", video_id=f"v{i}")
        if views is not None:
            store.update_stats("youtube", {f"v{i}": {"views": views, "likes": 1, "comments": 2}})
    return store


def test_insights_need_two_measured_videos(tmp_path):
    store = _store_with(tmp_path, {"하나뿐": 100, "성과 없음": None})
    assert store.channel_insights() == []


def test_insights_pick_best_and_worst_hooks(tmp_path):
    store = _store_with(tmp_path, {f"후킹{i}": v for i, v in enumerate([10, 5000, 300, 90000, 1, 700, 20])})
    hooks = [i["hook"] for i in store.channel_insights(top=2)]
    assert hooks == ["후킹3", "후킹1", "후킹0", "후킹4"]  # 잘된 2개 + 덜된 2개 (조회수 순)
    text = format_insights(store.channel_insights(top=1))
    assert text.splitlines()[0] == '- 조회 90,000 · 댓글 2: "후킹3"'
    prompt = build_prompt(ProductBrief("설거지통", insights=text))
    assert "내 채널 성과" in prompt and "후킹3" in prompt
    assert "내 채널 성과" not in build_prompt(ProductBrief("설거지통"))


@requires_ffmpeg
@pytest.mark.asyncio
async def test_shopping_script_uses_channel_insights(settings):
    writer = FakeWriter()
    app = create_app(settings, Providers(FakePlanner(), FakeTTS(), FakeImages(), scriptwriter=writer))
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post("/api/v1/scripts/shopping", json={"product_name": "설거지통"})
        assert r.json()["insights_used"] == 0 and writer.briefs[-1].insights == ""

        # 보관함에 성과가 있는 영상 2개를 넣음
        store = app.state.projects
        src = settings.output_dir / "seed"
        src.mkdir(parents=True)
        (src / "final.mp4").write_bytes(b"v")
        for i, views in enumerate([800, 20]):
            p = store.save(src, Project(project_id="", created_at="2026-10-10T00:00:00+00:00", title="t",
                                        script=f"첫 문장 {i}", tone="calm", video_file="final.mp4", job_id="j"))
            store.finish_publication(store.add_publication(p.project_id, "youtube", title="t", privacy="private"),
                                     url="u", video_id=f"v{i}")
            store.update_stats("youtube", {f"v{i}": {"views": views, "likes": 0, "comments": 0}})

        r = await client.post("/api/v1/scripts/shopping", json={"product_name": "설거지통"})
        assert r.json()["insights_used"] == 2
        assert "첫 문장 0" in writer.briefs[-1].insights
        r = await client.post("/api/v1/scripts/shopping", json={"product_name": "x", "use_channel_insights": False})
        assert r.json()["insights_used"] == 0
