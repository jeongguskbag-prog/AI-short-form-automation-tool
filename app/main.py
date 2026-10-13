import asyncio
import html
import json
import logging
import secrets
import shutil
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse

from .accounts import AccountStore
from .auth import is_local_request, key_required, require_api_key
from .config import Settings, get_settings
from .env_file import EnvValueError, key_status, update_values
from .errors import api_error_code, describe_error
from .jobs import JobStore
from .media import MediaError, MediaStore, MediaTooLarge, import_from_url
from .pipeline import Providers, ShortsPipeline
from .projects import Project, ProjectStore, now_iso
from .publishers.meta import MetaClient, MetaError
from .publishers.threads import ThreadsClient, ThreadsError
from .publishers.tiktok import TikTokClient, TikTokError
from .publishers.youtube import UploadRequest, YouTubeClient, YouTubeError
from .schemas import (
    ConnectTokenRequest,
    ImportMediaRequest,
    ProductBriefRequest,
    CreateJobResponse,
    JobStatus,
    JobStatusResponse,
    PublishRequest,
    ReviseRequest,
    ScriptRequest,
    ShoppingScriptRequest,
    ShoppingScriptResponse,
)
from .services import products, trends, video
from .services.media_search import MediaSearch
from .services.scriptwriter import ProductBrief, format_insights
from .services.setup_check import CheckFailed, SetupChecker
from .signing import LIBRARY_ROUTE_PREFIX, VIDEO_ROUTE_PREFIX, URLSigner

logger = logging.getLogger(__name__)

WEB_INDEX = Path(__file__).parent / "web" / "index.html"
WEB_TRENDS = Path(__file__).parent / "web" / "trends.html"
WEB_LIBRARY = Path(__file__).parent / "web" / "library.html"
WEB_SETUP = Path(__file__).parent / "web" / "setup.html"


def build_default_providers(settings: Settings) -> Providers:
    from google import genai

    from .services.footage import PexelsVideoSource, PixabayVideoSource
    from .services.images import (
        GeminiImageGenerator,
        ImageSourceChain,
        NotConfiguredSource,
        OpenverseImageSource,
        PexelsImageSource,
        PixabayImageSource,
    )
    from .services.matcher import GeminiMediaMatcher
    from .services.planner import GeminiPlanner
    from .services.product_brief import GeminiProductBriefer
    from .services.scriptwriter import GeminiScriptWriter
    from .services.trend_analysis import GeminiTrendAnalyzer
    from .services.tts import EdgeTTS, TypecastTTS

    # api_key가 None이면 SDK가 GEMINI_API_KEY / GOOGLE_API_KEY 환경변수를 사용합니다.
    client = genai.Client(api_key=settings.gemini_api_key)
    sources: list[tuple[str, object]] = []
    for name in (n.strip().lower() for n in settings.image_sources.split(",")):
        if name == "gemini":
            sources.append(("Gemini", GeminiImageGenerator(client, settings)))
        elif name == "pexels":
            if settings.pexels_api_key:
                sources.append(("Pexels", PexelsImageSource(settings.pexels_api_key, settings)))
            else:
                sources.append(("Pexels", NotConfiguredSource("키 없음 (PEXELS_API_KEY)")))
        elif name == "pixabay":
            if settings.pixabay_api_key:
                sources.append(("Pixabay", PixabayImageSource(settings.pixabay_api_key, settings)))
            else:
                sources.append(("Pixabay", NotConfiguredSource("키 없음 (PIXABAY_API_KEY)")))
        elif name == "openverse":
            sources.append(("Openverse", OpenverseImageSource(settings)))
        elif name:
            logger.warning(
                "알 수 없는 이미지 소스 %r는 건너뜁니다 (gemini, pexels, pixabay, openverse 중 선택)", name
            )
    footage = []
    if settings.stock_video:
        if settings.pexels_api_key:
            footage.append(("Pexels 영상", PexelsVideoSource(settings.pexels_api_key, settings)))
        if settings.pixabay_api_key:
            footage.append(("Pixabay 영상", PixabayVideoSource(settings.pixabay_api_key, settings)))
    return Providers(
        planner=GeminiPlanner(client, settings),
        tts=TypecastTTS(settings) if settings.typecast_api_key and settings.typecast_voice_id else EdgeTTS(settings),
        images=ImageSourceChain(sources, settings.image_source_cooldown_sec),
        scriptwriter=GeminiScriptWriter(client, settings),
        trend_analyzer=GeminiTrendAnalyzer(client, settings),
        matcher=GeminiMediaMatcher(client, settings),
        briefer=GeminiProductBriefer(client, settings),
        footage=ImageSourceChain(footage, settings.image_source_cooldown_sec) if footage else None,
    )


