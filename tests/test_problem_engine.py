"""Offline tests for task lifecycle, workspace separation and approval gates."""
import tempfile
import unittest
from pathlib import Path

from sovereign_core import ProblemEngine, TaskStore
from sovereign_core.models import PermissionLevel


class ProblemEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.temp.name) / "tasks.sqlite3")
        self.engine = ProblemEngine(self.store)
        self.ncs = self.store.create_workspace("NCS", "Creative Studio operations")
        self.smartflow = self.store.create_workspace("SmartFlow BW", "Thothi and water intelligence")

    def tearDown(self):
        self.temp.cleanup()

    def test_problem_is_scoped_to_workspace(self):
        problem = self.engine.intake(self.ncs.id, "Prepare blog post", "Create a useful draft")
        self.assertEqual(problem.workspace_id, self.ncs.id)
        self.assertEqual(len(self.store.list_problems(self.smartflow.id)), 0)

    def test_external_action_requires_approval(self):
        problem = self.engine.intake(self.ncs.id, "Publish", "Publish approved content")
        step = self.engine.add_step(problem.id, 1, "Publish post", PermissionLevel.EXTERNAL_EFFECT.value)
        self.assertEqual(step.status, "awaiting_approval")
        with self.assertRaises(ValueError):
            self.engine.start_step(step.id)
        self.engine.approve_step(step.id)
        self.engine.start_step(step.id)
        self.engine.verify_step(step.id, "Post is visible", "Open published URL", True, "https://example.invalid/post")
        self.store.set_problem_status(problem.id, "diagnosing")
        self.store.set_problem_status(problem.id, "planned")
        self.store.set_problem_status(problem.id, "running")
        completed = self.engine.complete(problem.id)
        self.assertEqual(completed.status, "completed")

    def test_completion_requires_verification(self):
        problem = self.engine.intake(self.smartflow.id, "Investigate alert", "Check alert data")
        self.engine.add_step(problem.id, 1, "Inspect sensor event")
        with self.assertRaises(ValueError):
            self.engine.complete(problem.id)

    def test_invalid_problem_transition_is_rejected(self):
        problem = self.engine.intake(self.ncs.id, "Test", "Test state guard")
        with self.assertRaises(ValueError):
            self.store.set_problem_status(problem.id, "completed")


if __name__ == "__main__":
    unittest.main()
