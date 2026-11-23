"""Shared, workspace-scoped execution service for registered agent tools.

The API and durable mission workers use this same path so validation, approval,
execution and verification rules cannot drift between entry points.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .agent_tools import (
    TOOLS,
    execute_workspace_tool,
    verify_workspace_tool,
    workspace_root,
)


class AgentStepExecutionError(Exception):
    """An expected, safe-to-report agent step execution failure."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def execute_registered_step(
    store,
    db_path: str | Path,
    workspace_id: str,
    problem_id: str,
    step_id: str | None = None,
) -> dict:
    """Execute exactly one registered plan step within its owning workspace.

    Workspace identity is resolved from persisted task records, never from tool
    arguments. Only registered tools can execute. Non-read-only steps require a
    persisted approved state. Every successful execution is verified and saved.
    """
    workspace = store.get_workspace(workspace_id)
    if workspace is None:
        raise AgentStepExecutionError(404, "Workspace not found")

    problem = store.get_problem(problem_id)
    if problem is None or problem.workspace_id != workspace.id:
        raise AgentStepExecutionError(404, "Task not found in this workspace")
    if problem.status in {"blocked", "failed", "completed", "cancelled"}:
        raise AgentStepExecutionError(409, "This task is not in an executable state.")

    steps = store.list_steps(problem.id)
    selected = None
    spec = None
    if step_id:
        selected = next((item for item in steps if item.id == step_id), None)
        if selected is None:
            raise AgentStepExecutionError(404, "Plan step not found in this task")
        spec = store.get_step_spec(selected.id)
    else:
        for candidate in steps:
            candidate_spec = store.get_step_spec(candidate.id)
            if candidate_spec and candidate.status in {"planned", "approved"}:
                selected, spec = candidate, candidate_spec
                break

    if selected is None or spec is None:
        raise AgentStepExecutionError(
            409, "No registered tool step is ready. Review pending approvals or add a supported tool step."
        )
    if spec.get("tool_name") not in TOOLS:
        raise AgentStepExecutionError(403, "This step does not reference a registered workspace tool.")
    if (
        selected.status == "awaiting_approval"
        and selected.permission_level != "read_only"
    ):
        raise AgentStepExecutionError(403, "This tool step requires explicit approval before execution.")
    if selected.status not in {"planned", "approved"}:
        raise AgentStepExecutionError(409, "This plan step is not ready for execution.")
    if selected.permission_level != "read_only" and selected.status != "approved":
        raise AgentStepExecutionError(403, "This tool step requires explicit approval before execution.")

    started_at = None
    started_clock = None
    run_recorded = False
    arguments = {}
    input_summary = "{}"
    try:
        arguments = json.loads(spec["input_json"])
        if not isinstance(arguments, dict):
            raise ValueError("Registered tool arguments must be a JSON object.")
        input_summary = json.dumps({
            key: (f"[{len(value)} characters]" if key == "content" and isinstance(value, str) else value)
            for key, value in arguments.items()
        }, ensure_ascii=False)
        root = workspace_root(db_path, workspace.id)
        if problem.status in {"planned", "awaiting_approval"}:
            store.set_problem_status(problem.id, "running")
        store.update_step_status(selected.id, "running")
        started_at = _timestamp()
        started_clock = time.perf_counter()

        result = execute_workspace_tool(spec["tool_name"], arguments, root)
        passed, criterion, evidence = verify_workspace_tool(
            spec["tool_name"], arguments, result, root
        )
        store.update_step_status(selected.id, "verifying")
        output_summary = (
            f"Listed {result.get('count')} workspace files"
            if spec["tool_name"] == "workspace.list_files"
            else f"Read {result.get('path')} ({result.get('bytes')} bytes)"
            if spec["tool_name"] == "workspace.read_file"
            else f"Created {result.get('path')} ({result.get('bytes')} bytes)"
        )
        store.record_tool_run(
            selected.id, spec["tool_name"], input_summary, output_summary,
            "success" if passed else "verification_failed", started_at, _timestamp(),
        )
        run_recorded = True
        store.record_verification(
            selected.id, spec["verification_criterion"] or criterion,
            "server-side workspace boundary and read-back check",
            passed, evidence,
        )
        if not passed:
            current = store.get_problem(problem.id)
            if current and current.status == "running":
                store.set_problem_status(problem.id, "failed")
            raise AgentStepExecutionError(500, "The tool ran, but its verification check failed.")

        remaining = store.list_steps(problem.id)
        if remaining and all(item.status == "verified" for item in remaining):
            store.complete_problem(problem.id)
        output = dict(result)
        if isinstance(output.get("content"), str) and len(output["content"]) > 20000:
            output["content"] = output["content"][:20000]
            output["truncated"] = True
        return {
            "task_id": problem.id,
            "step": store.get_step(selected.id).__dict__,
            "tool_result": output,
            "verified": True,
            "duration_ms": round((time.perf_counter() - started_clock) * 1000),
            "task_status": store.get_problem(problem.id).status,
        }
    except AgentStepExecutionError:
        raise
    except Exception as exc:
        if started_at and not run_recorded:
            try:
                store.record_tool_run(
                    selected.id, spec["tool_name"], input_summary,
                    "Tool failed: " + type(exc).__name__, "failed", started_at, _timestamp(),
                )
            except Exception:
                pass
        current_step = store.get_step(selected.id)
        if current_step and current_step.status in {"running", "verifying"}:
            try:
                if current_step.status == "running":
                    store.update_step_status(selected.id, "failed")
                else:
                    store.record_verification(
                        selected.id, spec.get("verification_criterion", ""),
                        "Execution error", False, type(exc).__name__,
                    )
            except Exception:
                pass
        current_problem = store.get_problem(problem.id)
        if current_problem and current_problem.status == "running":
            try:
                store.set_problem_status(problem.id, "failed")
            except ValueError:
                pass
        raise AgentStepExecutionError(
            500, "Agent tool failed safely: " + type(exc).__name__ + ": " + str(exc)
        ) from exc
