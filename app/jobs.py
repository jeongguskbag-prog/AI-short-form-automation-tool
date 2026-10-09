"""인메모리 작업 저장소.

단일 프로세스 배포를 전제로 합니다. 여러 워커/서버로 확장할 때는
Redis 같은 외부 저장소와 작업 큐(Celery, arq 등)로 교체하세요.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .media import MediaItem
from .schemas import JobStatus, JobStatusResponse
from .signing import VIDEO_ROUTE_PREFIX, URLSigner


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Job:
    job_id: str
    script: str
    tone: str
    media: list[MediaItem] = field(default_factory=list)  # 씬마다 순서대로 쓸 사용자 영상·사진
    thumbnail_text: str = ""  # 비우면 영상 제목
    thumbnail_files: list[str] = field(default_factory=list)
    status: JobStatus = JobStatus.QUEUED
    progress: int = 0
    title: str | None = None
    scene_count: int | None = None
    video_file: str | None = None  # 작업 폴더 안의 최종 영상 파일 이름
    duration_sec: float | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    image_credits: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    def update(self, **changes) -> None:
        for key, value in changes.items():
            setattr(self, key, value)
        # 진행률은 뒤로 가지 않도록 보장
        self.progress = max(0, min(100, self.progress))
        self.updated_at = _now()

    @property
    def video_path(self) -> str | None:
        return f"{VIDEO_ROUTE_PREFIX}/{self.job_id}/{self.video_file}" if self.video_file else None

    def served_files(self) -> set[str]:
        """서명된 링크로 내려받을 수 있는 파일 이름."""
        return {f for f in (self.video_file, *self.thumbnail_files) if f}

    def to_response(self, signer: URLSigner) -> JobStatusResponse:
        # 조회할 때마다 새 만료 시각으로 서명해서, 상태를 다시 조회하면 링크가 갱신됩니다.
        signed = signer.sign(self.video_path) if self.video_path else None
        return JobStatusResponse(
            job_id=self.job_id,
            status=self.status,
            progress=self.progress,
            title=self.title,
            scene_count=self.scene_count,
            video_url=signed.url if signed else None,
            video_url_expires_at=signed.expires_at if signed else None,
            thumbnail_urls=[
                signer.sign(f"{VIDEO_ROUTE_PREFIX}/{self.job_id}/{name}").url for name in self.thumbnail_files
            ],
            duration_sec=self.duration_sec,
            error=self.error,
            warnings=list(self.warnings),
            image_credits=list(self.image_credits),
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def create(
        self, script: str, tone: str, media: list[MediaItem] | None = None, thumbnail_text: str = ""
    ) -> Job:
        job = Job(
            job_id=uuid.uuid4().hex, script=script, tone=tone, media=list(media or []), thumbnail_text=thumbnail_text
        )
        self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def pop_expired(self, ttl: timedelta) -> list[Job]:
        """TTL이 지난 '종료된' 작업을 저장소에서 제거하고 반환합니다."""
        cutoff = _now() - ttl
        expired = [j for j in self._jobs.values() if j.status.is_terminal and j.updated_at < cutoff]
        for job in expired:
            del self._jobs[job.job_id]
        return expired

    def __len__(self) -> int:
        return len(self._jobs)
