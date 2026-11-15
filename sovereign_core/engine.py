"""Thin orchestration facade; planning and tool execution adapters plug in later."""
from .models import PermissionLevel
from .store import TaskStore


class ProblemEngine:
    def __init__(self, store: TaskStore):
        self.store = store

    def intake(self, workspace_id: str, title: str, description: str, priority: str = "normal"):
        """Register a problem in exactly one workspace; no model action is executed here."""
        return self.store.create_problem(workspace_id, title, description, priority)

    def add_step(self, problem_id: str, number: int, action: str,
                 permission: str = PermissionLevel.READ_ONLY.value):
        """Persist a plan step; consequential steps are held for explicit approval."""
        return self.store.add_plan_step(problem_id, number, action, permission)

    def approve_step(self, step_id: str):
        self.store.update_step_status(step_id, "approved")

    def start_step(self, step_id: str):
        """Only planned or approved steps can enter execution."""
        self.store.update_step_status(step_id, "running")

    def verify_step(self, step_id: str, criterion: str, method: str,
                    passed: bool, evidence_reference: str = ""):
        self.store.update_step_status(step_id, "verifying")
        self.store.record_verification(step_id, criterion, method, passed, evidence_reference)

    def complete(self, problem_id: str):
        return self.store.complete_problem(problem_id)
