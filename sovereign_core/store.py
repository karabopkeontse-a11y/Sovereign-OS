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
            "running": {"verifying", "blocked", "failed", "cancelled"},
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
        if decision not in {"approved", "rejected"}:
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
