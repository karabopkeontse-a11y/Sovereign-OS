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
    validate_tool_input,
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
    recover_interrupted: bool = False,
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
            candidate_receipt = (
                store.get_step_execution_receipt(workspace.id, candidate.id)
                if recover_interrupted else None
            )
            ready = candidate.status in {"planned", "approved"}
            recoverable = bool(candidate_receipt) and candidate.status in {
                "running", "verifying", "blocked"
            }
            if candidate_spec and (ready or recoverable):
                selected, spec = candidate, candidate_spec
                break

    if selected is None or spec is None:
        raise AgentStepExecutionError(
            409, "No registered tool step is ready. Review pending approvals or add a supported tool step."
        )
    if spec.get("tool_name") not in TOOLS:
        raise AgentStepExecutionError(403, "This step does not reference a registered workspace tool.")

    try:
        arguments = validate_tool_input(spec["tool_name"], json.loads(spec["input_json"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AgentStepExecutionError(400, "Registered tool input is invalid: " + str(exc)) from exc
    receipt = store.get_step_execution_receipt(workspace.id, selected.id)
    if receipt and (receipt["tool_name"] != spec["tool_name"] or receipt["input_json"] != spec["input_json"]):
        raise AgentStepExecutionError(409, "The registered step changed after its execution receipt was created.")

    if receipt and receipt["status"] in {"prepared", "executing", "effect_applied", "unknown"} and not recover_interrupted:
        raise AgentStepExecutionError(
            409, "This step has an existing execution receipt. Use the explicit recovery path to reconcile it."
        )

    if receipt and receipt["status"] == "verified":
        saved_result = json.loads(receipt["result_json"] or "{}")
        return {
            "task_id": problem.id, "step": store.get_step(selected.id).__dict__,
            "tool_result": saved_result, "verified": True, "duration_ms": 0,
            "task_status": store.get_problem(problem.id).status,
            "replayed": True,
        }

    root = workspace_root(db_path, workspace.id)
    if receipt and receipt["status"] in {"executing", "effect_applied", "unknown"}:
        if spec["tool_name"] == "workspace.create_note":
            target = (root / arguments["filename"]).resolve()
            try:
                target.relative_to(root.resolve())
            except ValueError as exc:
                raise AgentStepExecutionError(400, "Receipt target resolves outside this workspace.") from exc
            if target.exists():
                if not target.is_file() or target.read_text(encoding="utf-8") != arguments["content"]:
                    store.update_step_execution_receipt(
                        workspace.id, selected.id, "unknown",
                        error="A file exists at the approved path but does not match the approved content.",
                    )
                    current = store.get_step(selected.id)
                    if current and current.status == "running":
                        store.update_step_status(selected.id, "blocked")
                    raise AgentStepExecutionError(409, "Execution outcome is uncertain: the target file exists with different content. It was not overwritten.")
                reconciled_result = {
                    "tool": "workspace.create_note", "path": arguments["filename"],
                    "bytes": target.stat().st_size, "created": False, "reconciled": True,
                }
                passed, criterion, evidence = verify_workspace_tool(
                    spec["tool_name"], arguments, reconciled_result, root
                )
                current_step = store.get_step(selected.id)
                if current_step and current_step.status == "blocked":
                    store.update_step_status(selected.id, "planned")
                    store.update_step_status(selected.id, "running")
                    current_step = store.get_step(selected.id)
                elif current_step and current_step.status == "planned":
                    store.update_step_status(selected.id, "running")
                    current_step = store.get_step(selected.id)
                if current_step and current_step.status == "running":
                    store.update_step_status(selected.id, "verifying")
                if current_step and current_step.status in {"running", "verifying"}:
                    store.record_verification(
                        selected.id, spec["verification_criterion"] or criterion,
                        "durable receipt recovery and server-side read-back", passed, evidence,
                    )
                elif not current_step or current_step.status != "verified":
                    raise AgentStepExecutionError(409, "The interrupted step is not in a recoverable state.")
                store.update_step_execution_receipt(
                    workspace.id, selected.id, "verified" if passed else "failed",
                    result=reconciled_result, evidence_reference=evidence,
                    error="" if passed else "Recovered file failed read-back verification.",
                )
                if not passed:
                    raise AgentStepExecutionError(500, "Recovered tool effect failed verification.")
                remaining = store.list_steps(problem.id)
                if remaining and all(item.status == "verified" for item in remaining):
                    store.complete_problem(problem.id)
                return {
                    "task_id": problem.id, "step": store.get_step(selected.id).__dict__,
                    "tool_result": reconciled_result, "verified": True,
                    "duration_ms": 0, "task_status": store.get_problem(problem.id).status,
                    "reconciled": True,
                }
        elif receipt["status"] == "effect_applied":
            saved_result = json.loads(receipt["result_json"] or "{}")
            passed, criterion, evidence = verify_workspace_tool(
                spec["tool_name"], arguments, saved_result, root
            )
            current_step = store.get_step(selected.id)
            if current_step and current_step.status == "running":
                store.update_step_status(selected.id, "verifying")
            if current_step and current_step.status in {"running", "verifying"}:
                store.record_verification(
                    selected.id, spec["verification_criterion"] or criterion,
                    "durable receipt recovery and server-side verification", passed, evidence,
                )
            store.update_step_execution_receipt(
                workspace.id, selected.id, "verified" if passed else "failed",
                result=saved_result, evidence_reference=evidence,
                error="" if passed else "Recovered tool result failed verification.",
            )
            if not passed:
                raise AgentStepExecutionError(500, "Recovered tool result failed verification.")
            remaining = store.list_steps(problem.id)
            if remaining and all(item.status == "verified" for item in remaining):
                store.complete_problem(problem.id)
            return {
                "task_id": problem.id, "step": store.get_step(selected.id).__dict__,
                "tool_result": saved_result, "verified": True, "duration_ms": 0,
                "task_status": store.get_problem(problem.id).status, "reconciled": True,
            }
        elif spec["tool_name"] not in {"workspace.list_files", "workspace.read_file"}:
            raise AgentStepExecutionError(409, "This execution receipt requires manual reconciliation; the tool will not be repeated.")

    if problem.status in {"blocked", "failed", "completed", "cancelled"} and not receipt:
        raise AgentStepExecutionError(409, "This task is not in an executable state.")

    if receipt is None:
        if (
            selected.status == "awaiting_approval"
            and selected.permission_level != "read_only"
        ):
            raise AgentStepExecutionError(403, "This tool step requires explicit approval before execution.")
        if selected.status not in {"planned", "approved"}:
            raise AgentStepExecutionError(409, "This plan step is not ready for execution.")
        if selected.permission_level != "read_only" and selected.status != "approved":
            raise AgentStepExecutionError(403, "This tool step requires explicit approval before execution.")
        receipt = store.claim_step_execution(
            workspace.id, problem.id, selected.id, spec["tool_name"],
            spec["input_json"], selected.status,
        )
        if receipt is None:
            raise AgentStepExecutionError(409, "This plan step has already been claimed or is no longer ready.")
    elif receipt["status"] not in {"prepared", "executing"}:
        raise AgentStepExecutionError(409, "This execution receipt is not resumable.")

    started_at = None
    started_clock = None
    run_recorded = False
    input_summary = json.dumps({
            key: (f"[{len(value)} characters]" if key == "content" and isinstance(value, str) else value)
            for key, value in arguments.items()
        }, ensure_ascii=False)
    try:
        store.update_step_execution_receipt(workspace.id, selected.id, "executing")
        current_problem = store.get_problem(problem.id)
        if current_problem and current_problem.status in {"planned", "awaiting_approval"}:
            store.set_problem_status(problem.id, "running")
        started_at = _timestamp()
        started_clock = time.perf_counter()

        result = execute_workspace_tool(spec["tool_name"], arguments, root)
        store.update_step_execution_receipt(
            workspace.id, selected.id, "effect_applied", result=result,
        )
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
        store.update_step_execution_receipt(
            workspace.id, selected.id, "verified" if passed else "failed",
            result=result, evidence_reference=evidence,
            error="" if passed else "Tool effect failed server-side verification.",
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
        current_receipt = store.get_step_execution_receipt(workspace.id, selected.id)
        if current_receipt and current_receipt["status"] in {"prepared", "executing", "effect_applied"}:
            try:
                store.update_step_execution_receipt(
                    workspace.id, selected.id,
                    "failed" if selected.permission_level == "read_only" else "unknown",
                    error=type(exc).__name__ + ": " + str(exc),
                )
            except Exception:
                pass
        current_step = store.get_step(selected.id)
        if selected.permission_level == "read_only":
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
