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
            self._migrate_mission_runtime(db)

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


    def _migrate_mission_runtime(self, db) -> None:
        """Add a workspace-scoped durable mission queue (schema v6)."""
        db.executescript("""
            CREATE TABLE IF NOT EXISTS mission_jobs (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                title TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'queued'
                    CHECK(status IN ('scheduled','queued','running','paused','retryable','completed','failed','cancelled')),
                priority INTEGER NOT NULL DEFAULT 0,
                idempotency_key TEXT NOT NULL DEFAULT '',
                scheduled_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3
                    CHECK(max_attempts BETWEEN 1 AND 20),
                worker_id TEXT NOT NULL DEFAULT '',
                lease_expires_at TEXT,
                heartbeat_at TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                result_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                UNIQUE(id, workspace_id),
                FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_mission_jobs_idempotency
                ON mission_jobs(workspace_id, idempotency_key)
                WHERE idempotency_key <> '';
            CREATE INDEX IF NOT EXISTS idx_mission_jobs_claim
                ON mission_jobs(workspace_id, status, scheduled_at, priority, created_at);
            CREATE INDEX IF NOT EXISTS idx_mission_jobs_lease
                ON mission_jobs(status, lease_expires_at);

            CREATE TABLE IF NOT EXISTS mission_job_events (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                worker_id TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(mission_id, workspace_id)
                    REFERENCES mission_jobs(id, workspace_id)
            );
            CREATE INDEX IF NOT EXISTS idx_mission_job_events_mission
                ON mission_job_events(workspace_id, mission_id, created_at);
        """)
        version = db.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
        if version < 6:
            db.execute("""
                INSERT INTO schema_migrations(version, name, applied_at)
                VALUES (6, 'durable_mission_queue', ?)
            """, (_now(),))

        db.executescript("""
            CREATE TABLE IF NOT EXISTS mission_schedules (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                title TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                run_at_utc TEXT NOT NULL,
                timezone_name TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3
                    CHECK(max_attempts BETWEEN 1 AND 20),
                idempotency_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'scheduled'
                    CHECK(status IN ('scheduled','promoted','cancelled')),
                mission_job_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                promoted_at TEXT,
                UNIQUE(id, workspace_id),
                FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
                FOREIGN KEY(mission_job_id, workspace_id)
                    REFERENCES mission_jobs(id, workspace_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_mission_schedules_idempotency
                ON mission_schedules(workspace_id, idempotency_key);
            CREATE INDEX IF NOT EXISTS idx_mission_schedules_due
                ON mission_schedules(workspace_id, status, run_at_utc);

            CREATE TABLE IF NOT EXISTS mission_schedule_events (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                schedule_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(schedule_id, workspace_id)
                    REFERENCES mission_schedules(id, workspace_id)
            );
            CREATE INDEX IF NOT EXISTS idx_mission_schedule_events_schedule
                ON mission_schedule_events(workspace_id, schedule_id, created_at);
        """)
        version = db.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
        if version < 7:
            db.execute("""
                INSERT INTO schema_migrations(version, name, applied_at)
                VALUES (7, 'durable_one_time_mission_schedules', ?)
            """, (_now(),))

        db.executescript("""
            CREATE TABLE IF NOT EXISTS mission_recurring_schedules (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                title TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                recurrence_type TEXT NOT NULL CHECK(recurrence_type IN ('daily','weekly')),
                local_time TEXT NOT NULL,
                timezone_name TEXT NOT NULL,
                weekdays_json TEXT NOT NULL DEFAULT '[]',
                start_date TEXT NOT NULL,
                end_date TEXT,
                next_local_date TEXT NOT NULL,
                next_run_utc TEXT NOT NULL,
                dst_nonexistent_policy TEXT NOT NULL
                    CHECK(dst_nonexistent_policy IN ('skip','shift_forward')),
                dst_ambiguous_policy TEXT NOT NULL
                    CHECK(dst_ambiguous_policy IN ('first','second')),
                priority INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3
                    CHECK(max_attempts BETWEEN 1 AND 20),
                idempotency_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'enabled'
                    CHECK(status IN ('enabled','disabled','cancelled','completed')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(id, workspace_id),
                FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_mission_recurring_schedules_key
                ON mission_recurring_schedules(workspace_id, idempotency_key);
            CREATE INDEX IF NOT EXISTS idx_mission_recurring_schedules_due
                ON mission_recurring_schedules(workspace_id, status, next_run_utc);

            CREATE TABLE IF NOT EXISTS mission_recurring_occurrences (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                schedule_id TEXT NOT NULL,
                occurrence_key TEXT NOT NULL,
                local_date TEXT NOT NULL,
                scheduled_for_utc TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('promoted','skipped')),
                mission_job_id TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(schedule_id, occurrence_key),
                FOREIGN KEY(schedule_id, workspace_id)
                    REFERENCES mission_recurring_schedules(id, workspace_id),
                FOREIGN KEY(mission_job_id, workspace_id)
                    REFERENCES mission_jobs(id, workspace_id)
            );
            CREATE INDEX IF NOT EXISTS idx_mission_recurring_occurrences_schedule
                ON mission_recurring_occurrences(workspace_id, schedule_id, local_date);

            CREATE TABLE IF NOT EXISTS mission_recurring_schedule_events (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                schedule_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(schedule_id, workspace_id)
                    REFERENCES mission_recurring_schedules(id, workspace_id)
            );
        """)
        version = db.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
        if version < 8:
            db.execute("""
                INSERT INTO schema_migrations(version, name, applied_at)
                VALUES (8, 'durable_recurring_mission_schedules', ?)
            """, (_now(),))

    def create_mission_schedule(
        self, workspace_id: str, title: str, payload: dict | None,
        run_at: str, timezone_name: str = "UTC", priority: int = 0,
        max_attempts: int = 3, idempotency_key: str = "",
    ) -> dict:
        """Create a one-time schedule using an explicit timezone-aware timestamp."""
        import json
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        title = (title or "").strip()
        if not title:
            raise ValueError("Schedule title is required")
        if not 1 <= int(max_attempts) <= 20:
            raise ValueError("max_attempts must be between 1 and 20")
        if not -100 <= int(priority) <= 100:
            raise ValueError("Schedule priority must be between -100 and 100")
        timezone_name = (timezone_name or "").strip()
        try:
            zone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone_name must be a valid IANA timezone") from exc
        if not isinstance(run_at, str) or not run_at.strip():
            raise ValueError("run_at must be a timezone-aware ISO timestamp")
        try:
            parsed = datetime.fromisoformat(run_at.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError("run_at must be a valid timezone-aware timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("run_at must be timezone-aware and include an explicit UTC offset")
        if parsed.astimezone(zone).utcoffset() != parsed.utcoffset():
            raise ValueError("run_at offset does not match timezone_name")
        due = parsed.astimezone(timezone.utc).isoformat()
        try:
            payload_json = json.dumps(payload or {}, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("Schedule payload must be JSON serializable") from exc

        schedule_id = str(uuid4())
        key = (idempotency_key or "").strip() or schedule_id
        if len(key) > 200:
            raise ValueError("Schedule idempotency key is too long")
        now = _now()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute(
                "SELECT 1 FROM workspaces WHERE id=?", (workspace_id,)
            ).fetchone():
                raise ValueError("Workspace does not exist")
            existing = db.execute("""
                SELECT * FROM mission_schedules
                WHERE workspace_id=? AND idempotency_key=?
            """, (workspace_id, key)).fetchone()
            if existing:
                if (existing["title"] != title
                    or existing["payload_json"] != payload_json
                    or existing["run_at_utc"] != due
                    or existing["timezone_name"] != timezone_name
                    or existing["priority"] != int(priority)
                    or existing["max_attempts"] != int(max_attempts)):
                    raise ValueError("Schedule idempotency key was reused with different input")
                return dict(existing)
            db.execute("""
                INSERT INTO mission_schedules (
                    id, workspace_id, title, payload_json, run_at_utc,
                    timezone_name, priority, max_attempts, idempotency_key,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?)
            """, (
                schedule_id, workspace_id, title, payload_json, due,
                timezone_name, int(priority), int(max_attempts), key, now, now,
            ))
            db.execute("""
                INSERT INTO mission_schedule_events
                (id, workspace_id, schedule_id, event_type, detail, created_at)
                VALUES (?, ?, ?, 'created', ?, ?)
            """, (str(uuid4()), workspace_id, schedule_id, due, now))
            row = db.execute("""
                SELECT * FROM mission_schedules WHERE id=? AND workspace_id=?
            """, (schedule_id, workspace_id)).fetchone()
            return dict(row)

    def get_mission_schedule(
        self, workspace_id: str, schedule_id: str
    ) -> dict | None:
        with self._connection() as db:
            row = db.execute("""
                SELECT * FROM mission_schedules WHERE id=? AND workspace_id=?
            """, (schedule_id, workspace_id)).fetchone()
        return dict(row) if row else None

    def list_mission_schedules(
        self, workspace_id: str, limit: int = 50,
        statuses: list[str] | None = None,
    ) -> list[dict]:
        allowed = {"scheduled", "promoted", "cancelled"}
        selected = list(dict.fromkeys(statuses or []))
        if any(status not in allowed for status in selected):
            raise ValueError("Unknown mission schedule status filter")
        sql = "SELECT * FROM mission_schedules WHERE workspace_id=?"
        params = [workspace_id]
        if selected:
            sql += " AND status IN (" + ",".join("?" for _ in selected) + ")"
            params.extend(selected)
        sql += " ORDER BY run_at_utc, created_at LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        with self._connection() as db:
            rows = db.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def promote_due_mission_schedules(
        self, workspace_id: str, limit: int = 50, now: str | None = None,
    ) -> list[dict]:
        """Atomically turn due schedules into queue jobs exactly once."""
        current = datetime.now(timezone.utc)
        if now is not None:
            if not isinstance(now, str):
                raise ValueError("now must be a valid timezone-aware ISO timestamp")
            try:
                current = datetime.fromisoformat(now.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("now must be a valid timezone-aware ISO timestamp") from exc
            if current.tzinfo is None or current.utcoffset() is None:
                raise ValueError("now must include an explicit UTC offset")
            current = current.astimezone(timezone.utc)
        now_utc = current.isoformat()
        promoted = []
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            schedules = db.execute("""
                SELECT * FROM mission_schedules
                WHERE workspace_id=? AND status='scheduled' AND run_at_utc<=?
                ORDER BY run_at_utc, created_at, id LIMIT ?
            """, (workspace_id, now_utc, max(1, min(int(limit), 200)))).fetchall()
            for schedule in schedules:
                mission_id = str(uuid4())
                job_key = "schedule:" + schedule["id"]
                db.execute("""
                    INSERT INTO mission_jobs (
                        id, workspace_id, title, payload_json, status, priority,
                        idempotency_key, scheduled_at, attempts, max_attempts,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, 0, ?, ?, ?)
                """, (
                    mission_id, workspace_id, schedule["title"],
                    schedule["payload_json"], schedule["priority"], job_key,
                    schedule["run_at_utc"], schedule["max_attempts"], now_utc, now_utc,
                ))
                db.execute("""
                    INSERT INTO mission_job_events
                    (id, workspace_id, mission_id, event_type, detail, created_at)
                    VALUES (?, ?, ?, 'created', 'promoted from schedule', ?)
                """, (str(uuid4()), workspace_id, mission_id, now_utc))
                db.execute("""
                    UPDATE mission_schedules
                    SET status='promoted', mission_job_id=?, promoted_at=?, updated_at=?
                    WHERE id=? AND workspace_id=? AND status='scheduled'
                """, (mission_id, now_utc, now_utc, schedule["id"], workspace_id))
                db.execute("""
                    INSERT INTO mission_schedule_events
                    (id, workspace_id, schedule_id, event_type, detail, created_at)
                    VALUES (?, ?, ?, 'promoted', ?, ?)
                """, (
                    str(uuid4()), workspace_id, schedule["id"], mission_id, now_utc,
                ))
                job = db.execute("""
                    SELECT * FROM mission_jobs WHERE id=? AND workspace_id=?
                """, (mission_id, workspace_id)).fetchone()
                promoted.append(self._mission_dict(job))
        return promoted

    def cancel_mission_schedule(
        self, workspace_id: str, schedule_id: str, detail: str = "",
    ) -> dict:
        now = _now()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""
                SELECT * FROM mission_schedules WHERE id=? AND workspace_id=?
            """, (schedule_id, workspace_id)).fetchone()
            if not row:
                raise ValueError("Mission schedule not found in workspace")
            if row["status"] != "scheduled":
                raise ValueError("Only pending schedules can be cancelled")
            db.execute("""
                UPDATE mission_schedules SET status='cancelled', updated_at=?
                WHERE id=? AND workspace_id=? AND status='scheduled'
            """, (now, schedule_id, workspace_id))
            db.execute("""
                INSERT INTO mission_schedule_events
                (id, workspace_id, schedule_id, event_type, detail, created_at)
                VALUES (?, ?, ?, 'cancelled', ?, ?)
            """, (str(uuid4()), workspace_id, schedule_id, (detail or "")[:2000], now))
            updated = db.execute("""
                SELECT * FROM mission_schedules WHERE id=? AND workspace_id=?
            """, (schedule_id, workspace_id)).fetchone()
            return dict(updated)

    def list_mission_schedule_events(
        self, workspace_id: str, schedule_id: str,
    ) -> list[dict]:
        with self._connection() as db:
            if not db.execute("""
                SELECT 1 FROM mission_schedules WHERE id=? AND workspace_id=?
            """, (schedule_id, workspace_id)).fetchone():
                raise ValueError("Mission schedule not found in workspace")
            rows = db.execute("""
                SELECT * FROM mission_schedule_events
                WHERE workspace_id=? AND schedule_id=?
                ORDER BY rowid
            """, (workspace_id, schedule_id)).fetchall()
        return [dict(row) for row in rows]


    @staticmethod
    def _recurring_local_occurrence(local_date, local_time, timezone_name,
                                    nonexistent_policy, ambiguous_policy):
        """Resolve a local wall-clock occurrence with explicit DST behavior."""
        from datetime import date, datetime, time, timedelta
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(timezone_name)
        day = date.fromisoformat(local_date)
        wall = time.fromisoformat(local_time)
        naive = datetime.combine(day, wall)

        def valid_candidates(value):
            candidates = []
            for fold in (0, 1):
                aware = value.replace(tzinfo=zone, fold=fold)
                round_trip = aware.astimezone(timezone.utc).astimezone(zone)
                if round_trip.replace(tzinfo=None) == value:
                    if not any(
                        item.astimezone(timezone.utc) == aware.astimezone(timezone.utc)
                        for item in candidates
                    ):
                        candidates.append(aware)
            return candidates

        candidates = valid_candidates(naive)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            ordered = sorted(candidates, key=lambda item: item.astimezone(timezone.utc))
            return ordered[0] if ambiguous_policy == "first" else ordered[-1]
        if nonexistent_policy == "skip":
            return None
        # Shift forward to the first valid wall-clock minute after a DST gap.
        for minute in range(1, 181):
            candidates = valid_candidates(naive + timedelta(minutes=minute))
            if candidates:
                return sorted(
                    candidates, key=lambda item: item.astimezone(timezone.utc)
                )[0]
        raise ValueError("Could not resolve nonexistent local time within three hours")

    @staticmethod
    def _find_next_recurring_occurrence(
        recurrence_type, local_time, timezone_name, after_date,
        weekdays, end_date, nonexistent_policy, ambiguous_policy,
    ):
        from datetime import date, timedelta

        cursor = date.fromisoformat(after_date)
        end = date.fromisoformat(end_date) if end_date else None
        skipped = []
        for _ in range(3700):
            cursor += timedelta(days=1)
            if end and cursor > end:
                return None, None, skipped
            if recurrence_type == "weekly" and cursor.weekday() not in weekdays:
                continue
            occurrence = TaskStore._recurring_local_occurrence(
                cursor.isoformat(), local_time, timezone_name,
                nonexistent_policy, ambiguous_policy,
            )
            if occurrence is None:
                skipped.append(cursor.isoformat())
                continue
            return cursor.isoformat(), occurrence, skipped
        raise ValueError("Could not find a recurrence within ten years")

    def create_recurring_mission_schedule(
        self, workspace_id: str, title: str, payload: dict | None,
        recurrence_type: str, local_time: str, timezone_name: str,
        start_date: str, weekdays: list[int] | None = None,
        end_date: str | None = None, dst_nonexistent_policy: str = "skip",
        dst_ambiguous_policy: str = "first", priority: int = 0,
        max_attempts: int = 3, idempotency_key: str = "",
    ) -> dict:
        """Create a daily or selected-weekday recurring mission definition."""
        import json
        from datetime import date, datetime, time, timezone
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        title = (title or "").strip()
        if not title:
            raise ValueError("Recurring schedule title is required")
        if recurrence_type not in {"daily", "weekly"}:
            raise ValueError("recurrence_type must be daily or weekly")
        try:
            time.fromisoformat(local_time)
            if len(local_time) != 5:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError("local_time must use HH:MM format") from exc
        timezone_name = (timezone_name or "").strip()
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone_name must be a valid IANA timezone") from exc
        try:
            start = date.fromisoformat(start_date)
            end = date.fromisoformat(end_date) if end_date else None
        except (TypeError, ValueError) as exc:
            raise ValueError("start_date and end_date must be ISO dates") from exc
        if end and end < start:
            raise ValueError("end_date cannot precede start_date")
        selected_days = sorted(set(weekdays or []))
        if recurrence_type == "weekly" and (
            not selected_days or any(type(day) is not int or day < 0 or day > 6 for day in selected_days)
        ):
            raise ValueError("weekly schedules require weekdays from 0 (Monday) to 6 (Sunday)")
        if recurrence_type == "daily" and selected_days:
            raise ValueError("daily schedules do not accept weekdays")
        if dst_nonexistent_policy not in {"skip", "shift_forward"}:
            raise ValueError("dst_nonexistent_policy must be skip or shift_forward")
        if dst_ambiguous_policy not in {"first", "second"}:
            raise ValueError("dst_ambiguous_policy must be first or second")
        if not 1 <= int(max_attempts) <= 20:
            raise ValueError("max_attempts must be between 1 and 20")
        if not -100 <= int(priority) <= 100:
            raise ValueError("Schedule priority must be between -100 and 100")
        try:
            payload_json = json.dumps(payload or {}, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("Schedule payload must be JSON serializable") from exc

        first_date = start
        if recurrence_type == "weekly":
            while first_date.weekday() not in selected_days:
                first_date = date.fromordinal(first_date.toordinal() + 1)
                if end and first_date > end:
                    raise ValueError("No selected weekday occurs within the schedule range")
        first = self._recurring_local_occurrence(
            first_date.isoformat(), local_time, timezone_name,
            dst_nonexistent_policy, dst_ambiguous_policy,
        )
        if first is None:
            next_date, first, _ = self._find_next_recurring_occurrence(
                recurrence_type, local_time, timezone_name, first_date.isoformat(),
                selected_days, end_date, dst_nonexistent_policy, dst_ambiguous_policy,
            )
            if not next_date:
                raise ValueError("No valid occurrence exists within the schedule range")
            first_date = date.fromisoformat(next_date)
        if end and first_date > end:
            raise ValueError("No occurrence exists within the schedule range")

        key = (idempotency_key or "").strip() or str(uuid4())
        if len(key) > 200:
            raise ValueError("Schedule idempotency key is too long")
        schedule_id, now = str(uuid4()), _now()
        next_utc = first.astimezone(timezone.utc).isoformat()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM workspaces WHERE id=?", (workspace_id,)).fetchone():
                raise ValueError("Workspace does not exist")
            existing = db.execute("""
                SELECT * FROM mission_recurring_schedules
                WHERE workspace_id=? AND idempotency_key=?
            """, (workspace_id, key)).fetchone()
            if existing:
                expected = (
                    existing["title"], existing["payload_json"], existing["recurrence_type"],
                    existing["local_time"], existing["timezone_name"],
                    existing["weekdays_json"], existing["start_date"], existing["end_date"],
                    existing["dst_nonexistent_policy"], existing["dst_ambiguous_policy"],
                    existing["priority"], existing["max_attempts"],
                )
                supplied = (
                    title, payload_json, recurrence_type, local_time, timezone_name,
                    json.dumps(selected_days), start.isoformat(),
                    end.isoformat() if end else None, dst_nonexistent_policy,
                    dst_ambiguous_policy, int(priority), int(max_attempts),
                )
                if expected != supplied:
                    raise ValueError("Recurring schedule idempotency key was reused with different input")
                return dict(existing)
            db.execute("""
                INSERT INTO mission_recurring_schedules (
                    id, workspace_id, title, payload_json, recurrence_type, local_time,
                    timezone_name, weekdays_json, start_date, end_date, next_local_date,
                    next_run_utc, dst_nonexistent_policy, dst_ambiguous_policy,
                    priority, max_attempts, idempotency_key, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'enabled', ?, ?)
            """, (
                schedule_id, workspace_id, title, payload_json, recurrence_type,
                local_time, timezone_name, json.dumps(selected_days), start.isoformat(),
                end.isoformat() if end else None, first_date.isoformat(), next_utc,
                dst_nonexistent_policy, dst_ambiguous_policy, int(priority),
                int(max_attempts), key, now, now,
            ))
            db.execute("""
                INSERT INTO mission_recurring_schedule_events
                (id, workspace_id, schedule_id, event_type, detail, created_at)
                VALUES (?, ?, ?, 'created', ?, ?)
            """, (str(uuid4()), workspace_id, schedule_id, next_utc, now))
            row = db.execute("""
                SELECT * FROM mission_recurring_schedules WHERE id=? AND workspace_id=?
            """, (schedule_id, workspace_id)).fetchone()
            return dict(row)

    def get_recurring_mission_schedule(self, workspace_id: str, schedule_id: str) -> dict | None:
        import json
        with self._connection() as db:
            row = db.execute("""
                SELECT * FROM mission_recurring_schedules WHERE id=? AND workspace_id=?
            """, (schedule_id, workspace_id)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        item["weekdays"] = json.loads(item.pop("weekdays_json"))
        return item

    def list_recurring_mission_schedules(self, workspace_id: str, limit: int = 50) -> list[dict]:
        import json
        with self._connection() as db:
            rows = db.execute("""
                SELECT * FROM mission_recurring_schedules
                WHERE workspace_id=? ORDER BY next_run_utc, created_at LIMIT ?
            """, (workspace_id, max(1, min(int(limit), 200)))).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            item["weekdays"] = json.loads(item.pop("weekdays_json"))
            result.append(item)
        return result

    def list_recurring_mission_occurrences(
        self, workspace_id: str, schedule_id: str,
    ) -> list[dict]:
        with self._connection() as db:
            if not db.execute("""
                SELECT 1 FROM mission_recurring_schedules WHERE id=? AND workspace_id=?
            """, (schedule_id, workspace_id)).fetchone():
                raise ValueError("Recurring schedule not found in workspace")
            rows = db.execute("""
                SELECT * FROM mission_recurring_occurrences
                WHERE workspace_id=? AND schedule_id=? ORDER BY local_date, occurrence_key
            """, (workspace_id, schedule_id)).fetchall()
        return [dict(row) for row in rows]

    def set_recurring_mission_schedule_status(
        self, workspace_id: str, schedule_id: str, status: str,
    ) -> dict:
        if status not in {"enabled", "disabled", "cancelled"}:
            raise ValueError("Recurring schedule status must be enabled, disabled, or cancelled")
        now = _now()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""
                SELECT * FROM mission_recurring_schedules WHERE id=? AND workspace_id=?
            """, (schedule_id, workspace_id)).fetchone()
            if not row:
                raise ValueError("Recurring schedule not found in workspace")
            if row["status"] in {"cancelled", "completed"}:
                raise ValueError("Terminal recurring schedules cannot be re-enabled or changed")
            db.execute("""
                UPDATE mission_recurring_schedules SET status=?, updated_at=?
                WHERE id=? AND workspace_id=?
            """, (status, now, schedule_id, workspace_id))
            db.execute("""
                INSERT INTO mission_recurring_schedule_events
                (id, workspace_id, schedule_id, event_type, detail, created_at)
                VALUES (?, ?, ?, ?, '', ?)
            """, (str(uuid4()), workspace_id, schedule_id, status, now))
            updated = db.execute("""
                SELECT * FROM mission_recurring_schedules WHERE id=? AND workspace_id=?
            """, (schedule_id, workspace_id)).fetchone()
            return dict(updated)

    def promote_due_recurring_mission_schedules(
        self, workspace_id: str, limit: int = 50, now: str | None = None,
    ) -> list[dict]:
        """Promote all due recurring occurrences atomically into the one mission queue."""
        from datetime import datetime, timezone
        import json

        current = datetime.now(timezone.utc)
        if now is not None:
            try:
                current = datetime.fromisoformat(now.replace("Z", "+00:00"))
            except (TypeError, ValueError) as exc:
                raise ValueError("now must be a timezone-aware ISO timestamp") from exc
            if current.tzinfo is None or current.utcoffset() is None:
                raise ValueError("now must include an explicit UTC offset")
            current = current.astimezone(timezone.utc)
        now_utc = current.isoformat()
        promoted = []
        max_items = max(1, min(int(limit), 200))
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            schedules = db.execute("""
                SELECT * FROM mission_recurring_schedules
                WHERE workspace_id=? AND status='enabled' AND next_run_utc<=?
                ORDER BY next_run_utc, created_at LIMIT ?
            """, (workspace_id, now_utc, max_items)).fetchall()
            for schedule in schedules:
                current_schedule = schedule
                while current_schedule and current_schedule["next_run_utc"] <= now_utc and len(promoted) < max_items:
                    occurrence_date = current_schedule["next_local_date"]
                    occurrence_key = occurrence_date + "T" + current_schedule["local_time"]
                    local_dt = self._recurring_local_occurrence(
                        occurrence_date, current_schedule["local_time"],
                        current_schedule["timezone_name"],
                        current_schedule["dst_nonexistent_policy"],
                        current_schedule["dst_ambiguous_policy"],
                    )
                    if local_dt is None:
                        # A skipped spring-forward wall time is recorded and advanced without a job.
                        db.execute("""
                            INSERT OR IGNORE INTO mission_recurring_occurrences
                            (id, workspace_id, schedule_id, occurrence_key, local_date,
                             scheduled_for_utc, status, mission_job_id, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, 'skipped', NULL, ?)
                        """, (
                            str(uuid4()), workspace_id, current_schedule["id"],
                            occurrence_key, occurrence_date,
                            current_schedule["next_run_utc"], now_utc,
                        ))
                        db.execute("""
                            INSERT INTO mission_recurring_schedule_events
                            (id, workspace_id, schedule_id, event_type, detail, created_at)
                            VALUES (?, ?, ?, 'skipped_dst', ?, ?)
                        """, (
                            str(uuid4()), workspace_id, current_schedule["id"],
                            occurrence_key, now_utc,
                        ))
                    else:
                        run_utc = local_dt.astimezone(timezone.utc).isoformat()
                        job_id = str(uuid4())
                        job_key = "recurring:" + current_schedule["id"] + ":" + occurrence_key
                        db.execute("""
                            INSERT OR IGNORE INTO mission_jobs (
                                id, workspace_id, title, payload_json, status, priority,
                                idempotency_key, scheduled_at, attempts, max_attempts,
                                created_at, updated_at
                            ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, 0, ?, ?, ?)
                        """, (
                            job_id, workspace_id, current_schedule["title"],
                            current_schedule["payload_json"], current_schedule["priority"],
                            job_key, run_utc, current_schedule["max_attempts"], now_utc, now_utc,
                        ))
                        job = db.execute("""
                            SELECT * FROM mission_jobs WHERE workspace_id=? AND idempotency_key=?
                        """, (workspace_id, job_key)).fetchone()
                        db.execute("""
                            INSERT OR IGNORE INTO mission_job_events
                            (id, workspace_id, mission_id, event_type, detail, created_at)
                            VALUES (?, ?, ?, 'created', 'promoted from recurring schedule', ?)
                        """, (str(uuid4()), workspace_id, job["id"], now_utc))
                        db.execute("""
                            INSERT OR IGNORE INTO mission_recurring_occurrences
                            (id, workspace_id, schedule_id, occurrence_key, local_date,
                             scheduled_for_utc, status, mission_job_id, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, 'promoted', ?, ?)
                        """, (
                            str(uuid4()), workspace_id, current_schedule["id"],
                            occurrence_key, occurrence_date, run_utc, job["id"], now_utc,
                        ))
                        promoted.append(self._mission_dict(job))

                    next_date, next_dt, skipped_dates = self._find_next_recurring_occurrence(
                        current_schedule["recurrence_type"], current_schedule["local_time"],
                        current_schedule["timezone_name"], occurrence_date,
                        json.loads(current_schedule["weekdays_json"]),
                        current_schedule["end_date"],
                        current_schedule["dst_nonexistent_policy"],
                        current_schedule["dst_ambiguous_policy"],
                    )
                    for skipped_date in skipped_dates:
                        skipped_key = skipped_date + "T" + current_schedule["local_time"]
                        db.execute("""
                            INSERT OR IGNORE INTO mission_recurring_occurrences
                            (id, workspace_id, schedule_id, occurrence_key, local_date,
                             scheduled_for_utc, status, mission_job_id, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, 'skipped', NULL, ?)
                        """, (
                            str(uuid4()), workspace_id, current_schedule["id"],
                            skipped_key, skipped_date, now_utc, now_utc,
                        ))
                        db.execute("""
                            INSERT INTO mission_recurring_schedule_events
                            (id, workspace_id, schedule_id, event_type, detail, created_at)
                            VALUES (?, ?, ?, 'skipped_dst', ?, ?)
                        """, (str(uuid4()), workspace_id, current_schedule["id"], skipped_key, now_utc))
                    if not next_date:
                        db.execute("""
                            UPDATE mission_recurring_schedules
                            SET status='completed', updated_at=?
                            WHERE id=? AND workspace_id=?
                        """, (now_utc, current_schedule["id"], workspace_id))
                        db.execute("""
                            INSERT INTO mission_recurring_schedule_events
                            (id, workspace_id, schedule_id, event_type, detail, created_at)
                            VALUES (?, ?, ?, 'completed', 'recurrence range exhausted', ?)
                        """, (str(uuid4()), workspace_id, current_schedule["id"], now_utc))
                        break
                    next_run_utc = next_dt.astimezone(timezone.utc).isoformat()
                    db.execute("""
                        UPDATE mission_recurring_schedules
                        SET next_local_date=?, next_run_utc=?, updated_at=?
                        WHERE id=? AND workspace_id=? AND status='enabled'
                    """, (next_date, next_run_utc, now_utc, current_schedule["id"], workspace_id))
                    current_schedule = db.execute("""
                        SELECT * FROM mission_recurring_schedules
                        WHERE id=? AND workspace_id=?
                    """, (current_schedule["id"], workspace_id)).fetchone()
        return promoted

    @staticmethod
    def _mission_dict(row):
        if not row:
            return None
        import json
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        item["result"] = json.loads(item.pop("result_json"))
        return item

    def create_mission_job(
        self, workspace_id: str, title: str, payload: dict | None = None,
        scheduled_at: str | None = None, priority: int = 0,
        idempotency_key: str = "", max_attempts: int = 3,
    ) -> dict:
        """Persist a mission once; repeated idempotency keys return the same job."""
        import json
        title = (title or "").strip()
        if not title:
            raise ValueError("Mission title is required")
        if not 1 <= int(max_attempts) <= 20:
            raise ValueError("max_attempts must be between 1 and 20")
        if not -100 <= int(priority) <= 100:
            raise ValueError("Mission priority must be between -100 and 100")
        key = (idempotency_key or "").strip()
        if len(key) > 200:
            raise ValueError("Mission idempotency key is too long")
        try:
            payload_json = json.dumps(payload or {}, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("Mission payload must be JSON serializable") from exc
        now = _now()
        due_input = scheduled_at or now
        if not isinstance(due_input, str) or not due_input.strip():
            raise ValueError("scheduled_at must be a non-empty ISO timestamp")
        try:
            parsed_due = datetime.fromisoformat(due_input.replace("Z", "+00:00"))
            if parsed_due.tzinfo is None:
                parsed_due = parsed_due.replace(tzinfo=timezone.utc)
            due = parsed_due.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError) as exc:
            raise ValueError("scheduled_at must be a valid ISO timestamp") from exc
        status = "scheduled" if due > now else "queued"
        mission_id = str(uuid4())
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute(
                "SELECT 1 FROM workspaces WHERE id=?", (workspace_id,)
            ).fetchone():
                raise ValueError("Workspace does not exist")
            if key:
                existing = db.execute("""
                    SELECT * FROM mission_jobs
                    WHERE workspace_id=? AND idempotency_key=?
                """, (workspace_id, key)).fetchone()
                if existing:
                    if (existing["title"] != title
                        or existing["payload_json"] != payload_json
                        or (scheduled_at is not None
                            and existing["scheduled_at"] != due)
                        or existing["priority"] != int(priority)
                        or existing["max_attempts"] != int(max_attempts)):
                        raise ValueError(
                            "Mission idempotency key was reused with different input"
                        )
                    return self._mission_dict(existing)
            db.execute("""
                INSERT INTO mission_jobs (
                    id, workspace_id, title, payload_json, status, priority,
                    idempotency_key, scheduled_at, attempts, max_attempts,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """, (
                mission_id, workspace_id, title, payload_json, status,
                int(priority), key, due, int(max_attempts), now, now,
            ))
            db.execute("""
                INSERT INTO mission_job_events
                (id, workspace_id, mission_id, event_type, detail, created_at)
                VALUES (?, ?, ?, 'created', ?, ?)
            """, (str(uuid4()), workspace_id, mission_id, status, now))
            row = db.execute(
                "SELECT * FROM mission_jobs WHERE id=? AND workspace_id=?",
                (mission_id, workspace_id),
            ).fetchone()
            return self._mission_dict(row)

    def get_mission_job(self, workspace_id: str, mission_id: str) -> dict | None:
        with self._connection() as db:
            row = db.execute("""
                SELECT * FROM mission_jobs WHERE id=? AND workspace_id=?
            """, (mission_id, workspace_id)).fetchone()
        return self._mission_dict(row)

    def list_mission_jobs(
        self, workspace_id: str, limit: int = 50,
        statuses: list[str] | None = None,
    ) -> list[dict]:
        allowed = {
            "scheduled", "queued", "running", "paused", "retryable",
            "completed", "failed", "cancelled",
        }
        selected = list(dict.fromkeys(statuses or []))
        if any(status not in allowed for status in selected):
            raise ValueError("Unknown mission status filter")
        sql = "SELECT * FROM mission_jobs WHERE workspace_id=?"
        params = [workspace_id]
        if selected:
            sql += " AND status IN (" + ",".join("?" for _ in selected) + ")"
            params.extend(selected)
        sql += " ORDER BY priority DESC, created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        with self._connection() as db:
            rows = db.execute(sql, params).fetchall()
        return [self._mission_dict(row) for row in rows]

    def claim_mission_job(
        self, workspace_id: str, worker_id: str,
        lease_seconds: int = 60, mission_id: str | None = None,
    ) -> dict | None:
        """Atomically claim due work or reclaim a worker whose lease expired."""
        worker_id = (worker_id or "").strip()
        if not worker_id:
            raise ValueError("worker_id is required")
        if not 10 <= int(lease_seconds) <= 3600:
            raise ValueError("lease_seconds must be between 10 and 3600")
        from datetime import timedelta
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        expires = (now_dt + timedelta(seconds=int(lease_seconds))).isoformat()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # A worker that exhausted its retry budget must not remain running forever.
            exhausted = db.execute("""
                SELECT id, worker_id FROM mission_jobs
                WHERE workspace_id=? AND status='running'
                  AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
                  AND attempts >= max_attempts
            """, (workspace_id, now)).fetchall()
            for stale in exhausted:
                db.execute("""
                    UPDATE mission_jobs SET status='failed', worker_id='',
                        lease_expires_at=NULL, heartbeat_at=NULL,
                        last_error='Worker lease expired after maximum attempts',
                        completed_at=?, updated_at=?
                    WHERE id=? AND workspace_id=? AND status='running'
                """, (now, now, stale["id"], workspace_id))
                db.execute("""
                    INSERT INTO mission_job_events
                    (id, workspace_id, mission_id, event_type, worker_id, detail, created_at)
                    VALUES (?, ?, ?, 'failed', ?, ?, ?)
                """, (
                    str(uuid4()), workspace_id, stale["id"], stale["worker_id"],
                    "Worker lease expired after maximum attempts", now,
                ))
            sql = """
                SELECT * FROM mission_jobs
                WHERE workspace_id=? AND attempts < max_attempts
                  AND (
                    (status IN ('queued','retryable') AND scheduled_at <= ?)
                    OR (status='scheduled' AND scheduled_at <= ?)
                    OR (status='running' AND lease_expires_at IS NOT NULL
                        AND lease_expires_at <= ?)
                  )
            """
            params = [workspace_id, now, now, now]
            if mission_id:
                sql += " AND id=?"
                params.append(mission_id)
            sql += " ORDER BY priority DESC, scheduled_at, created_at LIMIT 1"
            row = db.execute(sql, params).fetchone()
            if not row:
                return None
            previous_worker = row["worker_id"]
            db.execute("""
                UPDATE mission_jobs
                SET status='running', worker_id=?, lease_expires_at=?,
                    heartbeat_at=?, attempts=attempts+1,
                    started_at=COALESCE(started_at, ?), updated_at=?
                WHERE id=? AND workspace_id=?
            """, (
                worker_id, expires, now, now, now,
                row["id"], workspace_id,
            ))
            event_type = "lease_reclaimed" if row["status"] == "running" else "claimed"
            detail = ("previous worker: " + previous_worker) if previous_worker else ""
            db.execute("""
                INSERT INTO mission_job_events
                (id, workspace_id, mission_id, event_type, worker_id, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                str(uuid4()), workspace_id, row["id"], event_type,
                worker_id, detail, now,
            ))
            claimed = db.execute("""
                SELECT * FROM mission_jobs WHERE id=? AND workspace_id=?
            """, (row["id"], workspace_id)).fetchone()
            return self._mission_dict(claimed)

    def heartbeat_mission_job(
        self, workspace_id: str, mission_id: str, worker_id: str,
        lease_seconds: int = 60,
    ) -> dict:
        from datetime import timedelta
        if not 10 <= int(lease_seconds) <= 3600:
            raise ValueError("lease_seconds must be between 10 and 3600")
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        expires = (now_dt + timedelta(seconds=int(lease_seconds))).isoformat()
        with self._connection() as db:
            cursor = db.execute("""
                UPDATE mission_jobs
                SET heartbeat_at=?, lease_expires_at=?, updated_at=?
                WHERE id=? AND workspace_id=? AND status='running'
                  AND worker_id=?
            """, (now, expires, now, mission_id, workspace_id, worker_id))
            if cursor.rowcount != 1:
                raise ValueError("Mission lease is not owned by this worker")
            row = db.execute("""
                SELECT * FROM mission_jobs WHERE id=? AND workspace_id=?
            """, (mission_id, workspace_id)).fetchone()
            return self._mission_dict(row)

    def set_mission_job_status(
        self, workspace_id: str, mission_id: str, status: str,
        worker_id: str | None = None, detail: str = "",
        result: dict | None = None, retry_at: str | None = None,
    ) -> dict:
        """Apply a guarded mission transition and append an immutable event."""
        import json
        transitions = {
            "scheduled": {"cancelled"},
            "queued": {"cancelled", "paused"},
            "running": {"paused", "retryable", "completed", "failed", "cancelled"},
            "paused": {"queued", "cancelled"},
            "retryable": {"queued", "cancelled"},
            "completed": set(), "failed": set(), "cancelled": set(),
        }
        if status not in transitions:
            raise ValueError("Unknown mission status")
        result_json = json.dumps(result or {}, sort_keys=True)
        if retry_at is not None:
            if status != "retryable":
                raise ValueError("retry_at is only valid for retryable missions")
            try:
                parsed_retry = datetime.fromisoformat(retry_at.replace("Z", "+00:00"))
                if parsed_retry.tzinfo is None or parsed_retry.utcoffset() is None:
                    raise ValueError
                retry_at = parsed_retry.astimezone(timezone.utc).isoformat()
            except (TypeError, ValueError) as exc:
                raise ValueError("retry_at must be a timezone-aware ISO timestamp") from exc
        now = _now()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""
                SELECT * FROM mission_jobs WHERE id=? AND workspace_id=?
            """, (mission_id, workspace_id)).fetchone()
            if not row:
                raise ValueError("Mission not found in workspace")
            if status not in transitions[row["status"]]:
                raise ValueError(
                    f"Invalid mission transition: {row['status']} -> {status}"
                )
            if row["status"] == "running" and (
                not worker_id or row["worker_id"] != worker_id
            ):
                raise ValueError("Only the lease owner can change a running mission")
            if status == "queued" and row["status"] not in {"paused", "retryable"}:
                raise ValueError("Only paused or retryable missions can be resumed")
            if status == "queued":
                db.execute("""
                    UPDATE mission_jobs SET status=?, worker_id='', lease_expires_at=NULL,
                        heartbeat_at=NULL, last_error='', updated_at=?
                    WHERE id=? AND workspace_id=?
                """, (status, now, mission_id, workspace_id))
            elif status in {"completed", "failed", "cancelled"}:
                db.execute("""
                    UPDATE mission_jobs SET status=?, worker_id='', lease_expires_at=NULL,
                        heartbeat_at=NULL, last_error=?, result_json=?,
                        completed_at=?, updated_at=?
                    WHERE id=? AND workspace_id=?
                """, (
                    status, (detail or "")[:2000], result_json, now, now,
                    mission_id, workspace_id,
                ))
            elif status == "retryable" and retry_at is not None:
                db.execute("""
                    UPDATE mission_jobs SET status=?, worker_id='', lease_expires_at=NULL,
                        heartbeat_at=NULL, last_error=?, scheduled_at=?, updated_at=?
                    WHERE id=? AND workspace_id=?
                """, (
                    status, (detail or "")[:2000], retry_at, now, mission_id, workspace_id,
                ))
            else:
                db.execute("""
                    UPDATE mission_jobs SET status=?, worker_id='', lease_expires_at=NULL,
                        heartbeat_at=NULL, last_error=?, updated_at=?
                    WHERE id=? AND workspace_id=?
                """, (
                    status, (detail or "")[:2000], now, mission_id, workspace_id,
                ))
            db.execute("""
                INSERT INTO mission_job_events
                (id, workspace_id, mission_id, event_type, worker_id, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                str(uuid4()), workspace_id, mission_id, status,
                worker_id or "", (detail or "")[:2000], now,
            ))
            updated = db.execute("""
                SELECT * FROM mission_jobs WHERE id=? AND workspace_id=?
            """, (mission_id, workspace_id)).fetchone()
            return self._mission_dict(updated)

    def list_mission_job_events(
        self, workspace_id: str, mission_id: str,
    ) -> list[dict]:
        with self._connection() as db:
            exists = db.execute("""
                SELECT 1 FROM mission_jobs WHERE id=? AND workspace_id=?
            """, (mission_id, workspace_id)).fetchone()
            if not exists:
                raise ValueError("Mission not found in workspace")
            rows = db.execute("""
                SELECT * FROM mission_job_events
                WHERE workspace_id=? AND mission_id=?
                ORDER BY created_at, id
            """, (workspace_id, mission_id)).fetchall()
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

    def claim_plan_step(self, step_id: str,
                        expected_statuses: tuple[str, ...] = ("planned", "approved")) -> bool:
        """Atomically claim a ready step for one executor.

        A single conditional UPDATE is the serialization point: concurrent
        callers can transition the same step to running only once.
        """
        allowed_statuses = {"planned", "approved"}
        statuses = tuple(dict.fromkeys(expected_statuses))
        if not statuses or any(status not in allowed_statuses for status in statuses):
            raise ValueError("Only planned or approved steps can be claimed")
        placeholders = ",".join("?" for _ in statuses)
        with self._connection() as db:
            cursor = db.execute(
                f"UPDATE plan_steps SET status='running' "
                f"WHERE id=? AND status IN ({placeholders})",
                (step_id, *statuses),
            )
            return cursor.rowcount == 1

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
