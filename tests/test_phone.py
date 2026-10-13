import httpx
import pytest

from app.lan import ensure_access_key, phone_banner
from app.main import create_app

from .conftest import requires_ffmpeg


def test_ensure_access_key_creates_once_and_keeps_other_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=g\nAPI_KEYS=\n", encoding="utf-8")
    key = ensure_access_key(env)
    assert len(key) >= 10 and f"API_KEYS={key}" in env.read_text()
    assert "GEMINI_API_KEY=g" in env.read_text()
    assert ensure_access_key(env) == key  # 다시 켜도 같은 키

    env2 = tmp_path / "other.env"
    env2.write_text("API_KEYS=mine,second  # 메모\n", encoding="utf-8")
    assert ensure_access_key(env2) == "mine"
    assert ensure_access_key(tmp_path / "missing.env")  # 파일이 없으면 만듦


def test_banner_shows_address_and_key():
    text = phone_banner("abc123", "192.168.0.12", 8000)
    assert "http://192.168.0.12:8000" in text and "abc123" in text


@requires_ffmpeg
@pytest.mark.asyncio
async def test_phone_mode_needs_key_only_from_other_devices(settings, providers):
    settings.phone_access = True
    settings.api_keys = "phone-key"
    app = create_app(settings, providers)
    async with app.router.lifespan_context(app):
        local = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5000)),
                                  base_url="http://localhost:8000")
        phone = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("192.168.0.30", 5000)),
                                  base_url="http://192.168.0.12:8000")
        async with local, phone:
            assert (await local.get("/health")).json()["auth_required"] is False
            assert (await local.get("/api/v1/trends")).status_code == 200
            # 다른 사이트가 PC 브라우저로 몰래 보내는 요청은 키 없이 통과하지 못함
            forged = await local.get("/api/v1/trends", headers={"Origin": "https://evil.example"})
            assert forged.status_code == 401

            assert (await phone.get("/health")).json()["auth_required"] is True
            assert (await phone.get("/api/v1/trends")).status_code == 401
            assert (await phone.get("/api/v1/trends", headers={"X-API-Key": "phone-key"})).status_code == 200
            # 키 저장은 PC에서만
            r = await phone.put("/api/v1/setup/keys", json={"GEMINI_API_KEY": "x" * 20},
                                headers={"X-API-Key": "phone-key"})
            assert r.status_code == 403


@requires_ffmpeg
@pytest.mark.asyncio
async def test_phone_mode_without_key_refuses_to_start(settings, providers):
    settings.phone_access = True
    app = create_app(settings, providers)
    with pytest.raises(RuntimeError, match="접속 키"):
        async with app.router.lifespan_context(app):
            pass
