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
        shutil.rmtree(self.library_dir / project_id, ignore_errors=True)
        return bool(deleted)

    def remove_older_than(self, seconds: float) -> int:
        cutoff = time.time() - seconds
        with self._connect() as db:
            ids = [r[0] for r in db.execute("SELECT project_id FROM projects WHERE created_ts < ?", (cutoff,))]
        for project_id in ids:
            self.delete(project_id)
        return len(ids)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
