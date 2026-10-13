"""아이템 찾기: 분야별 인기 쇼츠를 YouTube Data API로 모아 SQLite에 저장하고, 화면 조건대로 골라 정렬합니다.

화면이 열릴 때마다 API를 부르지 않고 정해진 주기로 한 번 모아 둔 저장본을 씁니다 (할당량 절약).
영상 파일은 내려받지 않고 공개 지표(조회수·좋아요·댓글 수·구독자 수)와 제목·태그·댓글 글만 저장합니다.
"""

import asyncio
import json
import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from ..config import Settings
from ..retry import with_retry

logger = logging.getLogger(__name__)

# 화면의 분야 드롭다운 순서. 분야마다 YouTube 검색어 2개 (검색 1회 = 할당량 100).
CATEGORIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "living": ("생활·가전·인테리어", ("살림템 추천", "인테리어 소품 추천")),
    "kitchen": ("주방", ("주방용품 추천", "주방 꿀템")),
    "beauty": ("뷰티", ("뷰티템 추천", "화장품 추천")),
    "digital": ("디지털", ("전자기기 추천", "디지털 꿀템")),
    "fashion": ("패션", ("패션 아이템 추천", "코디 추천템")),
    "baby": ("육아", ("육아템 추천", "아기용품 추천")),
    "pet": ("반려동물", ("강아지 용품 추천", "고양이 용품 추천")),
    "camping": ("캠핑", ("캠핑용품 추천", "캠핑 꿀템")),
}

PERIODS: dict[str, int] = {"1m": 30, "3m": 90, "6m": 180, "1y": 365}

SORTS: dict[str, str] = {
    "performance": "성과순",
    "views": "조회수순",
    "likes": "좋아요순",
    "comments": "댓글순",
    "latest": "최신순",
}

API_BASE = "https://www.googleapis.com/youtube/v3"


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- 수집


