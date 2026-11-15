"""Project-agnostic task routing and permission classification.

This module classifies an instruction. It does not execute tools, call models,
or perform external actions. Execution remains the responsibility of the
Sovereign problem engine and its approval gates.
"""
from dataclasses import dataclass
from enum import StrEnum
import re

from .models import PermissionLevel


class AgentRole(StrEnum):
    EXECUTIVE = "executive"
    RESEARCH = "research"
    PRODUCT = "product"
    ENGINEERING = "engineering"
    SALES = "sales"
    SUPPORT = "support"
    FINANCE = "finance"
    OPERATIONS = "operations"


@dataclass(frozen=True)
class RoutedTask:
    role: AgentRole
    instruction: str
    permission_level: str
    requires_approval: bool
    reason: str


_ROLE_PATTERNS = (
    (AgentRole.ENGINEERING, re.compile(r"\b(build|code|bug|debug|deploy|github|repository|app|api|software|script)\b", re.I)),
    (AgentRole.RESEARCH, re.compile(r"\b(research|competitor|market|customer|trend|find|compare|investigate)\b", re.I)),
    (AgentRole.SALES, re.compile(r"\b(sell|sales|lead|customer|marketing|campaign|prospect|offer)\b", re.I)),
    (AgentRole.FINANCE, re.compile(r"\b(balance|revenue|payment|refund|invoice|expense|bank|cash|budget|cost)\b", re.I)),
    (AgentRole.SUPPORT, re.compile(r"\b(support|complaint|ticket|customer question|troubleshoot|help desk)\b", re.I)),
    (AgentRole.PRODUCT, re.compile(r"\b(product|feature|pricing|mvp|roadmap|specification)\b", re.I)),
    (AgentRole.OPERATIONS, re.compile(r"\b(order|fulfil|fulfill|delivery|workflow|automation|schedule)\b", re.I)),
)

_HIGH_IMPACT = re.compile(
    r"\b(transfer|pay|withdraw|refund|purchase|charge|publish|post|send|"
    r"delete|erase|deploy|install|uninstall|control|unlock|shut\s*off|"
    r"open\s+valve|change\s+settings)\b",
    re.I,
)
_REVERSIBLE = re.compile(
    r"\b(create|write|edit|modify|update|build|generate|save|commit)\b",
    re.I,
)


def infer_role(instruction: str) -> AgentRole:
    """Infer a specialist role using transparent keyword rules."""
    text = (instruction or "").strip()
    for role, pattern in _ROLE_PATTERNS:
        if pattern.search(text):
            return role
    return AgentRole.EXECUTIVE


def route_instruction(instruction: str) -> RoutedTask:
    """Classify role and minimum permission; never executes the instruction."""
    text = (instruction or "").strip()
    if not text:
        raise ValueError("Instruction is required")

    role = infer_role(text)
    if _HIGH_IMPACT.search(text):
        permission = PermissionLevel.HIGH_IMPACT.value
        reason = "Potential external, financial, destructive, deployment or device effect"
    elif _REVERSIBLE.search(text):
        permission = PermissionLevel.REVERSIBLE.value
        reason = "Potential local change; review and rollback remain available"
    else:
        permission = PermissionLevel.READ_ONLY.value
        reason = "Read-only or planning request"

    requires_approval = permission in {
        PermissionLevel.EXTERNAL_EFFECT.value,
        PermissionLevel.HIGH_IMPACT.value,
    }
    return RoutedTask(
        role=role,
        instruction=text,
        permission_level=permission,
        requires_approval=requires_approval,
        reason=reason,
    )
