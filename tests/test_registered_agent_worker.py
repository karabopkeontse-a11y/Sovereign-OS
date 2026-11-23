"""Integration tests for durable missions executing registered agent steps."""
import json
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from sovereign_core.agent_execution import registered_agent_step_executor
from sovereign_core.store import TaskStore
from sovereign_core.worker import MissionWorkerSupervisor


class RegisteredAgentWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "worker.sqlite3"
        self.store = TaskStore(self.db_path)
        self.workspace = self.store.create_workspace("Agent Worker", "Integration")
        self.executor = registered_agent_step_executor(self.store, self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def create_step(self, *, tool="workspace.list_files", arguments=None,
                    permission="read_only", approved=False):
        problem = self.store.create_problem(
            self.workspace.id, "Worker task", "Execute one registered step"
        )
        self.store.set_problem_status(problem.id, "diagnosing")
        self.store.set_problem_status(problem.id, "planned")
        step = self.store.add_plan_step(
            problem.id, 1, "Execute registered tool", permission
        )
        self.store.add_step_spec(
            step.id, tool, json.dumps(arguments or {}),
            "The registered tool result is verified.",
        )
        if approved:
            self.store.update_step_status(step.id, "approved")
            self.store.record_approval_event(
                self.workspace.id, problem.id, step.id, "approved",
                "Approved in integration test.",
            )
        return problem, step

    def create_job(self, problem, step):
        return self.store.create_mission_job(
            self.workspace.id,
            "Execute registered agent step",
            {
                "kind": "registered_agent_step",
                "problem_id": problem.id,
                "step_id": step.id,
            },
        )

    def worker(self, **kwargs):
        return MissionWorkerSupervisor(
            self.store, self.executor, worker_id="registered-agent-worker",
            retry_base_seconds=0, retry_max_seconds=0, **kwargs
        )

    def test_worker_executes_and_persists_approved_registered_step(self):
        problem, step = self.create_step(
            tool="workspace.create_note",
            arguments={"filename": "worker-note.md", "content": "# Worker result"},
            permission="external_effect",
            approved=True,
        )
        job = self.create_job(problem, step)

        outcome = self.worker().run_once()

        saved_job = self.store.get_mission_job(self.workspace.id, job["id"])
        saved_step = self.store.get_step(step.id)
        target = Path(self.temp.name) / "agent_workspace" / self.workspace.id / "worker-note.md"
        self.assertEqual(outcome["claimed"], 1)
        self.assertEqual(saved_job["status"], "completed")
        self.assertTrue(saved_job["result"]["verified"])
        self.assertEqual(saved_job["result"]["task_id"], problem.id)
        self.assertEqual(saved_step.status, "verified")
        self.assertEqual(target.read_text(encoding="utf-8"), "# Worker result")
        self.assertEqual(
            self.store.get_step_execution_receipt(self.workspace.id, step.id)["status"],
            "verified",
        )

    def test_worker_does_not_bypass_persisted_approval(self):
        problem, step = self.create_step(
            tool="workspace.create_note",
            arguments={"filename": "not-approved.md", "content": "No"},
            permission="external_effect",
        )
        job = self.create_job(problem, step)

        self.worker().run_once()

        saved_job = self.store.get_mission_job(self.workspace.id, job["id"])
        target = Path(self.temp.name) / "agent_workspace" / self.workspace.id / "not-approved.md"
        self.assertEqual(saved_job["status"], "failed")
        self.assertIn("explicit approval", saved_job["last_error"])
        self.assertFalse(target.exists())
        self.assertEqual(self.store.get_step(step.id).status, "planned")

    def test_shutdown_after_verified_step_retries_queue_and_replays_receipt(self):
        problem, step = self.create_step(
            tool="workspace.create_note",
            arguments={"filename": "shutdown-note.md", "content": "Persist once"},
            permission="external_effect",
            approved=True,
        )
        job = self.create_job(problem, step)
        stop = Event()
        original = __import__(
            "sovereign_core.agent_execution", fromlist=["execute_workspace_tool"]
        ).execute_workspace_tool
        calls = []

        def create_then_request_shutdown(tool_name, arguments, root):
            calls.append(tool_name)
            result = original(tool_name, arguments, root)
            stop.set()
            return result

        with patch(
            "sovereign_core.agent_execution.execute_workspace_tool",
            side_effect=create_then_request_shutdown,
        ):
            self.worker(stop_event=stop).run_once()

        interrupted = self.store.get_mission_job(self.workspace.id, job["id"])
        self.assertEqual(interrupted["status"], "retryable")
        self.assertEqual(self.store.get_step(step.id).status, "verified")
        self.assertEqual(
            self.store.get_step_execution_receipt(self.workspace.id, step.id)["status"],
            "verified",
        )

        self.worker().run_once()
        completed = self.store.get_mission_job(self.workspace.id, job["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertTrue(completed["result"]["replayed"])
        self.assertEqual(calls, ["workspace.create_note"])

    def test_unknown_payload_kind_is_terminal_and_never_executes_a_tool(self):
        job = self.store.create_mission_job(
            self.workspace.id, "Unsupported job", {"kind": "shell.execute"}
        )
        self.worker().run_once()
        saved = self.store.get_mission_job(self.workspace.id, job["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertIn("Unsupported mission payload kind", saved["last_error"])


if __name__ == "__main__":
    unittest.main()
