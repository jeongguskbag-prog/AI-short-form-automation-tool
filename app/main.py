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
from .auth import require_api_key
from .config import Settings, get_settings
from .errors import api_error_code, describe_error
from .jobs import JobStore
from .media import MediaError, MediaStore, MediaTooLarge
from .pipeline import Providers, ShortsPipeline
from .projects import Project, ProjectStore, now_iso
from .publishers.youtube import UploadRequest, YouTubeClient, YouTubeError
from .schemas import (
    CreateJobResponse,
    JobStatus,
    JobStatusResponse,
    PublishRequest,
    ReviseRequest,
    ScriptRequest,
    ShoppingScriptRequest,
    ShoppingScriptResponse,
)
from .services import trends, video
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
    from .services.scriptwriter import GeminiScriptWriter
    from .services.trend_analysis import GeminiTrendAnalyzer
    from .services.tts import EdgeTTS

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
        tts=EdgeTTS(settings),
        images=ImageSourceChain(sources, settings.image_source_cooldown_sec),
        scriptwriter=GeminiScriptWriter(client, settings),
        trend_analyzer=GeminiTrendAnalyzer(client, settings),
        matcher=GeminiMediaMatcher(client, settings),
        footage=ImageSourceChain(footage, settings.image_source_cooldown_sec) if footage else None,
    )


