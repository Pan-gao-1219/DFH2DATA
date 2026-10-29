"""Transactional local storage, revision history and downloadable source files."""
import hashlib
import io
import json
import os
import re
import sqlite3
import uuid
import zipfile
from contextlib import contextmanager
from datetime import date, time
from pathlib import Path

from observations import GROUPS, TASK_BY_ID, now_china

MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_SUBMISSION_BYTES = 250 * 1024 * 1024
MAX_FILES = 20


def validate_submission(observation_date, task_id, reporter, actual_time, station, notes, files, submission_id=None):
    """Shared validation before a local or remote storage write."""
    date.fromisoformat(observation_date)
    task = TASK_BY_ID.get(task_id)
    if not task or reporter not in GROUPS[task.group]["members"]:
        raise ValueError("请选择该任务负责小组的汇报人。")
    time.fromisoformat(actual_time)
    if len(station) > 120 or len(notes) > 2000:
        raise ValueError("站位最多120字，备注最多2000字。")
    if not 1 <= len(files) <= MAX_FILES:
        raise ValueError(f"每次请提交1—{MAX_FILES}个材料文件。")
    total, categories = 0, set()
    for name, content, category in files:
        if not isinstance(content, bytes) or not content:
            raise ValueError(f"{name} 是空文件，请检查后上传。")
        if len(content) > MAX_FILE_BYTES:
            raise ValueError(f"{name} 超过单个文件100MB限制。")
        if category not in {"ctd", "weather", "data"}:
            raise ValueError("材料分类无效。")
        categories.add(category)
        total += len(content)
    if total > MAX_SUBMISSION_BYTES:
        raise ValueError("本次材料总大小超过250MB，请分批整理后重新提交。")
    if task.project == "ctd" and categories != {"ctd", "weather"}:
        raise ValueError("请同时上传CTD材料和同步气象材料。")
    if task.project != "ctd" and categories != {"data"}:
        raise ValueError("材料分类与当前任务不符。")
    return str(uuid.UUID(submission_id)) if submission_id else str(uuid.uuid4())


class Store:
    storage_label = "本机磁盘保存"
    can_write = True
    repository_url = None

    def __init__(self, root=None):
        self.root = Path(root or os.environ.get("MARINE_DATA_DIR") or Path(__file__).parent / "data").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS submissions (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    observation_date TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    reporter TEXT NOT NULL,
                    actual_time TEXT NOT NULL,
                    station TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reported_at TEXT,
                    reported_by TEXT
                );
                CREATE INDEX IF NOT EXISTS submissions_date_task
                ON submissions(observation_date, task_id, sequence);
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY,
                    submission_id TEXT NOT NULL REFERENCES submissions(id),
                    name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    relative_path TEXT NOT NULL UNIQUE,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL
                );
            """)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.root / "observations.sqlite3", timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def clean_name(name):
        name = str(name).replace("\\", "/").split("/")[-1]
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
        return (name or "未命名材料")[:180]

    def _hydrate(self, conn, rows):
        result = []
        for row in rows:
            record = dict(row)
            record["files"] = [dict(f) for f in conn.execute(
                "SELECT * FROM files WHERE submission_id=? ORDER BY rowid", (row["id"],)
            )]
            result.append(record)
        return result

    def list_records(self, observation_date):
        date.fromisoformat(observation_date)
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM submissions WHERE observation_date=? ORDER BY sequence DESC", (observation_date,)).fetchall()
            return self._hydrate(conn, rows)

    def get(self, submission_id):
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchall()
            return self._hydrate(conn, rows)[0] if rows else None

    def latest(self, observation_date):
        latest = {}
        for record in self.list_records(observation_date):
            latest.setdefault(record["task_id"], record)
        return latest

    def submit(self, *, observation_date, task_id, reporter, actual_time, station="", notes="", files, submission_id=None):
        submission_id = validate_submission(observation_date, task_id, reporter, actual_time, station, notes, files, submission_id)
        written = []
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchall()
            if existing:
                row = existing[0]
                if row["task_id"] != task_id or row["observation_date"] != observation_date:
                    raise ValueError("重复提交编号与任务不符。")
                return self._hydrate(conn, existing)[0]
            try:
                conn.execute("""INSERT INTO submissions
                    (id, observation_date, task_id, reporter, actual_time, station, notes, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (submission_id, observation_date, task_id, reporter, actual_time, station.strip(), notes.strip(), now_china().isoformat(timespec="seconds")))
                folder = self.root / "uploads" / observation_date / task_id / submission_id
                folder.mkdir(parents=True, exist_ok=True)
                for name, content, category in files:
                    file_id = str(uuid.uuid4())
                    name = self.clean_name(name)
                    target = folder / file_id
                    target.write_bytes(content)
                    written.append(target)
                    conn.execute("INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (file_id, submission_id, name, category, target.relative_to(self.root).as_posix(), len(content), hashlib.sha256(content).hexdigest()))
            except Exception:
                # Only remove files created by this failed submission; never existing data.
                for target in written:
                    target.unlink(missing_ok=True)
                raise
        return self.get(submission_id)

    def mark_reported(self, submission_id, reporter):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
            if not row:
                raise ValueError("找不到这条提交。")
            task = TASK_BY_ID[row["task_id"]]
            if reporter not in GROUPS[task.group]["members"]:
                raise ValueError("请选择负责小组的汇报确认人。")
            newest = conn.execute("SELECT id FROM submissions WHERE observation_date=? AND task_id=? ORDER BY sequence DESC LIMIT 1", (row["observation_date"], row["task_id"])).fetchone()
            if newest["id"] != submission_id:
                raise ValueError("已有更新的材料，请刷新后确认最新一条的群汇报。")
            conn.execute("UPDATE submissions SET reported_at=?, reported_by=? WHERE id=? AND reported_at IS NULL",
                (now_china().isoformat(timespec="seconds"), reporter, submission_id))

    def read_file(self, file):
        path = (self.root / file["relative_path"]).resolve()
        if not path.is_relative_to(self.root / "uploads"):
            raise ValueError("材料路径无效。")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != file["sha256"]:
            raise ValueError("材料校验未通过，请联系数据管理员检查存储。")
        return content

    def bundle(self, submission_id):
        record = self.get(submission_id)
        if not record:
            raise ValueError("找不到这条提交。")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("提交记录.json", json.dumps(record, ensure_ascii=False, indent=2))
            for i, file in enumerate(record["files"], 1):
                archive.writestr(f"{file['category']}/{i:02d}_{file['name']}", self.read_file(file))
        return buffer.getvalue()
