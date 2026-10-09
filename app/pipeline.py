"""쇼츠 생성 파이프라인: 기획 → (씬별 TTS + 이미지 + 렌더링, 병렬) → 이어 붙이기."""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import Settings
from .errors import describe_error, is_retryable, short_reason
from .jobs import Job
from .schemas import JobStatus, ScenePlan, ShortsPlan
from .services import video
from .services.images import make_placeholder
from .services.scriptwriter import ProductBrief, ShoppingScript
from .services.subtitles import WordTiming, karaoke_frames
from .services.thumbnail import Background, make_thumbnails

logger = logging.getLogger(__name__)

# 진행률 구간
PLAN_START, ASSETS_START, ASSETS_END, FINAL_DONE = 5, 15, 90, 100


class Planner(Protocol):
    async def plan(self, script: str, tone: str) -> ShortsPlan: ...


class TTSEngine(Protocol):
    async def synthesize(self, text: str, output_path: Path) -> list[WordTiming] | None:
        """음성을 output_path에 저장하고, 알 수 있으면 단어별 읽는 시각을 돌려줍니다."""
        ...


class ImageGenerator(Protocol):
    async def generate(self, prompt: str, output_path: Path, query: str = "") -> str | None:
        """이미지를 output_path에 저장하고, 출처 표기가 필요하면 그 문구를 돌려줍니다."""
        ...


class ScriptWriter(Protocol):
    async def write(self, brief: ProductBrief) -> ShoppingScript: ...


@dataclass
class Providers:
    planner: Planner
    tts: TTSEngine
    images: ImageGenerator
    scriptwriter: ScriptWriter | None = None


