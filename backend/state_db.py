"""
SQLite 处理记录库 —— 已处理 EPUB 的去重注册表

设计目标:
1. 零文件操作: WATCH_DIR 里的 EPUB 一律不移动、不加标记
2. 内容指纹去重: 用 sha256 判重,改一个字节也算新文件
3. 幂等: 同一文件重复投放,直接跳过,不再调 LLM
4. 可追溯: 记录 processed_at + issue_id,支持 --status 审计
"""

import hashlib
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_epubs (
    sha256        TEXT PRIMARY KEY,        -- EPUB 内容指纹
    filename      TEXT NOT NULL,           -- 投放时的文件名
    size          INTEGER NOT NULL,        -- 字节数 (辅助校验)
    processed_at  REAL NOT NULL,           -- Unix 时间戳
    issue_id      TEXT NOT NULL            -- 关联的 issue_id (issue_2026-07-11)
);
CREATE INDEX IF NOT EXISTS idx_issue_id ON processed_epubs(issue_id);
"""

# 从文件名 (TheEconomist.2026.07.11.epub) 推断 issue_id
_DATE_RE = re.compile(r"(\d{4})[-.](\d{2})[-.](\d{2})")


class StateDB:
    """SQLite 处理记录库薄包装"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")  # 允许多读单写,避免锁竞争
        return conn

    def _init_schema(self):
        with self._conn() as c:
            c.executescript(SCHEMA)
            c.commit()

    # ---------- 查 ----------

    def is_processed(self, sha256: str) -> bool:
        """该 sha256 是否已处理过"""
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM processed_epubs WHERE sha256 = ?",
                (sha256,),
            ).fetchone()
            return row is not None

    def get_by_sha(self, sha256: str) -> Optional[dict]:
        with self._conn() as c:
            cur = c.execute(
                "SELECT * FROM processed_epubs WHERE sha256 = ?",
                (sha256,),
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))

    def count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM processed_epubs").fetchone()[0]

    def list_all(self) -> list[dict]:
        """返回所有处理记录 (按时间倒序)"""
        with self._conn() as c:
            cur = c.execute(
                "SELECT * FROM processed_epubs ORDER BY processed_at DESC"
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in rows]

    # ---------- 写 ----------

    def mark_processed(self, sha256: str, filename: str, size: int,
                       issue_id: str, processed_at: Optional[float] = None):
        """标记某 sha256 已处理"""
        ts = processed_at if processed_at is not None else time.time()
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO processed_epubs
                   (sha256, filename, size, processed_at, issue_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (sha256, filename, size, ts, issue_id),
            )
            c.commit()

    def remove_by_issue(self, issue_id: str) -> int:
        """按 issue_id 删除记录 (供 --reprocess 使用)"""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM processed_epubs WHERE issue_id = ?",
                (issue_id,),
            )
            c.commit()
            return cur.rowcount

    def reset(self):
        """清空整张表 (供 --reset-db 使用)"""
        with self._conn() as c:
            c.execute("DELETE FROM processed_epubs")
            c.commit()

    # ---------- 迁移 ----------

    def import_from_archived(self, archived_dir: Path) -> int:
        """从 archived/*.epub 迁移历史记录 (一次性, 启动时自动跑)

        - 跳过 sha256 已存在的 (幂等)
        - issue_id 从文件名推断
        - processed_at 用文件 mtime,这样状态显示更接近历史

        Returns: 新增记录数
        """
        if not archived_dir.exists():
            return 0
        imported = 0
        for epub in sorted(archived_dir.glob("*.epub")):
            sha = compute_sha256(epub)
            if self.is_processed(sha):
                continue
            m = _DATE_RE.search(epub.stem)
            issue_id = (
                f"issue_{m.group(1)}-{m.group(2)}-{m.group(3)}"
                if m else "unknown"
            )
            self.mark_processed(
                sha256=sha,
                filename=epub.name,
                size=epub.stat().st_size,
                issue_id=issue_id,
                processed_at=epub.stat().st_mtime,
            )
            imported += 1
        return imported


# ---------- 工具 ----------

def compute_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """流式计算 SHA256 (1MB chunk, 7MB 文件 < 100ms)"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def infer_issue_id_from_filename(filename: str) -> str:
    """从文件名推断 issue_id, 失败返回 'unknown'"""
    m = _DATE_RE.search(filename)
    if m:
        return f"issue_{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return "unknown"