def create_app(
    settings: Settings | None = None,
    providers: Providers | None = None,
    trend_source: trends.YouTubeTrendSource | None = None,
    youtube: YouTubeClient | None = None,
    setup_checker: SetupChecker | None = None,
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

        background = [
            asyncio.create_task(_cleanup_loop(app, settings)),
            asyncio.create_task(app.state.trends.loop()),
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
        title="ai-shortform-video",
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

    @app.get("/library", include_in_schema=False)
    async def library_page() -> FileResponse:
        return FileResponse(WEB_LIBRARY)

    @app.get("/trends", include_in_schema=False)
    async def trends_page() -> FileResponse:
        return FileResponse(WEB_TRENDS)

    @app.get("/health", tags=["system"])
    async def health(request: Request) -> dict:
        # 웹 화면이 키 입력란을 띄울지 판단하는 데 씁니다. 작업 수 같은 내부 정보는 노출하지 않습니다.
        return {"status": "ok", "auth_required": bool(request.app.state.api_keys)}

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

    @app.put("/api/v1/trends/{video_id}/favorite", tags=["trends"], dependencies=[Depends(require_api_key)])
    async def add_favorite(video_id: str, request: Request) -> dict:
        return await _set_favorite(request, video_id, True)

    @app.delete("/api/v1/trends/{video_id}/favorite", tags=["trends"], dependencies=[Depends(require_api_key)])
    async def remove_favorite(video_id: str, request: Request) -> dict:
        return await _set_favorite(request, video_id, False)

    @app.get("/api/v1/accounts", tags=["publish"], dependencies=[Depends(require_api_key)])
    async def list_accounts(request: Request) -> dict:
        yt: YouTubeClient = request.app.state.youtube
        account = await asyncio.to_thread(request.app.state.accounts.get, "youtube")
        return {"youtube": {
            "configured": yt.configured,
            "connected": account is not None,
            "name": account[0] if account else None,
            "redirect_uri": settings.youtube_redirect_uri,
        }}

    @app.post("/api/v1/accounts/youtube/connect", tags=["publish"], dependencies=[Depends(require_api_key)])
    async def connect_youtube(request: Request) -> dict:
        """구글 로그인·권한 동의 페이지 주소를 돌려줍니다. 브라우저로 열면 끝나고 이 서버로 돌아옵니다."""
        yt: YouTubeClient = request.app.state.youtube
        if not yt.configured:
            raise HTTPException(status_code=409, detail="YOUTUBE_OAUTH_CLIENT_ID / SECRET을 먼저 설정하세요.")
        return {"auth_url": yt.auth_url()}

    @app.delete("/api/v1/accounts/youtube", tags=["publish"], dependencies=[Depends(require_api_key)])
    async def disconnect_youtube(request: Request) -> dict:
        await asyncio.to_thread(request.app.state.accounts.delete, "youtube")
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
        """보관함의 영상을 연결한 유튜브 채널에 올립니다 (뒤에서 진행, 결과는 프로젝트 상세의 publications)."""
        state = request.app.state
        store: ProjectStore = state.projects
        project = await asyncio.to_thread(store.get, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")
        account = await asyncio.to_thread(state.accounts.get, "youtube")
        if account is None:
            raise HTTPException(status_code=409, detail="유튜브 채널을 먼저 연결하세요.")
        pubs = await asyncio.to_thread(store.publications, project_id)
        if any(p["status"] == "uploading" for p in pubs):
            raise HTTPException(status_code=409, detail="이미 올리는 중이에요.")
        if any(p["status"] == "scheduled" for p in pubs):
            raise HTTPException(status_code=409, detail="이미 예약돼 있어요. 예약을 취소한 뒤 다시 하세요.")
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
            store.add_publication, project_id, "youtube", title=req.title, privacy=req.privacy,
            scheduled_at=scheduled_at,
        )
        if scheduled_at is None:
            _start_upload(state, pub_id, project, req.title, req.privacy, account[1]["refresh_token"])
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

    return SetupChecker(settings, gemini_check=gemini_check, tts_check=tts_check, youtube_status=youtube_status)


def _start_upload(state, pub_id: int, project: Project, title: str, privacy: str, refresh_token: str) -> None:
    store: ProjectStore = state.projects
    upload = UploadRequest(
        video_path=store.library_dir / project.project_id / project.video_file,
        title=title,
        description=project.description or project.title,
        tags=[w for w in project.description.split() if w.startswith("#") and w.lower() != "#shorts"],
        privacy=privacy,
    )
    task = asyncio.create_task(_upload_youtube(state, pub_id, refresh_token, upload), name=f"publish-{pub_id}")
    state.tasks.add(task)
    task.add_done_callback(state.tasks.discard)


async def _publish_scheduled_loop(app: FastAPI, interval_sec: float = 30) -> None:
    """예약 시각이 된 올리기를 시작합니다."""
    state = app.state
    store: ProjectStore = state.projects
    while True:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        for pub in await asyncio.to_thread(store.claim_due, now):
            project = await asyncio.to_thread(store.get, pub["project_id"])
            account = await asyncio.to_thread(state.accounts.get, "youtube")
            if project is None or account is None:
                reason = "보관함에서 영상이 지워졌어요." if project is None else "유튜브 채널 연결이 해제돼 있었어요."
                await asyncio.to_thread(store.finish_publication, pub["id"], error=f"예약 올리기 실패: {reason}")
                continue
            _start_upload(state, pub["id"], project, pub["title"], pub["privacy"], account[1]["refresh_token"])
        await asyncio.sleep(interval_sec)


async def _upload_youtube(state, pub_id: int, refresh_token: str, upload: UploadRequest) -> None:
    store: ProjectStore = state.projects
    try:
        video_id = await state.youtube.upload(refresh_token, upload)
    except Exception as exc:
        logger.warning("유튜브 업로드 실패 (%s): %s", pub_id, exc)
        message = str(exc) if isinstance(exc, YouTubeError) else f"업로드 실패: {exc}"
        await asyncio.to_thread(store.finish_publication, pub_id, error=message)
        return
    await asyncio.to_thread(
        store.finish_publication, pub_id, url=f"https://youtube.com/shorts/{video_id}", video_id=video_id
    )


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


def _oauth_page(message: str, *, ok: bool) -> HTMLResponse:
    color = "#12805c" if ok else "#c62f3b"
    body = (
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'><title>유튜브 연결</title></head>"
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
app = create_app()
