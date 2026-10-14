import httpx
import pytest

from app.config import Settings
from app.services.images import (
    ImageGenerationError,
    ImageSourceChain,
    NotConfiguredSource,
    PexelsError,
    PexelsImageSource,
)

from .test_errors import QUOTA

SETTINGS = Settings(max_retries=1, _env_file=None)
JPEG = b"\xff\xd8\xff fake jpeg"


def pexels_transport(photos_by_query: dict[str, list[dict]], seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.pexels.com":
            if request.headers.get("Authorization") != "good-key":
                return httpx.Response(401, text="Unauthorized")
            query = request.url.params["query"]
            assert request.url.params["orientation"] == "portrait"
            return httpx.Response(200, json={"photos": photos_by_query.get(query, [])})
        return httpx.Response(200, content=JPEG)  # 사진 다운로드

    return httpx.MockTransport(handler)


def photo(pid: int, who: str) -> dict:
    return {"id": pid, "photographer": who, "src": {"portrait": f"https://images.pexels.com/{pid}.jpg"}}


@pytest.mark.asyncio
async def test_pexels_downloads_photo_and_returns_credit(tmp_path):
    seen = []
    transport = pexels_transport({"glass of water": [photo(1, "Kim"), photo(2, "Lee")]}, seen)
    source = PexelsImageSource("good-key", SETTINGS, transport=transport)

    out = tmp_path / "a.png"
    assert await source.generate("ignored prompt", out, query="glass of water") == "Kim / Pexels"
    assert out.read_bytes() == JPEG

    # 같은 검색어라도 방금 쓴 사진은 건너뛰어 씬마다 다른 사진이 나옴
    out2 = tmp_path / "b.png"
    assert await source.generate("ignored", out2, query="glass of water") == "Lee / Pexels"


@pytest.mark.asyncio
async def test_pexels_retries_with_shorter_query_then_reports_no_result(tmp_path):
    seen = []
    transport = pexels_transport({"sunny kitchen": [photo(7, "Park")]}, seen)
    source = PexelsImageSource("good-key", SETTINGS, transport=transport)

    credit = await source.generate("x", tmp_path / "a.png", query="sunny kitchen morning table")
    assert credit == "Park / Pexels"
    queries = [r.url.params["query"] for r in seen if r.url.host == "api.pexels.com"]
    assert queries == ["sunny kitchen morning table", "sunny kitchen"]

    with pytest.raises(ImageGenerationError, match="찾지 못함"):
        await source.generate("x", tmp_path / "b.png", query="nothing matches here")


@pytest.mark.asyncio
async def test_pexels_bad_key_is_a_permanent_error(tmp_path):
    source = PexelsImageSource("bad-key", SETTINGS, transport=pexels_transport({}, []))
    with pytest.raises(PexelsError) as info:
        await source.generate("x", tmp_path / "a.png", query="water")
    assert info.value.code == 401


class Recorder:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, 0

    async def generate(self, prompt, output_path, query="", exclude=()):
        self.calls += 1
        if self.error:
            raise self.error
        output_path.write_bytes(b"img")
        return self.result


@pytest.mark.asyncio
async def test_chain_falls_back_and_skips_blocked_source(tmp_path):
    gemini = Recorder(error=QUOTA)
    pexels = Recorder(result="Kim / Pexels")
    chain = ImageSourceChain([("Gemini", gemini), ("Pexels", pexels)], cooldown_sec=3600)

    for i in range(3):
        assert await chain.generate("p", tmp_path / f"{i}.png", "q") == "Kim / Pexels"
    assert gemini.calls == 1  # 한도 0 오류 뒤에는 Gemini를 건너뜀
    assert pexels.calls == 3


@pytest.mark.asyncio
async def test_chain_keeps_trying_after_temporary_error(tmp_path):
    gemini = Recorder(error=RuntimeError("timeout"))
    pexels = Recorder(result="Kim / Pexels")
    chain = ImageSourceChain([("Gemini", gemini), ("Pexels", pexels)], cooldown_sec=3600)
    for i in range(2):
        await chain.generate("p", tmp_path / f"{i}.png")
    assert gemini.calls == 2  # 일시 오류는 막지 않음


@pytest.mark.asyncio
async def test_chain_reports_short_korean_reasons_when_all_fail(tmp_path):
    chain = ImageSourceChain(
        [("Gemini", Recorder(error=QUOTA)), ("Pexels", NotConfiguredSource("PEXELS_API_KEY를 설정하세요"))],
        cooldown_sec=3600,
    )
    with pytest.raises(ImageGenerationError) as info:
        await chain.generate("p", tmp_path / "a.png")
    message = str(info.value)
    assert message == "Gemini: 사용량 한도 초과 (무료 등급이거나 결제 필요) / Pexels: PEXELS_API_KEY를 설정하세요"
    assert "quota" not in message  # 영어 원문은 화면에 보이지 않음


def json_and_images(api_host: str, payload: dict, seen: list, broken: set[str] = frozenset()):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == api_host:
            return httpx.Response(200, json=payload)
        if str(request.url) in broken:
            return httpx.Response(404)
        return httpx.Response(200, content=JPEG)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_pixabay_parses_hits_and_sends_key(tmp_path):
    from app.services.images import PixabayImageSource

    seen = []
    payload = {"hits": [{"id": 5, "user": "Choi", "largeImageURL": "https://cdn.pixabay.com/5.jpg"}]}
    source = PixabayImageSource("pk", SETTINGS, transport=json_and_images("pixabay.com", payload, seen))
    assert await source.generate("x", tmp_path / "a.png", query="coffee cup") == "Choi / Pixabay"
    params = seen[0].url.params
    assert (params["key"], params["q"], params["orientation"]) == ("pk", "coffee cup", "vertical")


@pytest.mark.asyncio
async def test_openverse_needs_no_key_filters_licenses_and_skips_broken_images(tmp_path):
    from app.services.images import OpenverseImageSource

    seen = []
    payload = {
        "results": [
            {"id": "a", "url": "https://broken.example/a.jpg", "creator": "Gone", "license": "by"},
            {"id": "b", "url": "https://ok.example/b.jpg", "creator": "Jane", "license": "by",
             "license_version": "2.0"},
        ]
    }
    transport = json_and_images("api.openverse.org", payload, seen, broken={"https://broken.example/a.jpg"})
    source = OpenverseImageSource(SETTINGS, transport=transport)

    credit = await source.generate("x", tmp_path / "a.png", query="sunrise beach")
    assert credit == "Jane / Openverse (BY 2.0)"  # 첫 사진 다운로드 실패 → 다음 사진
    search = seen[0]
    assert "Authorization" not in search.headers
    assert search.url.params["license"] == "cc0,pdm,by"  # 출처 표기만으로 쓸 수 있는 라이선스만
    assert search.url.params["aspect_ratio"] == "tall"


def test_default_sources_work_without_any_photo_keys():
    from app.main import build_default_providers

    providers = build_default_providers(Settings(gemini_api_key="dummy", _env_file=None))
    assert providers.images.names == ["Gemini", "Pexels", "Pixabay", "Openverse"]


@pytest.mark.asyncio
async def test_missing_keys_are_skipped_silently_when_a_later_source_works(tmp_path):
    chain = ImageSourceChain(
        [
            ("Gemini", Recorder(error=QUOTA)),
            ("Pexels", NotConfiguredSource("키 없음 (PEXELS_API_KEY)")),
            ("Openverse", Recorder(result="Jane / Openverse (BY 2.0)")),
        ],
        cooldown_sec=3600,
    )
    assert await chain.generate("p", tmp_path / "a.png") == "Jane / Openverse (BY 2.0)"


@pytest.mark.asyncio
async def test_photos_of_the_wrong_kind_are_dropped(tmp_path):
    # 3쿠션을 검색했는데 포켓볼 사진이 섞여 나오는 경우
    pool = {**photo(1, "Pool"), "alt": "Man playing 8-ball pool", "url": "https://www.pexels.com/photo/pool-1/"}
    carom = {**photo(2, "Carom"), "alt": "Carom billiards table", "url": "https://www.pexels.com/photo/carom-2/"}
    transport = pexels_transport({"carom billiards table": [pool, carom]}, [])
    source = PexelsImageSource("good-key", SETTINGS, transport=transport)

    credit = await source.generate("x", tmp_path / "a.png", "carom billiards table", exclude=["pool", "8 ball"])
    assert credit == "Carom / Pexels"

    with pytest.raises(ImageGenerationError, match="찾지 못함"):  # 틀린 사진만 있으면 쓰지 않음
        await source.generate("x", tmp_path / "b.png", "carom billiards table", exclude=["pool", "carom"])


def test_mentions_any_matches_whole_words_only():
    from app.services.images import mentions_any

    assert mentions_any("snooker-table green", ["snooker"])
    assert mentions_any("Two pool tables", ["pool", "pocket"])
    assert mentions_any("9-ball rack", ["9 ball"])
    assert not mentions_any("whirlpool spa", ["pool"])
    assert not mentions_any("anything", [])
