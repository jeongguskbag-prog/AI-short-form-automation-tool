"""애플리케이션 설정. 모든 값은 환경변수 또는 .env 파일로 덮어쓸 수 있습니다."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- 인증 ---
    api_keys: str = Field(
        default="",
        description="쉼표로 구분한 API 키 목록 (API_KEYS). 비우면 인증을 끕니다.",
    )

    url_signing_secret: str = Field(
        default="",
        description="영상 링크 서명용 비밀키. 비우면 시작할 때마다 새로 만들어져 재시작 시 기존 링크가 무효가 됩니다.",
    )
    video_url_ttl_sec: int = Field(default=3600, ge=60, description="영상 링크 유효 시간(초)")

    # --- 외부 서비스 ---
    gemini_api_key: str | None = Field(default=None, description="Gemini API 키 (GEMINI_API_KEY)")
    # "-latest" 별칭은 Google이 최신 안정 모델로 자동 연결해 줘서, 특정 버전이 내려가도 404가 나지 않습니다.
    llm_model: str = "gemini-flash-latest"
    # 기본 모델이 과부하(503) 등으로 계속 실패하면 이 모델로 한 번 더 시도합니다. 비우면 사용하지 않습니다.
    llm_fallback_model: str = "gemini-flash-lite-latest"
    # Gemini 이미지 모델(generate_content)과 Imagen 모델(imagen-*, generate_images)을 모두 지원합니다.
    image_model: str = "gemini-3.1-flash-image"
    # 무료 사진 API 키 (선택). 키가 없는 서비스는 건너뜁니다.
    pexels_api_key: str = ""  # https://www.pexels.com/api/
    pixabay_api_key: str = ""  # https://pixabay.com/api/docs/ (가입 후 바로 발급)
    # Pexels·Pixabay 키가 있으면 씬 배경으로 사진보다 무료 스톡 영상을 먼저 찾습니다 (사용자 영상이 없을 때).
    stock_video: bool = True
    max_stock_video_mb: int = Field(default=40, ge=1, description="스톡 영상 한 개 최대 크기")
    # 이미지를 시도할 순서. 앞의 것이 실패하면 다음 것을 쓰고, 모두 실패하면 그라디언트 배경을 씁니다.
    # openverse는 키 없이 쓸 수 있습니다.
    image_sources: str = "gemini,pexels,pixabay,openverse"
    # 한도 초과·키 오류처럼 다시 해도 안 되는 오류가 난 이미지 소스는 이 시간(초) 동안 건너뜁니다.
    image_source_cooldown_sec: int = Field(default=3600, ge=0)

    # --- 아이템 찾기 (인기 쇼츠 수집) ---
    youtube_api_key: str = Field(default="", description="YouTube Data API v3 키 (YOUTUBE_API_KEY)")
    trends_db_path: Path = Path("data/trends.db")
    # 마지막 수집 후 이 시간이 지나면 다시 모읍니다. 1회 수집에 일일 기본 할당량(10,000)의 약 15%를 씁니다.
    trends_refresh_hours: float = Field(default=12.0, gt=0)
    trends_region: str = "KR"
    # 분야마다 성과가 높은 영상 몇 개의 공개 댓글을 모아 검색에 씁니다 (영상당 할당량 1).
    trends_comment_videos: int = Field(default=10, ge=0)

    # --- 사용자가 올린 영상·사진 ---
    media_dir: Path = Path("data/uploads")
    max_upload_mb: int = Field(default=200, ge=1)
    upload_ttl_hours: float = Field(default=72.0, gt=0, description="올린 파일을 보관하는 시간")

    # --- 보관함 (완성된 영상 저장) ---
    projects_db_path: Path = Path("data/projects.db")
    library_dir: Path = Path("data/library")
    library_ttl_days: float = Field(default=30.0, gt=0, description="보관함에 영상을 남겨 두는 기간(일)")

    # --- 배포 (유튜브) ---
    # Google Cloud 콘솔 → API 및 서비스 → 사용자 인증 정보 → OAuth 클라이언트 ID(웹 애플리케이션)
    youtube_oauth_client_id: str = ""
    youtube_oauth_client_secret: str = ""
    # OAuth 클라이언트의 '승인된 리디렉션 URI'에 똑같이 등록해야 합니다.
    youtube_redirect_uri: str = "http://localhost:8000/oauth/youtube/callback"
    # 업로드할 때 'AI로 만든(변형된) 콘텐츠' 표시를 켭니다 (AI 음성·이미지 사용).
    youtube_declare_synthetic: bool = True
    # 올린 영상의 조회수 등 성과를 이 간격(시간)으로 받아 옵니다.
    stats_refresh_hours: float = Field(default=6.0, gt=0)
    # 예약 올리기 시각이 됐는지 확인하는 간격(초).
    publish_check_sec: float = Field(default=30.0, gt=0)
    accounts_path: Path = Path("data/accounts.json")
    token_encryption_key: str = Field(default="", description="계정 토큰 암호화 키 (Fernet). 비우면 자동 생성")

    # --- 배경음악 ---
    bgm_volume: float = Field(default=0.25, ge=0.0, le=1.0, description="배경음악 기본 음량 (1.0 = 원래 크기)")
    bgm_ducking: bool = Field(default=True, description="내레이션이 나올 때 배경음악을 자동으로 줄임")

    # --- 쇼핑 대본 광고 표시 (공정위 추천·보증 심사지침) ---
    # 설명란 첫 줄에 들어갑니다. 쿠팡 파트너스가 아니면 쓰는 제휴 프로그램에 맞게 바꾸세요.
    ad_disclosure: str = "이 게시물은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
    # 대본 마지막 문장으로 읽고 자막에도 나옵니다.
    ad_disclosure_spoken: str = "이 영상은 쿠팡 파트너스 활동으로 수수료를 받을 수 있어요."

    tts_voice: str = "ko-KR-SunHiNeural"
    tts_rate: str = "+15%"

    # --- 출력 영상 ---
    output_dir: Path = Path("output")
    video_width: int = 720
    video_height: int = 1280
    video_fps: int = 30
    scene_padding_sec: float = Field(default=0.35, ge=0, description="씬 끝에 붙는 여백(초)")
    max_zoom: float = Field(default=1.15, ge=1.0, le=2.0, description="켄 번스 효과 최대 줌 배율")
    font_path: Path | None = Field(default=None, description="자막 폰트 경로. 비우면 한글 폰트를 자동 탐색")
    subtitle_font_size: int = 54
    # word: 읽는 단어를 노랗게 강조하며 내레이션을 짧은 구절로 보여 줌 (TTS가 단어 시각을 줄 때)
    # scene: 씬마다 요약 자막 한 줄 (예전 방식)
    subtitle_mode: Literal["word", "scene"] = "word"

    # --- 동시성 / 안정성 ---
    max_concurrent_jobs: int = Field(default=2, ge=1)
    max_concurrent_scenes: int = Field(default=3, ge=1)
    max_retries: int = Field(default=3, ge=1)
    ffmpeg_timeout_sec: float = 300.0
    job_ttl_hours: float = Field(default=24.0, gt=0, description="완료된 작업과 파일을 보관하는 시간")

    @property
    def api_key_list(self) -> tuple[str, ...]:
        return tuple(k.strip() for k in self.api_keys.split(",") if k.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
