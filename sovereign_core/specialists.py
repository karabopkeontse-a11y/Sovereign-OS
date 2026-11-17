"""Specialist identities and model-to-role assignment for SovereignOS.

Specialists are capabilities layered over configured model candidates. They do not
pretend an unconfigured model exists, and every invocation still passes through
ModelRouter's eligibility and fallback controls.
"""
from __future__ import annotations

ROLE_CATALOG = (
    ("strategist", "Strategist", "Break down objectives, trade-offs and execution plans.", ("reason", "planning", "priorit")),
    ("researcher", "Researcher", "Investigate questions, distinguish evidence from assumptions and cite uncertainty.", ("research", "search", "evidence")),
    ("engineer", "Engineer", "Design, debug and implement software and technical systems.", ("code", "coder", "dev", "qwen")),
    ("creative", "Creative Director", "Develop brand, editorial, design and campaign concepts.", ("creative", "design", "image")),
    ("analyst", "Analyst", "Analyse figures, operations, budgets and performance.", ("analyst", "finance", "math")),
    ("operator", "Operator", "Turn approved plans into careful, auditable tool workflows.", ("agent", "tool", "instruct")),
)

def specialist_for(index: int, candidate) -> dict:
    info = candidate.info
    identity = (info.provider + " " + info.model).casefold()
    role = next((entry for entry in ROLE_CATALOG if any(word in identity for word in entry[3])), ROLE_CATALOG[index % len(ROLE_CATALOG)])
    key, title, mission, _ = role
    return {
        "id": f"specialist-{index}",
        "index": index,
        "name": f"{title} · {info.model}",
        "role": key,
        "title": title,
        "mission": mission,
        "provider": info.provider,
        "model": info.model,
        "runtime": info.runtime,
    }

def specialist_prompt(specialist: dict, workspace_name: str) -> str:
    return (
        f"You are {specialist['title']}, a named SovereignOS Specialist in the "
        f"{workspace_name} workspace. Your speciality: {specialist['mission']} "
        "Work as a capable member of a coordinated specialist team. Use the "
        "conversation history supplied to you. Be concrete, state uncertainty, "
        "and do not claim external actions or tool execution that did not occur. "
        "Publishing, financial transfers, email sending, destructive changes and "
        "device control require an explicitly approved tool action."
    )
