"""보관함: 완성된 영상을 프로젝트로 저장합니다 (SQLite + 파일 복사).

작업(Job)은 메모리에만 있고 출력 파일은 JOB_TTL_HOURS 뒤 지워지므로, 완성되면 영상·썸네일을
`library_dir/<project_id>/`로 복사하고 대본·설명란·제목 후보 같은 정보와 함께 기록합니다.
보관 기간(LIBRARY_TTL_DAYS)이 지나면 자동으로 지웁니다.
"""

import json
import shutil
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ID_LEN = 32

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id         TEXT PRIMARY KEY,
    created_at         TEXT NOT NULL,
    created_ts         REAL NOT NULL,
    title              TEXT NOT NULL,
    product_name       TEXT NOT NULL DEFAULT '',
    script             TEXT NOT NULL,
    tone               TEXT NOT NULL,
    description        TEXT NOT NULL DEFAULT '',
    titles             TEXT NOT NULL DEFAULT '[]',
    reference_video_id TEXT,
    duration_sec       REAL,
    scene_count        INTEGER,
    video_file         TEXT NOT NULL,
    thumbnail_files    TEXT NOT NULL DEFAULT '[]',
    image_credits      TEXT NOT NULL DEFAULT '[]',
    job_id             TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT NOT NULL,
    platform    TEXT NOT NULL,
    status      TEXT NOT NULL,          -- scheduled | uploading | done | failed
    privacy     TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    url         TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


@dataclass
class Project:
    project_id: str
    created_at: str
    title: str
    script: str
    tone: str
    video_file: str
    job_id: str
    product_name: str = ""
    description: str = ""
    titles: list[str] = field(default_factory=list)
    reference_video_id: str | None = None
    duration_sec: float | None = None
    scene_count: int | None = None
    thumbnail_files: list[str] = field(default_factory=list)
    image_credits: list[str] = field(default_factory=list)

    def files(self) -> set[str]:
        return {self.video_file, *self.thumbnail_files}


_JSON_FIELDS = ("titles", "thumbnail_files", "image_credits")

# 올린 영상의 성과 (플랫폼에서 주기적으로 받아 옴)
_EXTRA_COLUMNS = (
    ("video_id", "TEXT"),
    ("views", "INTEGER"),
    ("likes", "INTEGER"),
    ("comments", "INTEGER"),
    ("stats_at", "TEXT"),
    ("scheduled_at", "TEXT"),  # 예약 올리기 (UTC ISO). status가 scheduled인 동안만 의미 있음
)


def valid_project_id(project_id: str) -> bool:
    return len(project_id) == PROJECT_ID_LEN and all(c in "0123456789abcdef" for c in project_id)


