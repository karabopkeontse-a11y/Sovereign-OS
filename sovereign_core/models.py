"""Typed records and lifecycle states for the SovereignOS task engine."""
from dataclasses import dataclass
from enum import StrEnum


class ProblemStatus(StrEnum):
    NEW = "new"
    DIAGNOSING = "diagnosing"
    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    RUNNING = "running"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PermissionLevel(StrEnum):
    READ_ONLY = "read_only"
    REVERSIBLE = "reversible"
    EXTERNAL_EFFECT = "external_effect"
    HIGH_IMPACT = "high_impact"


@dataclass(frozen=True)
class Workspace:
    id: str
    name: str
    purpose: str
    created_at: str


@dataclass(frozen=True)
class Problem:
    id: str
    workspace_id: str
    title: str
    description: str
    status: str
    priority: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PlanStep:
    id: str
    problem_id: str
    step_number: int
    action: str
    permission_level: str
    status: str
    created_at: str