def create_app(
    settings: Settings | None = None,
    providers: Providers | None = None,
    trend_source: trends.YouTubeTrendSource | None = None,
    youtube: YouTubeClient | None = None,
    setup_checker: SetupChecker | None = None,
    env_path: Path = Path(".env"),
    product_sources: list | None = None,
    meta: MetaClient | None = None,
    threads: ThreadsClient | None = None,
    tiktok: TikTokClient | None = None,
    import_options: dict | None = None,
    media_search: MediaSearch | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    settings.output_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        video.ensure_ffmpeg()
        spec = video.VideoSpec(
            width=settings.video_width,
            height=settings.video_height,
            fps=settings.video_fps,
            max_zoom=settings.max_zoom,
            padding_sec=settings.scene_padding_sec,
            font_path=video.find_korean_font(settings.font_path),
            font_size=settings.subtitle_font_size,
            timeout_sec=settings.ffmpeg_timeout_sec,
        )
        logger.info("자막 폰트: %s", spec.font_path)

        app.state.api_keys = settings.api_key_list
        app.state.local_without_key = settings.phone_access
        if settings.phone_access and not app.state.api_keys:
            raise RuntimeError("폰 접속 모드에는 접속 키(API_KEYS)가 필요합니다. python main.py --phone으로 켜면 자동으로 만듭니다.")
        if not app.state.api_keys:
            logger.warning("API_KEYS가 비어 있어 인증 없이 실행합니다. 외부에 공개하지 마세요.")

        if settings.url_signing_secret:
            secret = settings.url_signing_secret.encode()
        else:
            secret = secrets.token_bytes(32)
            logger.info("URL_SIGNING_SECRET이 없어 임시 비밀키를 만들었습니다. 재시작하면 기존 영상 링크는 무효가 됩니다.")
        app.state.signer = URLSigner(secret, settings.video_url_ttl_sec)

        app.state.settings = settings
        app.state.jobs = JobStore()
        app.state.media = MediaStore(settings.media_dir, settings.max_upload_mb * 1024 * 1024)
        app.state.projects = await asyncio.to_thread(
            ProjectStore, settings.projects_db_path, settings.library_dir
        )
        await asyncio.to_thread(app.state.projects.fail_interrupted)
        app.state.accounts = AccountStore(settings.accounts_path, settings.token_encryption_key)
        app.state.youtube = youtube or YouTubeClient(settings)
        app.state.meta = meta or MetaClient(settings)
        app.state.threads = threads or ThreadsClient(settings)
        app.state.tiktok = tiktok or TikTokClient(settings)
        app.state.stats = StatsRefresher(app)
        app.state.setup_checker = setup_checker or _default_setup_checker(app, settings)
        app.state.providers = providers or build_default_providers(settings)
        app.state.pipeline = ShortsPipeline(app.state.providers, settings, spec)
        app.state.tasks = set()

        source = trend_source
        if source is None and settings.youtube_api_key:
            source = trends.YouTubeTrendSource(settings)
        if source is None:
            logger.info("YOUTUBE_API_KEY가 없어 아이템 찾기 수집을 끕니다.")
        store = await asyncio.to_thread(trends.TrendStore, settings.trends_db_path)
        app.state.trends = trends.TrendCollector(store, source, settings)
        app.state.products = products.ProductCollector(settings, settings.products_path, product_sources)

        background = [
            asyncio.create_task(_cleanup_loop(app, settings)),
            asyncio.create_task(app.state.trends.loop()),
            asyncio.create_task(app.state.products.loop(settings.trends_refresh_hours * 3600)),
            asyncio.create_task(app.state.stats.loop(settings.stats_refresh_hours * 3600)),
            asyncio.create_task(_publish_scheduled_loop(app, settings.publish_check_sec)),
        ]
        try:
            yield
        finally:
            for task in [*background, *app.state.tasks]:
                task.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.gather(*background, *app.state.tasks, return_exceptions=True)

    app = FastAPI(
        title="AI 쇼핑 쇼츠 자동화",
        version="2.0.0",
        description="대본 한 편으로 자막·나레이션·이미지가 들어간 9:16 쇼츠 영상을 생성합니다.",
        lifespan=lifespan,
    )

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(WEB_INDEX)

    @app.get("/setup", include_in_schema=False)
    async def setup_page() -> FileResponse:
        return FileResponse(WEB_SETUP)

    @app.get("/api/v1/setup/check", tags=["system"], dependencies=[Depends(require_api_key)])
    async def setup_check(request: Request) -> dict:
        """키와 외부 서비스가 실제로 동작하는지 하나씩 시험합니다 (몇 초 걸림, 비용은 거의 없음)."""
        return {"checks": await request.app.state.setup_checker.run()}

    @app.get("/api/v1/setup/keys", tags=["system"], dependencies=[Depends(require_api_key)])
    async def setup_keys(request: Request) -> dict:
        """화면에서 넣을 수 있는 키 목록과 입력 여부 (값은 끝 4자리만)."""
        return {
            "keys": key_status(env_path),
            "editable": is_local_request(request),
            "auto_restart": getattr(request.app.state, "restart", None) is not None,
        }

    @app.put("/api/v1/setup/keys", tags=["system"], dependencies=[Depends(require_api_key)])
    async def save_setup_keys(request: Request, body: dict[str, str]) -> dict:
        """.env에 키를 저장합니다. 이 PC에서 연 화면에서만 됩니다. 바뀐 키는 다시 켜야 적용됩니다."""
        if not is_local_request(request):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "키 저장은 서버가 켜진 PC의 브라우저에서만 할 수 있어요.")
        try:
            saved = await asyncio.to_thread(update_values, env_path, body)
        except EnvValueError as exc:
            raise HTTPException(422, str(exc)) from None
        restart = getattr(request.app.state, "restart", None)
        if saved and restart is not None:
            # 응답을 먼저 보내고 다시 켭니다.
            asyncio.get_running_loop().call_later(0.5, restart)
        return {"saved": saved, "restarting": bool(saved and restart is not None)}

    @app.get("/api/v1/voices/typecast", tags=["system"], dependencies=[Depends(require_api_key)])
    async def typecast_voices() -> dict:
        """타입캐스트 목소리 목록 (설정 점검의 목소리 고르기용, 크레딧을 쓰지 않음)."""
        from .services.tts import TypecastError, TypecastTTS

        if not settings.typecast_api_key:
            raise HTTPException(status_code=409, detail="TYPECAST_API_KEY를 먼저 넣고 저장하세요.")
        try:
            voices = await TypecastTTS(settings).voices()
        except TypecastError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        return {"voices": voices, "selected": settings.typecast_voice_id}

    @app.get("/library", include_in_schema=False)
    async def library_page() -> FileResponse:
        return FileResponse(WEB_LIBRARY)

    @app.get("/trends", include_in_schema=False)
    async def trends_page() -> FileResponse:
        return FileResponse(WEB_TRENDS)

    @app.get("/health", tags=["system"])
    async def health(request: Request) -> dict:
        # 웹 화면이 키 입력란을 띄울지 판단하는 데 씁니다. 작업 수 같은 내부 정보는 노출하지 않습니다.
        return {"status": "ok", "auth_required": key_required(request)}

    @app.post(
        "/api/v1/shorts/generate",
        response_model=CreateJobResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["shorts"],
        dependencies=[Depends(require_api_key)],
    )
    async def create_shorts_job(req: ScriptRequest, request: Request) -> CreateJobResponse:
        state = request.app.state
        media = []
        for media_id in req.media_ids:
            item = await state.media.get(media_id)
            if item is None:
                raise HTTPException(
                    status_code=422, detail=f"올린 파일을 찾을 수 없습니다: {media_id} (다시 올려 주세요)"
                )
            if item.kind == "audio":
                raise HTTPException(status_code=422, detail="음악 파일은 배경음악(bgm_media_id)으로 넣어 주세요.")
            media.append(item)
        bgm = None
        if req.bgm_media_id:
            bgm = await state.media.get(req.bgm_media_id)
            if bgm is None or bgm.kind != "audio":
                raise HTTPException(status_code=422, detail="배경음악 파일을 찾을 수 없습니다 (다시 올려 주세요).")
        job = state.jobs.create(
            req.script, req.tone, media, req.thumbnail_text.strip(), bgm, req.bgm_volume
        )
        job.media_order = req.media_order
        if req.project is not None:
            job.project_info = req.project.model_dump()
        # BackgroundTasks 대신 직접 태스크를 관리해 종료 시 깔끔하게 취소합니다.
        task = asyncio.create_task(_run_and_save(request.app, job), name=f"shorts-{job.job_id}")
        state.tasks.add(task)
        task.add_done_callback(state.tasks.discard)
        return CreateJobResponse(
            job_id=job.job_id,
            status=job.status,
            status_url=str(request.url_for("get_job_status", job_id=job.job_id).path),
            message="작업이 대기열에 등록되었습니다.",
        )

    @app.get(
        "/api/v1/shorts/status/{job_id}",
        response_model=JobStatusResponse,
        tags=["shorts"],
        dependencies=[Depends(require_api_key)],
    )
    async def get_job_status(job_id: str, request: Request) -> JobStatusResponse:
        job = request.app.state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
        return job.to_response(request.app.state.signer)

    @app.put(
        "/api/v1/media",
        status_code=status.HTTP_201_CREATED,
        tags=["media"],
        dependencies=[Depends(require_api_key)],
    )
    async def upload_media(
        request: Request,
        filename: str = Query(min_length=1, max_length=200, description="원래 파일 이름 (확장자로 종류 판단)"),
    ) -> dict:
        """직접 찍었거나 사용 허락을 받은 영상·사진을 올립니다. 요청 본문에 파일 내용을 그대로 담아 보내세요.

        영상: mp4, mov, m4v, webm / 사진: jpg, jpeg, png, webp / 배경음악: mp3, m4a, aac, wav, ogg.
        """
        store: MediaStore = request.app.state.media
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > store.max_bytes:
            raise HTTPException(status_code=413, detail=f"파일이 너무 큽니다 (최대 {settings.max_upload_mb}MB).")
        try:
            item = await store.save(filename, request.stream())
        except MediaTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except MediaError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return item.to_dict()

    @app.post(
        "/api/v1/media/import",
        status_code=status.HTTP_201_CREATED,
        tags=["media"],
        dependencies=[Depends(require_api_key)],
    )
    async def import_media(req: ImportMediaRequest, request: Request) -> dict:
        """쇼핑몰이 제휴 홍보용으로 주는 상품 공식 사진·영상을 주소로 가져와 '내 영상·사진'에 넣습니다."""
        try:
            item = await import_from_url(request.app.state.media, req.url, settings.max_import_mb * 1024 * 1024,
                                         **(import_options or {}))  # 테스트에서 가짜 응답을 넣을 때 사용
        except MediaTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except MediaError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return item.to_dict()

    @app.post("/api/v1/products/brief", tags=["trends"], dependencies=[Depends(require_api_key)])
    async def product_brief(req: ProductBriefRequest, request: Request) -> dict:
        """긴 쇼핑몰 상품 제목을 짧은 제품명·특징·영어 검색어로 정리합니다 (대본 칸 자동 입력용)."""
        briefer = request.app.state.providers.briefer
        if briefer is None:
            raise HTTPException(status_code=503, detail="상품 정리 기능이 설정되지 않았습니다.")
        try:
            result = await briefer.brief(req.title, req.details)
        except Exception as exc:
            logger.warning("상품 정리 실패: %s", exc)
            raise HTTPException(status_code=502, detail=f"상품 정보를 정리하지 못했습니다: {describe_error(exc)}") from exc
        return result.model_dump()

    @app.get("/api/v1/media/search", tags=["media"], dependencies=[Depends(require_api_key)])
    async def search_product_media(
        request: Request,
        q: str = Query(min_length=1, max_length=100, description="제품명 (한국어 가능)"),
        q_en: str = Query("", max_length=100, description="영어 검색어. 비우면 AI가 만들어 씁니다"),
    ) -> dict:
        """그 제품의 영상·사진 후보를 알리익스프레스(공식 소재)·이베이·스톡 영상에서 한 번에 찾습니다."""
        searcher: MediaSearch = media_search or MediaSearch(settings)
        keyword_en = q_en.strip()
        briefer = request.app.state.providers.briefer
        if not keyword_en and briefer is not None and not q.isascii():
            try:
                keyword_en = (await briefer.brief(q)).search_keyword_en
            except Exception as exc:  # 번역이 안 돼도 한국어로 찾습니다
                logger.info("영어 검색어 만들기 실패: %s", exc)
        candidates, notes = await searcher.search(keyword_en or q, q)
        return {
            "keyword_en": keyword_en or q,
            "sources": [{"name": n, "configured": c} for n, c in searcher.sources()],
            "candidates": candidates,
            "notes": notes,
        }

    @app.post(
        "/api/v1/scripts/shopping",
        response_model=ShoppingScriptResponse,
        tags=["scripts"],
        dependencies=[Depends(require_api_key)],
    )
    async def write_shopping_script(req: ShoppingScriptRequest, request: Request) -> ShoppingScriptResponse:
        """제품 정보로 쇼핑 쇼츠 대본(후킹 3안)·제목·해시태그·설명란을 만듭니다. 몇 초 걸립니다."""
        writer = request.app.state.providers.scriptwriter
        if writer is None:
            raise HTTPException(status_code=503, detail="대본 작성 기능이 설정되지 않았습니다.")
        reference = ""
        if req.reference_video_id:
            video_ref = await asyncio.to_thread(request.app.state.trends.store.get, req.reference_video_id)
            if video_ref is not None:
                reference = trends.reference_summary(video_ref)
        insights = []
        if req.use_channel_insights:
            insights = await asyncio.to_thread(request.app.state.projects.channel_insights)
        brief = ProductBrief(
            req.product_name, req.features.strip(), req.tone, reference, format_insights(insights)
        )
        try:
            result = await writer.write(brief)
        except Exception as exc:
            logger.warning("쇼핑 대본 작성 실패: %s", exc)
            raise HTTPException(status_code=502, detail=f"대본을 만들지 못했습니다: {describe_error(exc)}") from exc
        return ShoppingScriptResponse(
            hooks=result.hooks,
            scripts=[result.full_script(i) for i in range(len(result.hooks))],
            body=result.body,
            cta=result.cta,
            disclosure=result.disclosure,
            titles=result.titles,
            hashtags=result.hashtags,
            description=result.description,
            warnings=result.warnings,
            reference_used=bool(reference),
            insights_used=len(insights),
        )

    @app.get("/api/v1/trends", tags=["trends"], dependencies=[Depends(require_api_key)])
    async def list_trends(
        request: Request,
        category: str = Query("all", description="all 또는 " + ", ".join(trends.CATEGORIES)),
        period: str = Query("1m", description=", ".join(trends.PERIODS)),
        sort: str = Query("performance", description=", ".join(trends.SORTS)),
        q: str = Query("", max_length=100, description="상품명·해시태그·작성자·공개 댓글 검색어"),
        limit: int = Query(120, ge=1, le=500),
    ) -> dict:
        """모아 둔 인기 쇼츠를 조건대로 골라 정렬합니다. 즐겨찾기는 조건과 관계없이 맨 앞에 옵니다."""
        if category != "all" and category not in trends.CATEGORIES:
            raise HTTPException(status_code=422, detail="알 수 없는 분야입니다.")
        if period not in trends.PERIODS:
            raise HTTPException(status_code=422, detail="알 수 없는 기간입니다.")
        if sort not in trends.SORTS:
            raise HTTPException(status_code=422, detail="알 수 없는 정렬입니다.")
        collector: trends.TrendCollector = request.app.state.trends
        rows, collection = await asyncio.gather(
            asyncio.to_thread(collector.store.all_rows), asyncio.to_thread(collector.store.last_collection)
        )
        result = trends.query_trends(
            rows, collection, category=category, period=period, sort=sort, q=q, limit=limit
        )
        return {
            "configured": collector.configured,
            "collecting": collector.running,
            "last_error": collector.last_error,
            "categories": [{"key": "all", "label": "전체 분야"}]
            + [{"key": k, "label": label} for k, (label, _) in trends.CATEGORIES.items()],
            "collection": vars(collection) if collection else None,
            "summary": {
                "sort": sort,
                "sort_label": trends.SORTS[sort],
                "total": result.total,
                "favorites": result.favorites,
                "favorites_kept": result.favorites_kept,
                "with_value": result.with_value,
                "missing_value": result.missing_value,
                "out_of_period": result.out_of_period,
            },
            "items": result.items,
        }

    @app.post("/api/v1/trends/{video_id}/analysis", tags=["trends"], dependencies=[Depends(require_api_key)])
    async def analyze_trend(video_id: str, request: Request, refresh: bool = False) -> dict:
        """인기 영상의 공개 정보로 "왜 뜨는지"를 AI가 한 줄로 분석합니다. 결과는 저장해 두고 다시 씁니다."""
        store: trends.TrendStore = request.app.state.trends.store
        video_ref = await asyncio.to_thread(store.get, video_id)
        if video_ref is None:
            raise HTTPException(status_code=404, detail="영상을 찾을 수 없습니다.")
        if not refresh:
            cached = await asyncio.to_thread(store.get_analysis, video_id)
            if cached is not None:
                return {**cached, "cached": True}
        analyzer = request.app.state.providers.trend_analyzer
        if analyzer is None:
            raise HTTPException(status_code=503, detail="AI 분석 기능이 설정되지 않았습니다.")
        try:
            result = (await analyzer.analyze(video_ref)).model_dump()
        except Exception as exc:
            logger.warning("트렌드 분석 실패 %s: %s", video_id, exc)
            raise HTTPException(status_code=502, detail=f"분석하지 못했습니다: {describe_error(exc)}") from exc
        await asyncio.to_thread(store.set_analysis, video_id, result)
        return {**result, "cached": False}

    @app.post(
        "/api/v1/trends/refresh",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["trends"],
        dependencies=[Depends(require_api_key)],
    )
    async def refresh_trends(request: Request) -> dict:
        """지금 바로 다시 모읍니다 (1회에 YouTube 할당량 약 1,500 사용)."""
        collector: trends.TrendCollector = request.app.state.trends
        if not collector.configured:
            raise HTTPException(status_code=409, detail="YOUTUBE_API_KEY를 설정해야 수집할 수 있습니다.")
        if not collector.running:
            task = asyncio.create_task(collector.refresh(), name="trends-refresh")
            request.app.state.tasks.add(task)
            task.add_done_callback(request.app.state.tasks.discard)
        return {"collecting": True}

    @app.get("/api/v1/products", tags=["trends"], dependencies=[Depends(require_api_key)])
    async def list_products(
        request: Request,
        source: str = Query("all", description="all 또는 " + ", ".join(products.SOURCES)),
        category: str = Query("all", description="all 또는 " + ", ".join(trends.CATEGORIES)),
        sort: str = Query("sales", description=", ".join(products.PRODUCT_SORTS)),
        q: str = Query("", max_length=100),
        limit: int = Query(120, ge=1, le=500),
    ) -> dict:
        """네이버 쇼핑·알리익스프레스·이베이에서 모아 둔 분야별 상품."""
        if source != "all" and source not in products.SOURCES:
            raise HTTPException(status_code=422, detail="알 수 없는 쇼핑몰입니다.")
        if category != "all" and category not in trends.CATEGORIES:
            raise HTTPException(status_code=422, detail="알 수 없는 분야입니다.")
        if sort not in products.PRODUCT_SORTS:
            raise HTTPException(status_code=422, detail="알 수 없는 정렬입니다.")
        collector: products.ProductCollector = request.app.state.products
        data = await asyncio.to_thread(collector.load)
        return {
            "configured": collector.configured,
            "collecting": collector.running,
            "collected_at": data.get("collected_at"),
            "errors": list((data.get("errors") or {}).values()),
            "sources": [{"key": s.name, "label": products.SOURCES[s.name], "configured": s.configured}
                        for s in collector.sources],
            "categories": [{"key": k, "label": label} for k, (label, _) in trends.CATEGORIES.items()],
            "category_trends": data.get("category_trends") or {},
            "items": products.query_products(data, source=source, category=category, sort=sort, q=q, limit=limit),
        }

    @app.post(
        "/api/v1/products/refresh",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["trends"],
        dependencies=[Depends(require_api_key)],
    )
    async def refresh_products(request: Request) -> dict:
        """쇼핑몰 상품을 지금 다시 모읍니다."""
        collector: products.ProductCollector = request.app.state.products
        if not collector.configured:
            raise HTTPException(status_code=409, detail="네이버·알리익스프레스·이베이 중 하나의 키를 넣어야 모을 수 있습니다.")
        if not collector.running:
            task = asyncio.create_task(collector.refresh(), name="products-refresh")
            request.app.state.tasks.add(task)
            task.add_done_callback(request.app.state.tasks.discard)
        return {"collecting": True}

    @app.put("/api/v1/trends/{video_id}/favorite", tags=["trends"], dependencies=[Depends(require_api_key)])
    async def add_favorite(video_id: str, request: Request) -> dict:
        return await _set_favorite(request, video_id, True)

    @app.delete("/api/v1/trends/{video_id}/favorite", tags=["trends"], dependencies=[Depends(require_api_key)])
    async def remove_favorite(video_id: str, request: Request) -> dict:
        return await _set_favorite(request, video_id, False)

    @app.get("/api/v1/accounts", tags=["publish"], dependencies=[Depends(require_api_key)])
    async def list_accounts(request: Request) -> dict:
        """플랫폼별 연결 상태. configured: 앱 키를 넣었는지, connected: 계정을 연결했는지."""
        state = request.app.state
        stored = {key: await asyncio.to_thread(state.accounts.get, key) for key in ("youtube", "meta", "threads", "tiktok")}
        meta_secret = stored["meta"][1] if stored["meta"] else {}
        return {
            "youtube": {
                "configured": state.youtube.configured,
                "connected": stored["youtube"] is not None,
                "name": stored["youtube"][0] if stored["youtube"] else None,
                "redirect_uri": settings.youtube_redirect_uri,
            },
            "instagram": {
                "configured": state.meta.configured,
                "connected": bool(meta_secret.get("ig_user_id")),
                "name": f"@{meta_secret['ig_username']}" if meta_secret.get("ig_user_id") else None,
            },
            "facebook": {
                "configured": state.meta.configured,
                "connected": bool(meta_secret),
                "name": meta_secret.get("page_name") or None,
            },
            "threads": {
                "configured": state.threads.configured,
                "connected": stored["threads"] is not None,
                "name": stored["threads"][0] if stored["threads"] else None,
            },
            "tiktok": {
                "configured": state.tiktok.configured,
                "connected": stored["tiktok"] is not None,
                "name": stored["tiktok"][0] if stored["tiktok"] else None,
                "redirect_uri": settings.tiktok_redirect_uri,
            },
        }

    @app.post("/api/v1/accounts/meta/connect", tags=["publish"], dependencies=[Depends(require_api_key)])
    async def connect_meta(req: ConnectTokenRequest, request: Request) -> dict:
        """그래프 API 탐색기에서 받은 토큰으로 페이스북 페이지·인스타그램 계정을 연결합니다."""
        client: MetaClient = request.app.state.meta
        if not client.configured:
            raise HTTPException(status_code=409, detail="META_APP_ID / META_APP_SECRET을 먼저 넣으세요.")
        try:
            name, secret = await client.connect(req.token)
        except MetaError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        await asyncio.to_thread(request.app.state.accounts.save, "meta", display_name=name, secret=secret)
        return {"connected": True, "name": name, "instagram": bool(secret["ig_user_id"])}

    @app.post("/api/v1/accounts/threads/connect", tags=["publish"], dependencies=[Depends(require_api_key)])
    async def connect_threads(req: ConnectTokenRequest, request: Request) -> dict:
        """스레드 사용자 토큰으로 계정을 연결합니다."""
        try:
            name, secret = await request.app.state.threads.connect(req.token)
        except ThreadsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        await asyncio.to_thread(request.app.state.accounts.save, "threads", display_name=name, secret=secret)
        return {"connected": True, "name": name}

    @app.post("/api/v1/accounts/tiktok/connect", tags=["publish"], dependencies=[Depends(require_api_key)])
    async def connect_tiktok(request: Request) -> dict:
        """틱톡 로그인·권한 동의 페이지 주소를 돌려줍니다."""
        client: TikTokClient = request.app.state.tiktok
        if not client.configured:
            raise HTTPException(status_code=409, detail="TIKTOK_CLIENT_KEY / SECRET을 먼저 넣으세요.")
        return {"auth_url": client.auth_url()}

    @app.get("/oauth/tiktok/callback", include_in_schema=False)
    async def tiktok_callback(request: Request, code: str = "", state: str = "", error: str = "") -> HTMLResponse:
        client: TikTokClient = request.app.state.tiktok
        if error or not code:
            return _oauth_page("연결을 취소했어요.", ok=False, platform="틱톡")
        verifier = client.consume_state(state)
        if verifier is None:
            return _oauth_page("연결 요청이 만료됐거나 올바르지 않아요. 보관함에서 다시 연결해 주세요.", ok=False, platform="틱톡")
        try:
            secret = await client.exchange_code(code, verifier)
            name = await client.display_name(secret)
        except TikTokError as exc:
            return _oauth_page(str(exc), ok=False, platform="틱톡")
        await asyncio.to_thread(request.app.state.accounts.save, "tiktok", display_name=name, secret=secret)
        return _oauth_page(f"틱톡 계정 '{name}'을(를) 연결했어요.", ok=True, platform="틱톡")

    @app.post("/api/v1/accounts/youtube/connect", tags=["publish"], dependencies=[Depends(require_api_key)])
    async def connect_youtube(request: Request) -> dict:
        """구글 로그인·권한 동의 페이지 주소를 돌려줍니다. 브라우저로 열면 끝나고 이 서버로 돌아옵니다."""
        yt: YouTubeClient = request.app.state.youtube
        if not yt.configured:
            raise HTTPException(status_code=409, detail="YOUTUBE_OAUTH_CLIENT_ID / SECRET을 먼저 설정하세요.")
        return {"auth_url": yt.auth_url()}

    @app.delete("/api/v1/accounts/{platform}", tags=["publish"], dependencies=[Depends(require_api_key)])
    async def disconnect_account(platform: str, request: Request) -> dict:
        """연결 해제 (youtube, meta, threads, tiktok). 인스타그램·페이스북은 meta 하나로 함께 해제됩니다."""
        if platform not in ("youtube", "meta", "threads", "tiktok"):
            raise HTTPException(status_code=404, detail="알 수 없는 플랫폼입니다.")
        await asyncio.to_thread(request.app.state.accounts.delete, platform)
        return {"connected": False}

    @app.get("/oauth/youtube/callback", include_in_schema=False)
    async def youtube_callback(request: Request, code: str = "", state: str = "", error: str = "") -> HTMLResponse:
        # API 키 대신 연결을 시작할 때 만든 일회용 state로 요청을 확인합니다.
        yt: YouTubeClient = request.app.state.youtube
        if error or not code:
            return _oauth_page("연결을 취소했어요.", ok=False)
        if not yt.consume_state(state):
            return _oauth_page("연결 요청이 만료됐거나 올바르지 않아요. 보관함에서 다시 연결해 주세요.", ok=False)
        try:
            tokens = await yt.exchange_code(code)
            name = await yt.channel_title(tokens["access_token"])
        except YouTubeError as exc:
            return _oauth_page(str(exc), ok=False)
        await asyncio.to_thread(
            request.app.state.accounts.save, "youtube",
            display_name=name, secret={"refresh_token": tokens["refresh_token"]},
        )
        return _oauth_page(f"유튜브 채널 '{name}'을(를) 연결했어요.", ok=True)

    @app.post(
        "/api/v1/projects/{project_id}/publish",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["publish"],
        dependencies=[Depends(require_api_key)],
    )
    async def publish_project(project_id: str, req: PublishRequest, request: Request) -> dict:
        """보관함의 영상을 연결한 플랫폼(유튜브·인스타그램·페이스북·스레드·틱톡)에 올립니다.
        뒤에서 진행하며, 결과는 프로젝트 상세의 publications에 나옵니다. 플랫폼마다 한 번씩 부르세요."""
        state = request.app.state
        store: ProjectStore = state.projects
        label = PLATFORM_LABELS[req.platform]
        project = await asyncio.to_thread(store.get, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")
        account = await asyncio.to_thread(state.accounts.get, ACCOUNT_KEYS[req.platform])
        if account is None or (req.platform == "instagram" and not account[1].get("ig_user_id")):
            raise HTTPException(status_code=409, detail=f"{label} 계정을 먼저 연결하세요.")
        pubs = [p for p in await asyncio.to_thread(store.publications, project_id) if p["platform"] == req.platform]
        if any(p["status"] == "uploading" for p in pubs):
            raise HTTPException(status_code=409, detail=f"{label}에 이미 올리는 중이에요.")
        if any(p["status"] == "scheduled" for p in pubs):
            raise HTTPException(status_code=409, detail=f"{label}에 이미 예약돼 있어요. 예약을 취소한 뒤 다시 하세요.")
        scheduled_at = None
        if req.scheduled_at is not None:
            when = req.scheduled_at
            if when.tzinfo is None:
                raise HTTPException(status_code=422, detail="예약 시각에 시간대가 필요합니다 (예: +09:00).")
            now = datetime.now(UTC)
            if when <= now + timedelta(minutes=1) or when > now + timedelta(days=60):
                raise HTTPException(status_code=422, detail="예약은 지금부터 1분 뒤 ~ 60일 안으로 정해 주세요.")
            scheduled_at = when.astimezone(UTC).isoformat(timespec="seconds")
        pub_id = await asyncio.to_thread(
            store.add_publication, project_id, req.platform, title=req.title, privacy=req.privacy,
            scheduled_at=scheduled_at,
        )
        if scheduled_at is None:
            _start_publish(state, pub_id, project, req.platform, req.title, req.privacy)
        return {"publication_id": pub_id, "status": "scheduled" if scheduled_at else "uploading",
                "scheduled_at": scheduled_at}

    @app.delete(
        "/api/v1/projects/{project_id}/publications/{pub_id}", tags=["publish"],
        dependencies=[Depends(require_api_key)],
    )
    async def cancel_publication(project_id: str, pub_id: int, request: Request) -> dict:
        """예약된 올리기를 취소합니다 (이미 올리기 시작했거나 끝난 것은 취소할 수 없음)."""
        if not await asyncio.to_thread(request.app.state.projects.cancel_scheduled, project_id, pub_id):
            raise HTTPException(status_code=404, detail="취소할 예약이 없습니다.")
        return {"cancelled": pub_id}

    @app.get("/api/v1/stats", tags=["publish"], dependencies=[Depends(require_api_key)])
    async def get_stats(request: Request) -> dict:
        """올린 영상의 성과 요약 (조회수 많은 순)."""
        summary = await asyncio.to_thread(request.app.state.projects.stats_summary)
        refresher: StatsRefresher = request.app.state.stats
        return {**summary, "refreshing": refresher.running, "last_error": refresher.last_error}

    @app.post(
        "/api/v1/stats/refresh",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["publish"],
        dependencies=[Depends(require_api_key)],
    )
    async def refresh_stats(request: Request) -> dict:
        """지금 바로 성과를 다시 받아 옵니다 (유튜브 할당량: 영상 50개당 1)."""
        refresher: StatsRefresher = request.app.state.stats
        if not refresher.running:
            task = asyncio.create_task(refresher.refresh(), name="stats-refresh")
            request.app.state.tasks.add(task)
            task.add_done_callback(request.app.state.tasks.discard)
        return {"refreshing": True}

    @app.get("/api/v1/projects", tags=["projects"], dependencies=[Depends(require_api_key)])
    async def list_projects(request: Request, limit: int = Query(100, ge=1, le=500)) -> dict:
        """보관함에 저장된 완성 영상 목록 (최신순)."""
        items = await asyncio.to_thread(request.app.state.projects.list, limit)
        signer: URLSigner = request.app.state.signer
        return {"items": [_project_dict(p, signer, detail=False) for p in items]}

    @app.get("/api/v1/projects/{project_id}", tags=["projects"], dependencies=[Depends(require_api_key)])
    async def get_project(project_id: str, request: Request) -> dict:
        project = await asyncio.to_thread(request.app.state.projects.get, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")
        data = _project_dict(project, request.app.state.signer, detail=True)
        data["publications"] = await asyncio.to_thread(request.app.state.projects.publications, project_id)
        return data

    @app.delete("/api/v1/projects/{project_id}", tags=["projects"], dependencies=[Depends(require_api_key)])
    async def delete_project(project_id: str, request: Request) -> dict:
        if not await asyncio.to_thread(request.app.state.projects.delete, project_id):
            raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")
        return {"deleted": project_id}

    @app.get(f"{LIBRARY_ROUTE_PREFIX}/{{project_id}}/{{filename}}", include_in_schema=False)
    async def get_library_file(
        project_id: str, filename: str, request: Request, expires: int | None = None, sig: str | None = None
    ) -> FileResponse:
        path = f"{LIBRARY_ROUTE_PREFIX}/{project_id}/{filename}"
        if expires is None or not sig or not request.app.state.signer.verify(path, expires, sig):
            raise HTTPException(status_code=403, detail="링크가 만료되었거나 올바르지 않습니다.")
        store: ProjectStore = request.app.state.projects
        project = await asyncio.to_thread(store.get, project_id)
        # 파일 경로는 요청 값이 아니라 프로젝트에 기록된 이름으로만 허용해 경로 조작을 막습니다.
        if project is None or filename not in project.files():
            raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다.")
        file_path = store.library_dir / project.project_id / filename
        if not file_path.is_file():
            raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다.")
        max_age = max(0, expires - int(time.time()))
        return FileResponse(
            file_path,
            media_type="image/jpeg" if filename.endswith(".jpg") else "video/mp4",
            headers={"Cache-Control": f"private, max-age={max_age}", "Referrer-Policy": "no-referrer"},
        )

    @app.get("/api/v1/shorts/{job_id}/scenes", tags=["shorts"], dependencies=[Depends(require_api_key)])
    async def list_scenes(job_id: str, request: Request) -> dict:
        """완성된 영상의 장면 목록 (미리보기·내레이션·배경). 작업 보관 기간(JOB_TTL_HOURS) 안에만 고칠 수 있습니다."""
        job, job_dir, data = await _load_scenes(request, job_id)
        signer: URLSigner = request.app.state.signer
        scenes = []
        for i, scene in enumerate(data["scenes"]):
            preview = f"preview_{i + 1:02d}.jpg"
            if not (job_dir / preview).is_file():
                await video.run_command(
                    ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", "0.5",
                     "-i", str(job_dir / scene["scene_file"]), "-frames:v", "1", "-vf", "scale=240:-2",
                     "-q:v", "4", str(job_dir / preview)],
                    timeout=settings.ffmpeg_timeout_sec,
                )
            if preview not in job.extra_files:
                job.extra_files.append(preview)
            scenes.append({
                "index": i,
                "narration": scene["narration"],
                "subtitle_text": scene["subtitle_text"],
                "media_id": scene.get("media_id"),
                "preview_url": signer.sign(f"{VIDEO_ROUTE_PREFIX}/{job.job_id}/{preview}").url,
            })
        media = [{"media_id": m.media_id, "kind": m.kind} for m in job.media if m.kind in ("image", "video")]
        return {"job_id": job.job_id, "title": data["title"], "scenes": scenes, "media": media,
                "subtitle_mode": settings.subtitle_mode}

    @app.post(
        "/api/v1/shorts/{job_id}/revise",
        response_model=CreateJobResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["shorts"],
        dependencies=[Depends(require_api_key)],
    )
    async def revise_job(job_id: str, req: ReviseRequest, request: Request) -> CreateJobResponse:
        """바뀐 장면만 다시 만들어 새 영상을 만듭니다 (AI 기획은 다시 하지 않음). 결과는 새 작업·새 보관함 항목."""
        state = request.app.state
        source, source_dir, data = await _load_scenes(request, job_id)
        if len(req.scenes) != len(data["scenes"]):
            raise HTTPException(status_code=422, detail="장면 수가 원본과 같아야 합니다.")
        revised, changed = [], 0
        for edit, old in zip(req.scenes, data["scenes"], strict=True):
            background = Path(old["background"])
            if not background.is_absolute():
                background = source_dir / background
            video_start = old.get("video_start")
            media_id = old.get("media_id")
            media_changed = bool(edit.media_id) and edit.media_id != media_id
            if media_changed:
                item = await state.media.get(edit.media_id)
                if item is None or item.kind not in ("image", "video"):
                    raise HTTPException(
                        status_code=422, detail="배경으로 고른 파일을 찾을 수 없습니다 (다시 올려 주세요)."
                    )
                background, video_start, media_id = item.path, (0.0 if item.kind == "video" else None), item.media_id
            narration_changed = edit.narration != old["narration"]
            subtitle = edit.subtitle_text or (edit.narration if narration_changed else old["subtitle_text"])
            is_changed = narration_changed or media_changed or subtitle != old["subtitle_text"]
            reuse = source_dir / old["scene_file"]
            changed += is_changed
            revised.append({
                "narration": edit.narration, "subtitle_text": subtitle,
                "visual_prompt": old.get("visual_prompt", ""), "stock_query": old.get("stock_query", ""),
                "background": str(background), "video_start": video_start, "media_id": media_id,
                "reuse": None if is_changed or not reuse.is_file() else str(reuse),
            })
        thumbnail_changed = req.thumbnail_text is not None and req.thumbnail_text.strip() != source.thumbnail_text
        if not changed and not thumbnail_changed:
            raise HTTPException(status_code=422, detail="바뀐 장면이 없어요.")
        job = state.jobs.create(
            " ".join(s["narration"] for s in revised), source.tone, source.media,
            (req.thumbnail_text or source.thumbnail_text).strip(), source.bgm, source.bgm_volume,
        )
        job.revision = {"title": data["title"], "scenes": revised}
        job.project_info = dict(source.project_info)
        task = asyncio.create_task(_run_and_save(request.app, job), name=f"shorts-{job.job_id}")
        state.tasks.add(task)
        task.add_done_callback(state.tasks.discard)
        return CreateJobResponse(
            job_id=job.job_id,
            status=job.status,
            status_url=str(request.url_for("get_job_status", job_id=job.job_id).path),
            message=f"바뀐 장면 {changed}개만 다시 만들어요.",
        )

    @app.get(f"{VIDEO_ROUTE_PREFIX}/{{job_id}}/{{filename}}", include_in_schema=False)
    async def get_video(
        job_id: str,
        filename: str,
        request: Request,
        expires: int | None = None,
        sig: str | None = None,
    ) -> FileResponse:
        # API 키 대신 서명으로 접근을 허용합니다 (<video> 태그는 헤더를 붙일 수 없음).
        path = f"{VIDEO_ROUTE_PREFIX}/{job_id}/{filename}"
        if expires is None or not sig or not request.app.state.signer.verify(path, expires, sig):
            raise HTTPException(status_code=403, detail="링크가 만료되었거나 올바르지 않습니다.")
        job = request.app.state.jobs.get(job_id)
        # 파일 경로는 요청 값이 아니라 작업에 기록된 이름으로 만들어 경로 조작을 막습니다.
        if job is None or filename not in job.served_files():
            raise HTTPException(status_code=404, detail="영상을 찾을 수 없습니다.")
        file_path = settings.output_dir / job.job_id / filename
        if not file_path.is_file():
            raise HTTPException(status_code=404, detail="영상을 찾을 수 없습니다.")
        max_age = max(0, expires - int(time.time()))
        return FileResponse(
            file_path,
            media_type="image/jpeg" if filename.endswith(".jpg") else "video/mp4",
            headers={"Cache-Control": f"private, max-age={max_age}", "Referrer-Policy": "no-referrer"},
        )

    return app




def _default_setup_checker(app: FastAPI, settings: Settings) -> SetupChecker:
    async def gemini_check() -> str:
        from google import genai

        client = genai.Client(api_key=settings.gemini_api_key)
        try:
            model = await client.aio.models.get(model=settings.llm_model)
        except Exception as exc:
            if api_error_code(exc) is None:
                raise  # 네트워크 오류 등은 점검기가 공통 문구로 알려 줌
            raise CheckFailed(
                describe_error(exc),
                "https://aistudio.google.com/apikey 에서 키를 확인하고, LLM_MODEL 이름이 맞는지 보세요.",
            ) from None
        return f"키가 동작합니다 · 대본 모델 {model.display_name or settings.llm_model}"

    async def tts_check(path: Path) -> object:
        from .services.tts import EdgeTTS

        # 타입캐스트를 쓰더라도 점검은 크레딧이 들지 않는 Edge TTS로 합니다 (타입캐스트는 따로 점검).
        return await EdgeTTS(settings).synthesize("설정 점검입니다.", path)

    async def youtube_status() -> tuple[bool, bool, str | None]:
        yt: YouTubeClient = app.state.youtube
        account = await asyncio.to_thread(app.state.accounts.get, "youtube")
        if not yt.configured or account is None:
            return yt.configured, False, None
        try:
            await yt.access_token(account[1]["refresh_token"])  # 연결이 아직 살아 있는지
        except YouTubeError:
            return True, False, None
        return True, True, account[0]

    async def social_status(platform: str) -> tuple[bool, bool, str | None]:
        state = app.state
        key = ACCOUNT_KEYS[platform]
        client = {"meta": state.meta, "threads": state.threads, "tiktok": state.tiktok}[key]
        account = await asyncio.to_thread(state.accounts.get, key)
        if platform == "threads" and account is None:
            return False, False, None  # 스레드는 앱 키 없이 토큰만 있으면 되므로, 연결 전에는 '선택'으로 보여 줌
        if not client.configured or account is None:
            return client.configured, False, None
        name, secret = account
        if platform == "instagram" and not secret.get("ig_user_id"):
            return True, False, None
        try:
            if key == "tiktok":
                secret = await _fresh(state, "tiktok", client, name, secret)
                await client.display_name(secret)
            else:
                await client.check(secret)
        except (MetaError, ThreadsError, TikTokError):
            return True, False, None
        label = {"instagram": f"@{secret.get('ig_username', '')}", "facebook": f"페이지 '{secret.get('page_name', '')}'"}
        return True, True, label.get(platform, name)

    return SetupChecker(settings, gemini_check=gemini_check, tts_check=tts_check, youtube_status=youtube_status,
                        social_status=social_status)


PLATFORM_LABELS = {"youtube": "유튜브", "instagram": "인스타그램", "facebook": "페이스북", "threads": "스레드",
                   "tiktok": "틱톡"}
# 플랫폼 → 계정 저장 이름 (인스타그램·페이스북은 Meta 연결 하나를 같이 씀)
ACCOUNT_KEYS = {"youtube": "youtube", "instagram": "meta", "facebook": "meta", "threads": "threads", "tiktok": "tiktok"}


def _caption(project: Project, title: str) -> str:
    """인스타그램·페이스북·스레드·틱톡 본문: 제목 + 설명란(광고 표시·해시태그 포함)."""
    description = (project.description or "").strip()
    return f"{title}\n\n{description}".strip() if description else title


def _start_publish(state, pub_id: int, project: Project, platform: str, title: str, privacy: str) -> None:
    task = asyncio.create_task(_publish(state, pub_id, project, platform, title, privacy), name=f"publish-{pub_id}")
    state.tasks.add(task)
    task.add_done_callback(state.tasks.discard)


async def _fresh(state, key: str, client, name: str, secret: dict) -> dict:
    """만료가 가까운 토큰을 연장해 저장하고, 쓸 비밀 정보를 돌려줍니다."""
    fresh = await client.fresh_secret(secret)
    if fresh is None:
        return secret
    await asyncio.to_thread(state.accounts.save, key, display_name=name, secret=fresh)
    return fresh


async def _publish(state, pub_id: int, project: Project, platform: str, title: str, privacy: str) -> None:
    store: ProjectStore = state.projects
    path = store.library_dir / project.project_id / project.video_file
    caption = _caption(project, title)
    try:
        account = await asyncio.to_thread(state.accounts.get, ACCOUNT_KEYS[platform])
        if account is None:
            raise RuntimeError(f"{PLATFORM_LABELS[platform]} 연결이 해제돼 있어요. 다시 연결해 주세요.")
        name, secret = account
        if platform == "youtube":
            upload = UploadRequest(
                video_path=path, title=title, description=project.description or project.title,
                tags=[w for w in project.description.split() if w.startswith("#") and w.lower() != "#shorts"],
                privacy=privacy,
            )
            video_id = await state.youtube.upload(secret["refresh_token"], upload)
            url = f"https://youtube.com/shorts/{video_id}"
        elif platform == "instagram":
            video_id, url = await state.meta.publish_instagram(secret, path, caption)
        elif platform == "facebook":
            video_id, url = await state.meta.publish_facebook(secret, path, caption)
        elif platform == "threads":
            secret = await _fresh(state, "threads", state.threads, name, secret)
            video_id, url = await state.threads.publish(secret, path, caption)
        elif platform == "tiktok":
            secret = await _fresh(state, "tiktok", state.tiktok, name, secret)
            video_id, url = await state.tiktok.publish(secret, path, caption, privacy)
        else:
            raise RuntimeError(f"알 수 없는 플랫폼입니다: {platform}")
    except Exception as exc:
        logger.warning("%s 올리기 실패 (%s): %s", platform, pub_id, exc)
        message = str(exc) if isinstance(exc, RuntimeError) else f"업로드 실패: {exc}"
        await asyncio.to_thread(store.finish_publication, pub_id, error=message)
        return
    await asyncio.to_thread(store.finish_publication, pub_id, url=url, video_id=video_id)


async def _publish_scheduled_loop(app: FastAPI, interval_sec: float = 30) -> None:
    """예약 시각이 된 올리기를 시작합니다."""
    state = app.state
    store: ProjectStore = state.projects
    while True:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        for pub in await asyncio.to_thread(store.claim_due, now):
            project = await asyncio.to_thread(store.get, pub["project_id"])
            if project is None:
                await asyncio.to_thread(store.finish_publication, pub["id"],
                                        error="예약 올리기 실패: 보관함에서 영상이 지워졌어요.")
                continue
            _start_publish(state, pub["id"], project, pub["platform"], pub["title"], pub["privacy"])
        await asyncio.sleep(interval_sec)


class StatsRefresher:
    """올린 영상의 성과(조회수·좋아요·댓글)를 플랫폼에서 받아 보관함에 기록합니다. 한 번에 하나만 실행."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app
        self._lock = asyncio.Lock()
        self.last_error: str | None = None

    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def refresh(self) -> int:
        if self._lock.locked():
            return 0
        async with self._lock:
            state = self._app.state
            account = await asyncio.to_thread(state.accounts.get, "youtube")
            ids = await asyncio.to_thread(state.projects.published_videos, "youtube")
            if account is None or not ids:
                self.last_error = None
                return 0
            try:
                stats = await state.youtube.video_stats(account[1]["refresh_token"], ids)
            except Exception as exc:
                self.last_error = str(exc) if isinstance(exc, YouTubeError) else f"성과 조회 실패: {exc}"
                logger.warning("성과 조회 실패: %s", exc)
                return 0
            self.last_error = None
            return await asyncio.to_thread(state.projects.update_stats, "youtube", stats)

    async def loop(self, interval_sec: float) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(interval_sec)


def _oauth_page(message: str, *, ok: bool, platform: str = "유튜브") -> HTMLResponse:
    color = "#12805c" if ok else "#c62f3b"
    body = (
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(platform)} 연결</title></head>"
        "<body style='font-family:system-ui,sans-serif;max-width:480px;margin:60px auto;padding:0 16px;"
        "line-height:1.6;background:#fff;color:#17171f'>"
        f"<h1 style='font-size:20px;color:{color}'>{'연결 완료' if ok else '연결 실패'}</h1>"
        f"<p>{html.escape(message)}</p><p><a href='/library'>보관함으로 돌아가기</a></p></body></html>"
    )
    return HTMLResponse(body, status_code=200 if ok else 400)


async def _run_and_save(app: FastAPI, job) -> None:
    """작업을 끝까지 실행하고, 완성되면 보관함에 저장합니다 (저장 실패는 경고로만 남김)."""
    await app.state.pipeline.run(job)
    if job.status != JobStatus.COMPLETED or not job.video_file:
        return
    info = job.project_info or {}
    project = Project(
        project_id="",
        created_at=now_iso(),
        title=job.title or info.get("product_name") or "제목 없음",
        script=job.script,
        tone=job.tone,
        video_file=job.video_file,
        job_id=job.job_id,
        product_name=info.get("product_name", ""),
        description=info.get("description", ""),
        titles=list(info.get("titles") or []),
        reference_video_id=info.get("reference_video_id"),
        duration_sec=job.duration_sec,
        scene_count=job.scene_count,
        thumbnail_files=list(job.thumbnail_files),
        image_credits=list(job.image_credits),
    )
    try:
        saved = await asyncio.to_thread(
            app.state.projects.save, app.state.settings.output_dir / job.job_id, project
        )
        job.project_id = saved.project_id
    except Exception as exc:
        logger.warning("작업 %s 보관함 저장 실패: %s", job.job_id, exc)
        job.warnings.append("보관함에 저장하지 못했어요. 영상은 지금 내려받아 두세요.")


def _project_dict(p: Project, signer: URLSigner, *, detail: bool) -> dict:
    def url(name: str) -> str:
        return signer.sign(f"{LIBRARY_ROUTE_PREFIX}/{p.project_id}/{name}").url

    data = {
        "project_id": p.project_id,
        "created_at": p.created_at,
        "title": p.title,
        "product_name": p.product_name,
        "duration_sec": p.duration_sec,
        "scene_count": p.scene_count,
        "thumbnail_url": url(p.thumbnail_files[0]) if p.thumbnail_files else None,
    }
    if detail:
        data.update(
            script=p.script,
            tone=p.tone,
            description=p.description,
            titles=p.titles,
            reference_video_id=p.reference_video_id,
            image_credits=p.image_credits,
            video_url=url(p.video_file),
            thumbnail_urls=[url(n) for n in p.thumbnail_files],
        )
    return data


async def _load_scenes(request: Request, job_id: str):
    job = request.app.state.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다 (보관 기간이 지났을 수 있어요).")
    if job.status != JobStatus.COMPLETED:
        raise HTTPException(status_code=409, detail="완성된 영상만 고칠 수 있어요.")
    job_dir = request.app.state.settings.output_dir / job.job_id
    scenes_file = job_dir / "scenes.json"
    if not scenes_file.is_file():
        raise HTTPException(status_code=410, detail="장면 정보가 없어 고칠 수 없어요. 새로 만들어 주세요.")
    data = json.loads(await asyncio.to_thread(scenes_file.read_text, encoding="utf-8"))
    return job, job_dir, data


async def _set_favorite(request: Request, video_id: str, on: bool) -> dict:
    store: trends.TrendStore = request.app.state.trends.store
    if not await asyncio.to_thread(store.set_favorite, video_id, on):
        raise HTTPException(status_code=404, detail="영상을 찾을 수 없습니다.")
    return {"video_id": video_id, "favorite": on}


async def _cleanup_loop(app: FastAPI, settings: Settings, interval_sec: float = 600) -> None:
    """보관 기간이 지난 작업과 출력 파일을 주기적으로 삭제합니다."""
    ttl = timedelta(hours=settings.job_ttl_hours)
    while True:
        await asyncio.sleep(interval_sec)
        for job in app.state.jobs.pop_expired(ttl):
            await asyncio.to_thread(shutil.rmtree, settings.output_dir / job.job_id, True)
            logger.info("만료된 작업 삭제: %s", job.job_id)
        old = await asyncio.to_thread(app.state.projects.remove_older_than, settings.library_ttl_days * 86400)
        if old:
            logger.info("보관 기간이 지난 프로젝트 %d개 삭제", old)
        removed = await asyncio.to_thread(app.state.media.remove_older_than, settings.upload_ttl_hours * 3600)
        if removed:
            logger.info("보관 기간이 지난 올린 파일 %d개 삭제", removed)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx는 요청마다 전체 주소를 INFO로 남기는데, YouTube·Pixabay 등은 주소에 API 키가 들어가 로그에 키가 노출됩니다.
logging.getLogger("httpx").setLevel(logging.WARNING)
app = create_app()
