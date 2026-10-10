from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

MAX_SCENES = 20


class PublishRequest(BaseModel):
    platform: Literal["youtube"] = "youtube"
    title: str = Field(min_length=1, max_length=100)
    privacy: Literal["private", "unlisted", "public"] = Field(
        default="private", description="구글 앱 심사 전에는 무엇을 골라도 비공개로 올라갑니다"
    )


class ProjectInfo(BaseModel):
    """보관함에 함께 저장할 정보 (쇼핑 대본 자동 작성 결과 등). 영상 생성에는 쓰지 않습니다."""

    product_name: str = Field(default="", max_length=100)
    description: str = Field(default="", max_length=5000, description="업로드 설명란")
    titles: list[str] = Field(default_factory=list, max_length=10)
    reference_video_id: str | None = Field(default=None, max_length=20)


class ScriptRequest(BaseModel):
    script: str = Field(min_length=10, max_length=5000, description="쇼츠로 만들 대본")
    tone: str = Field(default="energetic", min_length=1, max_length=50, description="영상 분위기")
    thumbnail_text: str = Field(default="", max_length=40, description="썸네일 문구. 비우면 영상 제목")
    bgm_media_id: str | None = Field(
        default=None, max_length=40, description="PUT /api/v1/media로 올린 배경음악 (mp3, m4a, aac, wav, ogg)"
    )
    bgm_volume: float | None = Field(default=None, ge=0.0, le=1.0, description="배경음악 음량. 비우면 서버 기본값")
    project: ProjectInfo | None = Field(default=None, description="보관함에 함께 저장할 정보")
    media_order: Literal["auto", "upload"] = Field(
        default="auto", description="auto: AI가 장면 내용에 맞게 소재 배치 / upload: 올린 순서대로"
    )
    media_ids: list[str] = Field(
        default_factory=list, max_length=20,
        description="PUT /api/v1/media로 올린 영상·사진. 씬마다 순서대로 배경으로 쓰고, 씬이 더 많으면 반복합니다.",
    )

    @field_validator("script", "tone")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("공백만으로 이루어질 수 없습니다.")
        return value


class ShoppingScriptRequest(BaseModel):
    product_name: str = Field(min_length=1, max_length=100, description="제품명")
    features: str = Field(default="", max_length=2000, description="제품 특징 메모 (장점, 용도, 사용법 등)")
    tone: str = Field(default="energetic", min_length=1, max_length=50)
    reference_video_id: str | None = Field(
        default=None, max_length=20, description="아이템 찾기에서 고른 참고 영상 ID (후킹·시청자 반응만 참고)"
    )
    use_channel_insights: bool = Field(default=True, description="내 채널에서 잘된·덜된 영상의 첫 문장을 참고")

    @field_validator("product_name", "tone")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("공백만으로 이루어질 수 없습니다.")
        return value


class ShoppingScriptResponse(BaseModel):
    hooks: list[str] = Field(description="첫 문장(후킹) 후보")
    scripts: list[str] = Field(description="후킹별 완성 대본 (후킹 + 본문 + 행동 유도 + 광고 표시)")
    body: str
    cta: str
    disclosure: str = Field(description="대본 끝에 붙인 광고 표시 문장")
    titles: list[str]
    hashtags: list[str]
    description: str = Field(description="업로드 설명란 (첫 줄 광고 표시)")
    warnings: list[str] = Field(description="과장·허위로 보일 수 있는 표현")
    reference_used: bool
    insights_used: int = Field(default=0, description="참고한 내 채널 영상 수")


class ScenePlan(BaseModel):
    scene_id: int
    narration: str
    visual_prompt: str
    subtitle_text: str
    # 무료 사진 사이트 검색용 영어 키워드 (예: "glass of water morning")
    stock_query: str = ""


class ShortsPlan(BaseModel):
    """LLM 응답 스키마. Gemini의 response_schema로도 쓰이므로 단순하게 유지합니다."""

    title: str
    scenes: list[ScenePlan]


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    PLANNING = "PLANNING"
    GENERATING_ASSETS = "GENERATING_ASSETS"
    RENDERING_FINAL = "RENDERING_FINAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in (JobStatus.COMPLETED, JobStatus.FAILED)


class CreateJobResponse(BaseModel):
    job_id: str
    status: JobStatus
    status_url: str
    message: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    progress: int = Field(ge=0, le=100)
    title: str | None = None
    scene_count: int | None = None
    video_url: str | None = Field(default=None, description="서명된 영상 링크. 만료되면 상태를 다시 조회하세요.")
    video_url_expires_at: datetime | None = None
    thumbnail_urls: list[str] = Field(default_factory=list, description="썸네일 3안 (서명된 링크)")
    project_id: str | None = Field(default=None, description="완성 후 보관함에 저장된 프로젝트")
    duration_sec: float | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    image_credits: list[str] = Field(default_factory=list, description="사용한 사진의 출처 표기")
    created_at: datetime
    updated_at: datetime
