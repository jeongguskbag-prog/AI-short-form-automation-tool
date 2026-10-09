from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

MAX_SCENES = 20


class ScriptRequest(BaseModel):
    script: str = Field(min_length=10, max_length=5000, description="쇼츠로 만들 대본")
    tone: str = Field(default="energetic", min_length=1, max_length=50, description="영상 분위기")

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
    duration_sec: float | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    image_credits: list[str] = Field(default_factory=list, description="사용한 사진의 출처 표기")
    created_at: datetime
    updated_at: datetime
