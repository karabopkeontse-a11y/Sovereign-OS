"""Provider-neutral workspace and memory service for SovereignOS clients.

Desktop, web and mobile clients should call this service rather than implement
separate memory rules. The caller must retain the selected workspace ID in its
own authenticated/client session; never trust a workspace ID supplied by an LLM.
"""
from __future__ import annotations

from .memory import MemoryRecord, MemoryRevision, MemoryStore
from .models import Workspace
from .store import TaskStore


class WorkspaceService:
    """A small application service shared by Windows, web and mobile adapters."""

    DEFAULT_WORKSPACES = (
        ("General", "Personal assistant context and cross-project preferences."),
        ("Nikolai Creative Studio", "NCS publishing, design, marketing and business work."),
        ("Yard Truths", "Yard Truths editorial, brand and publishing work."),
        ("SmartFlow BW", "SmartFlow BW product development and Thothi assistant work."),
        ("SovereignOS", "SovereignOS core platform, architecture and engineering."),
    )

    def __init__(self, db_path):
        self.tasks = TaskStore(db_path)
        self.memory = MemoryStore(db_path)

    def list_workspaces(self) -> list[Workspace]:
        return self.tasks.list_workspaces()

    def select_options(self) -> dict[str, str]:
        """Return NiceGUI select options as value -> display label."""
        return {workspace.id: workspace.name for workspace in self.list_workspaces()}

    def get_workspace(self, workspace_id: str) -> Workspace:
        item = self.tasks.get_workspace((workspace_id or "").strip())
        if item is None:
            raise ValueError("Workspace does not exist or is not available to this local store.")
        return item

    def create_workspace(self, name: str, purpose: str = "") -> Workspace:
        name = (name or "").strip()
        if not name:
            raise ValueError("Workspace name is required.")
        existing = next((w for w in self.list_workspaces()
                         if w.name.casefold() == name.casefold()), None)
        if existing:
            return existing
        try:
            return self.tasks.create_workspace(name, (purpose or "").strip())
        except Exception:
            # A second client may have created the same unique name concurrently.
            existing = next((w for w in self.list_workspaces()
                             if w.name.casefold() == name.casefold()), None)
            if existing:
                return existing
            raise

    def ensure_default_workspaces(self) -> list[Workspace]:
        for name, purpose in self.DEFAULT_WORKSPACES:
            self.create_workspace(name, purpose)
        return self.list_workspaces()

    def _scope(self, workspace_id: str) -> str:
        return self.get_workspace(workspace_id).id

    def remember(self, workspace_id: str, content: str, *, kind: str = "fact",
                 source: str = "user", confidence: float = 1.0,
                 verified: bool = False) -> MemoryRecord:
        return self.memory.remember(self._scope(workspace_id), content, kind,
                                    source, confidence, verified)

    def search(self, workspace_id: str, query: str, *, limit: int = 10,
               verified_only: bool = False) -> list[MemoryRecord]:
        return self.memory.search(self._scope(workspace_id), query, limit, verified_only)

    def list_memories(self, workspace_id: str, *, include_inactive: bool = False) -> list[MemoryRecord]:
        return self.memory.list_memories(self._scope(workspace_id), include_inactive)

    def correct(self, workspace_id: str, memory_id: str, content: str, *,
                source: str = "user correction", confidence: float = 1.0) -> MemoryRecord:
        current = self.memory.get(memory_id)
        if current is None or current.workspace_id != self._scope(workspace_id):
            raise ValueError("Memory does not belong to the selected workspace.")
        return self.memory.supersede(memory_id, content, source, confidence, verified=False)

    def history(self, workspace_id: str, memory_id: str) -> list[MemoryRevision]:
        current = self.memory.get(memory_id)
        if current is None or current.workspace_id != self._scope(workspace_id):
            raise ValueError("Memory does not belong to the selected workspace.")
        return self.memory.history(memory_id)

    def forget(self, workspace_id: str, memory_id: str) -> bool:
        current = self.memory.get(memory_id)
        if current is None:
            return False
        if current.workspace_id != self._scope(workspace_id):
            raise ValueError("Memory does not belong to the selected workspace.")
        return self.memory.delete(memory_id)


    def verify(self, workspace_id: str, memory_id: str) -> MemoryRecord:
        current = self.memory.get(memory_id)
        if current is None or current.workspace_id != self._scope(workspace_id):
            raise ValueError("Memory does not belong to the selected workspace.")
        return self.memory.revise(
            memory_id, current.content, source="user verified in workspace memory panel",
            verified=True,
        )

    def export_json(self, workspace_id: str) -> str:
        return self.memory.export_json(self._scope(workspace_id))
