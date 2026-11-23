"""SQLite persistence for workspaces, problems, steps, tool runs and verification."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from uuid import uuid4
from datetime import datetime, timezone

from .models import Workspace, Problem, PlanStep, PermissionLevel, StepStatus


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    def __init__(self, db_path: str | Path = "sovereign_workspace/sovereign_tasks.sqlite3"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                    purpose TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS problems (
                    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
                    title TEXT NOT NULL, description TEXT NOT NULL,
                    status TEXT NOT NULL, priority TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
                );
                CREATE INDEX IF NOT EXISTS idx_problems_workspace ON problems(workspace_id, updated_at);
                CREATE TABLE IF NOT EXISTS plan_steps (
                    id TEXT PRIMARY KEY, problem_id TEXT NOT NULL,
                    step_number INTEGER NOT NULL, action TEXT NOT NULL,
                    permission_level TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(problem_id, step_number),
                    FOREIGN KEY(problem_id) REFERENCES problems(id)
                );
                CREATE TABLE IF NOT EXISTS tool_runs (
                    id TEXT PRIMARY KEY, step_id TEXT NOT NULL, tool_name TEXT NOT NULL,
                    input_summary TEXT NOT NULL, output_summary TEXT NOT NULL,
                    outcome TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
                    FOREIGN KEY(step_id) REFERENCES plan_steps(id)
                );
                CREATE TABLE IF NOT EXISTS verifications (
                    id TEXT PRIMARY KEY, step_id TEXT NOT NULL, criterion TEXT NOT NULL,
                    method TEXT NOT NULL, passed INTEGER NOT NULL,
                    evidence_reference TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(step_id) REFERENCES plan_steps(id)
                );
                CREATE TABLE IF NOT EXISTS approval_events (
                    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
                    problem_id TEXT NOT NULL, step_id TEXT NOT NULL,
                    decision TEXT NOT NULL, note TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
                    FOREIGN KEY(problem_id) REFERENCES problems(id),
                    FOREIGN KEY(step_id) REFERENCES plan_steps(id)
                );
                CREATE INDEX IF NOT EXISTS idx_approval_events_workspace
                    ON approval_events(workspace_id, created_at);
                CREATE TABLE IF NOT EXISTS agent_step_specs (
                    step_id TEXT PRIMARY KEY, tool_name TEXT NOT NULL,
                    input_json TEXT NOT NULL, verification_criterion TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(step_id) REFERENCES plan_steps(id)
                );
                CREATE TABLE IF NOT EXISTS memory_records (
                    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, kind TEXT NOT NULL,
                    content TEXT NOT NULL, source TEXT NOT NULL, confidence REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
                );
            """)
            self._migrate_plugin_registry(db)
            self._migrate_agent_collaboration(db)
            self._migrate_agent_assignments(db)
            self._migrate_agent_collaboration_recovery(db)

    def _migrate_plugin_registry(self, db) -> None:
        """Add the versioned plugin registry without rewriting existing records."""
        db.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS plugins (
                plugin_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                version TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                source_type TEXT NOT NULL
                    CHECK(source_type IN ('builtin','local','git','cloud','external')),
                source_uri TEXT,
                install_path TEXT,
                manifest_hash TEXT,
                signature TEXT,
                status TEXT NOT NULL DEFAULT 'registered'
                    CHECK(status IN ('registered','enabled','disabled','error','uninstalled')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS plugin_capabilities (
                plugin_id TEXT NOT NULL,
                capability_id TEXT NOT NULL,
                version TEXT NOT NULL,
                contract_json TEXT NOT NULL,
                execution_target TEXT NOT NULL
                    CHECK(execution_target IN ('local','cloud','either')),
                risk_level TEXT NOT NULL
                    CHECK(risk_level IN ('low','medium','high','critical')),
                enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(plugin_id, capability_id),
                FOREIGN KEY(plugin_id) REFERENCES plugins(plugin_id)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS workspace_metadata (
                workspace_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL
                    CHECK(kind IN ('core','plugin','user','placeholder','legacy')),
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active','disabled','archived')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS workspace_plugins (
                workspace_id TEXT NOT NULL,
                plugin_id TEXT NOT NULL,
                installed_version TEXT NOT NULL,
                config_json TEXT NOT NULL DEFAULT '{}',
                permissions_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'disabled'
                    CHECK(status IN ('enabled','disabled','error')),
                installed_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(workspace_id, plugin_id),
                FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
                FOREIGN KEY(plugin_id) REFERENCES plugins(plugin_id)
            )
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_workspace_metadata_status
            ON workspace_metadata(status, kind)
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_workspace_plugins_plugin
            ON workspace_plugins(plugin_id, status)
        """)

        version = db.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]

        if version < 1:
            now = _now()
            existing = db.execute(
                "SELECT id, name FROM workspaces"
            ).fetchall()

            for workspace_id, name in existing:
                if name in ("General", "SovereignOS"):
                    kind = "core"
                elif name in ("Nikolai Creative Studio", "Yard Truths"):
                    kind = "placeholder"
                else:
                    kind = "legacy"

                db.execute("""
                    INSERT OR IGNORE INTO workspace_metadata
                    (workspace_id, kind, status, created_at, updated_at)
                    VALUES (?, ?, 'active', ?, ?)
                """, (workspace_id, kind, now, now))

            db.execute("""
                INSERT INTO schema_migrations(version, name, applied_at)
                VALUES (1, 'plugin_registry_foundation', ?)
            """, (now,))

    def _migrate_agent_collaboration(self, db) -> None:
        """Add persistent, workspace-isolated agent identities and discussions."""
        db.executescript("""
            CREATE TABLE IF NOT EXISTS agent_identities (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                role TEXT NOT NULL,
                name TEXT NOT NULL,
                mission TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                runtime TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(id, workspace_id),
                UNIQUE(workspace_id, role, name),
                FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
            );

            CREATE INDEX IF NOT EXISTS idx_agent_identities_workspace
                ON agent_identities(workspace_id, role, name);

            CREATE TABLE IF NOT EXISTS agent_discussions (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK(status IN ('open','completed','cancelled')),
                leader_agent_id TEXT,
                conversation_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(id, workspace_id),
                FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
                FOREIGN KEY(leader_agent_id, workspace_id)
                    REFERENCES agent_identities(id, workspace_id)
            );

            CREATE INDEX IF NOT EXISTS idx_agent_discussions_workspace
                ON agent_discussions(workspace_id, updated_at);

            CREATE TABLE IF NOT EXISTS agent_messages (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                discussion_id TEXT NOT NULL,
                agent_id TEXT,
                speaker_type TEXT NOT NULL
                    CHECK(speaker_type IN ('user','agent','leader','system')),
                sequence INTEGER NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(discussion_id, sequence),
                FOREIGN KEY(discussion_id, workspace_id)
                    REFERENCES agent_discussions(id, workspace_id),
                FOREIGN KEY(agent_id, workspace_id)
                    REFERENCES agent_identities(id, workspace_id)
            );

            CREATE INDEX IF NOT EXISTS idx_agent_messages_discussion
                ON agent_messages(workspace_id, discussion_id, sequence);
        """)

        version = db.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
        if version < 2:
            db.execute("""
                INSERT INTO schema_migrations(version, name, applied_at)
                VALUES (2, 'workspace_scoped_agent_collaboration', ?)
            """, (_now(),))

    def _migrate_agent_assignments(self, db) -> None:
        """Add persistent, workspace-isolated agent task assignments."""
        db.executescript("""
            CREATE TABLE IF NOT EXISTS agent_assignments (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                discussion_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                task TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'assigned'
                    CHECK(status IN (
                        'assigned','in_progress','completed','blocked','failed'
                    )),
                result TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(discussion_id, workspace_id)
                    REFERENCES agent_discussions(id, workspace_id),
                FOREIGN KEY(agent_id, workspace_id)
                    REFERENCES agent_identities(id, workspace_id)
            );

            CREATE INDEX IF NOT EXISTS idx_agent_assignments_discussion
                ON agent_assignments(workspace_id, discussion_id, created_at);

            CREATE INDEX IF NOT EXISTS idx_agent_assignments_agent
                ON agent_assignments(workspace_id, agent_id, status);
        """)

        version = db.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
        if version < 3:
            db.execute("""
                INSERT INTO schema_migrations(version, name, applied_at)
                VALUES (3, 'workspace_scoped_agent_assignments', ?)
            """, (_now(),))


    def _migrate_agent_collaboration_recovery(self, db) -> None:
        """Persist collaboration runs and per-agent stage checkpoints (schema v4)."""
        db.executescript("""
            CREATE TABLE IF NOT EXISTS agent_collaboration_runs (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                discussion_id TEXT NOT NULL,
                task TEXT NOT NULL,
                conversation_id TEXT,
                memory_context TEXT NOT NULL DEFAULT '',
                specialist_ids_json TEXT NOT NULL DEFAULT '[]',
                max_agents INTEGER NOT NULL DEFAULT 4 CHECK(max_agents BETWEEN 1 AND 4),
                status TEXT NOT NULL DEFAULT 'queued'
                    CHECK(status IN ('queued','running','retryable','completed','failed','cancelled')),
                current_stage TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(id, workspace_id),
                FOREIGN KEY(discussion_id, workspace_id)
                    REFERENCES agent_discussions(id, workspace_id)
            );

            CREATE INDEX IF NOT EXISTS idx_agent_collaboration_runs_workspace
                ON agent_collaboration_runs(workspace_id, updated_at);

            CREATE INDEX IF NOT EXISTS idx_agent_collaboration_runs_status
                ON agent_collaboration_runs(status, updated_at);

            CREATE TABLE IF NOT EXISTS agent_collaboration_checkpoints (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                discussion_id TEXT NOT NULL,
                stage TEXT NOT NULL
                    CHECK(stage IN ('proposals','reviews','responses','leader_synthesis')),
                agent_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','in_progress','completed','failed')),
                result TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                message_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(run_id, stage, agent_id),
                FOREIGN KEY(run_id, workspace_id)
                    REFERENCES agent_collaboration_runs(id, workspace_id),
                FOREIGN KEY(discussion_id, workspace_id)
                    REFERENCES agent_discussions(id, workspace_id),
                FOREIGN KEY(agent_id, workspace_id)
                    REFERENCES agent_identities(id, workspace_id)
            );

            CREATE INDEX IF NOT EXISTS idx_agent_collaboration_checkpoints_run
                ON agent_collaboration_checkpoints(workspace_id, run_id, stage);
        """)

        version = db.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
        if version < 4:
            db.execute("""
                INSERT INTO schema_migrations(version, name, applied_at)
                VALUES (4, 'durable_agent_collaboration_recovery', ?)
            """, (_now(),))

        version = db.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
        if version < 5:
            columns = {
                row[1] for row in db.execute(
                    "PRAGMA table_info(agent_collaboration_runs)"
                ).fetchall()
            }
            if "request_key" not in columns:
                db.execute(
                    "ALTER TABLE agent_collaboration_runs "
                    "ADD COLUMN request_key TEXT NOT NULL DEFAULT ''"
                )
            db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                idx_agent_collaboration_runs_request_key
                ON agent_collaboration_runs(workspace_id, request_key)
                WHERE request_key <> ''
            """)
            db.execute("""
                INSERT INTO schema_migrations(version, name, applied_at)
                VALUES (5, 'idempotent_agent_collaboration_requests', ?)
            """, (_now(),))

    def create_agent_collaboration_run(
        self, workspace_id: str, discussion_id: str, task: str,
        conversation_id: str | None = None, memory_context: str = "",
        specialist_ids: list[str] | None = None, max_agents: int = 4,
        request_key: str = ""
    ) -> dict:
        import json

        task = (task or "").strip()
        if not task:
            raise ValueError("Collaboration task is required")
        if not 1 <= int(max_agents) <= 4:
            raise ValueError("max_agents must be between 1 and 4")
        request_key = (request_key or "").strip()
        if len(request_key) > 200:
            raise ValueError("Idempotency key is too long")
        now, run_id = _now(), str(uuid4())
        selected_json = json.dumps(specialist_ids or [])
        with self._connection() as db:
            if request_key:
                existing = db.execute("""
                    SELECT * FROM agent_collaboration_runs
                    WHERE workspace_id=? AND request_key=?
                """, (workspace_id, request_key)).fetchone()
                if existing:
                    if (existing["task"] != task
                        or existing["conversation_id"] != conversation_id
                        or existing["specialist_ids_json"] != selected_json
                        or existing["max_agents"] != int(max_agents)):
                        raise ValueError(
                            "Idempotency key was already used for a different mission"
                        )
                    return dict(existing)
            discussion = db.execute("""
                SELECT id, status, conversation_id FROM agent_discussions
                WHERE id=? AND workspace_id=?
            """, (discussion_id, workspace_id)).fetchone()
            if not discussion:
                raise ValueError("Discussion not found in workspace")
            if discussion["status"] != "open":
                raise ValueError("Discussion is not open")
            if conversation_id and discussion["conversation_id"] not in (None, conversation_id):
                raise ValueError("Conversation does not match the discussion")
            db.execute("""
                INSERT OR IGNORE INTO agent_collaboration_runs (
                    id, workspace_id, discussion_id, task, conversation_id,
                    memory_context, specialist_ids_json, max_agents, status,
                    current_stage, attempts, last_error, created_at, updated_at,
                    request_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', 'queued', 0, '', ?, ?, ?)
            """, (
                run_id, workspace_id, discussion_id, task, conversation_id,
                memory_context or "", selected_json, int(max_agents), now, now,
                request_key
            ))
            row = db.execute("""
                SELECT * FROM agent_collaboration_runs
                WHERE workspace_id=? AND (
                    id=? OR (? <> '' AND request_key=?)
                )
                ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END LIMIT 1
            """, (workspace_id, run_id, request_key, request_key, run_id)).fetchone()
            if not row:
                raise RuntimeError("Could not persist collaboration run")
            if row["id"] != run_id and (
                row["task"] != task
                or row["conversation_id"] != conversation_id
                or row["specialist_ids_json"] != selected_json
                or row["max_agents"] != int(max_agents)
            ):
                raise ValueError(
                    "Idempotency key was already used for a different mission"
                )
            return dict(row)

    def create_agent_collaboration_run_with_discussion(
        self, workspace_id: str, leader_agent_id: str, task: str,
        conversation_id: str | None = None, memory_context: str = "",
        specialist_ids: list[str] | None = None, max_agents: int = 4,
        request_key: str = ""
    ) -> dict:
        """Atomically create one discussion and its idempotent collaboration run."""
        import json

        task = (task or "").strip()
        request_key = (request_key or "").strip()
        if not task:
            raise ValueError("Collaboration task is required")
        if not request_key or len(request_key) > 200:
            raise ValueError("A valid idempotency key is required")
        if not 1 <= int(max_agents) <= 4:
            raise ValueError("max_agents must be between 1 and 4")
        selected_json = json.dumps(specialist_ids or [])
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("""
                SELECT * FROM agent_collaboration_runs
                WHERE workspace_id=? AND request_key=?
            """, (workspace_id, request_key)).fetchone()
            if existing:
                if (existing["task"] != task
                    or existing["conversation_id"] != conversation_id
                    or existing["specialist_ids_json"] != selected_json
                    or existing["max_agents"] != int(max_agents)):
                    raise ValueError(
                        "Idempotency key was already used for a different mission"
                    )
                return dict(existing)
            leader = db.execute("""
                SELECT id FROM agent_identities
                WHERE id=? AND workspace_id=?
            """, (leader_agent_id, workspace_id)).fetchone()
            if not leader:
                raise ValueError("Leader agent does not belong to this workspace")
            now = _now()
            discussion_id, run_id = str(uuid4()), str(uuid4())
            db.execute("""
                INSERT INTO agent_discussions (
                    id, workspace_id, title, status, leader_agent_id,
                    conversation_id, created_at, updated_at
                ) VALUES (?, ?, ?, 'open', ?, ?, ?, ?)
            """, (
                discussion_id, workspace_id, task[:240], leader_agent_id,
                conversation_id, now, now,
            ))
            db.execute("""
                INSERT INTO agent_collaboration_runs (
                    id, workspace_id, discussion_id, task, conversation_id,
                    memory_context, specialist_ids_json, max_agents, status,
                    current_stage, attempts, last_error, created_at, updated_at,
                    request_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', 'queued', 0, '', ?, ?, ?)
            """, (
                run_id, workspace_id, discussion_id, task, conversation_id,
                memory_context or "", selected_json, int(max_agents),
                now, now, request_key,
            ))
            row = db.execute("""
                SELECT * FROM agent_collaboration_runs
                WHERE id=? AND workspace_id=?
            """, (run_id, workspace_id)).fetchone()
            return dict(row)

    def claim_agent_collaboration_run(
        self, workspace_id: str, run_id: str
    ) -> bool:
        """Atomically claim a queued run so concurrent workers cannot duplicate it."""
        with self._connection() as db:
            cursor = db.execute("""
                UPDATE agent_collaboration_runs
                SET status='running', updated_at=?
                WHERE id=? AND workspace_id=?
                  AND status IN ('queued', 'retryable')
            """, (_now(), run_id, workspace_id))
            return cursor.rowcount == 1

    def get_agent_collaboration_run_by_request_key(
        self, workspace_id: str, request_key: str
    ) -> dict | None:
        import json

        with self._connection() as db:
            row = db.execute("""
                SELECT * FROM agent_collaboration_runs
                WHERE workspace_id=? AND request_key=?
            """, (workspace_id, (request_key or "").strip())).fetchone()
        if not row:
            return None
        result = dict(row)
        result["specialist_ids"] = json.loads(result.pop("specialist_ids_json"))
        return result

    def get_agent_collaboration_run(
        self, workspace_id: str, run_id: str
    ) -> dict | None:
        import json

        with self._connection() as db:
            row = db.execute("""
                SELECT * FROM agent_collaboration_runs
                WHERE id=? AND workspace_id=?
            """, (run_id, workspace_id)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["specialist_ids"] = json.loads(result.pop("specialist_ids_json"))
        return result

    def list_agent_collaboration_runs(
        self, workspace_id: str, limit: int = 50
    ) -> list[dict]:
        import json

        with self._connection() as db:
            rows = db.execute("""
                SELECT * FROM agent_collaboration_runs
                WHERE workspace_id=? ORDER BY updated_at DESC LIMIT ?
            """, (workspace_id, max(1, min(int(limit), 200)))).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["specialist_ids"] = json.loads(item.pop("specialist_ids_json"))
            results.append(item)
        return results

    def update_agent_collaboration_run(
        self, workspace_id: str, run_id: str, status: str | None = None,
        current_stage: str | None = None, last_error: str | None = None,
        increment_attempts: bool = False
    ) -> dict:
        allowed = {"queued", "running", "retryable", "completed", "failed", "cancelled"}
        if status is not None and status not in allowed:
            raise ValueError("Invalid collaboration run status")
        with self._connection() as db:
            row = db.execute("""
                SELECT * FROM agent_collaboration_runs
                WHERE id=? AND workspace_id=?
            """, (run_id, workspace_id)).fetchone()
            if not row:
                raise ValueError("Collaboration run not found in workspace")
            if row["status"] in {"completed", "cancelled"} and status not in (None, row["status"]):
                raise ValueError(f"Cannot change terminal run status: {row['status']}")
            db.execute("""
                UPDATE agent_collaboration_runs
                SET status=COALESCE(?, status),
                    current_stage=COALESCE(?, current_stage),
                    last_error=COALESCE(?, last_error),
                    attempts=attempts + ?,
                    updated_at=?
                WHERE id=? AND workspace_id=?
            """, (
                status, current_stage, last_error,
                1 if increment_attempts else 0, _now(), run_id, workspace_id
            ))
            updated = db.execute("""
                SELECT * FROM agent_collaboration_runs
                WHERE id=? AND workspace_id=?
            """, (run_id, workspace_id)).fetchone()
            result = dict(updated)
            import json
            result["specialist_ids"] = json.loads(result.pop("specialist_ids_json"))
            return result

    def create_agent_collaboration_checkpoint(
        self, workspace_id: str, run_id: str, discussion_id: str,
        stage: str, agent_id: str
    ) -> dict:
        stages = {"proposals", "reviews", "responses", "leader_synthesis"}
        if stage not in stages:
            raise ValueError("Invalid collaboration checkpoint stage")
        now, checkpoint_id = _now(), str(uuid4())
        with self._connection() as db:
            run = db.execute("""
                SELECT discussion_id FROM agent_collaboration_runs
                WHERE id=? AND workspace_id=?
            """, (run_id, workspace_id)).fetchone()
            if not run or run["discussion_id"] != discussion_id:
                raise ValueError("Collaboration run not found in workspace")
            db.execute("""
                INSERT OR IGNORE INTO agent_collaboration_checkpoints (
                    id, run_id, workspace_id, discussion_id, stage, agent_id,
                    status, result, provider, model, attempts, last_error,
                    message_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', '', '', '', 0, '', NULL, ?, ?)
            """, (
                checkpoint_id, run_id, workspace_id, discussion_id,
                stage, agent_id, now, now
            ))
            row = db.execute("""
                SELECT * FROM agent_collaboration_checkpoints
                WHERE run_id=? AND stage=? AND agent_id=? AND workspace_id=?
            """, (run_id, stage, agent_id, workspace_id)).fetchone()
            return dict(row)

    def list_agent_collaboration_checkpoints(
        self, workspace_id: str, run_id: str
    ) -> list[dict]:
        with self._connection() as db:
            if not db.execute("""
                SELECT 1 FROM agent_collaboration_runs
                WHERE id=? AND workspace_id=?
            """, (run_id, workspace_id)).fetchone():
                raise ValueError("Collaboration run not found in workspace")
            rows = db.execute("""
                SELECT * FROM agent_collaboration_checkpoints
                WHERE run_id=? AND workspace_id=?
                ORDER BY CASE stage
                    WHEN 'proposals' THEN 1 WHEN 'reviews' THEN 2
                    WHEN 'responses' THEN 3 ELSE 4 END, created_at, id
            """, (run_id, workspace_id)).fetchall()
            return [dict(row) for row in rows]

    def update_agent_collaboration_checkpoint(
        self, workspace_id: str, checkpoint_id: str, status: str,
        result: str | None = None, provider: str | None = None,
        model: str | None = None, last_error: str | None = None,
        increment_attempts: bool = False
    ) -> dict:
        if status not in {"pending", "in_progress", "completed", "failed"}:
            raise ValueError("Invalid collaboration checkpoint status")
        with self._connection() as db:
            row = db.execute("""
                SELECT * FROM agent_collaboration_checkpoints
                WHERE id=? AND workspace_id=?
            """, (checkpoint_id, workspace_id)).fetchone()
            if not row:
                raise ValueError("Collaboration checkpoint not found in workspace")
            if row["status"] == "completed" and status != "completed":
                raise ValueError("Completed checkpoints cannot be reset")
            db.execute("""
                UPDATE agent_collaboration_checkpoints
                SET status=?, result=COALESCE(?, result),
                    provider=COALESCE(?, provider), model=COALESCE(?, model),
                    last_error=COALESCE(?, last_error),
                    attempts=attempts + ?, updated_at=?
                WHERE id=? AND workspace_id=?
            """, (
                status, result, provider, model, last_error,
                1 if increment_attempts else 0, _now(), checkpoint_id, workspace_id
            ))
            updated = db.execute("""
                SELECT * FROM agent_collaboration_checkpoints
                WHERE id=? AND workspace_id=?
            """, (checkpoint_id, workspace_id)).fetchone()
            return dict(updated)

    def complete_agent_collaboration_checkpoint(
        self, workspace_id: str, checkpoint_id: str, result: str,
        speaker_type: str, agent_id: str, provider: str = "", model: str = ""
    ) -> dict:
        """Atomically persist a completed stage result and its discussion message."""
        if speaker_type not in {"agent", "leader"}:
            raise ValueError("Checkpoint messages must be agent or leader messages")
        if not (result or "").strip():
            raise ValueError("Checkpoint result is required")
        now = _now()
        with self._connection() as db:
            checkpoint = db.execute("""
                SELECT * FROM agent_collaboration_checkpoints
                WHERE id=? AND workspace_id=?
            """, (checkpoint_id, workspace_id)).fetchone()
            if not checkpoint:
                raise ValueError("Collaboration checkpoint not found in workspace")
            if checkpoint["agent_id"] != agent_id:
                raise ValueError("Checkpoint agent does not match")
            if checkpoint["status"] == "completed":
                existing = db.execute("""
                    SELECT * FROM agent_messages
                    WHERE id=? AND workspace_id=?
                """, (checkpoint["message_id"], workspace_id)).fetchone()
                return {
                    "checkpoint": dict(checkpoint),
                    "message": dict(existing) if existing else None,
                    "already_completed": True,
                }
            sequence = db.execute("""
                SELECT COALESCE(MAX(sequence), 0) + 1 FROM agent_messages
                WHERE workspace_id=? AND discussion_id=?
            """, (workspace_id, checkpoint["discussion_id"])).fetchone()[0]
            message_id = str(uuid4())
            db.execute("""
                INSERT INTO agent_messages
                (id, workspace_id, discussion_id, agent_id, speaker_type,
                 sequence, content, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                message_id, workspace_id, checkpoint["discussion_id"],
                agent_id, speaker_type, sequence, result.strip(), now
            ))
            db.execute("""
                UPDATE agent_discussions SET updated_at=?
                WHERE id=? AND workspace_id=?
            """, (now, checkpoint["discussion_id"], workspace_id))
            db.execute("""
                UPDATE agent_collaboration_checkpoints
                SET status='completed', result=?, provider=?, model=?,
                    message_id=?, last_error='', updated_at=?
                WHERE id=? AND workspace_id=?
            """, (
                result.strip(), provider or "", model or "", message_id,
                now, checkpoint_id, workspace_id
            ))
            updated = db.execute("""
                SELECT * FROM agent_collaboration_checkpoints
                WHERE id=? AND workspace_id=?
            """, (checkpoint_id, workspace_id)).fetchone()
            message = db.execute("""
                SELECT * FROM agent_messages WHERE id=? AND workspace_id=?
            """, (message_id, workspace_id)).fetchone()
            return {
                "checkpoint": dict(updated),
                "message": dict(message),
                "already_completed": False,
            }

    def create_agent_assignment(
        self, workspace_id: str, discussion_id: str,
        agent_id: str, task: str
    ) -> dict:
        task = task.strip()
        if not task:
            raise ValueError("Assignment task is required")

        with self._connection() as db:
            discussion = db.execute("""
                SELECT status FROM agent_discussions
                WHERE id = ? AND workspace_id = ?
            """, (discussion_id, workspace_id)).fetchone()
            if not discussion:
                raise ValueError("Discussion not found in workspace")
            if discussion["status"] != "open":
                raise ValueError("Discussion is not open")

            agent = db.execute("""
                SELECT id FROM agent_identities
                WHERE id = ? AND workspace_id = ?
            """, (agent_id, workspace_id)).fetchone()
            if not agent:
                raise ValueError("Agent not found in workspace")

            now, assignment_id = _now(), str(uuid4())
            db.execute("""
                INSERT INTO agent_assignments (
                    id, workspace_id, discussion_id, agent_id,
                    task, status, result, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'assigned', '', ?, ?)
            """, (
                assignment_id, workspace_id, discussion_id,
                agent_id, task, now, now
            ))
            row = db.execute("""
                SELECT * FROM agent_assignments
                WHERE id = ? AND workspace_id = ?
            """, (assignment_id, workspace_id)).fetchone()
            return dict(row)

    def update_agent_assignment(
        self, workspace_id: str, assignment_id: str,
        status: str, result: str = ""
    ) -> dict:
        transitions = {
            "assigned": {"in_progress", "blocked", "failed"},
            "in_progress": {"completed", "blocked", "failed"},
            "blocked": {"in_progress", "failed"},
            "failed": {"in_progress"},
            "completed": set(),
        }
        if status not in transitions:
            raise ValueError("Invalid assignment status")

        with self._connection() as db:
            row = db.execute("""
                SELECT * FROM agent_assignments
                WHERE id = ? AND workspace_id = ?
            """, (assignment_id, workspace_id)).fetchone()
            if not row:
                raise ValueError("Assignment not found in workspace")

            current = row["status"]
            if status != current and status not in transitions[current]:
                raise ValueError(
                    f"Invalid assignment transition: {current} -> {status}"
                )

            db.execute("""
                UPDATE agent_assignments
                SET status = ?, result = ?, updated_at = ?
                WHERE id = ? AND workspace_id = ?
            """, (
                status, result, _now(), assignment_id, workspace_id
            ))
            updated = db.execute("""
                SELECT * FROM agent_assignments
                WHERE id = ? AND workspace_id = ?
            """, (assignment_id, workspace_id)).fetchone()
            return dict(updated)

    def list_agent_assignments(
        self, workspace_id: str, discussion_id: str
    ) -> list[dict]:
        with self._connection() as db:
            discussion = db.execute("""
                SELECT id FROM agent_discussions
                WHERE id = ? AND workspace_id = ?
            """, (discussion_id, workspace_id)).fetchone()
            if not discussion:
                raise ValueError("Discussion not found in workspace")

            rows = db.execute("""
                SELECT * FROM agent_assignments
                WHERE workspace_id = ? AND discussion_id = ?
                ORDER BY created_at, id
            """, (workspace_id, discussion_id)).fetchall()
            return [dict(row) for row in rows]

    def create_agent_identity(
        self, workspace_id: str, role: str, name: str, mission: str = "",
        provider: str = "", model: str = "", runtime: str = ""
    ) -> dict:
        if not self.get_workspace(workspace_id):
            raise ValueError("Workspace not found")
        role, name = role.strip(), name.strip()
        if not role or not name:
            raise ValueError("Agent role and name are required")
        now, identity_id = _now(), str(uuid4())
        with self._connection() as db:
            db.execute("""
                INSERT INTO agent_identities
                (id, workspace_id, role, name, mission, provider, model,
                 runtime, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (identity_id, workspace_id, role, name, mission.strip(),
                  provider.strip(), model.strip(), runtime.strip(), now, now))
            row = db.execute(
                "SELECT * FROM agent_identities WHERE id=? AND workspace_id=?",
                (identity_id, workspace_id)
            ).fetchone()
            return dict(row)

    def get_agent_identity(self, workspace_id: str, agent_id: str) -> dict | None:
        with self._connection() as db:
            row = db.execute("""
                SELECT * FROM agent_identities
                WHERE id=? AND workspace_id=?
            """, (agent_id, workspace_id)).fetchone()
            return dict(row) if row else None

    def list_agent_identities(self, workspace_id: str) -> list[dict]:
        with self._connection() as db:
            rows = db.execute("""
                SELECT * FROM agent_identities
                WHERE workspace_id=? ORDER BY role, name
            """, (workspace_id,)).fetchall()
            return [dict(row) for row in rows]

    def create_agent_discussion(
        self, workspace_id: str, title: str,
        leader_agent_id: str | None = None,
        conversation_id: str | None = None
    ) -> dict:
        if not self.get_workspace(workspace_id):
            raise ValueError("Workspace not found")
        title = title.strip()
        if not title:
            raise ValueError("Discussion title is required")
        if leader_agent_id and not self.get_agent_identity(workspace_id, leader_agent_id):
            raise ValueError("Leader agent does not belong to this workspace")
        now, discussion_id = _now(), str(uuid4())
        with self._connection() as db:
            db.execute("""
                INSERT INTO agent_discussions
                (id, workspace_id, title, leader_agent_id, conversation_id,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (discussion_id, workspace_id, title, leader_agent_id,
                  conversation_id, now, now))
            row = db.execute("""
                SELECT * FROM agent_discussions
                WHERE id=? AND workspace_id=?
            """, (discussion_id, workspace_id)).fetchone()
            return dict(row)

    def get_agent_discussion(
        self, workspace_id: str, discussion_id: str
    ) -> dict | None:
        with self._connection() as db:
            row = db.execute("""
                SELECT * FROM agent_discussions
                WHERE id=? AND workspace_id=?
            """, (discussion_id, workspace_id)).fetchone()
            return dict(row) if row else None

    def list_agent_discussions(self, workspace_id: str) -> list[dict]:
        with self._connection() as db:
            rows = db.execute("""
                SELECT * FROM agent_discussions
                WHERE workspace_id=? ORDER BY updated_at DESC
            """, (workspace_id,)).fetchall()
            return [dict(row) for row in rows]

    def set_agent_discussion_status(
        self, workspace_id: str, discussion_id: str, status: str
    ) -> dict:
        if status not in ("completed", "cancelled"):
            raise ValueError("Discussion status must be completed or cancelled")

        with self._connection() as db:
            discussion = db.execute("""
                SELECT * FROM agent_discussions
                WHERE id = ? AND workspace_id = ?
            """, (discussion_id, workspace_id)).fetchone()
            if not discussion:
                raise ValueError("Discussion not found in workspace")
            if discussion["status"] != "open":
                raise ValueError("Only open discussions can change status")

            if status == "completed":
                unfinished = db.execute("""
                    SELECT COUNT(*) AS total FROM agent_assignments
                    WHERE workspace_id = ? AND discussion_id = ?
                      AND status != 'completed'
                """, (workspace_id, discussion_id)).fetchone()["total"]
                if unfinished:
                    raise ValueError(
                        "Cannot complete discussion while assignments are unfinished"
                    )

            db.execute("""
                UPDATE agent_discussions
                SET status = ?, updated_at = ?
                WHERE id = ? AND workspace_id = ?
            """, (status, _now(), discussion_id, workspace_id))
            row = db.execute("""
                SELECT * FROM agent_discussions
                WHERE id = ? AND workspace_id = ?
            """, (discussion_id, workspace_id)).fetchone()
            return dict(row)

    def append_agent_message(
        self, workspace_id: str, discussion_id: str,
        speaker_type: str, content: str, agent_id: str | None = None
    ) -> dict:
        if speaker_type not in ("user", "agent", "leader", "system"):
            raise ValueError("Invalid discussion speaker type")
        if not content.strip():
            raise ValueError("Message content is required")
        if speaker_type in ("agent", "leader"):
            if not agent_id or not self.get_agent_identity(workspace_id, agent_id):
                raise ValueError("Agent does not belong to this workspace")
        elif agent_id is not None:
            raise ValueError("User and system messages cannot have an agent identity")

        now, message_id = _now(), str(uuid4())
        with self._connection() as db:
            discussion = db.execute("""
                SELECT id FROM agent_discussions
                WHERE id=? AND workspace_id=?
            """, (discussion_id, workspace_id)).fetchone()
            if not discussion:
                raise ValueError("Discussion does not belong to this workspace")

            sequence = db.execute("""
                SELECT COALESCE(MAX(sequence), 0) + 1
                FROM agent_messages
                WHERE workspace_id=? AND discussion_id=?
            """, (workspace_id, discussion_id)).fetchone()[0]

            db.execute("""
                INSERT INTO agent_messages
                (id, workspace_id, discussion_id, agent_id, speaker_type,
                 sequence, content, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (message_id, workspace_id, discussion_id, agent_id,
                  speaker_type, sequence, content.strip(), now))
            db.execute("""
                UPDATE agent_discussions SET updated_at=?
                WHERE id=? AND workspace_id=?
            """, (now, discussion_id, workspace_id))
            row = db.execute(
                "SELECT * FROM agent_messages WHERE id=? AND workspace_id=?",
                (message_id, workspace_id)
            ).fetchone()
            return dict(row)

    def list_agent_messages(
        self, workspace_id: str, discussion_id: str
    ) -> list[dict]:
        with self._connection() as db:
            discussion = db.execute("""
                SELECT id FROM agent_discussions
                WHERE id=? AND workspace_id=?
            """, (discussion_id, workspace_id)).fetchone()
            if not discussion:
                raise ValueError("Discussion does not belong to this workspace")
            rows = db.execute("""
                SELECT * FROM agent_messages
                WHERE workspace_id=? AND discussion_id=?
                ORDER BY sequence
            """, (workspace_id, discussion_id)).fetchall()
            return [dict(row) for row in rows]

    def create_workspace(self, name: str, purpose: str) -> Workspace:
        name = name.strip()
        if not name:
            raise ValueError("Workspace name is required")
        item = Workspace(str(uuid4()), name, purpose.strip(), _now())
        with self._connection() as db:
            db.execute("INSERT INTO workspaces VALUES (?, ?, ?, ?)",
                       (item.id, item.name, item.purpose, item.created_at))
        return item

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
        return Workspace(**dict(row)) if row else None

    def list_workspaces(self) -> list[Workspace]:
        with self._connection() as db:
            rows = db.execute("SELECT * FROM workspaces ORDER BY name").fetchall()
        return [Workspace(**dict(row)) for row in rows]

    def create_problem(self, workspace_id: str, title: str, description: str, priority: str = "normal") -> Problem:
        if not self.get_workspace(workspace_id):
            raise ValueError("Workspace does not exist")
        now = _now()
        item = Problem(str(uuid4()), workspace_id, title.strip(), description.strip(),
                       "new", priority, now, now)
        if not item.title or not item.description:
            raise ValueError("Problem title and description are required")
        with self._connection() as db:
            db.execute("INSERT INTO problems VALUES (?, ?, ?, ?, ?, ?, ?, ?)", tuple(item.__dict__.values()))
        return item

    def get_problem(self, problem_id: str) -> Problem | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM problems WHERE id=?", (problem_id,)).fetchone()
        return Problem(**dict(row)) if row else None

    def list_problems(self, workspace_id: str) -> list[Problem]:
        with self._connection() as db:
            rows = db.execute("SELECT * FROM problems WHERE workspace_id=? ORDER BY updated_at DESC", (workspace_id,)).fetchall()
        return [Problem(**dict(row)) for row in rows]

    def set_problem_status(self, problem_id: str, status: str) -> Problem:
        allowed = {
            "new": {"diagnosing", "cancelled"},
            "diagnosing": {"planned", "blocked", "failed", "cancelled"},
            "planned": {"awaiting_approval", "running", "blocked", "cancelled"},
            "awaiting_approval": {"planned", "running", "blocked", "cancelled"},
            "running": {"verifying", "awaiting_approval", "blocked", "failed", "cancelled"},
            "verifying": {"running", "completed", "blocked", "failed", "cancelled"},
            "blocked": {"diagnosing", "planned", "cancelled"},
            "failed": {"diagnosing", "cancelled"},
            "completed": set(), "cancelled": set(),
        }
        current = self.get_problem(problem_id)
        if not current:
            raise ValueError("Problem does not exist")
        if status not in allowed.get(current.status, set()):
            raise ValueError(f"Invalid problem transition: {current.status} -> {status}")
        with self._connection() as db:
            db.execute("UPDATE problems SET status=?, updated_at=? WHERE id=?", (status, _now(), problem_id))
        return self.get_problem(problem_id)

    def add_plan_step(self, problem_id: str, step_number: int, action: str,
                      permission_level: str = PermissionLevel.READ_ONLY.value) -> PlanStep:
        if not self.get_problem(problem_id):
            raise ValueError("Problem does not exist")
        if step_number < 1 or not action.strip():
            raise ValueError("A positive step number and action are required")
        if permission_level not in {p.value for p in PermissionLevel}:
            raise ValueError("Unknown permission level")
        status = (StepStatus.AWAITING_APPROVAL.value
                  if permission_level in {PermissionLevel.EXTERNAL_EFFECT.value, PermissionLevel.HIGH_IMPACT.value}
                  else StepStatus.PLANNED.value)
        item = PlanStep(str(uuid4()), problem_id, step_number, action.strip(), permission_level, status, _now())
        with self._connection() as db:
            db.execute("INSERT INTO plan_steps VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (item.id, item.problem_id, item.step_number, item.action,
                        item.permission_level, item.status, item.created_at))
        return item

    def list_steps(self, problem_id: str) -> list[PlanStep]:
        with self._connection() as db:
            rows = db.execute("SELECT * FROM plan_steps WHERE problem_id=? ORDER BY step_number", (problem_id,)).fetchall()
        return [PlanStep(**dict(row)) for row in rows]

    def update_step_status(self, step_id: str, status: str) -> None:
        allowed = {
            "planned": {"running", "cancelled", "blocked"},
            "awaiting_approval": {"approved", "cancelled", "blocked"},
            "approved": {"running", "cancelled", "blocked"},
            "running": {"verifying", "failed", "cancelled", "blocked"},
            "verifying": {"verified", "failed", "blocked"},
            "verified": set(), "failed": {"planned"}, "blocked": {"planned", "cancelled"}, "cancelled": set(),
        }
        with self._connection() as db:
            row = db.execute("SELECT status FROM plan_steps WHERE id=?", (step_id,)).fetchone()
            if not row:
                raise ValueError("Plan step does not exist")
            if status not in allowed.get(row["status"], set()):
                raise ValueError(f"Invalid step transition: {row['status']} -> {status}")
            db.execute("UPDATE plan_steps SET status=? WHERE id=?", (status, step_id))

    def record_verification(self, step_id: str, criterion: str, method: str,
                            passed: bool, evidence_reference: str = "") -> None:
        now = _now()
        with self._connection() as db:
            row = db.execute("SELECT status FROM plan_steps WHERE id=?", (step_id,)).fetchone()
            if not row or row["status"] not in {"running", "verifying"}:
                raise ValueError("Only a running or verifying step can be verified")
            db.execute("INSERT INTO verifications VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (str(uuid4()), step_id, criterion, method, int(passed), evidence_reference, now))
            db.execute("UPDATE plan_steps SET status=? WHERE id=?",
                       ("verified" if passed else "failed", step_id))

    def record_tool_run(self, step_id: str, tool_name: str, input_summary: str,
                        output_summary: str, outcome: str, started_at: str, ended_at: str | None = None) -> None:
        with self._connection() as db:
            if not db.execute("SELECT 1 FROM plan_steps WHERE id=?", (step_id,)).fetchone():
                raise ValueError("Plan step does not exist")
            db.execute("INSERT INTO tool_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (str(uuid4()), step_id, tool_name, input_summary, output_summary,
                        outcome, started_at, ended_at))



    def add_step_spec(self, step_id: str, tool_name: str,
                      input_json: str, verification_criterion: str) -> dict:
        if not self.get_step(step_id):
            raise ValueError("Plan step does not exist")
        item = {
            "step_id": step_id, "tool_name": tool_name,
            "input_json": input_json,
            "verification_criterion": (verification_criterion or "").strip()[:500],
            "created_at": _now(),
        }
        with self._connection() as db:
            db.execute("INSERT OR REPLACE INTO agent_step_specs VALUES (?, ?, ?, ?, ?)",
                       tuple(item.values()))
        return item

    def get_step(self, step_id: str) -> PlanStep | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT s.* FROM plan_steps s WHERE s.id=?", (step_id,)
            ).fetchone()
        return PlanStep(**dict(row)) if row else None

    def get_step_spec(self, step_id: str) -> dict | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM agent_step_specs WHERE step_id=?",
                             (step_id,)).fetchone()
        return dict(row) if row else None

    def list_step_specs(self, problem_id: str) -> list[dict]:
        with self._connection() as db:
            rows = db.execute(
                """SELECT sp.step_id, sp.tool_name,
                          sp.verification_criterion, sp.created_at
                   FROM agent_step_specs sp
                   JOIN plan_steps s ON s.id=sp.step_id
                   WHERE s.problem_id=? ORDER BY s.step_number""",
                (problem_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_approval_event(self, workspace_id: str, problem_id: str,
                              step_id: str, decision: str, note: str = "") -> dict:
        if decision not in {"approved", "rejected", "cancelled"}:
            raise ValueError("Approval decision must be approved or rejected")
        problem = self.get_problem(problem_id)
        if not problem or problem.workspace_id != workspace_id:
            raise ValueError("Problem does not belong to this workspace")
        if not any(step.id == step_id for step in self.list_steps(problem_id)):
            raise ValueError("Step does not belong to this task")
        event = {
            "id": str(uuid4()), "workspace_id": workspace_id,
            "problem_id": problem_id, "step_id": step_id,
            "decision": decision, "note": (note or "").strip()[:1000],
            "created_at": _now(),
        }
        with self._connection() as db:
            db.execute("INSERT INTO approval_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                       tuple(event.values()))
        return event

    def list_approval_events(self, workspace_id: str, problem_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM approval_events WHERE workspace_id=?"
        params = [workspace_id]
        if problem_id:
            sql += " AND problem_id=?"
            params.append(problem_id)
        sql += " ORDER BY created_at, id"
        with self._connection() as db:
            rows = db.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def complete_problem(self, problem_id: str) -> Problem:
        steps = self.list_steps(problem_id)
        if not steps or any(step.status != StepStatus.VERIFIED.value for step in steps):
            raise ValueError("Cannot complete: every plan step must have a passing verification")
        current = self.get_problem(problem_id)
        if not current:
            raise ValueError("Problem does not exist")
        if current.status == "running":
            self.set_problem_status(problem_id, "verifying")
        return self.set_problem_status(problem_id, "completed")
