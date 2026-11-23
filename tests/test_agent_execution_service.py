import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch
from pathlib import Path

from sovereign_core.agent_execution import AgentStepExecutionError, execute_registered_step
from sovereign_core.store import TaskStore


class RegisteredAgentExecutionServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "tasks.sqlite3"
        self.store = TaskStore(self.db_path)
        self.workspace = self.store.create_workspace("Workspace A", "Private test workspace")
        self.other_workspace = self.store.create_workspace("Workspace B", "Separate test workspace")

    def tearDown(self):
        self.temp.cleanup()

    def create_task(self, workspace=None, *, tool="workspace.list_files", arguments=None,
                    permission="read_only"):
        workspace = workspace or self.workspace
        problem = self.store.create_problem(workspace.id, "Test task", "Test registered tool execution")
        self.store.set_problem_status(problem.id, "diagnosing")
        self.store.set_problem_status(problem.id, "planned")
        step = self.store.add_plan_step(problem.id, 1, "Run registered workspace tool", permission)
        self.store.add_step_spec(
            step.id, tool, json.dumps(arguments or {}),
            "The scoped workspace tool result is verified.",
        )
        return problem, step

    def test_read_only_tool_uses_shared_workspace_execution_and_verification(self):
        problem, step = self.create_task()
        result = execute_registered_step(
            self.store, self.db_path, self.workspace.id, problem.id, step.id
        )
        self.assertTrue(result["verified"])
        self.assertEqual(result["step"]["status"], "verified")
        self.assertEqual(result["tool_result"]["tool"], "workspace.list_files")
        self.assertEqual(self.store.get_problem(problem.id).status, "completed")

    def test_cross_workspace_task_reference_is_rejected(self):
        problem, step = self.create_task(self.workspace)
        with self.assertRaises(AgentStepExecutionError) as caught:
            execute_registered_step(
                self.store, self.db_path, self.other_workspace.id, problem.id, step.id
            )
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(self.store.get_step(step.id).status, "planned")

    def test_non_read_only_step_cannot_run_without_persisted_approval(self):
        problem, step = self.create_task(
            tool="workspace.create_note",
            arguments={"filename": "approved.md", "content": "Approved content"},
            permission="external_effect",
        )
        with self.assertRaises(AgentStepExecutionError) as caught:
            execute_registered_step(
                self.store, self.db_path, self.workspace.id, problem.id, step.id
            )
        self.assertEqual(caught.exception.status_code, 403)
        target = Path(self.temp.name) / "agent_workspace" / self.workspace.id / "approved.md"
        self.assertFalse(target.exists())

    def test_approved_note_is_created_and_readback_verified(self):
        problem, step = self.create_task(
            tool="workspace.create_note",
            arguments={"filename": "approved.md", "content": "# Approved\nPersisted safely."},
            permission="external_effect",
        )
        self.store.update_step_status(step.id, "approved")
        self.store.record_approval_event(
            self.workspace.id, problem.id, step.id, "approved", "Owner approved the draft."
        )
        result = execute_registered_step(
            self.store, self.db_path, self.workspace.id, problem.id, step.id
        )
        self.assertTrue(result["verified"])
        self.assertTrue(result["tool_result"]["created"])
        target = Path(self.temp.name) / "agent_workspace" / self.workspace.id / "approved.md"
        self.assertEqual(target.read_text(encoding="utf-8"), "# Approved\nPersisted safely.")
        receipt = self.store.get_step_execution_receipt(self.workspace.id, step.id)
        self.assertEqual(receipt["status"], "verified")
        replay = execute_registered_step(
            self.store, self.db_path, self.workspace.id, problem.id, step.id
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(target.read_text(encoding="utf-8"), "# Approved\\nPersisted safely.")
        approvals = self.store.list_approval_events(self.workspace.id, problem.id)
        self.assertEqual([event["decision"] for event in approvals], ["approved"])

    def test_concurrent_execution_claims_step_only_once(self):
        problem, step = self.create_task()
        entered_tool = Event()
        release_tool = Event()
        calls = []

        def blocking_tool(tool_name, arguments, root):
            calls.append(tool_name)
            entered_tool.set()
            self.assertTrue(release_tool.wait(5), "test did not release the first executor")
            return {"tool": tool_name, "count": 0, "files": []}

        def execute():
            return execute_registered_step(
                self.store, self.db_path, self.workspace.id, problem.id, step.id
            )

        with patch("sovereign_core.agent_execution.execute_workspace_tool", side_effect=blocking_tool):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(execute)
                self.assertTrue(entered_tool.wait(5), "first executor never entered the tool")
                second = pool.submit(execute)
                try:
                    with self.assertRaises(AgentStepExecutionError) as caught:
                        second.result(timeout=5)
                    self.assertEqual(caught.exception.status_code, 409)
                finally:
                    release_tool.set()
                result = first.result(timeout=5)

        self.assertTrue(result["verified"])
        self.assertEqual(calls, ["workspace.list_files"])
        self.assertEqual(self.store.get_step(step.id).status, "verified")

    def test_crash_after_note_creation_is_reconciled_without_repeating_tool(self):
        problem, step = self.create_task(
            tool="workspace.create_note",
            arguments={"filename": "recovered.md", "content": "# Recovered\\nCreated before crash."},
            permission="external_effect",
        )
        self.store.update_step_status(step.id, "approved")
        self.store.record_approval_event(self.workspace.id, problem.id, step.id, "approved")
        target = Path(self.temp.name) / "agent_workspace" / self.workspace.id / "recovered.md"
        calls = []

        def create_then_crash(tool_name, arguments, root):
            calls.append(tool_name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(arguments["content"], encoding="utf-8")
            raise SystemExit("simulated process termination after side effect")

        with patch("sovereign_core.agent_execution.execute_workspace_tool", side_effect=create_then_crash):
            with self.assertRaises(SystemExit):
                execute_registered_step(
                    self.store, self.db_path, self.workspace.id, problem.id, step.id
                )

        interrupted_receipt = self.store.get_step_execution_receipt(self.workspace.id, step.id)
        self.assertEqual(interrupted_receipt["status"], "executing")
        self.assertEqual(self.store.get_step(step.id).status, "running")

        recovered = execute_registered_step(
            self.store, self.db_path, self.workspace.id, problem.id,
            recover_interrupted=True,
        )
        self.assertTrue(recovered["reconciled"])
        self.assertTrue(recovered["verified"])
        self.assertEqual(calls, ["workspace.create_note"])
        self.assertEqual(target.read_text(encoding="utf-8"), "# Recovered\\nCreated before crash.")
        self.assertEqual(
            self.store.get_step_execution_receipt(self.workspace.id, step.id)["status"],
            "verified",
        )

    def test_receipt_conflict_never_overwrites_existing_file(self):
        problem, step = self.create_task(
            tool="workspace.create_note",
            arguments={"filename": "conflict.md", "content": "Approved original"},
            permission="external_effect",
        )
        self.store.update_step_status(step.id, "approved")
        self.store.record_approval_event(self.workspace.id, problem.id, step.id, "approved")
        target = Path(self.temp.name) / "agent_workspace" / self.workspace.id / "conflict.md"

        def create_then_crash(tool_name, arguments, root):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(arguments["content"], encoding="utf-8")
            raise SystemExit("simulated process termination")

        with patch("sovereign_core.agent_execution.execute_workspace_tool", side_effect=create_then_crash):
            with self.assertRaises(SystemExit):
                execute_registered_step(
                    self.store, self.db_path, self.workspace.id, problem.id, step.id
                )
        target.write_text("A different file was placed here.", encoding="utf-8")

        with self.assertRaises(AgentStepExecutionError) as caught:
            execute_registered_step(
                self.store, self.db_path, self.workspace.id, problem.id, step.id,
                recover_interrupted=True,
            )
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(target.read_text(encoding="utf-8"), "A different file was placed here.")
        self.assertEqual(self.store.get_step(step.id).status, "blocked")
        self.assertEqual(
            self.store.get_step_execution_receipt(self.workspace.id, step.id)["status"],
            "unknown",
        )

    def test_effect_applied_receipt_recovers_without_rerunning_read_tool(self):
        problem, step = self.create_task()
        with patch(
            "sovereign_core.agent_execution.verify_workspace_tool",
            side_effect=SystemExit("simulated crash before verification"),
        ):
            with self.assertRaises(SystemExit):
                execute_registered_step(
                    self.store, self.db_path, self.workspace.id, problem.id, step.id
                )
        receipt = self.store.get_step_execution_receipt(self.workspace.id, step.id)
        self.assertEqual(receipt["status"], "effect_applied")
        recovered = execute_registered_step(
            self.store, self.db_path, self.workspace.id, problem.id, step.id,
            recover_interrupted=True,
        )
        self.assertTrue(recovered["reconciled"])
        self.assertTrue(recovered["verified"])
        self.assertEqual(
            self.store.get_step_execution_receipt(self.workspace.id, step.id)["status"],
            "verified",
        )

    def test_unknown_registered_tool_is_rejected_without_execution(self):
        problem, step = self.create_task(tool="shell.execute", arguments={"command": "whoami"})
        with self.assertRaises(AgentStepExecutionError) as caught:
            execute_registered_step(
                self.store, self.db_path, self.workspace.id, problem.id, step.id
            )
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(self.store.get_step(step.id).status, "planned")


if __name__ == "__main__":
    unittest.main()