class ProjectStore:
    """SQLite 저장소. 호출마다 연결을 새로 열어 asyncio.to_thread에서 안전하게 씁니다."""

    def __init__(self, db_path: Path, library_dir: Path) -> None:
        self.library_dir = library_dir
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        library_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(_SCHEMA)
            # 예전 버전에서 만든 DB에 성과 칼럼을 더합니다.
            existing = {r["name"] for r in db.execute("PRAGMA table_info(publications)")}
            for name, kind in _EXTRA_COLUMNS:
                if name not in existing:
                    db.execute(f"ALTER TABLE publications ADD COLUMN {name} {kind}")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._db_path)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _row(r: sqlite3.Row) -> Project:
        data = {k: r[k] for k in r.keys() if k != "created_ts"}
        for key in _JSON_FIELDS:
            data[key] = json.loads(data[key])
        return Project(**data)

    def save(self, source_dir: Path, project: Project) -> Project:
        """source_dir(작업 폴더)의 영상·썸네일을 보관함 폴더로 복사하고 기록합니다."""
        project.project_id = project.project_id or uuid.uuid4().hex
        target = self.library_dir / project.project_id
        target.mkdir(parents=True, exist_ok=True)
        try:
            for name in project.files():
                shutil.copy2(source_dir / name, target / name)
            data = asdict(project)
            for key in _JSON_FIELDS:
                data[key] = json.dumps(data[key], ensure_ascii=False)
            data["created_ts"] = time.time()
            with self._connect() as db:
                db.execute(
                    f"INSERT INTO projects ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
                    tuple(data.values()),
                )
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
        return project

    def list(self, limit: int = 100) -> list[Project]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM projects ORDER BY created_ts DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def get(self, project_id: str) -> Project | None:
        if not valid_project_id(project_id):
            return None
        with self._connect() as db:
            r = db.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,)).fetchone()
        return self._row(r) if r else None

    def delete(self, project_id: str) -> bool:
        if not valid_project_id(project_id):
            return False
        with self._connect() as db:
            deleted = db.execute("DELETE FROM projects WHERE project_id = ?", (project_id,)).rowcount
            db.execute("DELETE FROM publications WHERE project_id = ?", (project_id,))
        shutil.rmtree(self.library_dir / project_id, ignore_errors=True)
        return bool(deleted)

    # ------------------------------------------------------------------ 배포 기록
    def add_publication(
        self, project_id: str, platform: str, *, title: str, privacy: str, scheduled_at: str | None = None
    ) -> int:
        """scheduled_at(UTC ISO)이 있으면 예약, 없으면 바로 올리는 중으로 기록합니다."""
        now = now_iso()
        with self._connect() as db:
            cur = db.execute(
                "INSERT INTO publications "
                "(project_id, platform, status, privacy, title, scheduled_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (project_id, platform, "scheduled" if scheduled_at else "uploading", privacy, title,
                 scheduled_at, now, now),
            )
            return cur.lastrowid

    def claim_due(self, now: str) -> "list[dict]":
        """예약 시각이 된 것을 올리는 중으로 바꾸고 돌려줍니다 (같은 예약을 두 번 올리지 않게)."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, project_id, platform, title, privacy FROM publications "
                "WHERE status = 'scheduled' AND scheduled_at <= ? ORDER BY scheduled_at",
                (now,),
            ).fetchall()
            claimed = []
            for r in rows:
                if db.execute(
                    "UPDATE publications SET status = 'uploading', updated_at = ? "
                    "WHERE id = ? AND status = 'scheduled'",
                    (now, r["id"]),
                ).rowcount:
                    claimed.append(dict(r))
        return claimed

    def cancel_scheduled(self, project_id: str, pub_id: int) -> bool:
        with self._connect() as db:
            return bool(db.execute(
                "DELETE FROM publications WHERE id = ? AND project_id = ? AND status = 'scheduled'",
                (pub_id, project_id),
            ).rowcount)

    def finish_publication(
        self, pub_id: int, *, url: str | None = None, error: str | None = None, video_id: str | None = None
    ) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE publications SET status = ?, url = ?, error = ?, video_id = ?, updated_at = ? WHERE id = ?",
                ("failed" if error else "done", url, error, video_id, now_iso(), pub_id),
            )

    def published_videos(self, platform: str) -> "list[str]":
        """성과를 받아 올 영상 ID (올리기에 성공한 것만)."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT DISTINCT video_id FROM publications WHERE platform = ? AND status = 'done' "
                "AND video_id IS NOT NULL",
                (platform,),
            ).fetchall()
        return [r[0] for r in rows]

    def update_stats(self, platform: str, stats: dict[str, dict]) -> int:
        """stats: 영상 ID → {"views", "likes", "comments"}. 갱신한 기록 수."""
        now = now_iso()
        updated = 0
        with self._connect() as db:
            for video_id, s in stats.items():
                updated += db.execute(
                    "UPDATE publications SET views = ?, likes = ?, comments = ?, stats_at = ? "
                    "WHERE platform = ? AND video_id = ?",
                    (s.get("views"), s.get("likes"), s.get("comments"), now, platform, video_id),
                ).rowcount
        return updated

    def channel_insights(self, *, min_videos: int = 2, top: int = 3) -> "list[dict]":
        """성과가 있는 프로젝트의 첫 문장(후킹)과 조회수 — 잘된 것 top개와 덜된 것 top개.

        성과가 있는 영상이 min_videos개보다 적으면 비교할 수 없어 빈 목록입니다.
        """
        summary = self.stats_summary()["projects"]
        measured = [p for p in summary if p["stats_at"]]
        if len(measured) < min_videos:
            return []
        picked = measured[:top] + [p for p in measured[-top:] if p not in measured[:top]]
        result = []
        for item in picked:
            project = self.get(item["project_id"])
            if project is None:
                continue
            hook = next((line.strip() for line in project.script.splitlines() if line.strip()), "")
            result.append({"hook": hook[:80], "title": project.title, "views": item["views"],
                           "likes": item["likes"], "comments": item["comments"]})
        return result

    def stats_summary(self) -> dict:
        """보관함 위쪽 요약과 카드별 조회수."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT p.project_id, p.title, pub.views, pub.likes, pub.comments, pub.stats_at, pub.url "
                "FROM publications pub JOIN projects p ON p.project_id = pub.project_id "
                "WHERE pub.status = 'done'"
            ).fetchall()
        per_project: dict[str, dict] = {}
        for r in rows:
            cur = per_project.setdefault(r["project_id"], {
                "project_id": r["project_id"], "title": r["title"], "views": 0, "likes": 0, "comments": 0,
                "uploads": 0, "stats_at": None,
            })
            cur["uploads"] += 1
            for key in ("views", "likes", "comments"):
                cur[key] += r[key] or 0
            if r["stats_at"] and (cur["stats_at"] is None or r["stats_at"] > cur["stats_at"]):
                cur["stats_at"] = r["stats_at"]
        items = sorted(per_project.values(), key=lambda x: x["views"], reverse=True)
        return {
            "uploaded_videos": sum(x["uploads"] for x in items),
            "total_views": sum(x["views"] for x in items),
            "total_likes": sum(x["likes"] for x in items),
            "total_comments": sum(x["comments"] for x in items),
            "stats_at": max((x["stats_at"] for x in items if x["stats_at"]), default=None),
            "projects": items,
        }

    def publications(self, project_id: str) -> "list[dict]":  # 클래스 안에서 list는 메서드 이름
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, platform, status, privacy, title, url, error, created_at, updated_at, "
                "video_id, views, likes, comments, stats_at, scheduled_at "
                "FROM publications WHERE project_id = ? ORDER BY id DESC",
                (project_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def fail_interrupted(self) -> None:
        """서버가 업로드 도중 꺼졌던 기록을 실패로 정리합니다 (시작할 때 한 번)."""
        with self._connect() as db:
            db.execute(
                "UPDATE publications SET status = 'failed', error = ?, updated_at = ? WHERE status = 'uploading'",
                ("서버가 다시 시작돼 업로드가 중단됐어요. 다시 올려 주세요.", now_iso()),
            )

    def remove_older_than(self, seconds: float) -> int:
        cutoff = time.time() - seconds
        with self._connect() as db:
            ids = [r[0] for r in db.execute("SELECT project_id FROM projects WHERE created_ts < ?", (cutoff,))]
        for project_id in ids:
            self.delete(project_id)
        return len(ids)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
