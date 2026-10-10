import httpx
import pytest

from app.env_file import EnvValueError, key_status, read_values, update_values
from app.main import create_app

from .conftest import requires_ffmpeg


def test_update_keeps_other_lines_and_appends_missing(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# 필수\nGEMINI_API_KEY=your_gemini_api_key_here\n"
        "PEXELS_API_KEY=     # https://www.pexels.com/api/\nURL_SIGNING_SECRET=abc\n",
        encoding="utf-8",
    )
    saved = update_values(env, {"GEMINI_API_KEY": " AIzaNEW1234567 ", "YOUTUBE_API_KEY": "yt-key_1", "PIXABAY_API_KEY": ""})
    assert saved == ["GEMINI_API_KEY", "YOUTUBE_API_KEY"]
    text = env.read_text(encoding="utf-8")
    assert text.startswith("# 필수\nGEMINI_API_KEY=AIzaNEW1234567\n")
    assert "URL_SIGNING_SECRET=abc" in text and text.rstrip().endswith("YOUTUBE_API_KEY=yt-key_1")
    values = read_values(env)
    assert values["PEXELS_API_KEY"] == "" and values["GEMINI_API_KEY"] == "AIzaNEW1234567"

    status = {k["key"]: k for k in key_status(env)}
    assert status["GEMINI_API_KEY"]["set"] and status["GEMINI_API_KEY"]["masked"] == "…4567"
    assert "AIzaNEW" not in str(status)
    assert not status["PEXELS_API_KEY"]["set"]


def test_placeholder_counts_as_missing(tmp_path):
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=your_gemini_api_key_here\n", encoding="utf-8")
    assert not {k["key"]: k for k in key_status(env)}["GEMINI_API_KEY"]["set"]


@pytest.mark.parametrize("updates", [
    {"GEMINI_API_KEY": "abc\nAPI_KEYS="},
    {"GEMINI_API_KEY": "abc # x"},
    {"API_KEYS": "x"},
])
def test_rejects_injection_and_unknown_keys(tmp_path, updates):
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=\n", encoding="utf-8")
    with pytest.raises(EnvValueError):
        update_values(env, updates)
    assert env.read_text(encoding="utf-8") == "GEMINI_API_KEY=\n"


@requires_ffmpeg
@pytest.mark.asyncio
async def test_keys_endpoint_saves_and_restarts(settings, providers, tmp_path):
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=\n", encoding="utf-8")
    app = create_app(settings, providers, env_path=env)
    restarted = []
    app.state.restart = lambda: restarted.append(True)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        body = (await client.get("/api/v1/setup/keys")).json()
        assert body["editable"] and body["auto_restart"] and body["keys"][0]["key"] == "GEMINI_API_KEY"

        r = await client.put("/api/v1/setup/keys", json={"GEMINI_API_KEY": "AIzaSecret9999"})
        assert r.json() == {"saved": ["GEMINI_API_KEY"], "restarting": True}
        assert "GEMINI_API_KEY=AIzaSecret9999" in env.read_text(encoding="utf-8")

        # 다른 사이트에서 보낸 요청은 막습니다.
        r = await client.put("/api/v1/setup/keys", json={"GEMINI_API_KEY": "evil"},
                             headers={"Origin": "http://evil.example"})
        assert r.status_code == 403
        r = await client.put("/api/v1/setup/keys", json={"GEMINI_API_KEY": "bad key"})
        assert r.status_code == 422

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.168.0.5", 1234)), base_url="http://t"
    ) as client:
        app.state.api_keys = ()
        r = await client.put("/api/v1/setup/keys", json={"GEMINI_API_KEY": "AIzaRemote"})
        assert r.status_code == 403
        assert not (await client.get("/api/v1/setup/keys")).json()["editable"]
    assert "AIzaRemote" not in env.read_text(encoding="utf-8")