class ShortsPipeline:
    def __init__(self, providers: Providers, settings: Settings, spec: video.VideoSpec) -> None:
        self._providers = providers
        self._settings = settings
        self._spec = spec
        self._job_slots = asyncio.Semaphore(settings.max_concurrent_jobs)
        self._scene_slots = asyncio.Semaphore(settings.max_concurrent_scenes)

    async def run(self, job: Job) -> None:
        """작업을 끝까지 실행합니다. 예외는 삼키지 않고 job 상태에 기록합니다."""
        job_dir = self._settings.output_dir / job.job_id
        try:
            async with self._job_slots:
                job_dir.mkdir(parents=True, exist_ok=True)
                await self._run(job, job_dir)
        except asyncio.CancelledError:
            job.update(status=JobStatus.FAILED, error="서버 종료로 작업이 취소되었습니다.")
            raise
        except Exception as exc:
            logger.exception("작업 %s 실패", job.job_id)
            root: BaseException = exc
            while isinstance(root, BaseExceptionGroup) and root.exceptions:
                root = root.exceptions[0]  # TaskGroup이 감싼 실제 원인을 보여줌
            job.update(status=JobStatus.FAILED, error=describe_error(root))

    async def _run(self, job: Job, job_dir: Path) -> None:
        job.update(status=JobStatus.PLANNING, progress=PLAN_START)
        plan = await self._providers.planner.plan(job.script, job.tone)
        (job_dir / "plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
        job.update(
            status=JobStatus.GENERATING_ASSETS,
            progress=ASSETS_START,
            title=plan.title,
            scene_count=len(plan.scenes),
        )

        done = 0
        step = (ASSETS_END - ASSETS_START) / len(plan.scenes)
        images = _ImageFailures()

        async def build(index: int, scene: ScenePlan) -> tuple[Path, Background]:
            nonlocal done
            async with self._scene_slots:
                result = await self._build_scene(job, job_dir, index, scene, images)
            done += 1
            job.update(progress=ASSETS_START + int(step * done))
            return result

        # TaskGroup: 하나라도 실패하면 나머지 씬 작업을 즉시 취소합니다.
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(build(i, s)) for i, s in enumerate(plan.scenes)]
        scene_paths = [t.result()[0] for t in tasks]
        backgrounds = [t.result()[1] for t in tasks]
        job.warnings.extend(images.summary())

        job.update(status=JobStatus.RENDERING_FINAL, progress=ASSETS_END)
        final_name = f"final_{job.job_id}.mp4"
        duration = await video.concat_scenes(
            scene_paths, job_dir / final_name, timeout=self._settings.ffmpeg_timeout_sec
        )
        for path in scene_paths:  # 중간 산출물 정리 (이미지·음성·기획은 디버깅용으로 남김)
            path.unlink(missing_ok=True)

        # 썸네일은 실패해도 영상은 완성으로 처리합니다. 마지막 씬(광고 표시)은 되도록 피합니다.
        candidates = backgrounds[:-1] if len(backgrounds) > 1 else backgrounds
        try:
            job.thumbnail_files = await make_thumbnails(
                candidates, job.thumbnail_text or plan.title, job_dir, self._spec
            )
        except Exception as exc:
            logger.warning("작업 %s 썸네일 실패: %s", job.job_id, exc)
            job.warnings.append(f"썸네일을 만들지 못했어요 ({short_reason(exc)})")

        job.update(
            status=JobStatus.COMPLETED,
            progress=FINAL_DONE,
            duration_sec=round(duration, 2),
            video_file=final_name,
        )
        logger.info("작업 %s 완료: %d개 씬, %.1f초", job.job_id, len(plan.scenes), duration)

    async def _build_scene(
        self, job: Job, job_dir: Path, index: int, scene: ScenePlan, images: "_ImageFailures"
    ) -> tuple[Path, Background]:
        n = index + 1
        audio_path = job_dir / f"audio_{n:02d}.mp3"
        image_path = job_dir / f"image_{n:02d}.png"
        video_start: float | None = None
        subtitle_path = job_dir / f"subtitle_{n:02d}.png"
        scene_path = job_dir / f"scene_{n:02d}.mp4"

        if job.media:
            # 사용자 영상·사진을 순서대로 쓰고, 씬이 더 많으면 반복합니다.
            # 같은 영상을 다시 쓸 때는 3초씩 뒤에서 시작해 같은 장면이 반복되지 않게 합니다.
            item = job.media[index % len(job.media)]
            image_path = item.path
            if item.kind == "video" and item.duration:
                start = (index // len(job.media)) * 3.0 % item.duration
                video_start = start if start < item.duration - 0.5 else 0.0

        async def image_with_fallback() -> None:
            if job.media:
                return
            if images.blocked:
                # 한도 0·키 오류처럼 다시 해도 안 되는 오류가 이미 났다면 요청을 아낍니다.
                images.add(n, images.blocked)
            else:
                try:
                    credit = await self._providers.images.generate(
                        scene.visual_prompt, image_path, scene.stock_query
                    )
                    if credit and credit not in job.image_credits:
                        job.image_credits.append(credit)
                    return
                except Exception as exc:
                    reason = short_reason(exc)
                    logger.warning("작업 %s 씬 %d 이미지 생성 실패, 대체 배경 사용: %s", job.job_id, n, reason)
                    images.add(n, reason)
                    if not is_retryable(exc):
                        images.blocked = reason
            await make_placeholder(scene.visual_prompt, self._spec.size, image_path)

        # 음성은 필수, 이미지는 실패해도 대체 배경으로 진행
        words, _ = await asyncio.gather(
            self._providers.tts.synthesize(scene.narration, audio_path),
            image_with_fallback(),
        )
        timed: list[tuple[Path, float, float | None]] | None = None
        frames = karaoke_frames(words) if words and self._settings.subtitle_mode == "word" else []
        if frames:
            timed = []
            for k, frame in enumerate(frames):
                png = job_dir / f"subtitle_{n:02d}_{k:03d}.png"
                await video.render_subtitle_png(frame.text, self._spec, png, highlight=frame.highlight)
                timed.append((png, frame.start, frame.end))
        else:
            await video.render_subtitle_png(scene.subtitle_text, self._spec, subtitle_path)
        await video.render_scene(
            image_path=image_path,
            audio_path=audio_path,
            subtitle_png=None if timed else subtitle_path,
            timed_subtitles=timed,
            output_path=scene_path,
            spec=self._spec,
            zoom_in=index % 2 == 0,  # 씬마다 줌 인/아웃을 번갈아 단조로움을 줄임
            video_start=video_start,
        )
        return scene_path, Background(image_path, video_start)


class _ImageFailures:
    """작업 하나의 이미지 실패를 모아 같은 이유끼리 경고 하나로 묶습니다."""

    def __init__(self) -> None:
        self.blocked: str | None = None
        self._scenes_by_reason: dict[str, list[int]] = {}

    def add(self, scene_no: int, reason: str) -> None:
        self._scenes_by_reason.setdefault(reason, []).append(scene_no)

    def summary(self) -> list[str]:
        return [
            f"씬 {', '.join(map(str, sorted(scenes)))}: 이미지를 만들지 못해 기본 배경을 사용했어요 ({reason})"
            for reason, scenes in self._scenes_by_reason.items()
        ]
