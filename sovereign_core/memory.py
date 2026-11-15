"""Offline-first, workspace-scoped memory with provenance and revision history.

This store is deliberately independent of cloud identity and model providers.
Pass the same SQLite path used by TaskStore to share a local database.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
import re
import sqlite3
from uuid import uuid4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    workspace_id: str
    kind: str
    content: str
    source: str
    confidence: float
    verified: bool
    status: str
    revision: int
    supersedes_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class MemoryRevision:
    id: str
    memory_id: str
    revision: int
    content: str
    source: str
    confidence: float
    verified: bool
    status: str
    changed_at: str


class MemoryStore:
    """SQLite memory store. Workspace IDs are the local isolation boundary."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"

    def __init__(self, db_path: str | Path = "sovereign_workspace/sovereign_tasks.sqlite3"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _initialize(self) -> None:
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS memory_records_v2 (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
                    verified INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active', 'superseded', 'retracted')),
                    revision INTEGER NOT NULL DEFAULT 1,
                    supersedes_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(supersedes_id) REFERENCES memory_records_v2(id)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_scope_status
                    ON memory_records_v2(workspace_id, status, updated_at);
                CREATE TABLE IF NOT EXISTS memory_revisions (
                    id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    verified INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    changed_at TEXT NOT NULL,
                    UNIQUE(memory_id, revision),
                    FOREIGN KEY(memory_id) REFERENCES memory_records_v2(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_memory_revisions
                    ON memory_revisions(memory_id, revision);
            """)

    @staticmethod
    def _validate(workspace_id: str, content: str, kind: str,
                  source: str, confidence: float) -> tuple[str, str, str, str, float]:
        workspace_id = (workspace_id or "").strip()
        content = (content or "").strip()
        kind = (kind or "").strip()
        source = (source or "").strip()
        if not workspace_id:
            raise ValueError("workspace_id is required")
        if not content:
            raise ValueError("Memory content is required")
        if not kind or not source:
            raise ValueError("Memory kind and source are required")
        confidence = float(confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        return workspace_id, content, kind, source, confidence

    @staticmethod
    def _record(row: sqlite3.Row) -> MemoryRecord:
        values = dict(row)
        values["verified"] = bool(values["verified"])
        return MemoryRecord(**values)

    @staticmethod
    def _revision(row: sqlite3.Row) -> MemoryRevision:
        values = dict(row)
        values["verified"] = bool(values["verified"])
        return MemoryRevision(**values)

    @staticmethod
    def _snapshot(db: sqlite3.Connection, record: MemoryRecord, changed_at: str) -> None:
        db.execute(
            """INSERT INTO memory_revisions
               (id, memory_id, revision, content, source, confidence, verified, status, changed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid4()), record.id, record.revision, record.content, record.source,
             record.confidence, int(record.verified), record.status, changed_at),
        )

    def remember(self, workspace_id: str, content: str, kind: str = "fact",
                 source: str = "user", confidence: float = 1.0,
                 verified: bool = False) -> MemoryRecord:
        workspace_id, content, kind, source, confidence = self._validate(
            workspace_id, content, kind, source, confidence
        )
        now = _now()
        record = MemoryRecord(str(uuid4()), workspace_id, kind, content, source,
                              confidence, bool(verified), self.ACTIVE, 1, None, now, now)
        with self._connection() as db:
            db.execute(
                """INSERT INTO memory_records_v2
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (record.id, record.workspace_id, record.kind, record.content,
                 record.source, record.confidence, int(record.verified), record.status,
                 record.revision, record.supersedes_id, record.created_at, record.updated_at),
            )
            self._snapshot(db, record, now)
        return record

    def get(self, memory_id: str) -> MemoryRecord | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM memory_records_v2 WHERE id=?", (memory_id,)).fetchone()
        return self._record(row) if row else None

    def list_memories(self, workspace_id: str, include_inactive: bool = False) -> list[MemoryRecord]:
        sql = "SELECT * FROM memory_records_v2 WHERE workspace_id=?"
        params: list[object] = [workspace_id]
        if not include_inactive:
            sql += " AND status='active'"
        sql += " ORDER BY updated_at DESC, id"
        with self._connection() as db:
            rows = db.execute(sql, params).fetchall()
        return [self._record(row) for row in rows]

    def search(self, workspace_id: str, query: str, limit: int = 20,
               verified_only: bool = False) -> list[MemoryRecord]:
        if not workspace_id.strip():
            raise ValueError("workspace_id is required")
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200")
        terms = [term for term in re.findall(r"[\w'-]+", query.casefold()) if term]
        if not terms:
            return []
        clauses = " AND ".join("LOWER(content) LIKE ?" for _ in terms)
        params: list[object] = [workspace_id, *(f"%{term}%" for term in terms)]
        sql = f"""SELECT * FROM memory_records_v2
                  WHERE workspace_id=? AND status='active' AND {clauses}"""
        if verified_only:
            sql += " AND verified=1"
        sql += " ORDER BY verified DESC, confidence DESC, updated_at DESC LIMIT ?"
        params.append(limit)
        with self._connection() as db:
            rows = db.execute(sql, params).fetchall()
        return [self._record(row) for row in rows]

    def revise(self, memory_id: str, content: str, source: str | None = None,
               confidence: float | None = None, verified: bool | None = None) -> MemoryRecord:
        content = (content or "").strip()
        if not content:
            raise ValueError("Memory content is required")
        now = _now()
        with self._connection() as db:
            row = db.execute("SELECT * FROM memory_records_v2 WHERE id=?", (memory_id,)).fetchone()
            if not row:
                raise ValueError("Memory does not exist")
            current = self._record(row)
            if current.status != self.ACTIVE:
                raise ValueError("Only active memories can be revised")
            next_source = (source or current.source).strip()
            next_confidence = current.confidence if confidence is None else float(confidence)
            if not next_source or not 0 <= next_confidence <= 1:
                raise ValueError("Source is required and confidence must be between 0 and 1")
            updated = MemoryRecord(
                current.id, current.workspace_id, current.kind, content, next_source,
                next_confidence, current.verified if verified is None else bool(verified),
                self.ACTIVE, current.revision + 1, current.supersedes_id,
                current.created_at, now
            )
            db.execute(
                """UPDATE memory_records_v2 SET content=?, source=?, confidence=?,
                   verified=?, revision=?, updated_at=? WHERE id=?""",
                (updated.content, updated.source, updated.confidence, int(updated.verified),
                 updated.revision, updated.updated_at, updated.id),
            )
            self._snapshot(db, updated, now)
        return updated

    def supersede(self, memory_id: str, content: str, source: str,
                  confidence: float = 1.0, verified: bool = False) -> MemoryRecord:
        content = (content or "").strip()
        source = (source or "").strip()
        confidence = float(confidence)
        if not content or not source or not 0 <= confidence <= 1:
            raise ValueError("Content, source and valid confidence are required")
        now = _now()
        with self._connection() as db:
            row = db.execute("SELECT * FROM memory_records_v2 WHERE id=?", (memory_id,)).fetchone()
            if not row:
                raise ValueError("Memory does not exist")
            old = self._record(row)
            if old.status != self.ACTIVE:
                raise ValueError("Only active memories can be superseded")
            new = MemoryRecord(str(uuid4()), old.workspace_id, old.kind, content, source,
                               confidence, bool(verified), self.ACTIVE, 1, old.id, now, now)
            db.execute(
                """INSERT INTO memory_records_v2
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (new.id, new.workspace_id, new.kind, new.content, new.source,
                 new.confidence, int(new.verified), new.status, new.revision,
                 new.supersedes_id, new.created_at, new.updated_at),
            )
            self._snapshot(db, new, now)
            replaced = MemoryRecord(
                old.id, old.workspace_id, old.kind, old.content, old.source,
                old.confidence, old.verified, self.SUPERSEDED, old.revision + 1,
                old.supersedes_id, old.created_at, now
            )
            db.execute(
                "UPDATE memory_records_v2 SET status=?, revision=?, updated_at=? WHERE id=?",
                (replaced.status, replaced.revision, now, old.id),
            )
            self._snapshot(db, replaced, now)
        return new

    def retract(self, memory_id: str) -> MemoryRecord:
        now = _now()
        with self._connection() as db:
            row = db.execute("SELECT * FROM memory_records_v2 WHERE id=?", (memory_id,)).fetchone()
            if not row:
                raise ValueError("Memory does not exist")
            old = self._record(row)
            if old.status != self.ACTIVE:
                raise ValueError("Only active memories can be retracted")
            updated = MemoryRecord(
                old.id, old.workspace_id, old.kind, old.content, old.source,
                old.confidence, old.verified, self.RETRACTED, old.revision + 1,
                old.supersedes_id, old.created_at, now
            )
            db.execute(
                "UPDATE memory_records_v2 SET status=?, revision=?, updated_at=? WHERE id=?",
                (updated.status, updated.revision, now, old.id),
            )
            self._snapshot(db, updated, now)
        return updated

    def history(self, memory_id: str) -> list[MemoryRevision]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM memory_revisions WHERE memory_id=? ORDER BY revision",
                (memory_id,),
            ).fetchall()
        return [self._revision(row) for row in rows]

    def delete(self, memory_id: str) -> bool:
        """Permanently erase a memory and its revision history."""
        with self._connection() as db:
            cursor = db.execute("DELETE FROM memory_records_v2 WHERE id=?", (memory_id,))
        return cursor.rowcount > 0

    def export_workspace(self, workspace_id: str) -> dict:
        records = self.list_memories(workspace_id, include_inactive=True)
        with self._connection() as db:
            revisions = db.execute(
                """SELECT r.* FROM memory_revisions r
                   JOIN memory_records_v2 m ON m.id=r.memory_id
                   WHERE m.workspace_id=? ORDER BY r.memory_id, r.revision""",
                (workspace_id,),
            ).fetchall()
        return {
            "schema_version": 1,
            "workspace_id": workspace_id,
            "exported_at": _now(),
            "memories": [asdict(record) for record in records],
            "revisions": [asdict(self._revision(row)) for row in revisions],
        }

    def export_json(self, workspace_id: str) -> str:
        return json.dumps(self.export_workspace(workspace_id), ensure_ascii=False, indent=2)
