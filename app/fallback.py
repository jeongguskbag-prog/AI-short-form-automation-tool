"""여러 외부 서비스를 순서대로 시도하는 공통 로직 (이미지 소스, 나레이션 음성 등)."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

from .errors import is_retryable, short_reason

S = TypeVar("S")
T = TypeVar("T")
logger = logging.getLogger(__name__)


class AllSourcesFailed(RuntimeError):
    pass


class FallbackChain(Generic[S]):
    """앞의 서비스가 실패하면 다음 서비스로 넘어갑니다.

    한도 0·키 오류·접근 차단처럼 다시 해도 안 되는 오류가 난 서비스는 cooldown 동안 건너뛰어,
    씬마다 헛된 요청을 보내지 않습니다.
    """

    def __init__(
        self,
        sources: list[tuple[str, S]],
        cooldown_sec: float,
        what: str,
        error_cls: type[Exception] = AllSourcesFailed,
    ) -> None:
        self._error_cls = error_cls
        self._sources = sources
        self._cooldown = cooldown_sec
        self._what = what
        self._disabled_until: dict[str, tuple[float, str]] = {}

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self._sources]

    async def run(self, call: Callable[[S], Awaitable[T]]) -> T:
        reasons: list[str] = []
        for name, source in self._sources:
            skipped = self._disabled_until.get(name)
            if skipped and skipped[0] > time.monotonic():
                reasons.append(f"{name}: {skipped[1]}")
                continue
            try:
                return await call(source)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = short_reason(exc)
                logger.warning("%s(%s) 실패: %s", self._what, name, exc)
                reasons.append(f"{name}: {reason}")
                if not is_retryable(exc) and self._cooldown > 0:
                    self._disabled_until[name] = (time.monotonic() + self._cooldown, reason)
        if not self._sources:
            reasons.append(f"사용할 {self._what} 서비스가 없습니다")
        raise self._error_cls(" / ".join(reasons))