class TrendCollectionError(RuntimeError):
    def __init__(self, code: int, reason: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason


_REASON_HINTS = {
    "quotaExceeded": "YouTube API 일일 할당량을 다 썼습니다. 한국 시간 오후 4~5시(태평양 자정)에 초기화됩니다.",
    "keyInvalid": "YOUTUBE_API_KEY가 올바르지 않습니다.",
    "accessNotConfigured": "Google Cloud 프로젝트에서 YouTube Data API v3를 사용 설정해 주세요.",
    "forbidden": "YouTube API 키에 권한이 없습니다.",
}


@dataclass
class TrendVideo:
    video_id: str
    category: str
    title: str
    channel_id: str
    channel_title: str
    thumbnail_url: str
    published_at: str
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    subscribers: int | None = None
    tags: str = ""
    comments_text: str = ""
    description: str = ""  # 영상 설명란 앞부분 (제품 목록·구매 링크가 자주 적혀 있음)

    @property
    def performance(self) -> float | None:
        if self.views is None or not self.subscribers:
            return None
        return self.views / self.subscribers


class YouTubeTrendSource:
    """search.list → videos.list → channels.list (→ 상위 영상 commentThreads.list) 순서로 모읍니다."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport  # 테스트에서 가짜 응답을 넣을 때 사용

    async def _get(self, client: httpx.AsyncClient, endpoint: str, params: dict) -> dict:
        async def call() -> dict:
            response = await client.get(
                f"{API_BASE}/{endpoint}", params={**params, "key": self._settings.youtube_api_key}
            )
            if response.status_code == 200:
                return response.json()
            try:
                error = response.json().get("error") or {}
                reason = (error.get("errors") or [{}])[0].get("reason", "")
            except ValueError:
                reason = ""
            message = _REASON_HINTS.get(reason, f"YouTube API 오류 (HTTP {response.status_code} {reason})".strip())
            raise TrendCollectionError(response.status_code, reason, message)

        return await with_retry(call, attempts=self._settings.max_retries, what=f"YouTube {endpoint}")

    async def description(self, video_id: str) -> str:
        """영상 하나의 설명란 (설명란을 저장하기 전에 모은 영상을 분석할 때, 할당량 1)."""
        async with httpx.AsyncClient(transport=self._transport, timeout=httpx.Timeout(20.0)) as client:
            data = await self._get(client, "videos", {"part": "snippet", "id": video_id})
        items = data.get("items") or []
        return ((items[0].get("snippet") or {}).get("description") or "") if items else ""

    async def collect(self, categories: Iterable[str] | None = None) -> list[TrendVideo]:
        published_after = _iso(_now() - timedelta(days=PERIODS["1y"]))
        found: dict[str, str] = {}  # video_id → 처음 찾은 분야
        async with httpx.AsyncClient(transport=self._transport, timeout=httpx.Timeout(20.0)) as client:
            for key in categories or CATEGORIES:
                for query in CATEGORIES[key][1]:
                    data = await self._get(client, "search", {
                        "part": "snippet", "type": "video", "videoDuration": "short",
                        "q": f"{query} #shorts", "order": "viewCount", "maxResults": 50,
                        "publishedAfter": published_after,
                        "regionCode": self._settings.trends_region, "relevanceLanguage": "ko",
                    })
                    for item in data.get("items") or []:
                        video_id = (item.get("id") or {}).get("videoId")
                        if video_id:
                            found.setdefault(video_id, key)

            videos: list[TrendVideo] = []
            ids = list(found)
            for start in range(0, len(ids), 50):
                data = await self._get(client, "videos", {
                    "part": "snippet,statistics", "id": ",".join(ids[start:start + 50]), "maxResults": 50,
                })
                for item in data.get("items") or []:
                    snippet = item.get("snippet") or {}
                    stats = item.get("statistics") or {}
                    thumbs = snippet.get("thumbnails") or {}
                    thumb = next((thumbs[k]["url"] for k in ("high", "medium", "default") if k in thumbs), "")
                    videos.append(TrendVideo(
                        video_id=item["id"],
                        category=found[item["id"]],
                        title=snippet.get("title", ""),
                        channel_id=snippet.get("channelId", ""),
                        channel_title=snippet.get("channelTitle", ""),
                        thumbnail_url=thumb,
                        published_at=snippet.get("publishedAt", ""),
                        # 좋아요·댓글 수는 작성자가 숨기면 응답에 없습니다 → "알 수 없음"
                        views=_int(stats.get("viewCount")),
                        likes=_int(stats.get("likeCount")),
                        comments=_int(stats.get("commentCount")),
                        tags=" ".join(snippet.get("tags") or []),
                        description=(snippet.get("description") or "")[:DESCRIPTION_CHARS],
                    ))

            channel_ids = list(dict.fromkeys(v.channel_id for v in videos if v.channel_id))
            subscribers: dict[str, int | None] = {}
            for start in range(0, len(channel_ids), 50):
                data = await self._get(client, "channels", {
                    "part": "statistics", "id": ",".join(channel_ids[start:start + 50]), "maxResults": 50,
                })
                for item in data.get("items") or []:
                    stats = item.get("statistics") or {}
                    hidden = stats.get("hiddenSubscriberCount")
                    subscribers[item["id"]] = None if hidden else _int(stats.get("subscriberCount"))
            for v in videos:
                v.subscribers = subscribers.get(v.channel_id)

            await self._collect_comments(client, videos)
        return videos

    async def _collect_comments(self, client: httpx.AsyncClient, videos: list[TrendVideo]) -> None:
        limit = self._settings.trends_comment_videos
        if limit <= 0:
            return
        by_category: dict[str, list[TrendVideo]] = {}
        for v in videos:
            by_category.setdefault(v.category, []).append(v)
        for group in by_category.values():
            group.sort(key=lambda v: v.performance or 0, reverse=True)
            for v in group[:limit]:
                try:
                    data = await self._get(client, "commentThreads", {
                        "part": "snippet", "videoId": v.video_id, "maxResults": 20,
                        "order": "relevance", "textFormat": "plainText",
                    })
                except TrendCollectionError as exc:
                    if exc.reason == "quotaExceeded":
                        raise
                    continue  # 댓글을 막아 둔 영상(commentsDisabled) 등은 건너뜀
                texts = [
                    ((item.get("snippet") or {}).get("topLevelComment") or {})
                    .get("snippet", {}).get("textDisplay", "")
                    for item in data.get("items") or []
                ]
                v.comments_text = "\n".join(t for t in texts if t)


# --------------------------------------------------------------------------- 저장

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trend_videos (
    video_id      TEXT PRIMARY KEY,
    category      TEXT NOT NULL,
    title         TEXT NOT NULL,
    channel_id    TEXT NOT NULL,
    channel_title TEXT NOT NULL,
    thumbnail_url TEXT NOT NULL,
    published_at  TEXT NOT NULL,
    views         INTEGER,
    likes         INTEGER,
    comments      INTEGER,
    subscribers   INTEGER,
    tags          TEXT NOT NULL DEFAULT '',
    comments_text TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    collection_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS analyses (
    video_id   TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    data       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS favorites (
    video_id TEXT PRIMARY KEY,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS collections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    video_count INTEGER NOT NULL
);
"""

_COLUMNS = (
    "video_id", "category", "title", "channel_id", "channel_title", "thumbnail_url", "published_at",
    "views", "likes", "comments", "subscribers", "tags", "comments_text", "description",
)
DESCRIPTION_CHARS = 1500


@dataclass
class Collection:
    id: int
    started_at: str
    finished_at: str
    video_count: int


class TrendStore:
    """SQLite 저장소. 호출마다 연결을 새로 열어 스레드(asyncio.to_thread)에서 안전하게 씁니다."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(_SCHEMA)
            columns = {r["name"] for r in db.execute("PRAGMA table_info(trend_videos)")}
            if "description" not in columns:  # 설명란을 저장하기 전에 만든 저장본
                db.execute("ALTER TABLE trend_videos ADD COLUMN description TEXT NOT NULL DEFAULT ''")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._path)
        db.row_factory = sqlite3.Row
        return db

    def last_collection(self) -> Collection | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM collections ORDER BY id DESC LIMIT 1").fetchone()
        return Collection(**dict(row)) if row else None

    def replace(self, videos: list[TrendVideo], started_at: datetime, finished_at: datetime) -> Collection:
        """새 수집 결과로 바꿉니다. 즐겨찾기한 영상은 이번에 안 잡혀도 이전 값으로 남깁니다."""
        with self._connect() as db:
            cur = db.execute(
                "INSERT INTO collections (started_at, finished_at, video_count) VALUES (?, ?, ?)",
                (_iso(started_at), _iso(finished_at), len(videos)),
            )
            collection_id = cur.lastrowid
            db.execute("DELETE FROM trend_videos WHERE video_id NOT IN (SELECT video_id FROM favorites)")
            db.executemany(
                f"INSERT OR REPLACE INTO trend_videos ({', '.join(_COLUMNS)}, collection_id) "
                f"VALUES ({', '.join('?' * len(_COLUMNS))}, ?)",
                [tuple(getattr(v, c) for c in _COLUMNS) + (collection_id,) for v in videos],
            )
            db.execute("DELETE FROM collections WHERE id < ?", (collection_id - 20,))
            # 목록에서 빠진 영상의 분석은 지웁니다 (남은 영상은 다시 부르지 않도록 그대로 둠).
            db.execute("DELETE FROM analyses WHERE video_id NOT IN (SELECT video_id FROM trend_videos) "
                       "AND video_id NOT IN (SELECT video_id FROM favorites)")
        return Collection(collection_id, _iso(started_at), _iso(finished_at), len(videos))

    def set_favorite(self, video_id: str, on: bool) -> bool:
        """영상이 저장소에 없으면 False."""
        with self._connect() as db:
            if not db.execute("SELECT 1 FROM trend_videos WHERE video_id = ?", (video_id,)).fetchone():
                return False
            if on:
                db.execute(
                    "INSERT OR IGNORE INTO favorites (video_id, added_at) VALUES (?, ?)", (video_id, _iso(_now()))
                )
            else:
                db.execute("DELETE FROM favorites WHERE video_id = ?", (video_id,))
        return True

    def get_analysis(self, video_id: str) -> dict | None:
        with self._connect() as db:
            r = db.execute("SELECT data FROM analyses WHERE video_id = ?", (video_id,)).fetchone()
        return json.loads(r["data"]) if r else None

    def set_analysis(self, video_id: str, data: dict) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO analyses (video_id, created_at, data) VALUES (?, ?, ?)",
                (video_id, _iso(_now()), json.dumps(data, ensure_ascii=False)),
            )

    def set_description(self, video_id: str, description: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE trend_videos SET description = ? WHERE video_id = ?",
                       (description[:DESCRIPTION_CHARS], video_id))

    def get(self, video_id: str) -> TrendVideo | None:
        with self._connect() as db:
            r = db.execute("SELECT * FROM trend_videos WHERE video_id = ?", (video_id,)).fetchone()
        return TrendVideo(**{c: r[c] for c in _COLUMNS}) if r else None

    def all_rows(self) -> list[tuple[TrendVideo, bool, int]]:
        """(영상, 즐겨찾기 여부, 수집 회차)"""
        with self._connect() as db:
            rows = db.execute(
                "SELECT t.*, f.video_id IS NOT NULL AS fav FROM trend_videos t "
                "LEFT JOIN favorites f ON f.video_id = t.video_id"
            ).fetchall()
        return [(TrendVideo(**{c: r[c] for c in _COLUMNS}), bool(r["fav"]), r["collection_id"]) for r in rows]


# --------------------------------------------------------------------------- 조회


@dataclass
class TrendQueryResult:
    items: list[dict]
    sort: str
    collection: Collection | None
    favorites: int = 0
    favorites_kept: int = 0  # 이번 수집에 없었지만 즐겨찾기라 남긴 수
    with_value: int = 0  # 정렬 기준 값이 있는 수 (즐겨찾기 제외)
    missing_value: int = 0  # 값을 알 수 없어 뒤로 보낸 수
    out_of_period: int = 0  # 기간 밖이라 뺀 수
    total: int = 0  # 조건에 맞는 전체 수 (limit 전)


def _sort_value(v: TrendVideo, sort: str) -> float | None:
    if sort == "performance":
        return v.performance
    if sort == "views":
        return v.views
    if sort == "likes":
        return v.likes
    if sort == "comments":
        return v.comments
    return None  # latest는 게시일로 정렬


def _matches(v: TrendVideo, q: str) -> bool:
    haystack = " ".join((v.title, v.tags, v.channel_title, v.comments_text)).lower()
    return all(word in haystack for word in q.lower().split())


def _published(v: TrendVideo) -> datetime | None:
    try:
        return datetime.fromisoformat(v.published_at.replace("Z", "+00:00"))
    except ValueError:
        return None


def _ordered(videos: list[TrendVideo], sort: str) -> tuple[list[TrendVideo], int]:
    """정렬 기준 값이 있는 것을 먼저(내림차순), 없는 것은 뒤에 최신순으로. (목록, 값 있는 수)"""
    by_date = sorted(videos, key=lambda v: v.published_at, reverse=True)
    if sort == "latest":
        return by_date, len(by_date)
    known = [v for v in by_date if _sort_value(v, sort) is not None]
    unknown = [v for v in by_date if _sort_value(v, sort) is None]
    known.sort(key=lambda v: _sort_value(v, sort), reverse=True)  # type: ignore[arg-type, return-value]
    return known + unknown, len(known)


def to_item(v: TrendVideo, favorite: bool) -> dict:
    return {
        "video_id": v.video_id,
        "category": v.category,
        "category_label": CATEGORIES.get(v.category, (v.category,))[0],
        "title": v.title,
        "channel_title": v.channel_title,
        "thumbnail_url": v.thumbnail_url,
        "published_at": v.published_at,
        "views": v.views,
        "likes": v.likes,
        "comments": v.comments,
        "subscribers": v.subscribers,
        "performance": round(v.performance, 1) if v.performance is not None else None,
        "favorite": favorite,
        "url": f"https://www.youtube.com/shorts/{v.video_id}",
    }


def query_trends(
    rows: list[tuple[TrendVideo, bool, int]],
    collection: Collection | None,
    *,
    category: str = "all",
    period: str = "1m",
    sort: str = "performance",
    q: str = "",
    limit: int = 120,
    now: datetime | None = None,
) -> TrendQueryResult:
    now = now or _now()
    cutoff = now - timedelta(days=PERIODS[period])
    q = q.strip()
    result = TrendQueryResult(items=[], sort=sort, collection=collection)

    # 즐겨찾기는 검색 조건과 관계없이 맨 앞에 둡니다.
    favorites = [v for v, fav, _ in rows if fav]
    result.favorites = len(favorites)
    if collection:
        result.favorites_kept = sum(1 for _, fav, cid in rows if fav and cid != collection.id)

    candidates: list[TrendVideo] = []
    for v, fav, _ in rows:
        if fav:
            continue
        if category != "all" and v.category != category:
            continue
        if q and not _matches(v, q):
            continue
        published = _published(v)
        if published is None or published < cutoff:
            result.out_of_period += 1
            continue
        candidates.append(v)

    ordered, result.with_value = _ordered(candidates, sort)
    result.missing_value = len(ordered) - result.with_value
    result.total = len(favorites) + len(ordered)
    fav_ordered, _ = _ordered(favorites, sort)
    result.items = [to_item(v, True) for v in fav_ordered] + [to_item(v, False) for v in ordered]
    result.items = result.items[:limit]
    return result


def reference_summary(v: TrendVideo, max_comment_chars: int = 800) -> str:
    """대본 작성 AI에 넘길 참고 영상 요약. 영상 내용이 아니라 공개 정보만 씁니다."""
    lines = [f"제목: {v.title}"]
    if v.tags:
        lines.append(f"태그: {v.tags}")
    stats = [f"{label} {value:,}" for label, value in (("조회수", v.views), ("댓글", v.comments)) if value is not None]
    if v.performance is not None:
        stats.append(f"구독자 대비 {v.performance:.0f}배")
    if stats:
        lines.append("반응: " + ", ".join(stats))
    if v.description:
        lines.append("설명란:\n" + v.description[:max_comment_chars])
    if v.comments_text:
        lines.append("시청자 댓글:\n" + v.comments_text[:max_comment_chars])
    return "\n".join(lines)


# --------------------------------------------------------------------------- 주기 수집


class TrendCollector:
    """저장본이 오래되면 다시 모읍니다. 한 번에 하나만 실행합니다."""

    def __init__(self, store: TrendStore, source: YouTubeTrendSource | None, settings: Settings) -> None:
        self.store = store
        self.source = source
        self._settings = settings
        self._lock = asyncio.Lock()
        self.last_error: str | None = None
        self._failed_at: datetime | None = None

    @property
    def configured(self) -> bool:
        return self.source is not None

    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def is_stale(self) -> bool:
        last = await asyncio.to_thread(self.store.last_collection)
        if last is None:
            return True
        finished = datetime.fromisoformat(last.finished_at)
        return _now() - finished >= timedelta(hours=self._settings.trends_refresh_hours)

    async def refresh(self) -> Collection | None:
        if self.source is None or self._lock.locked():
            return None
        async with self._lock:
            started = _now()
            logger.info("인기 쇼츠 수집 시작")
            try:
                videos = await self.source.collect()
            except Exception as exc:
                self.last_error = str(exc)
                self._failed_at = _now()
                logger.warning("인기 쇼츠 수집 실패: %s", exc)
                return None
            collection = await asyncio.to_thread(self.store.replace, videos, started, _now())
            self.last_error = None
            self._failed_at = None
            logger.info("인기 쇼츠 %d개 수집 완료", len(videos))
            return collection

    async def loop(self, check_interval_sec: float = 600) -> None:
        while True:
            # 실패하면(할당량 초과 등) 한 시간 동안은 자동으로 다시 시도하지 않습니다.
            cooling = self._failed_at is not None and _now() - self._failed_at < timedelta(hours=1)
            if self.source is not None and not cooling and await self.is_stale():
                await self.refresh()
            await asyncio.sleep(check_interval_sec)
